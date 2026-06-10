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
  4. Keep an *append-only, hash-chained audit trail*. Every action AND every
     blocked attempt appends to ``audit_log`` with a chained SHA-256 of the
     payload, so any decision is reconstructable — and tampering is detectable —
     after the fact.

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
from enum import StrEnum

from sqlalchemy import DateTime, Index, Numeric, String, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.orm.exc import StaleDataError

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

# Claim lifecycle. A claim is "active" until it reaches CLAIM_CLOSED; the partial
# unique index below only constrains active claims, so a fresh leak on the same
# (policy_no, reason_code) can be re-claimed after the prior claim is closed.
CLAIM_PENDING = "pending"      # row reserved, insurer not yet called
CLAIM_SUBMITTED = "submitted"  # lodged with the insurer
CLAIM_CLOSED = "closed"        # settled / written off — no longer active


# --------------------------------------------------------------------------- #
# ORM: the commission_claims table ("we are claiming this shortfall back")
# --------------------------------------------------------------------------- #
class CommissionClaim(Base):
    """A shortfall we are claiming back from the insurer.

    Uniqueness is enforced by a **partial unique index** on
    ``(policy_no, reason_code)`` restricted to *active* claims (``status`` other
    than ``closed``). That database constraint — not a SELECT-then-INSERT in the
    application — is the authority: even under a concurrent race, two callers can
    never create two active claims for the same logical leak. The loser of the
    race hits the index, rolls back its savepoint, and returns the existing row.

    ``idempotency_key`` is the stable key handed to the insurer (derived from
    ``policy_no`` + ``reason_code``); it is indexed but intentionally NOT globally
    unique, so a closed claim does not block re-claiming a recurrence later.
    """

    __tablename__ = "commission_claims"

    id: Mapped[int] = mapped_column(primary_key=True)
    policy_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    claim_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    external_ref: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        # One ACTIVE claim per (policy_no, reason_code). Closed claims are exempt,
        # so a later recurrence can be claimed afresh. Works on Postgres and SQLite.
        Index(
            "uq_commission_claims_active",
            "policy_no",
            "reason_code",
            unique=True,
            postgresql_where=text("status <> 'closed'"),
            sqlite_where=text("status <> 'closed'"),
        ),
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
class GateCode(StrEnum):
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
    """SHA-256 of the logical claim identity ``(policy_no, reason_code)``.

    The amount is deliberately NOT part of the key: a data correction that
    changes the shortfall must *update* the existing claim, not mint a second one
    (the same fact is being claimed). Encoding via ``json.dumps`` of a list
    avoids delimiter-injection ambiguity that an f-string with ``|`` separators
    would allow if a field ever contained the separator.
    """
    raw = json.dumps([finding.policy_no, finding.reason_code], sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Audit trail (property #4) — shared by remediation AND escalation
# --------------------------------------------------------------------------- #
def _canonical_payload(payload: dict) -> str:
    """Stable JSON encoding of an action payload (sorted keys)."""
    return json.dumps(payload, sort_keys=True, default=str)


def payload_hash(payload: dict) -> str:
    """SHA-256 of the canonical JSON encoding of a single action payload."""
    return hashlib.sha256(_canonical_payload(payload).encode("utf-8")).hexdigest()


def record_audit(
    session: Session,
    *,
    action: str,
    payload: dict,
    actor: str,
    detail: str,
) -> AuditLog:
    """Append one row to the append-only, hash-chained ``audit_log`` and return it.

    Called for EVERY action and EVERY blocked attempt — the audit row is the one
    write that is never gated, because "we refused to act" is itself a fact we
    must be able to prove later.

    The row's ``payload_sha256`` is ``sha256(prev_hash + canonical_json(payload))``
    and ``prev_hash`` is the previous row's hash, so deleting or reordering any
    row breaks the chain from that point on; ``sequence_no`` is gap-free and
    UNIQUE, so a missing row is detectable even without recomputing hashes.
    """
    last = session.execute(
        select(AuditLog.sequence_no, AuditLog.payload_sha256)
        .order_by(AuditLog.sequence_no.desc())
        .limit(1)
    ).first()
    prev_seq = last.sequence_no if last is not None else 0
    prev_hash = (last.payload_sha256 if last is not None else None) or ""

    chained = hashlib.sha256(
        (prev_hash + _canonical_payload(payload)).encode("utf-8")
    ).hexdigest()

    entry = AuditLog(
        sequence_no=prev_seq + 1,
        action=action,
        payload_sha256=chained,
        prev_hash=(prev_hash or None),
        actor=actor,
        detail=detail,
    )
    session.add(entry)
    session.flush()
    return entry


def verify_audit_chain(session: Session) -> tuple[bool, str | None]:
    """Verify the audit log's integrity. Returns ``(ok, reason)``.

    Checks, in ``sequence_no`` order, that (a) the sequence is gap-free from 1
    and (b) each row's ``prev_hash`` links to the previous row's hash. A deleted
    or reordered row breaks one of these, so ``ok=False`` is tamper evidence.
    """
    rows = (
        session.execute(select(AuditLog).order_by(AuditLog.sequence_no))
        .scalars()
        .all()
    )
    prev: AuditLog | None = None
    for i, row in enumerate(rows, start=1):
        if row.sequence_no != i:
            return False, f"sequence gap: expected {i}, found {row.sequence_no}"
        expected_link = prev.payload_sha256 if prev is not None else None
        if (row.prev_hash or None) != (expected_link or None):
            return False, f"broken hash chain at sequence_no {row.sequence_no}"
        prev = row
    return True, None


# --------------------------------------------------------------------------- #
# Action outcome
# --------------------------------------------------------------------------- #
class OutcomeStatus(StrEnum):
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

        # (2) RESERVE — INSERT the claim first, inside a SAVEPOINT, so the DB's
        # partial unique index (not a racy SELECT) arbitrates who wins. The
        # insurer is NOT contacted yet — we only talk to them once the row is ours.
        amount = _money(finding.amount)
        claim = CommissionClaim(
            policy_no=finding.policy_no,
            claim_amount=amount,
            reason_code=finding.reason_code,
            idempotency_key=key,
            status=CLAIM_PENDING,
        )
        try:
            with sess.begin_nested():
                sess.add(claim)
                sess.flush()
        except IntegrityError:
            # (3) IDEMPOTENT — an active claim already exists for this leak: this
            # is a retry, or we lost a concurrent race. Return the existing row;
            # never a second row, and crucially never a second insurer call. A
            # changed amount UPDATES the existing claim in place (data correction).
            if claim in sess.new:
                sess.expunge(claim)
            existing = sess.execute(
                select(CommissionClaim).where(
                    CommissionClaim.policy_no == finding.policy_no,
                    CommissionClaim.reason_code == finding.reason_code,
                    CommissionClaim.status != CLAIM_CLOSED,
                )
            ).scalars().first()
            updated = existing is not None and existing.claim_amount != amount
            if updated:
                existing.claim_amount = amount
                sess.flush()
            audit = record_audit(
                sess,
                action="create_commission_claim:IDEMPOTENT",
                payload={
                    **base_payload,
                    "claim_id": existing.id if existing else None,
                    "amount_updated": updated,
                },
                actor=actor,
                detail=(
                    f"idempotent retry for {finding.policy_no}: active claim "
                    f"#{existing.id if existing else '?'} already exists "
                    f"(₹{existing.claim_amount if existing else '?'}); "
                    + ("amount updated in place; " if updated else "")
                    + "no new claim, no second insurer call."
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

        # (4) ACT — the row is ours. Now (and only now) call the insurer exactly
        # once, flip the status to submitted, and audit the success.
        resp = used_api.submit_claim(
            policy_no=finding.policy_no,
            amount=amount,
            reason_code=finding.reason_code,
            idempotency_key=key,
        )
        claim.status = CLAIM_SUBMITTED
        claim.external_ref = resp.get("external_ref")
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

        # (3) ACT — flip the record(s) inside a SAVEPOINT so a concurrent flip
        # cannot double-apply, and only contact the billing system once the flip
        # is durably ours.
        try:
            with sess.begin_nested():
                for r in rows:
                    r.resolution_state = ResolutionState.DISPUTED.value
                sess.flush()
        except (IntegrityError, StaleDataError):
            # Lost a concurrent race: another worker already flipped these rows.
            # Idempotent — no billing call, no change.
            audit = record_audit(
                sess,
                action="flag_for_rebilling:IDEMPOTENT",
                payload=base_payload,
                actor=actor,
                detail=(
                    f"concurrent retry for {finding.policy_no}: rebilling already "
                    f"applied by another worker; no change, no second billing call."
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

        resp = used_api.request_rebilling(
            policy_no=finding.policy_no,
            reason_code=finding.reason_code,
            idempotency_key=key,
        )
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
