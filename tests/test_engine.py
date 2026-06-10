"""Precision/recall backbone for the reconciliation engine.

Runs the full pipeline against the live Postgres (loader -> engine) and asserts
the engine's per-class output matches data/synthetic/ground_truth.json EXACTLY.
A leak the engine invents (false positive) or misses (false negative) fails the
test. Skipped if Postgres or the synthetic data isn't present.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from leaksentinel.db import SessionLocal
from leaksentinel.db import engine as db_engine
from leaksentinel.ingestion import loader
from leaksentinel.reconciliation import engine
from leaksentinel.reconciliation.engine import (
    ReconciliationClass,
    find_missing_commission_orm,
    find_missing_commission_sql,
    planted_by_class,
)
from leaksentinel.reconciliation.models import Policy

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "synthetic"
GROUND_TRUTH = DATA_DIR / "ground_truth.json"

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def ground_truth() -> dict:
    if not GROUND_TRUTH.exists():
        pytest.skip("ground_truth.json missing; run scripts/generate_synthetic_data.py")
    return json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def detected() -> dict[str, set[str]]:
    """Load feeds + run the engine against the live DB; return detected sets."""
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

    loader.run()           # populate insurer_commission_feeds from the CSVs
    return engine.run()    # reconcile + persist, return {status: {policy_no}}


# --------------------------------------------------------------------------- #
# Exact per-scenario assertions (the precision/recall backbone)
# --------------------------------------------------------------------------- #
def test_missing_commission_exactly_matches_oracle(detected, ground_truth):
    planted = planted_by_class(ground_truth)
    assert detected[ReconciliationClass.MISSING_COMMISSION.value] == \
        planted[ReconciliationClass.MISSING_COMMISSION.value]


def test_underpaid_exactly_matches_oracle(detected, ground_truth):
    planted = planted_by_class(ground_truth)
    assert detected[ReconciliationClass.UNDERPAID.value] == \
        planted[ReconciliationClass.UNDERPAID.value]


def test_overpaid_duplicate_exactly_matches_oracle(detected, ground_truth):
    planted = planted_by_class(ground_truth)
    assert detected[ReconciliationClass.OVERPAID_DUPLICATE.value] == \
        planted[ReconciliationClass.OVERPAID_DUPLICATE.value]


def test_matched_exactly_matches_oracle(detected, ground_truth):
    planted = planted_by_class(ground_truth)
    assert detected[ReconciliationClass.MATCHED.value] == \
        planted[ReconciliationClass.MATCHED.value]


def test_crm_mismatch_empty(detected):
    # No CRM-divergence scenario was planted; the engine must invent none.
    assert detected.get(ReconciliationClass.CRM_MISMATCH.value, set()) == set()


def test_every_class_is_exact_no_fp_no_fn(detected, ground_truth):
    """Aggregate guard: zero false positives and zero false negatives anywhere."""
    planted = planted_by_class(ground_truth)
    for cls in ReconciliationClass:
        p = planted.get(cls.value, set())
        d = detected.get(cls.value, set())
        assert d - p == set(), f"{cls.value}: false positives {sorted(d - p)[:5]}"
        assert p - d == set(), f"{cls.value}: false negatives {sorted(p - d)[:5]}"


def test_counts_are_seven_each(detected):
    assert len(detected[ReconciliationClass.MISSING_COMMISSION.value]) == 7
    assert len(detected[ReconciliationClass.UNDERPAID.value]) == 7
    assert len(detected[ReconciliationClass.OVERPAID_DUPLICATE.value]) == 7


def test_all_policies_classified(detected, ground_truth):
    total = sum(len(s) for s in detected.values())
    assert total == ground_truth["n_policies"] == 200


# --------------------------------------------------------------------------- #
# Raw SQL vs SQLAlchemy equivalence (the query shown in the interview)
# --------------------------------------------------------------------------- #
def test_missing_query_sql_equals_orm_equals_oracle(detected, ground_truth):
    session = SessionLocal()
    try:
        via_sql = set(find_missing_commission_sql(session))
        via_orm = set(find_missing_commission_orm(session))
    finally:
        session.close()

    oracle_missing = planted_by_class(ground_truth)[
        ReconciliationClass.MISSING_COMMISSION.value
    ]
    assert via_sql == via_orm, "raw SQL and SQLAlchemy disagree"
    assert via_sql == oracle_missing, "LEFT JOIN ... IS NULL != oracle MISSING set"
