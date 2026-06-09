"""Tests for the actions layer's three non-negotiable safety properties.

Each property gets its own assertion, run against an isolated in-memory SQLite
database (no Postgres / no synthetic data needed — these tests prove governance,
not detection accuracy):

  1. GATED      — an unvalidated or too-small action is BLOCKED and writes no
                  commission_claims row.
  2. IDEMPOTENT — calling the same remediation twice creates exactly ONE claim.
  3. AUDITED    — both a success and a blocked attempt record a SHA-256 audit row.

Plus: a high-value finding ROUTES to the escalation queue instead of auto-acting.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from leaksentinel.actions.escalation import (
    ESCALATION_THRESHOLD,
    Escalation,
    EscalationOutcome,
    EscalationReason,
    route,
)
from leaksentinel.actions.externalapi import ExternalClaimsAPI
from leaksentinel.actions.remediation import (
    MIN_CLAIM_THRESHOLD,
    ActionableFinding,
    CommissionClaim,
    GateCode,
    OutcomeStatus,
    RemediationOutcome,
    create_commission_claim,
    idempotency_key,
    validate_action,
)
from leaksentinel.db import Base
from leaksentinel.detection.rules import DetectionReason
from leaksentinel.reconciliation.models import AuditLog

CONFIRMED = "open"
MISSING = DetectionReason.MISSING_COMMISSION.value


@pytest.fixture()
def session():
    """Fresh in-memory SQLite with every table created."""
    # Importing the action modules above already registered their models on
    # Base.metadata; create_all here picks up the whole schema.
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Sess = sessionmaker(bind=engine, future=True)
    sess = Sess()
    try:
        yield sess
    finally:
        sess.close()


def _finding(amount, *, reason=MISSING, status=CONFIRMED, policy_no="POL-001"):
    return ActionableFinding(
        policy_no=policy_no,
        reason_code=reason,
        amount=Decimal(amount),
        status=status,
        detail="planted leak",
    )


def _claims(session):
    return session.execute(select(CommissionClaim)).scalars().all()


def _audits(session):
    return session.execute(select(AuditLog)).scalars().all()


# --------------------------------------------------------------------------- #
# 1. GATED
# --------------------------------------------------------------------------- #
def test_gate_blocks_unconfirmed_finding(session):
    """A finding that isn't a confirmed open leak is refused — no claim written."""
    api = ExternalClaimsAPI()
    finding = _finding("500.00", status="in_review")  # not 'open'

    outcome = create_commission_claim(finding, session=session, api=api)

    assert outcome.blocked
    assert outcome.decision.code is GateCode.NOT_CONFIRMED
    assert _claims(session) == []          # never wrote
    assert api.calls == []                 # never called the insurer


def test_gate_blocks_too_small(session):
    """An amount below MIN_CLAIM_THRESHOLD is refused — no claim written."""
    api = ExternalClaimsAPI()
    finding = _finding(MIN_CLAIM_THRESHOLD - Decimal("0.01"))

    outcome = create_commission_claim(finding, session=session, api=api)

    assert outcome.blocked
    assert outcome.decision.code is GateCode.TOO_SMALL
    assert _claims(session) == []
    assert api.calls == []


def test_gate_blocks_invalid_reason(session):
    """A finding with no recognised reason code is refused."""
    finding = _finding("500.00", reason="NOT_A_REAL_CODE")
    decision = validate_action(finding)
    assert not decision.allowed
    assert decision.code is GateCode.INVALID_REASON


def test_valid_finding_passes_gate_and_writes_one_claim(session):
    api = ExternalClaimsAPI()
    finding = _finding("500.00")

    outcome = create_commission_claim(finding, session=session, api=api)

    assert outcome.status is OutcomeStatus.CREATED
    claims = _claims(session)
    assert len(claims) == 1
    assert claims[0].claim_amount == Decimal("500.00")
    assert claims[0].status == "submitted"
    assert claims[0].external_ref  # populated from the (mocked) insurer response
    assert len(api.calls) == 1     # exactly one external call


