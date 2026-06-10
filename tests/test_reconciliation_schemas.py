"""Unit tests for the canonical ReconciliationView (no DB required)."""

from decimal import Decimal

from leaksentinel.detection.rules import DetectionReason
from leaksentinel.reconciliation.schemas import (
    ReconciliationView,
    ResolutionState,
)


def test_view_strips_and_defaults() -> None:
    v = ReconciliationView(policy_no="  P-1 ")
    assert v.policy_no == "P-1"
    assert v.normalization_notes == []
    assert v.has_amounts is False
    assert v.delta is None


def test_view_delta_when_amounts_present() -> None:
    v = ReconciliationView(
        expected_commission=Decimal("100.00"),
        actual_commission=Decimal("82.50"),
    )
    assert v.has_amounts is True
    assert v.delta == Decimal("-17.50")


def test_enum_values() -> None:
    # The single canonical reason vocabulary now lives in DetectionReason.
    assert DetectionReason.UNDERPAID_BELOW_RATE.value == "UNDERPAID_BELOW_RATE"
    assert DetectionReason.DUPLICATE_PAYMENT.value == "DUPLICATE_PAYMENT"
    assert ResolutionState.OPEN.value == "open"
