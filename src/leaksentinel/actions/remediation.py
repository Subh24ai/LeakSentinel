"""Remediation actions on confirmed commission leaks — the *act* half of a
deliberate "judgment vs. action" split.

Why this looks the way it does (the Temporal/durable-execution pattern)
-----------------------------------------------------------------------
A durable-execution engine (Temporal, AWS SWF, Restate, ...) earns its keep by
giving four guarantees around side effects. We are only doing one side effect
here — "claim a shortfall back from an insurer" — but it touches real money, so
we reproduce the same four properties by hand and TEST each one:

  1. Separate the *judgment* from the *action*. Detection (``detection/rules``)
     decides a leak is real; this module decides whether we are *allowed* to act
     on it, and only then acts. :func:`validate_action` is that gate, and it is
     the single chokepoint every write goes through.
  2. Make writes *idempotent*. Every action derives an idempotency key from the
     finding; a retry returns the *existing* record instead of creating a second
     claim — exactly how a Temporal activity must behave when a workflow replays
     it. Retries never double-pay.
  3. *Gate* side effects. No external API call and no row write happen until the
     gate passes. A blocked action refuses loudly and writes nothing except an
     audit row.
  4. Keep an *immutable audit trail*. Every action AND every blocked attempt
     appends to ``audit_log`` with a SHA-256 of the action payload, so any
     decision is reconstructable after the fact.

The insurer/CRM call itself is hidden behind :class:`ExternalClaimsAPI` (a local
stub in :mod:`leaksentinel.actions.externalapi`) so it is obviously swappable for
a real client later.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from sqlalchemy import DateTime, Numeric, String, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from leaksentinel.actions.externalapi import ExternalClaimsAPI
from leaksentinel.db import Base, SessionLocal
from leaksentinel.detection.rules import DetectionReason
from leaksentinel.reconciliation.models import AuditLog, ReconciliationResult
from leaksentinel.reconciliation.schemas import ResolutionState

# --------------------------------------------------------------------------- #
# Thresholds / policy knobs (would live in config/feature-flags in production)
# --------------------------------------------------------------------------- #
# Below this, a claim isn't worth lodging — the gate refuses as TOO_SMALL and we
# do NOT escalate (it's noise, not a judgement call). Tunable.
MIN_CLAIM_THRESHOLD = Decimal("50.00")

# Default actor stamped on autonomous (non-human) actions.
DEFAULT_ACTOR = "system:auto-remediation"

_TWOPLACES = Decimal("0.01")

# Reason codes the remediation layer knows how to act on. A finding carrying
# anything outside this set is treated as not auto-remediable: the gate fails
# with INVALID_REASON, which (via escalation.py) routes to a human rather than
# being silently dropped.
VALID_REASON_CODES: frozenset[str] = frozenset(
    {
        DetectionReason.MISSING_COMMISSION.value,
        DetectionReason.UNDERPAID_BELOW_RATE.value,
        DetectionReason.DUPLICATE_PAYMENT.value,
        DetectionReason.RENEWAL_1PLUS1_NOT_PROVISIONED.value,
    }
)

# The only resolution state we consider a *confirmed, open* leak worth acting on.
CONFIRMED_STATE = ResolutionState.OPEN.value


# --------------------------------------------------------------------------- #
# ORM: the commission_claims table ("we are claiming this shortfall back")
# --------------------------------------------------------------------------- #
class CommissionClaim(Base):
    """A shortfall we are claiming back from the insurer.

    ``idempotency_key`` is UNIQUE — that database constraint is the last line of
    defence behind the in-code check: even under a race, two retries can never
    create two claims for the same (policy_no, reason_code, amount).
    """

    __tablename__ = "commission_claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    policy_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    claim_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# The finding contract the actions operate on
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ActionableFinding:
    """The minimal, self-contained contract an action needs to decide and act.

    Bridges a :class:`~leaksentinel.detection.rules.Finding` plus its
    reconciliation context (resolution state + claimable amount) into one shape,
    so the gate never has to reach back into the database to make its call.
    """

    policy_no: str
    reason_code: str
    amount: Decimal          # the claimable shortfall (positive), in rupees
    status: str              # resolution_state; must equal CONFIRMED_STATE to act
    detail: str = ""         # human-readable context, carried into the audit log


# --------------------------------------------------------------------------- #
# The gate (property #1 + #3): judgment is separate from, and blocks, the action
# --------------------------------------------------------------------------- #
class GateCode(str, Enum):
    """Why the gate allowed or refused an action."""

    OK = "OK"
    NOT_CONFIRMED = "NOT_CONFIRMED"   # not an open, confirmed leak
    INVALID_REASON = "INVALID_REASON"  # missing / unrecognised reason_code
    TOO_SMALL = "TOO_SMALL"           # below MIN_CLAIM_THRESHOLD


@dataclass(frozen=True)
class GateDecision:
    """The gate's verdict. ``allowed`` is the only thing that opens a write."""

    allowed: bool
    code: GateCode
    message: str


