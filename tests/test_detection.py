"""Detection-layer tests.

Asserts each rule reason_code fires on EXACTLY the planted rows from the oracle,
with special attention to the renewal detector: it must hit the 7 planted
not-provisioned renewals and skip the eligible-AND-provisioned trap sales.
The anomaly layer is checked only for shape/secondariness (novelty, not verdicts).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from leaksentinel.db import SessionLocal, engine as db_engine
from leaksentinel.detection.anomaly import run_anomaly
from leaksentinel.detection.rules import (
    DetectionReason,
    Severity,
    findings_by_reason,
    run_rules,
)
from leaksentinel.ingestion import loader
from leaksentinel.reconciliation import engine
from leaksentinel.reconciliation.models import Policy

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "synthetic"
GROUND_TRUTH = DATA_DIR / "ground_truth.json"


@pytest.fixture(scope="module")
def ground_truth() -> dict:
    if not GROUND_TRUTH.exists():
        pytest.skip("ground_truth.json missing; run scripts/generate_synthetic_data.py")
    return json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def detection(ground_truth):
    """Load feeds, reconcile, then run the rule + anomaly detectors."""
    try:
        db_engine.connect().close()
    except OperationalError:
        pytest.skip("Postgres not reachable")

    session = SessionLocal()
    try:
        if session.query(Policy).count() == 0:
            pytest.skip("No policies in DB; run scripts/generate_synthetic_data.py first")
    finally:
        session.close()

    loader.run()
    engine.run()

    session = SessionLocal()
    try:
        rule_findings = run_rules(session)
        by_reason = findings_by_reason(rule_findings)
        rule_covered = {f.policy_no for f in rule_findings}
        anomaly_findings = run_anomaly(session, exclude=rule_covered)
    finally:
        session.close()

    return {
        "rule_findings": rule_findings,
        "by_reason": by_reason,
        "rule_covered": rule_covered,
        "anomaly_findings": anomaly_findings,
    }


def _oracle_scenario_set(ground_truth: dict, scenario: str) -> set[str]:
    return {
        pno for pno, info in ground_truth["policies"].items()
        if info["scenario"] == scenario
    }


# --------------------------------------------------------------------------- #
# Each reason_code fires on exactly the planted rows
# --------------------------------------------------------------------------- #
def test_missing_commission_exact(detection, ground_truth):
    got = detection["by_reason"].get(DetectionReason.MISSING_COMMISSION.value, set())
    assert got == _oracle_scenario_set(ground_truth, "MISSING_COMMISSION")
    assert len(got) == 7


def test_underpaid_below_rate_exact(detection, ground_truth):
    got = detection["by_reason"].get(DetectionReason.UNDERPAID_BELOW_RATE.value, set())
    assert got == _oracle_scenario_set(ground_truth, "UNDERPAID")
    assert len(got) == 7


def test_duplicate_payment_exact(detection, ground_truth):
    got = detection["by_reason"].get(DetectionReason.DUPLICATE_PAYMENT.value, set())
    assert got == _oracle_scenario_set(ground_truth, "DUPLICATE")
    assert len(got) == 7


def test_rounding_delta_exact(detection, ground_truth):
    got = detection["by_reason"].get(DetectionReason.ROUNDING_DELTA.value, set())
    assert got == _oracle_scenario_set(ground_truth, "ROUNDING_DELTA")
    assert len(got) == 7


# --------------------------------------------------------------------------- #
# The renewal detector + its deliberate trap
# --------------------------------------------------------------------------- #
def test_renewal_hits_planted_and_skips_trap(detection, ground_truth):
    got = detection["by_reason"].get(
        DetectionReason.RENEWAL_1PLUS1_NOT_PROVISIONED.value, set()
    )
    planted = _oracle_scenario_set(ground_truth, "RENEWAL_1PLUS1_NOT_PROVISIONED")

    # The trap: clean sales that are eligible AND provisioned must never fire.
    trap = {
        pno for pno, info in ground_truth["policies"].items()
        if info["renewal_eligible"] and info["renewal_provisioned"]
    }
    assert trap, "expected the generator to plant eligible+provisioned trap sales"

    assert got == planted, "renewal detector must match the planted not-provisioned set"
    assert len(got) == 7
    assert got.isdisjoint(trap), "renewal detector fired on an eligible+provisioned sale"


def test_renewal_finding_is_high_severity(detection):
    renewals = [
        f for f in detection["rule_findings"]
        if f.reason_code == DetectionReason.RENEWAL_1PLUS1_NOT_PROVISIONED.value
    ]
    assert renewals
    assert all(f.severity == Severity.HIGH.value for f in renewals)


def test_rounding_is_informational(detection):
    rounding = [
        f for f in detection["rule_findings"]
        if f.reason_code == DetectionReason.ROUNDING_DELTA.value
    ]
    assert rounding
    assert all(f.severity == Severity.INFO.value for f in rounding)


# --------------------------------------------------------------------------- #
# Anomaly layer: secondary / novelty only
# --------------------------------------------------------------------------- #
def test_anomaly_is_secondary_and_novel(detection):
    anomalies = detection["anomaly_findings"]
    # All anomaly findings carry the unclassified label and a numeric score.
    assert all(
        f.reason_code == DetectionReason.ANOMALY_UNCLASSIFIED.value for f in anomalies
    )
    assert all(isinstance(f.score, float) for f in anomalies)
    # Novelty: it must not re-report anything the rules already covered.
    assert all(f.policy_no not in detection["rule_covered"] for f in anomalies)
