"""Reconciliation engine.

Joins the normalized insurer feeds against our *expected* commissions (derived
from ``policies``) and our own ``crm_records``, on ``policy_no``, and classifies
each policy into one of:

    MATCHED              feed amount == expected (within rounding tolerance)
    MISSING_COMMISSION   policy sold but absent from every feed (pure leak)
    UNDERPAID            single payment materially below expected
    OVERPAID_DUPLICATE   more than one payment for the same policy
    CRM_MISMATCH         feed agrees with expected, but our CRM books differ

Results are persisted to ``reconciliation_results`` with a ``status`` and a
preliminary ``reason_code``.

The core leakage query (policies with no matching feed) is written BOTH as
readable raw SQL (:data:`MISSING_COMMISSION_SQL`) and as the SQLAlchemy
equivalent (:func:`find_missing_commission_orm`); both are kept on purpose.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum

from sqlalchemy import func, select, text

from leaksentinel.db import SessionLocal
from leaksentinel.reconciliation.models import (
    CrmRecord,
    InsurerCommissionFeed,
    Policy,
    ReconciliationResult,
)
from leaksentinel.reconciliation.schemas import ReasonCode, ResolutionState

# Rounding tolerance: deltas at or below this are treated as a clean match.
# Comfortably above planted rounding deltas (<= ₹0.50) and rate-reconstruction
# error (<= ₹0.01), and far below any real underpayment.
TOLERANCE = Decimal("1.00")
_TWOPLACES = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(_TWOPLACES, rounding=ROUND_HALF_UP)


class ReconciliationClass(str, Enum):
    """The engine's per-policy verdict (stored in reconciliation_results.status)."""

    MATCHED = "MATCHED"
    MISSING_COMMISSION = "MISSING_COMMISSION"
    UNDERPAID = "UNDERPAID"
    OVERPAID_DUPLICATE = "OVERPAID_DUPLICATE"
    CRM_MISMATCH = "CRM_MISMATCH"


# How an oracle/ground-truth scenario is expected to surface in THIS engine.
# (Renewal & rounding leaks are not commission-amount leaks, so they read as
# MATCHED here; renewal leakage is caught by a separate detector.)
ORACLE_SCENARIO_TO_CLASS: dict[str, ReconciliationClass] = {
    "CLEAN": ReconciliationClass.MATCHED,
    "ROUNDING_DELTA": ReconciliationClass.MATCHED,
    "RENEWAL_1PLUS1_NOT_PROVISIONED": ReconciliationClass.MATCHED,
    "MISSING_COMMISSION": ReconciliationClass.MISSING_COMMISSION,
    "UNDERPAID": ReconciliationClass.UNDERPAID,
    "DUPLICATE": ReconciliationClass.OVERPAID_DUPLICATE,
}


# --------------------------------------------------------------------------- #
# Core leakage query — kept in BOTH forms on purpose
# --------------------------------------------------------------------------- #
# Pure leak: a policy that exists on our books but appears in NO insurer feed.
MISSING_COMMISSION_SQL = """
SELECT p.policy_no
FROM policies p
LEFT JOIN insurer_commission_feeds f
       ON f.policy_no = p.policy_no
WHERE f.id IS NULL
ORDER BY p.policy_no
""".strip()


def find_missing_commission_sql(session) -> list[str]:
    """Run the readable raw-SQL LEFT JOIN ... WHERE feed IS NULL."""
    return [row[0] for row in session.execute(text(MISSING_COMMISSION_SQL))]


def find_missing_commission_orm(session) -> list[str]:
    """SQLAlchemy equivalent of :data:`MISSING_COMMISSION_SQL`."""
    stmt = (
        select(Policy.policy_no)
        .outerjoin(
            InsurerCommissionFeed,
            InsurerCommissionFeed.policy_no == Policy.policy_no,
        )
        .where(InsurerCommissionFeed.id.is_(None))
        .order_by(Policy.policy_no)
    )
    return [row[0] for row in session.execute(stmt)]


# --------------------------------------------------------------------------- #
# Full reconciliation
# --------------------------------------------------------------------------- #
def _classify(
    expected: Decimal,
    feed_count: int,
    feed_sum: Decimal,
    crm_recorded: Decimal | None,
) -> tuple[ReconciliationClass, ReasonCode, Decimal, Decimal]:
    """Return (class, reason_code, actual, delta) for one policy."""
    if feed_count == 0:
        actual = Decimal("0.00")
        return (
            ReconciliationClass.MISSING_COMMISSION,
            ReasonCode.NO_PAYMENT,
            actual,
            _money(actual - expected),
        )

    if feed_count >= 2:
        actual = _money(feed_sum)
        return (
            ReconciliationClass.OVERPAID_DUPLICATE,
            ReasonCode.DUPLICATE_PAYMENT,
            actual,
            _money(actual - expected),
        )

    # Exactly one payment.
    actual = _money(feed_sum)
    delta = _money(actual - expected)

    if delta < -TOLERANCE:
        return ReconciliationClass.UNDERPAID, ReasonCode.AMOUNT_MISMATCH, actual, delta
    if delta > TOLERANCE:
        # A lone over-payment (not a duplicate); rare. Flag as overpayment.
        return ReconciliationClass.OVERPAID_DUPLICATE, ReasonCode.AMOUNT_MISMATCH, actual, delta

    # Insurer side matches expected. Cross-check our own CRM books.
    if crm_recorded is not None and abs(_money(crm_recorded) - expected) > TOLERANCE:
        return ReconciliationClass.CRM_MISMATCH, ReasonCode.DATA_QUALITY, actual, delta

    return ReconciliationClass.MATCHED, ReasonCode.OK, actual, delta


