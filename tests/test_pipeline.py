"""End-to-end test of the LangGraph pipeline against the synthetic data.

Runs the WHOLE graph (Intake → Reconcile → Detect → Decide → Remediate/Escalate
→ Finalize) on the live Postgres and asserts the end-to-end numbers reconcile
with the ground-truth oracle — most importantly that **every planted
MISSING_COMMISSION becomes a claim or an escalation, none lost**, and that the
disposition buckets conserve (nothing detected falls through the cracks).

Skipped if Postgres or the synthetic data isn't present (same guard as the other
DB-backed tests).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from leaksentinel.actions.remediation import CommissionClaim
from leaksentinel.db import SessionLocal, create_all
from leaksentinel.db import engine as db_engine
from leaksentinel.detection.rules import DetectionReason
from leaksentinel.graph.run import run_pipeline
from leaksentinel.reconciliation.models import Policy

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "synthetic"
GROUND_TRUTH = DATA_DIR / "ground_truth.json"

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def ground_truth() -> dict:
    if not GROUND_TRUTH.exists():
        pytest.skip("ground_truth.json missing; run scripts/generate_synthetic_data.py")
    return json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))


def _scenario_set(gt: dict, scenario: str) -> set[str]:
    return {p for p, info in gt["policies"].items() if info["scenario"] == scenario}


@pytest.fixture(scope="module")
def pipeline(ground_truth):
    """Run the full graph once and return (final_state, claim_rows)."""
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

    create_all()  # ensure the action tables exist
    state = run_pipeline(reset_actions=True)

    session = SessionLocal()
    try:
        claims = session.execute(select(CommissionClaim)).scalars().all()
        claim_policies = {c.policy_no for c in claims}
        claim_total = sum((c.claim_amount for c in claims), Decimal("0.00"))
    finally:
        session.close()

    return {
        "state": state,
        "claim_policies": claim_policies,
        "claim_total": claim_total,
    }


def test_all_policies_processed(pipeline, ground_truth):
    assert pipeline["state"].summary["policies_processed"] == ground_truth["n_policies"] == 200


def test_leaks_by_reason_match_oracle(pipeline):
    by_reason = pipeline["state"].summary["leaks_by_reason"]
    assert by_reason.get(DetectionReason.MISSING_COMMISSION.value) == 7
    assert by_reason.get(DetectionReason.UNDERPAID_BELOW_RATE.value) == 7
    assert by_reason.get(DetectionReason.DUPLICATE_PAYMENT.value) == 7
    assert by_reason.get(DetectionReason.RENEWAL_1PLUS1_NOT_PROVISIONED.value) == 7


def test_every_missing_commission_becomes_claim_or_escalation_none_lost(pipeline, ground_truth):
    state = pipeline["state"]
    oracle_missing = _scenario_set(ground_truth, "MISSING_COMMISSION")
    assert len(oracle_missing) == 7

    claimed = {o.policy_no for o in state.remediated if o.action == "claim"}
    escalated = {o.policy_no for o in state.escalated}

    # None lost: every planted MISSING leak ended up actioned somewhere.
    lost = oracle_missing - (claimed | escalated)
    assert lost == set(), f"MISSING leaks neither claimed nor escalated: {sorted(lost)}"

    # On this data every MISSING amount clears the threshold -> all claimed,
    # and the claims actually landed in commission_claims.
    assert oracle_missing <= claimed
    assert oracle_missing <= pipeline["claim_policies"]


def test_renewals_route_to_escalation(pipeline, ground_truth):
    oracle_renewals = _scenario_set(ground_truth, "RENEWAL_1PLUS1_NOT_PROVISIONED")
    escalated = {o.policy_no for o in pipeline["state"].escalated}
    assert oracle_renewals <= escalated, "renewal leaks must route to the human queue"


def test_disposition_conserves(pipeline):
    """Nothing detected falls through: actionable == remediated + escalated + skipped."""
    state = pipeline["state"]
    n_plan = len(state.plan)
    accounted = len(state.remediated) + len(state.escalated) + len(state.skipped)
    assert accounted == n_plan

    s = state.summary
    assert s["auto_remediated"] + s["escalated"] + s["below_threshold"] == n_plan


def test_claimed_total_matches_db(pipeline):
    """The summary's claimed total equals the sum of commission_claims rows."""
    state = pipeline["state"]
    assert Decimal(state.summary["total_claimed"]) == pipeline["claim_total"]
    assert pipeline["claim_total"] > Decimal("0.00")
    # Exposure is split by sign and never mixed into one figure (M3).
    assert Decimal(state.summary["underpayment_exposure"]) > 0
    assert Decimal(state.summary["clawback_exposure"]) > 0
