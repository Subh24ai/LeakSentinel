"""Rule-based, explainable leakage detectors (the PRIMARY detection layer).

Why rules first (and ML second)?
--------------------------------
In commission finance, every flag eventually lands in front of a human — an
ops analyst raising a dispute with an insurer, or finance writing off a balance.
That person has to be able to say *exactly why* a rupee is considered missing,
and defend it to the counterparty. A deterministic rule gives a reason code, a
plain-English explanation, and a number anyone can re-derive by hand
("paid ₹300.61 vs expected ₹388.38 = 22.6% short"). That is auditable and
disputable. A black-box anomaly score is not: "the model gave it -0.62" is not
something you can put in a dispute email.

So the rules below are the source of truth. They are intentionally simple,
testable against ground truth, and each emits a specific ``reason_code`` +
``severity`` + ``explanation``. The Isolation Forest in :mod:`anomaly` is a
strictly secondary safety net for *novel* patterns the rules don't yet encode;
it never overrides a rule.

Each detector reads from ``reconciliation_results`` (the engine's verdict) and,
where needed, the ``sales`` / ``policies`` master tables, and returns
:class:`Finding` objects. Detectors do not mutate the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from sqlalchemy import func, select

from leaksentinel.reconciliation.engine import TOLERANCE
from leaksentinel.reconciliation.models import (
    InsurerCommissionFeed,
    Policy,
    ReconciliationResult,
    Sale,
)

# Deltas at/below TOLERANCE are "matched"; clean policies reconstruct to a delta
# of 0.00 (rate-feed rounding error is <= ₹0.01). So a MATCHED policy whose
# |delta| sits in this band is a genuine sub-tolerance rounding artefact.
ROUNDING_MIN = Decimal("0.02")  # above clean reconstruction noise
UNDERPAID_HIGH_SHORTFALL_PCT = Decimal("20")  # >= this is HIGH severity


class Severity(str, Enum):
    INFO = "info"      # not actionable; recorded for completeness
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DetectionReason(str, Enum):
    MISSING_COMMISSION = "MISSING_COMMISSION"
    UNDERPAID_BELOW_RATE = "UNDERPAID_BELOW_RATE"
    DUPLICATE_PAYMENT = "DUPLICATE_PAYMENT"
    RENEWAL_1PLUS1_NOT_PROVISIONED = "RENEWAL_1PLUS1_NOT_PROVISIONED"
    ROUNDING_DELTA = "ROUNDING_DELTA"
    ANOMALY_UNCLASSIFIED = "ANOMALY_UNCLASSIFIED"  # emitted by anomaly.py


@dataclass(frozen=True)
class Finding:
    """One explainable detection result for a single policy."""

    policy_no: str
    reason_code: str
    severity: str
    explanation: str
    detector: str            # e.g. "rule:renewal_1plus1" or "anomaly:isolation_forest"
    score: float | None = None  # populated by the ML layer only


# --------------------------------------------------------------------------- #
# Rule detectors
# --------------------------------------------------------------------------- #
def detect_missing_commission(session) -> list[Finding]:
    """Carry through the engine's MISSING_COMMISSION verdict (pure leak)."""
    rows = session.execute(
        select(ReconciliationResult.policy_no, ReconciliationResult.expected)
        .where(ReconciliationResult.status == "MISSING_COMMISSION")
    ).all()
    return [
        Finding(
            policy_no=policy_no,
            reason_code=DetectionReason.MISSING_COMMISSION.value,
            severity=Severity.HIGH.value,
            explanation=(
                f"Policy {policy_no} was sold but appears in no insurer commission "
                f"feed; expected commission ₹{expected} is unpaid."
            ),
            detector="rule:missing_commission",
        )
        for policy_no, expected in rows
    ]


def detect_underpaid_below_rate(session) -> list[Finding]:
    """Refine the engine's UNDERPAID verdict with the actual shortfall percent."""
    rows = session.execute(
        select(
            ReconciliationResult.policy_no,
            ReconciliationResult.expected,
            ReconciliationResult.actual,
        ).where(ReconciliationResult.status == "UNDERPAID")
    ).all()

    findings: list[Finding] = []
    for policy_no, expected, actual in rows:
        expected = expected or Decimal(0)
        actual = actual or Decimal(0)
        shortfall = expected - actual
        pct = (shortfall / expected * 100) if expected else Decimal(0)
        severity = (
            Severity.HIGH if pct >= UNDERPAID_HIGH_SHORTFALL_PCT else Severity.MEDIUM
        )
        findings.append(
            Finding(
                policy_no=policy_no,
                reason_code=DetectionReason.UNDERPAID_BELOW_RATE.value,
                severity=severity.value,
                explanation=(
                    f"Policy {policy_no} paid ₹{actual} vs expected ₹{expected} — "
                    f"short by ₹{shortfall} ({pct:.1f}% below the contracted rate)."
                ),
                detector="rule:underpaid_below_rate",
            )
        )
    return findings


