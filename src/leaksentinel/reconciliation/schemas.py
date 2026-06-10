"""Canonical schemas for the reconciliation domain.

Every insurer commission feed — however messy — is normalized into a
:class:`ReconciliationView` before matching. Keeping a single canonical shape
means the reconciliation engine never has to know an insurer's idiosyncratic
column names, status vocabulary, or date formats.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# Rounding tolerance shared by the reconciliation engine and the rounding-delta
# detector: a |delta| at or below this reads as a clean match. It lives in this
# dependency-free module so the engine and the detection layer can both import it
# without forming an import cycle.
#
# The system's single canonical reason-code vocabulary is
# :class:`leaksentinel.detection.rules.DetectionReason`. The engine maps its
# per-policy verdicts onto those same codes at persist time (the old parallel
# ``ReconStatus`` / ``ReasonCode`` enums were removed).
TOLERANCE = Decimal("1.00")


class ResolutionState(StrEnum):
    """Lifecycle of a flagged discrepancy."""

    OPEN = "open"
    IN_REVIEW = "in_review"
    DISPUTED = "disputed"
    RESOLVED = "resolved"
    WRITTEN_OFF = "written_off"


class PaymentStatus(StrEnum):
    """Canonical payment status, mapped from each insurer's free-text status."""

    PAID = "paid"
    PENDING = "pending"
    REVERSED = "reversed"
    UNKNOWN = "unknown"


class ReconciliationView(BaseModel):
    """Canonical, normalized representation of a single commission line.

    All insurer feeds (and, where useful, our own CRM/policy rows) are mapped
    into this shape. Fields are deliberately permissive — anything that can be
    missing or malformed upstream is ``Optional`` here so normalization never
    fails on dirty input. Quality issues are flagged via ``normalization_notes``
    instead.
    """

    model_config = ConfigDict(use_enum_values=False, str_strip_whitespace=True)

    # Identity / linkage
    policy_no: str | None = Field(
        default=None, description="Normalized policy number used for matching."
    )
    insurer_name: str | None = Field(default=None)
    source_ref: str | None = Field(
        default=None, description="Original raw reference from the feed (audit trail)."
    )

    # Money
    premium: Decimal | None = Field(default=None)
    expected_commission_rate: Decimal | None = Field(default=None)
    expected_commission: Decimal | None = Field(
        default=None, description="premium * rate, or insurer-stated expected amount."
    )
    actual_commission: Decimal | None = Field(
        default=None, description="What the insurer reports having paid."
    )

    # Dates
    issued_date: dt.date | None = Field(default=None)
    paid_date: dt.date | None = Field(default=None)

    # Provenance / quality
    raw_status: str | None = Field(
        default=None, description="Untranslated status text from the feed."
    )
    payment_status: PaymentStatus | None = Field(
        default=None, description="Canonical status mapped from raw_status."
    )
    source_system: str | None = Field(
        default=None, description="Which feed/parser produced this view."
    )
    normalization_notes: list[str] = Field(
        default_factory=list,
        description="Data-quality flags raised while normalizing.",
    )

    @property
    def has_amounts(self) -> bool:
        """True when both expected and actual amounts are present."""
        return self.expected_commission is not None and self.actual_commission is not None

    @property
    def delta(self) -> Decimal | None:
        """``actual - expected`` when both are known, else ``None``."""
        if not self.has_amounts:
            return None
        return self.actual_commission - self.expected_commission