def validate_action(finding: ActionableFinding) -> GateDecision:
    """The single chokepoint. BLOCKS the write unless the finding is a confirmed,
    open leak with a real reason code and an amount worth claiming.

    Pure function: it reads nothing and writes nothing, so it is trivially
    testable and can be reasoned about in isolation from any side effect.
    """
    if finding.status != CONFIRMED_STATE:
        return GateDecision(
            False,
            GateCode.NOT_CONFIRMED,
            f"refused: finding for {finding.policy_no} is '{finding.status}', "
            f"not a confirmed open leak ('{CONFIRMED_STATE}').",
        )

    if not finding.reason_code or finding.reason_code not in VALID_REASON_CODES:
        return GateDecision(
            False,
            GateCode.INVALID_REASON,
            f"refused: finding for {finding.policy_no} has no actionable reason "
            f"code (got {finding.reason_code!r}).",
        )

    if finding.amount < MIN_CLAIM_THRESHOLD:
        return GateDecision(
            False,
            GateCode.TOO_SMALL,
            f"refused: amount ₹{_money(finding.amount)} for {finding.policy_no} is "
            f"below the ₹{MIN_CLAIM_THRESHOLD} minimum claim threshold.",
        )

    return GateDecision(True, GateCode.OK, "allowed")


# --------------------------------------------------------------------------- #
# Idempotency (property #2)
# --------------------------------------------------------------------------- #
def _money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


def idempotency_key(finding: ActionableFinding) -> str:
    """SHA-256 of policy_no + reason_code + amount.

    Amount is canonicalised to 2dp so ``100`` and ``100.00`` hash identically;
    the same logical claim always produces the same key, which is what makes a
    retry a no-op.
    """
    raw = f"{finding.policy_no}|{finding.reason_code}|{_money(finding.amount)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Audit trail (property #4) — shared by remediation AND escalation
# --------------------------------------------------------------------------- #
def payload_hash(payload: dict) -> str:
    """SHA-256 of a canonical JSON encoding of the action payload."""
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def record_audit(
    session: Session,
    *,
    action: str,
    payload: dict,
    actor: str,
    detail: str,
) -> AuditLog:
    """Append one immutable row to ``audit_log`` and return it.

    Called for EVERY action and EVERY blocked attempt — the audit row is the one
    write that is never gated, because "we refused to act" is itself a fact we
    must be able to prove later.
    """
    entry = AuditLog(
        action=action,
        payload_sha256=payload_hash(payload),
        actor=actor,
        detail=detail,
    )
    session.add(entry)
    session.flush()
    return entry


# --------------------------------------------------------------------------- #
# Action outcome
# --------------------------------------------------------------------------- #
class OutcomeStatus(str, Enum):
    CREATED = "created"     # a new record was written
    EXISTING = "existing"   # idempotent hit: returned the prior record
    BLOCKED = "blocked"     # the gate refused; nothing written but an audit row


