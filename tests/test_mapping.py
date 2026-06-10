"""Unit tests for build_actionable_finding — the detection→action seam.

Pure mapping, no database: construct a Finding and a (transient, unpersisted)
ReconciliationResult and assert the resulting ActionableFinding carries the right
claimable amount and confirmed status for each leak shape.
"""

from __future__ import annotations

from decimal import Decimal

from leaksentinel.actions.mapping import build_actionable_finding
from leaksentinel.actions.remediation import CONFIRMED_STATE, validate_action
from leaksentinel.detection.rules import DetectionReason, Finding, Severity
from leaksentinel.reconciliation.models import ReconciliationResult


def _finding(reason: DetectionReason, policy_no="POL-1") -> Finding:
    return Finding(
        policy_no=policy_no,
        reason_code=reason.value,
        severity=Severity.HIGH.value,
        explanation=f"{reason.value} on {policy_no}",
        detector="rule:test",
    )


def _recon(**kw) -> ReconciliationResult:
    # Transient ORM instance (never added to a session) — just an attribute bag.
    return ReconciliationResult(**kw)


def test_missing_commission_amount_is_full_expected_and_open():
    finding = _finding(DetectionReason.MISSING_COMMISSION)
    recon = _recon(
        policy_no="POL-1",
        status="MISSING_COMMISSION",
        reason_code="NO_PAYMENT",
        expected=Decimal("100.35"),
        actual=Decimal("0.00"),
        delta=Decimal("-100.35"),
        resolution_state="open",
    )
    af = build_actionable_finding(finding, recon)

    assert af.policy_no == "POL-1"
    assert af.reason_code == DetectionReason.MISSING_COMMISSION.value
    assert af.amount == Decimal("100.35")        # whole unpaid commission at risk
    assert af.status == CONFIRMED_STATE          # 'open' -> gate will allow
    assert af.detail.startswith("MISSING_COMMISSION")
    # And it actually passes the gate (above the 50.00 threshold).
    assert validate_action(af).allowed


def test_underpaid_amount_is_the_shortfall():
    finding = _finding(DetectionReason.UNDERPAID_BELOW_RATE)
    recon = _recon(
        policy_no="POL-2",
        status="UNDERPAID",
        expected=Decimal("300.00"),
        actual=Decimal("247.15"),
        delta=Decimal("-52.85"),
        resolution_state="open",
    )
    af = build_actionable_finding(finding, recon)

    assert af.amount == Decimal("52.85")         # expected - actual
    assert af.status == "open"
    assert validate_action(af).allowed


def test_matched_row_maps_to_expected_and_unconfirmed_status():
    """A renewal leak sits on an otherwise-MATCHED policy (delta 0, no resolution
    state): amount falls back to expected and the status is non-confirming, so the
    gate refuses to auto-act and the finding routes to a human."""
    finding = _finding(DetectionReason.RENEWAL_1PLUS1_NOT_PROVISIONED)
    recon = _recon(
        policy_no="POL-3",
        status="MATCHED",
        expected=Decimal("180.00"),
        actual=Decimal("180.00"),
        delta=Decimal("0.00"),
        resolution_state=None,
    )
    af = build_actionable_finding(finding, recon)

    assert af.amount == Decimal("180.00")        # value at stake, fallback to expected
    assert af.status == ""                       # not 'open'
    decision = validate_action(af)
    assert not decision.allowed                  # gate blocks -> escalation path


def test_missing_recon_row_is_safe():
    af = build_actionable_finding(_finding(DetectionReason.ANOMALY_UNCLASSIFIED), None)
    assert af.amount == Decimal("0.00")
    assert af.status == ""