def detect_duplicate_payment(session) -> list[Finding]:
    """Flag policies the engine marked OVERPAID_DUPLICATE; report entry count."""
    # Per-policy feed entry counts, to state how many times we were paid.
    feed_counts = dict(
        session.execute(
            select(InsurerCommissionFeed.policy_no, func.count(InsurerCommissionFeed.id))
            .where(InsurerCommissionFeed.policy_no.is_not(None))
            .group_by(InsurerCommissionFeed.policy_no)
        ).all()
    )

    rows = session.execute(
        select(
            ReconciliationResult.policy_no,
            ReconciliationResult.expected,
            ReconciliationResult.actual,
            ReconciliationResult.delta,
        ).where(ReconciliationResult.status == "OVERPAID_DUPLICATE")
    ).all()

    findings: list[Finding] = []
    for policy_no, expected, actual, delta in rows:
        n = feed_counts.get(policy_no, 0)
        findings.append(
            Finding(
                policy_no=policy_no,
                reason_code=DetectionReason.DUPLICATE_PAYMENT.value,
                severity=Severity.MEDIUM.value,
                explanation=(
                    f"Policy {policy_no} has {n} commission entries (expected 1); "
                    f"total paid ₹{actual} vs expected ₹{expected} — ₹{delta} "
                    f"overpayment / clawback risk."
                ),
                detector="rule:duplicate_payment",
            )
        )
    return findings


def detect_renewal_1plus1_not_provisioned(session) -> list[Finding]:
    """SureDrive's flagship 1+1 free-renewal leak.

    A "1+1" sale promises the customer a second year of cover for free. The
    sale is flagged ``renewal_1plus1_eligible`` when we made that promise, and
    ``renewal_provisioned`` once we've actually booked the second-year policy.

    The leak is the gap between the two: **eligible AND NOT provisioned** — we
    owe the customer a renewal we never provisioned. This is NOT a commission
    mismatch (the base policy was paid correctly, so reconciliation marks it
    MATCHED); it is an unmet service obligation visible only on the sale flags.

    Crucially, this must NOT fire on sales that are eligible AND provisioned —
    those fulfilled the promise. The ``renewal_provisioned IS FALSE`` predicate
    is exactly what separates the real leak from that look-alike.
    """
    rows = session.execute(
        select(Policy.policy_no)
        .join(Sale, Policy.sale_id == Sale.id)
        .where(Sale.renewal_1plus1_eligible.is_(True))
        .where(Sale.renewal_provisioned.is_(False))  # <-- the discriminator
    ).all()

    return [
        Finding(
            policy_no=policy_no,
            reason_code=DetectionReason.RENEWAL_1PLUS1_NOT_PROVISIONED.value,
            severity=Severity.HIGH.value,
            explanation=(
                f"Sale for policy {policy_no} is flagged 1+1 free-renewal eligible "
                f"but the renewal was never provisioned — a second-year cover owed "
                f"to the customer that has not been booked."
            ),
            detector="rule:renewal_1plus1",
        )
        for (policy_no,) in rows
    ]


def detect_rounding_delta(session) -> list[Finding]:
    """Sub-tolerance deltas on otherwise-MATCHED policies — informational only."""
    rows = session.execute(
        select(
            ReconciliationResult.policy_no,
            ReconciliationResult.delta,
        )
        .where(ReconciliationResult.status == "MATCHED")
        .where(func.abs(ReconciliationResult.delta) >= ROUNDING_MIN)
        .where(func.abs(ReconciliationResult.delta) <= TOLERANCE)
    ).all()

    return [
        Finding(
            policy_no=policy_no,
            reason_code=DetectionReason.ROUNDING_DELTA.value,
            severity=Severity.INFO.value,
            explanation=(
                f"Policy {policy_no} differs from expected by ₹{delta} — within the "
                f"₹{TOLERANCE} rounding tolerance; informational, not actionable."
            ),
            detector="rule:rounding_delta",
        )
        for policy_no, delta in rows
    ]


# --------------------------------------------------------------------------- #
# Orchestration helpers
# --------------------------------------------------------------------------- #
RULE_DETECTORS = (
    detect_missing_commission,
    detect_underpaid_below_rate,
    detect_duplicate_payment,
    detect_renewal_1plus1_not_provisioned,
    detect_rounding_delta,
)


def run_rules(session) -> list[Finding]:
    """Run every rule detector and return the combined findings."""
    findings: list[Finding] = []
    for detector in RULE_DETECTORS:
        findings.extend(detector(session))
    return findings


def findings_by_reason(findings: list[Finding]) -> dict[str, set[str]]:
    """Group findings into ``{reason_code: {policy_no, ...}}``."""
    grouped: dict[str, set[str]] = {}
    for f in findings:
        grouped.setdefault(f.reason_code, set()).add(f.policy_no)
    return grouped