@dataclass(frozen=True)
class RemediationOutcome:
    """What an action did (or refused to do), with the audit + idempotency keys."""

    status: OutcomeStatus
    finding: ActionableFinding
    decision: GateDecision
    audit_id: int
    idempotency_key: str | None = None
    claim: CommissionClaim | None = None

    @property
    def blocked(self) -> bool:
        return self.status is OutcomeStatus.BLOCKED


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
_DEFAULT_API = ExternalClaimsAPI()


def _resolve_session(session: Session | None) -> tuple[Session, bool]:
    """Use the caller's session, or open (and own) a fresh one."""
    if session is not None:
        return session, False
    return SessionLocal(), True


def create_commission_claim(
    finding: ActionableFinding,
    *,
    session: Session | None = None,
    actor: str = DEFAULT_ACTOR,
    api: ExternalClaimsAPI | None = None,
) -> RemediationOutcome:
    """Claim a MISSING_COMMISSION / UNDERPAID shortfall back from the insurer.

    Gated, idempotent, audited — the three properties, in that order:
      1. ``validate_action`` must pass, or we audit the block and return.
      2. If a claim with this idempotency key already exists, return it (no
         second row, no second external call).
      3. Otherwise call the external API, persist the claim, and audit success.
    """
    sess, owned = _resolve_session(session)
    used_api = api if api is not None else _DEFAULT_API
    try:
        key = idempotency_key(finding)
        base_payload = {
            "action": "create_commission_claim",
            "policy_no": finding.policy_no,
            "reason_code": finding.reason_code,
            "amount": str(_money(finding.amount)),
            "idempotency_key": key,
        }

        # (1) GATE — block before any external call or write.
        decision = validate_action(finding)
        if not decision.allowed:
            audit = record_audit(
                sess,
                action="create_commission_claim:BLOCKED",
                payload={**base_payload, "gate_code": decision.code.value},
                actor=actor,
                detail=f"BLOCKED [{decision.code.value}] {decision.message}",
            )
            if owned:
                sess.commit()
            return RemediationOutcome(
                status=OutcomeStatus.BLOCKED,
                finding=finding,
                decision=decision,
                audit_id=audit.id,
                idempotency_key=key,
            )

        # (2) IDEMPOTENCY — a retry returns the existing claim, never a duplicate.
        existing = sess.execute(
            select(CommissionClaim).where(CommissionClaim.idempotency_key == key)
        ).scalar_one_or_none()
        if existing is not None:
            audit = record_audit(
                sess,
                action="create_commission_claim:IDEMPOTENT",
                payload={**base_payload, "claim_id": existing.id},
                actor=actor,
                detail=(
                    f"idempotent retry for {finding.policy_no}: returned existing "
                    f"claim #{existing.id} (₹{existing.claim_amount}); no new claim, "
                    f"no second insurer call."
                ),
            )
            if owned:
                sess.commit()
            return RemediationOutcome(
                status=OutcomeStatus.EXISTING,
                finding=finding,
                decision=decision,
                audit_id=audit.id,
                idempotency_key=key,
                claim=existing,
            )

        # (3) ACT — external call, then persist, then audit the success.
        resp = used_api.submit_claim(
            policy_no=finding.policy_no,
            amount=_money(finding.amount),
            reason_code=finding.reason_code,
            idempotency_key=key,
        )
        claim = CommissionClaim(
            policy_no=finding.policy_no,
            claim_amount=_money(finding.amount),
            reason_code=finding.reason_code,
            idempotency_key=key,
            status="submitted",
            external_ref=resp.get("external_ref"),
        )
        sess.add(claim)
        sess.flush()
        audit = record_audit(
            sess,
            action="create_commission_claim",
            payload={**base_payload, "claim_id": claim.id, "external_ref": claim.external_ref},
            actor=actor,
            detail=(
                f"claimed ₹{claim.claim_amount} shortfall back from insurer for "
                f"{finding.policy_no} ({finding.reason_code}); external_ref "
                f"{claim.external_ref}. {finding.detail}".strip()
            ),
        )
        if owned:
            sess.commit()
        return RemediationOutcome(
            status=OutcomeStatus.CREATED,
            finding=finding,
            decision=decision,
            audit_id=audit.id,
            idempotency_key=key,
            claim=claim,
        )
    except Exception:
        if owned:
            sess.rollback()
        raise
    finally:
        if owned:
            sess.close()


