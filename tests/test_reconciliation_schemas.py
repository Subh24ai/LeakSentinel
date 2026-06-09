"""Unit tests for the canonical ReconciliationView (no DB required)."""

from decimal import Decimal

from leaksentinel.reconciliation.schemas import (
    ReasonCode,
    ReconciliationView,
    ReconStatus,
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
    assert ReconStatus.SHORT_PAID.value == "short_paid"
    assert ReasonCode.AMOUNT_MISMATCH.value == "AMOUNT_MISMATCH"
    assert ResolutionState.OPEN.value == "open"