# --------------------------------------------------------------------------- #
# 2. IDEMPOTENT
# --------------------------------------------------------------------------- #
def test_idempotent_retry_creates_exactly_one_row(session):
    """The headline property: run the SAME remediation twice -> ONE claim row."""
    api = ExternalClaimsAPI()
    finding = _finding("1234.56")

    first = create_commission_claim(finding, session=session, api=api)
    second = create_commission_claim(finding, session=session, api=api)

    # Exactly one row, despite two calls.
    assert len(_claims(session)) == 1

    assert first.status is OutcomeStatus.CREATED
    assert second.status is OutcomeStatus.EXISTING
    # Same logical claim => same idempotency key => same row returned.
    assert first.idempotency_key == second.idempotency_key
    assert second.claim is not None
    assert second.claim.id == first.claim.id
    # And the insurer was only ever called once — retries never double-pay.
    assert len(api.calls) == 1


def test_idempotency_key_is_sha256_of_policy_reason_amount(session):
    key = idempotency_key(_finding("100.00"))
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)
    # 100 and 100.00 are the same logical amount -> same key.
    assert key == idempotency_key(_finding("100"))


# --------------------------------------------------------------------------- #
# 3. AUDITED — every action and every blocked attempt records a hash
# --------------------------------------------------------------------------- #
def test_success_is_audited_with_hash(session):
    out = create_commission_claim(_finding("500.00"), session=session, api=ExternalClaimsAPI())
    audits = _audits(session)
    assert len(audits) == 1
    row = audits[0]
    assert row.action == "create_commission_claim"
    assert row.id == out.audit_id
    assert row.payload_sha256 and len(row.payload_sha256) == 64
    assert row.actor == "system:auto-remediation"
    assert "claimed ₹500.00" in row.detail


def test_blocked_attempt_is_audited_with_hash(session):
    create_commission_claim(_finding("10.00"), session=session, api=ExternalClaimsAPI())
    audits = _audits(session)
    assert len(audits) == 1
    row = audits[0]
    assert row.action == "create_commission_claim:BLOCKED"
    assert row.payload_sha256 and len(row.payload_sha256) == 64
    assert "BLOCKED" in row.detail
    # The block was audited even though NO claim was written.
    assert _claims(session) == []


# --------------------------------------------------------------------------- #
# Escalation routing
# --------------------------------------------------------------------------- #
def test_high_value_routes_to_escalation_not_a_claim(session):
    """A high-value finding goes to the human queue, not an auto-claim."""
    api = ExternalClaimsAPI()
    finding = _finding(ESCALATION_THRESHOLD + Decimal("1.00"))

    outcome = route(finding, session=session, api=api)

    assert isinstance(outcome, EscalationOutcome)
    assert outcome.reason is EscalationReason.HIGH_VALUE
    # Routed for human sign-off, not auto-lodged.
    assert _claims(session) == []
    assert api.calls == []
    # The escalation row carries the FULL finding context + reason code.
    escs = session.execute(select(Escalation)).scalars().all()
    assert len(escs) == 1
    assert escs[0].reason_code == MISSING
    assert escs[0].status == "queued"
    assert str(finding.policy_no) in escs[0].finding_context
    # And the handoff was audited.
    assert any(a.action == "escalation" for a in _audits(session))


def test_unconfirmed_finding_routes_to_escalation(session):
    """Gate-failed-not-too-small (here: not confirmed) escalates to a human."""
    finding = _finding("800.00", status="resolved")
    outcome = route(finding, session=session, api=ExternalClaimsAPI())
    assert isinstance(outcome, EscalationOutcome)
    assert outcome.reason is EscalationReason.NOT_CONFIRMED
    assert session.execute(select(Escalation)).scalars().all()


def test_too_small_refuses_without_escalating(session):
    """TOO_SMALL is noise: refuse, but do NOT escalate."""
    finding = _finding("5.00")
    outcome = route(finding, session=session, api=ExternalClaimsAPI())
    assert isinstance(outcome, RemediationOutcome)
    assert outcome.blocked
    assert outcome.decision.code is GateCode.TOO_SMALL
    assert session.execute(select(Escalation)).scalars().all() == []
