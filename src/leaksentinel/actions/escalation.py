"""Escalation routing — the human-in-the-loop safety valve.

Auto-remediation (``remediation.py``) is only allowed to act on small, clearly
confirmed leaks. Two situations must NOT be auto-handled and must NOT be silently
dropped either:

  * the gate refused for a reason a *human* (not a threshold tweak) has to
    resolve — e.g. the finding isn't a confirmed open leak, or carries no
    recognised reason code; or
  * the amount is large enough (``>= ESCALATION_THRESHOLD``) that we never want a
    bot lodging the claim — a person signs off on high-value money.

In both cases we route the finding to a human queue (``escalations``) carrying
the FULL finding context + reason code, and audit the handoff. This is the same
stance a durable workflow takes when an activity exhausts its retries or hits a
non-retryable error: the work *parks* in a queryable state for a human, rather
than being lost. Judgment and action stay separated right to the end — the bot's
final "judgment" can be "this isn't mine to act on."
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import DateTime, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, Session, mapped_column

from leaksentinel.actions.externalapi import ExternalClaimsAPI
from leaksentinel.actions.remediation import (
    DEFAULT_ACTOR,
    ActionableFinding,
    GateCode,
    OutcomeStatus,
    RemediationOutcome,
    _money,
    _resolve_session,
    create_commission_claim,
    flag_for_rebilling,
    record_audit,
    validate_action,
)
from leaksentinel.db import Base
from leaksentinel.detection.rules import DetectionReason

# At or above this, even a perfectly valid claim is too big to auto-lodge — a
# human signs off. Tunable; would be a config/feature-flag value in production.
ESCALATION_THRESHOLD = Decimal("5000.00")

# Reason codes we auto-claim vs. auto-flag-for-rebilling. Anything not auto-
# claimable that still passes the gate is sent down the rebilling path.
_CLAIMABLE = {
    DetectionReason.MISSING_COMMISSION.value,
    DetectionReason.UNDERPAID_BELOW_RATE.value,
}


class EscalationReason(StrEnum):
    """Why a finding was routed to a human instead of auto-remediated."""

    HIGH_VALUE = "HIGH_VALUE"                # amount >= ESCALATION_THRESHOLD
    NOT_CONFIRMED = "GATE_NOT_CONFIRMED"     # gate: not a confirmed open leak
    INVALID_REASON = "GATE_INVALID_REASON"   # gate: no actionable reason code


# --------------------------------------------------------------------------- #
# ORM: the human-review queue
# --------------------------------------------------------------------------- #
class Escalation(Base):
    """One finding parked for human review, with the full context preserved."""

    __tablename__ = "escalations"

    id: Mapped[int] = mapped_column(primary_key=True)
    policy_no: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reason_code: Mapped[str] = mapped_column(String(64), nullable=False)
    escalation_reason: Mapped[str] = mapped_column(String(64), nullable=False)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    # The full ActionableFinding, serialised, so a human sees exactly what the
    # bot saw at decision time.
    finding_context: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


@dataclass(frozen=True)
class EscalationOutcome:
    """Result of routing a finding to the human queue."""

    escalation: Escalation
    reason: EscalationReason
    audit_id: int


def _finding_context(finding: ActionableFinding) -> str:
    return json.dumps(
        {
            "policy_no": finding.policy_no,
            "reason_code": finding.reason_code,
            "amount": str(_money(finding.amount)),
            "status": finding.status,
            "detail": finding.detail,
        },
        sort_keys=True,
    )


def should_escalate(
    finding: ActionableFinding, decision=None
) -> EscalationReason | None:
    """Decide whether a finding belongs to a human, not the bot.

    High-value wins first (we never auto-lodge big money, even when the gate
    would allow it). Otherwise, a gate refusal for anything *other than*
    TOO_SMALL is a judgement call a human must make. TOO_SMALL is just noise and
    is NOT escalated.
    """
    if finding.amount >= ESCALATION_THRESHOLD:
        return EscalationReason.HIGH_VALUE

    decision = decision if decision is not None else validate_action(finding)
    if not decision.allowed:
        if decision.code is GateCode.NOT_CONFIRMED:
            return EscalationReason.NOT_CONFIRMED
        if decision.code is GateCode.INVALID_REASON:
            return EscalationReason.INVALID_REASON
        # GateCode.TOO_SMALL — deliberately not escalated.
    return None


def create_escalation(
    finding: ActionableFinding,
    reason: EscalationReason,
    *,
    session: Session | None = None,
    actor: str = DEFAULT_ACTOR,
) -> EscalationOutcome:
    """Write the human-review record and audit the handoff."""
    sess, owned = _resolve_session(session)
    try:
        esc = Escalation(
            policy_no=finding.policy_no,
            reason_code=finding.reason_code or "UNKNOWN",
            escalation_reason=reason.value,
            amount=_money(finding.amount),
            finding_context=_finding_context(finding),
            status="queued",
        )
        sess.add(esc)
        sess.flush()
        audit = record_audit(
            sess,
            action="escalation",
            payload={
                "action": "escalation",
                "policy_no": finding.policy_no,
                "reason_code": finding.reason_code,
                "amount": str(_money(finding.amount)),
                "escalation_reason": reason.value,
                "escalation_id": esc.id,
            },
            actor=actor,
            detail=(
                f"routed {finding.policy_no} (₹{_money(finding.amount)}, "
                f"{finding.reason_code}) to human queue #{esc.id} "
                f"[{reason.value}]."
            ),
        )
        if owned:
            sess.commit()
        return EscalationOutcome(escalation=esc, reason=reason, audit_id=audit.id)
    except Exception:
        if owned:
            sess.rollback()
        raise
    finally:
        if owned:
            sess.close()


def route(
    finding: ActionableFinding,
    *,
    session: Session | None = None,
    actor: str = DEFAULT_ACTOR,
    api: ExternalClaimsAPI | None = None,
) -> RemediationOutcome | EscalationOutcome:
    """The orchestrator that ties judgment to action.

    1. High-value short-circuits straight to a human (no auto-claim of big money).
    2. Otherwise attempt the matching remediation action (claim vs. rebilling).
    3. If that action was BLOCKED for anything other than TOO_SMALL, the leak is
       real-but-not-auto-handleable, so escalate. A TOO_SMALL block just refuses.
    """
    # (1) High-value never auto-acts.
    if finding.amount >= ESCALATION_THRESHOLD:
        return create_escalation(
            finding, EscalationReason.HIGH_VALUE, session=session, actor=actor
        )

    # (2) Dispatch to the right action by reason code.
    action = (
        create_commission_claim
        if finding.reason_code in _CLAIMABLE
        else flag_for_rebilling
    )
    outcome = action(finding, session=session, actor=actor, api=api)

    # (3) Blocked for a non-trivial reason => hand to a human.
    if outcome.status is OutcomeStatus.BLOCKED and outcome.decision.code is not GateCode.TOO_SMALL:
        reason = (
            EscalationReason.NOT_CONFIRMED
            if outcome.decision.code is GateCode.NOT_CONFIRMED
            else EscalationReason.INVALID_REASON
        )
        return create_escalation(finding, reason, session=session, actor=actor)

    return outcome