def reconcile(session) -> dict[str, set[str]]:
    """Classify every policy, persist results, and return ``{status: {policy_no}}``.

    Mutates/clears ``reconciliation_results``; the caller commits.
    """
    # Expected side.
    policies = session.execute(
        select(
            Policy.policy_no,
            Policy.premium,
            Policy.expected_commission_rate,
        )
    ).all()

    # CRM side: policy_no -> recorded_commission.
    crm = {
        pno: amt
        for pno, amt in session.execute(
            select(CrmRecord.policy_no, CrmRecord.recorded_commission)
        ).all()
    }

    # Actual side: aggregate feed rows per normalized policy_no.
    feed_rows = session.execute(
        select(
            InsurerCommissionFeed.policy_no,
            func.count(InsurerCommissionFeed.id),
            func.coalesce(func.sum(InsurerCommissionFeed.commission_amount), 0),
        )
        .where(InsurerCommissionFeed.policy_no.is_not(None))
        .group_by(InsurerCommissionFeed.policy_no)
    ).all()
    feed_count = {pno: cnt for pno, cnt, _ in feed_rows}
    feed_sum = {pno: Decimal(str(total)) for pno, _, total in feed_rows}

    # Fresh slate.
    session.query(ReconciliationResult).delete()

    detected: dict[str, set[str]] = defaultdict(set)
    for policy_no, premium, rate in policies:
        expected = _money((premium or Decimal(0)) * (rate or Decimal(0)))
        cnt = feed_count.get(policy_no, 0)
        total = feed_sum.get(policy_no, Decimal("0.00"))

        cls, reason, actual, delta = _classify(expected, cnt, total, crm.get(policy_no))

        resolution = (
            None
            if cls is ReconciliationClass.MATCHED
            else ResolutionState.OPEN.value
        )
        session.add(
            ReconciliationResult(
                policy_no=policy_no,
                status=cls.value,
                reason_code=reason.value,
                expected=expected,
                actual=actual,
                delta=delta,
                resolution_state=resolution,
            )
        )
        detected[cls.value].add(policy_no)

    session.flush()
    return detected


def run() -> dict[str, set[str]]:
    """Open a session, reconcile, commit, return the detected sets."""
    session = SessionLocal()
    try:
        detected = reconcile(session)
        session.commit()
        return detected
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Confusion summary (planted vs. detected)
# --------------------------------------------------------------------------- #
def planted_by_class(ground_truth: dict) -> dict[str, set[str]]:
    """Group oracle policies by the engine class they SHOULD land in."""
    planted: dict[str, set[str]] = defaultdict(set)
    for policy_no, info in ground_truth["policies"].items():
        cls = ORACLE_SCENARIO_TO_CLASS[info["scenario"]]
        planted[cls.value].add(policy_no)
    return planted


def confusion_summary(detected: dict[str, set[str]], ground_truth: dict) -> bool:
    """Print planted vs. detected per class; return True if every class is exact."""
    planted = planted_by_class(ground_truth)
    classes = [c.value for c in ReconciliationClass]

    print("\n=== Confusion summary: planted (oracle) vs. detected (engine) ===")
    print(f"{'Class':<22}{'Planted':>8}{'Detected':>10}{'TP':>6}{'FP':>6}{'FN':>6}")
    print("-" * 58)
    all_exact = True
    for cls in classes:
        p = planted.get(cls, set())
        d = detected.get(cls, set())
        tp = len(p & d)
        fp = len(d - p)
        fn = len(p - d)
        if fp or fn:
            all_exact = False
        print(f"{cls:<22}{len(p):>8}{len(d):>10}{tp:>6}{fp:>6}{fn:>6}")
    print("-" * 58)
    total_p = sum(len(s) for s in planted.values())
    total_d = sum(len(s) for s in detected.values())
    print(f"{'TOTAL':<22}{total_p:>8}{total_d:>10}")
    print("\n" + ("EXACT MATCH — zero false positives/negatives."
                  if all_exact else "MISMATCH — see FP/FN columns above."))
    return all_exact


def main() -> None:
    import json
    from pathlib import Path

    detected = run()
    gt_path = Path(__file__).resolve().parents[3] / "data" / "synthetic" / "ground_truth.json"
    if gt_path.exists():
        ground_truth = json.loads(gt_path.read_text(encoding="utf-8"))
        confusion_summary(detected, ground_truth)
    else:
        print("ground_truth.json not found; skipping confusion summary.")
        for cls, pnos in sorted(detected.items()):
            print(f"  {cls:<22}{len(pnos):>5}")


if __name__ == "__main__":
    main()
