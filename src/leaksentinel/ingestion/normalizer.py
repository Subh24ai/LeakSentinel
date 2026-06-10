"""Insurer-feed normalizers.

Each insurer sends commission statements in its own messy format. A
:class:`Normalizer` maps one raw CSV row into the canonical
:class:`~leaksentinel.reconciliation.schemas.ReconciliationView`, handling:

* differing column names,
* four date encodings (DD/MM/YYYY, YYYY-MM-DD, epoch seconds, DD-Mon-YYYY),
* commission expressed as an absolute amount vs. a rate to apply to premium,
* policy references that are either exact or embedded in a longer string,
* inconsistent free-text status normalized to :class:`PaymentStatus`.

Strategy/registry pattern: subclass :class:`Normalizer`, decorate it with
:func:`register`, and it becomes available via :func:`get_normalizer`. Adding a
fifth insurer means adding one class — no dispatcher to edit.
"""

from __future__ import annotations

import datetime as dt
from abc import ABC, abstractmethod
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from leaksentinel.reconciliation.schemas import PaymentStatus, ReconciliationView

_TWOPLACES = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    """Round a Decimal to 2 places, half-up (currency)."""
    return value.quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


def _to_decimal(raw: str | None) -> Decimal | None:
    """Parse a possibly-messy numeric string; ``None`` if blank/invalid."""
    if raw is None:
        return None
    cleaned = raw.strip().replace(",", "").replace("₹", "").replace("Rs.", "").strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Canonical status mapping
# --------------------------------------------------------------------------- #
_STATUS_SYNONYMS: dict[str, PaymentStatus] = {
    # paid / settled
    "settled": PaymentStatus.PAID,
    "processed": PaymentStatus.PAID,
    "paid": PaymentStatus.PAID,
    "cleared": PaymentStatus.PAID,
    "success": PaymentStatus.PAID,
    "done": PaymentStatus.PAID,
    "completed": PaymentStatus.PAID,
    # pending
    "pending": PaymentStatus.PENDING,
    "in process": PaymentStatus.PENDING,
    "in_process": PaymentStatus.PENDING,
    "processing": PaymentStatus.PENDING,
    "initiated": PaymentStatus.PENDING,
    # reversed
    "reversed": PaymentStatus.REVERSED,
    "reversal": PaymentStatus.REVERSED,
    "refund": PaymentStatus.REVERSED,
    "refunded": PaymentStatus.REVERSED,
    "clawback": PaymentStatus.REVERSED,
}


def normalize_status(raw: str | None) -> tuple[PaymentStatus, str | None]:
    """Map free-text status to a canonical enum, plus a note if unrecognized."""
    if raw is None or not raw.strip():
        return PaymentStatus.UNKNOWN, "missing_status"
    key = raw.strip().lower()
    status = _STATUS_SYNONYMS.get(key)
    if status is None:
        return PaymentStatus.UNKNOWN, f"ambiguous_status:{raw.strip()}"
    return status, None