def flag_for_rebilling(
    finding: ActionableFinding,
    *,
    session: Session | None = None,
    actor: str = DEFAULT_ACTOR,
    api: ExternalClaimsAPI | None = None,
) -> RemediationOutcome:
    """Mark a policy's reconciliation record for re-billing.

    Same governance shape as :func:`create_commission_claim`: gated, idempotent
    (flipping ``resolution_state`` to DISPUTED is a no-op if already disputed),
    and audited. The "record" being acted on is the matching
    ``reconciliation_results`` row(s) for the policy.
    """
    sess, owned = _resolve_session(session)
    used_api = api if api is not None else _DEFAULT_API
    try:
        key = idempotency_key(finding)
        base_payload = {
            "action": "flag_for_rebilling",
            "policy_no": finding.policy_no,
            "reason_code": finding.reason_code,
            "amount": str(_money(finding.amount)),
            "idempotency_key": key,
        }

        # (1) GATE.
        decision = validate_action(finding)
        if not decision.allowed:
            audit = record_audit(
                sess,
                action="flag_for_rebilling:BLOCKED",
                payload={**base_payload, "gate_code": decision.code.value},
                actor=actor,
                detail=f"BLOCKED [{decision.code.value}] {decision.message}",
            )
            if owned:
                sess.commit()
            return RemediationOutcome(
                status=OutcomeStatus.BLOCKED,
                finding=finding,
                decision=decision,
                audit_id=audit.id,
                idempotency_key=key,
            )

        rows = (
            sess.execute(
                select(ReconciliationResult).where(
                    ReconciliationResult.policy_no == finding.policy_no
                )
            )
            .scalars()
            .all()
        )

        # (2) IDEMPOTENCY — already flagged for rebilling => return unchanged.
        already = rows and all(
            r.resolution_state == ResolutionState.DISPUTED.value for r in rows
        )
        if already:
            audit = record_audit(
                sess,
                action="flag_for_rebilling:IDEMPOTENT",
                payload=base_payload,
                actor=actor,
                detail=(
                    f"idempotent retry for {finding.policy_no}: already flagged for "
                    f"rebilling (resolution_state=disputed); no change."
                ),
            )
            if owned:
                sess.commit()
            return RemediationOutcome(
                status=OutcomeStatus.EXISTING,
                finding=finding,
                decision=decision,
                audit_id=audit.id,
                idempotency_key=key,
            )

        # (3) ACT — flip the record(s), tell the billing system, audit.
        for r in rows:
            r.resolution_state = ResolutionState.DISPUTED.value
        resp = used_api.request_rebilling(
            policy_no=finding.policy_no,
            reason_code=finding.reason_code,
            idempotency_key=key,
        )
        sess.flush()
        audit = record_audit(
            sess,
            action="flag_for_rebilling",
            payload={**base_payload, "external_ref": resp.get("external_ref"), "rows": len(rows)},
            actor=actor,
            detail=(
                f"flagged {len(rows)} reconciliation record(s) for {finding.policy_no} "
                f"for re-billing ({finding.reason_code}); external_ref "
                f"{resp.get('external_ref')}."
            ),
        )
        if owned:
            sess.commit()
        return RemediationOutcome(
            status=OutcomeStatus.CREATED,
            finding=finding,
            decision=decision,
            audit_id=audit.id,
            idempotency_key=key,
        )
    except Exception:
        if owned:
            sess.rollback()
        raise
    finally:
        if owned:
            sess.close()