# --------------------------------------------------------------------------- #
# Base interface
# --------------------------------------------------------------------------- #
class Normalizer(ABC):
    """Base strategy: turn one raw feed row into a ``ReconciliationView``.

    Subclasses declare their column names and commission mode, and implement
    :meth:`parse_policy_no` and :meth:`parse_date`. The template
    :meth:`normalize_row` wires everything together and accumulates
    data-quality notes.
    """

    insurer_name: str
    source_system: str
    source_file: str
    commission_mode: str  # "absolute" | "rate_pct"

    # Column names in this insurer's raw CSV.
    COL_REF: str
    COL_PREMIUM: str
    COL_COMMISSION: str  # holds an absolute amount OR a rate, per commission_mode
    COL_DATE: str
    COL_STATUS: str

    # -- per-insurer hooks --------------------------------------------------- #
    @abstractmethod
    def parse_policy_no(self, raw_ref: str | None) -> tuple[str | None, str | None]:
        """Return ``(policy_no, note)``; ``note`` is set if the ref is unparseable."""

    @abstractmethod
    def parse_date(self, raw_date: str | None) -> tuple[dt.date | None, str | None]:
        """Return ``(date, note)``; ``note`` is set if the date is unparseable."""

    # -- template method ----------------------------------------------------- #
    def normalize_row(self, row: dict[str, str]) -> ReconciliationView:
        notes: list[str] = []

        raw_ref = (row.get(self.COL_REF) or "").strip()
        policy_no, ref_note = self.parse_policy_no(raw_ref)
        if ref_note:
            notes.append(ref_note)

        premium = _to_decimal(row.get(self.COL_PREMIUM))
        if premium is None:
            notes.append("missing_or_invalid_premium")

        actual_commission, comm_note = self._compute_amount(row, premium)
        if comm_note:
            notes.append(comm_note)

        paid_date, date_note = self.parse_date(row.get(self.COL_DATE))
        if date_note:
            notes.append(date_note)

        raw_status = row.get(self.COL_STATUS)
        payment_status, status_note = normalize_status(raw_status)
        if status_note:
            notes.append(status_note)

        return ReconciliationView(
            policy_no=policy_no,
            insurer_name=self.insurer_name,
            source_ref=raw_ref or None,
            premium=premium,
            actual_commission=actual_commission,
            paid_date=paid_date,
            raw_status=raw_status,
            payment_status=payment_status,
            source_system=self.source_system,
            normalization_notes=notes,
        )

    def normalize_feed(self, rows: Iterable[dict[str, str]]) -> list[ReconciliationView]:
        return [self.normalize_row(row) for row in rows]

    # -- shared helpers ------------------------------------------------------ #
    def _compute_amount(
        self, row: dict[str, str], premium: Decimal | None
    ) -> tuple[Decimal | None, str | None]:
        """Resolve the commission to an absolute amount per ``commission_mode``."""
        raw = row.get(self.COL_COMMISSION)
        value = _to_decimal(raw)

        if self.commission_mode == "absolute":
            if value is None:
                return None, "missing_commission_amount"
            return _money(value), None

        # rate_pct: commission is a percentage to apply to premium.
        if value is None:
            return None, "missing_commission_rate"
        if premium is None:
            return None, "rate_without_premium"
        return _money(premium * value / Decimal(100)), None

    @staticmethod
    def _parse_with_format(
        raw: str | None, fmt: str
    ) -> tuple[dt.date | None, str | None]:
        if raw is None or not raw.strip():
            return None, "missing_date"
        try:
            return dt.datetime.strptime(raw.strip(), fmt).date(), None
        except ValueError:
            return None, f"unparseable_date:{raw.strip()}"


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
_REGISTRY: dict[str, type[Normalizer]] = {}


def register(cls: type[Normalizer]) -> type[Normalizer]:
    """Class decorator: register a normalizer under its ``insurer_name``."""
    _REGISTRY[cls.insurer_name] = cls
    return cls


def get_normalizer(insurer_name: str) -> Normalizer:
    """Instantiate the normalizer registered for ``insurer_name``."""
    try:
        return _REGISTRY[insurer_name]()
    except KeyError as exc:
        raise KeyError(
            f"No normalizer registered for {insurer_name!r}. "
            f"Registered: {sorted(_REGISTRY)}"
        ) from exc


def registered_insurers() -> list[str]:
    return sorted(_REGISTRY)


# --------------------------------------------------------------------------- #
# Concrete normalizers — one per insurer
# --------------------------------------------------------------------------- #
@register
class ICICILombardNormalizer(Normalizer):
    insurer_name = "ICICI Lombard"
    source_system = "icici_lombard"
    source_file = "icici_lombard_feed.csv"
    commission_mode = "absolute"

    COL_REF = "Policy Reference"
    COL_PREMIUM = "Premium Collected"
    COL_COMMISSION = "Commission Payable"
    COL_DATE = "Payment Date"
    COL_STATUS = "Settlement Status"

    def parse_policy_no(self, raw_ref: str | None) -> tuple[str | None, str | None]:
        ref = (raw_ref or "").strip()
        if not ref:
            return None, "unparseable_policy_ref:<empty>"
        return ref, None  # exact match

    def parse_date(self, raw_date: str | None) -> tuple[dt.date | None, str | None]:
        return self._parse_with_format(raw_date, "%d/%m/%Y")


@register
class BajajNormalizer(Normalizer):
    insurer_name = "Bajaj"
    source_system = "bajaj"
    source_file = "bajaj_feed.csv"
    commission_mode = "rate_pct"

    COL_REF = "policy_ref"
    COL_PREMIUM = "gross_premium"
    COL_COMMISSION = "commission_rate_pct"
    COL_DATE = "settled_on"
    COL_STATUS = "txn_status"

    def parse_policy_no(self, raw_ref: str | None) -> tuple[str | None, str | None]:
        # Embedded: BAJAJ/<year>/<policy_no>/<state>
        ref = (raw_ref or "").strip()
        parts = ref.split("/")
        if len(parts) == 4 and parts[0].upper() == "BAJAJ" and parts[2].strip():
            return parts[2].strip(), None
        return None, f"unparseable_policy_ref:{ref or '<empty>'}"

    def parse_date(self, raw_date: str | None) -> tuple[dt.date | None, str | None]:
        if raw_date is None or not raw_date.strip():
            return None, "missing_date"
        try:
            return dt.date.fromisoformat(raw_date.strip()), None
        except ValueError:
            return None, f"unparseable_date:{raw_date.strip()}"


@register
class DigitNormalizer(Normalizer):
    insurer_name = "Digit"
    source_system = "digit"
    source_file = "digit_feed.csv"
    commission_mode = "absolute"

    COL_REF = "ReferenceID"
    COL_PREMIUM = "PremiumAmount"
    COL_COMMISSION = "CommissionAmt"
    COL_DATE = "PaidEpoch"
    COL_STATUS = "Status"

    _PREFIX = "DIGIT-"
    _SUFFIX = "-TW"

    def parse_policy_no(self, raw_ref: str | None) -> tuple[str | None, str | None]:
        # Embedded: DIGIT-<policy_no>-TW (policy_no itself contains hyphens).
        ref = (raw_ref or "").strip()
        upper = ref.upper()
        if upper.startswith(self._PREFIX) and upper.endswith(self._SUFFIX):
            inner = ref[len(self._PREFIX): len(ref) - len(self._SUFFIX)]
            if inner.strip():
                return inner.strip(), None
        return None, f"unparseable_policy_ref:{ref or '<empty>'}"

    def parse_date(self, raw_date: str | None) -> tuple[dt.date | None, str | None]:
        if raw_date is None or not raw_date.strip():
            return None, "missing_date"
        try:
            epoch = int(float(raw_date.strip()))
            # Interpret the epoch in UTC (not the host's local zone) so the parsed
            # date is identical regardless of where the ingestion runs (M8).
            return dt.datetime.fromtimestamp(epoch, tz=dt.UTC).date(), None
        except (ValueError, OverflowError, OSError):
            return None, f"unparseable_date:{raw_date.strip()}"


@register
class TataAIGNormalizer(Normalizer):
    insurer_name = "Tata AIG"
    source_system = "tata_aig"
    source_file = "tata_aig_feed.csv"
    commission_mode = "rate_pct"

    COL_REF = "PolicyNumber"
    COL_PREMIUM = "Premium"
    COL_COMMISSION = "BrokeragePercent"
    COL_DATE = "DateOfPayment"
    COL_STATUS = "PaymentStatus"

    def parse_policy_no(self, raw_ref: str | None) -> tuple[str | None, str | None]:
        ref = (raw_ref or "").strip()
        if not ref:
            return None, "unparseable_policy_ref:<empty>"
        return ref, None  # exact match

    def parse_date(self, raw_date: str | None) -> tuple[dt.date | None, str | None]:
        return self._parse_with_format(raw_date, "%d-%b-%Y")
