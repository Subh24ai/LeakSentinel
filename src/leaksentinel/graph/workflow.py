"""The LeakSentinel reconciliation graph (LangGraph).

    Intake ─▶ Reconcile ─▶ Detect ─▶ Decide ─┬─▶ (clean) ─────────────▶ Finalize
                                             └─▶ Remediate ┐
                                                 Escalate  ┴──────────▶ Finalize

* **Intake**     — load + normalize the four insurer feeds (the ingestion loader).
* **Reconcile**  — run the reconciliation engine over expected vs. actual.
* **Detect**     — run the explainable rule detectors + the anomaly net.
* **Decide**     — map each finding to a gateable ActionableFinding and tag it
                   ``remediate`` or ``escalate`` using the *same* predicates the
                   actions gate uses; informational (rounding) findings are set
                   aside. A conditional edge then routes: nothing actionable →
                   straight to Finalize; otherwise fan out to Remediate + Escalate.
* **Remediate**  — for each remediate-tagged finding, call ``route()`` (which
                   gates, dedupes idempotently, and audits) and record the claim.
* **Escalate**   — for each escalate-tagged finding, call ``route()`` (high-value
                   or gate-failing) to park it on the human queue.
* **Finalize**   — assemble the end-to-end summary.

Remediate and Escalate both delegate the actual side effect to
:func:`leaksentinel.actions.escalation.route`, so the gate / idempotency / audit
guarantees hold no matter which path a finding takes.
"""

from __future__ import annotations

import datetime as dt
import logging
import uuid
from collections import Counter
from decimal import Decimal

from langgraph.graph import END, START, StateGraph
from sqlalchemy import select

from leaksentinel.actions.escalation import EscalationOutcome, route, should_escalate
from leaksentinel.actions.mapping import build_actionable_finding
from leaksentinel.actions.remediation import (
    ActionableFinding,
    CommissionClaim,
    OutcomeStatus,
    RemediationOutcome,
)
from leaksentinel.db import SessionLocal
from leaksentinel.detection.anomaly import run_anomaly
from leaksentinel.detection.rules import (
    CLAWBACK_REASONS,
    UNDERPAYMENT_REASONS,
    Finding,
    Severity,
    run_rules,
)
from leaksentinel.graph.state import (
    FindingRec,
    OutcomeItem,
    PlanItem,
    ReconciliationState,
)
from leaksentinel.ingestion import loader
from leaksentinel.reconciliation import engine
from leaksentinel.reconciliation.models import (
    InsurerFeedUpload,
    PersistedFinding,
    Policy,
    ReconciliationResult,
    ReconciliationRun,
)

logger = logging.getLogger(__name__)

_ZERO = Decimal("0.00")


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #
def intake(state: ReconciliationState) -> dict:
    """Optionally reset this run's action tables, then load + normalize feeds.

    Once any real feed has been uploaded (a processed ``InsurerFeedUpload``), the
    pipeline runs on ``source='all'`` — uploaded data overlaid on the synthetic
    baseline, with uploads winning per policy. A fresh install with no uploads
    keeps the original ``source='synthetic'`` behaviour.
    """
    if state.reset_actions:
        _reset_action_tables()

    session = SessionLocal()
    try:
        has_uploads = (
            session.query(InsurerFeedUpload).filter_by(status="processed").count() > 0
        )
    finally:
        session.close()

    source = "all" if has_uploads else "synthetic"
    summary = loader.run(source=source)
    loaded = sum(c.get("loaded", 0) for c in summary.values())
    logger.info("Intake: loaded %d normalized feed rows (source=%s).", loaded, source)
    return {"ingest_summary": summary}


def reconcile(state: ReconciliationState) -> dict:
    """Run the reconciliation engine; record per-class policy sets."""
    detected = engine.run()  # {status: {policy_no}}
    detected_lists = {k: sorted(v) for k, v in detected.items()}
    processed = sum(len(v) for v in detected.values())
    logger.info("Reconcile: classified %d policies.", processed)
    return {"detected": detected_lists, "policies_processed": processed}


def detect(state: ReconciliationState) -> dict:
    """Run the rule detectors (primary) + anomaly net (secondary, novelty)."""
    session = SessionLocal()
    try:
        rule_findings = run_rules(session)
        covered = {f.policy_no for f in rule_findings}
        anomaly_findings = run_anomaly(session, exclude=covered)
    finally:
        session.close()

    findings = [
        FindingRec(
            policy_no=f.policy_no,
            reason_code=f.reason_code,
            severity=f.severity,
            explanation=f.explanation,
            detector=f.detector,
            score=f.score,
        )
        for f in (*rule_findings, *anomaly_findings)
    ]
    logger.info("Detect: %d findings (%d rule, %d anomaly).",
                len(findings), len(rule_findings), len(anomaly_findings))
    return {"findings": findings}


def decide(state: ReconciliationState) -> dict:
    """Map each finding to a gateable shape and tag its routing target.

    INFO-severity findings (rounding noise) are informational only. Every other
    finding becomes a PlanItem whose ``target`` is chosen with ``should_escalate``
    — exactly the predicate ``route()`` will apply — so Decide and the action
    layer never disagree.
    """
    session = SessionLocal()
    try:
        recon_by_policy = {
            r.policy_no: r
            for r in session.execute(select(ReconciliationResult)).scalars()
        }
    finally:
        session.close()

    plan: list[PlanItem] = []
    informational: list[PlanItem] = []
    for rec in state.findings:
        finding = Finding(
            policy_no=rec.policy_no,
            reason_code=rec.reason_code,
            severity=rec.severity,
            explanation=rec.explanation,
            detector=rec.detector,
            score=rec.score,
        )
        af = build_actionable_finding(finding, recon_by_policy.get(rec.policy_no))
        item_kwargs = dict(
            policy_no=af.policy_no,
            reason_code=af.reason_code,
            severity=rec.severity,
            amount=af.amount,
            status=af.status,
            detail=af.detail,
        )
        if rec.severity == Severity.INFO.value:
            informational.append(PlanItem(target="informational", **item_kwargs))
            continue
        target = "escalate" if should_escalate(af) is not None else "remediate"
        plan.append(PlanItem(target=target, **item_kwargs))

    n_rem = sum(1 for p in plan if p.target == "remediate")
    n_esc = sum(1 for p in plan if p.target == "escalate")
    logger.info("Decide: %d actionable (%d remediate, %d escalate), %d informational.",
                len(plan), n_rem, n_esc, len(informational))
    return {"plan": plan, "informational": informational}


def remediate(state: ReconciliationState) -> dict:
    """Auto-remediate every remediate-tagged finding via ``route()``."""
    remediated: list[OutcomeItem] = []
    skipped: list[OutcomeItem] = []
    for item in state.plan:
        if item.target != "remediate":
            continue
        result = route(_to_actionable(item))
        if isinstance(result, RemediationOutcome):
            if result.status is OutcomeStatus.BLOCKED:
                skipped.append(_outcome(item, "blocked", result.decision.code.value))
            else:
                action = "claim" if result.claim is not None else "rebill"
                ref = str(result.claim.id) if result.claim is not None else None
                remediated.append(_outcome(item, action, result.status.value, ref))
        else:  # defensive: route escalated something we tagged remediate
            remediated.append(_outcome(item, "escalation", result.reason.value,
                                       str(result.escalation.id)))
    logger.info("Remediate: %d remediated, %d below-threshold.",
                len(remediated), len(skipped))
    return {"remediated": remediated, "skipped": skipped}


def escalate(state: ReconciliationState) -> dict:
    """Route every escalate-tagged finding to the human queue via ``route()``."""
    escalated: list[OutcomeItem] = []
    for item in state.plan:
        if item.target != "escalate":
            continue
        result = route(_to_actionable(item))
        if isinstance(result, EscalationOutcome):
            escalated.append(_outcome(item, "escalation", result.reason.value,
                                      str(result.escalation.id)))
        else:  # defensive: route remediated something we tagged escalate
            action = "claim" if result.claim is not None else "rebill"
            ref = str(result.claim.id) if result.claim is not None else None
            escalated.append(_outcome(item, action, result.status.value, ref))
    logger.info("Escalate: %d routed to human queue.", len(escalated))
    return {"escalated": escalated}


def finalize(state: ReconciliationState) -> dict:
    """Assemble the end-to-end summary and persist findings for the read API.

    The summary splits exposure into underpayment (commission owed *to* us) and
    clawback (overpayment we owe *back*) — they have opposite sign and must not
    be summed together. Every finding is then materialised into the ``findings``
    table tagged with this run's id, so the API serves leaks from the table
    instead of re-running detection (and the Isolation Forest) per request.
    """
    leaks_by_reason = dict(sorted(Counter(p.reason_code for p in state.plan).items()))
    claimed = [o for o in state.remediated if o.action == "claim"]
    total_claimed = sum((o.amount for o in claimed), _ZERO)

    underpayment_exposure = sum(
        (p.amount for p in state.plan if p.reason_code in UNDERPAYMENT_REASONS), _ZERO
    )
    clawback_exposure = sum(
        (p.amount for p in state.plan if p.reason_code in CLAWBACK_REASONS), _ZERO
    )

    run_id = str(uuid.uuid4())
    summary = {
        "run_id": run_id,
        "policies_processed": state.policies_processed,
        "leaks_by_reason": leaks_by_reason,
        "auto_remediated": len(state.remediated),
        "escalated": len(state.escalated),
        "below_threshold": len(state.skipped),
        "informational": len(state.informational),
        "underpayment_exposure": str(underpayment_exposure),
        "clawback_exposure": str(clawback_exposure),
        "total_claimed": str(total_claimed),
    }

    _persist_run(state, run_id, summary)
    logger.info("Finalize: %s", summary)
    return {"summary": summary}


def _persist_run(state: ReconciliationState, run_id: str, summary: dict) -> None:
    """Write the per-finding rows and the run record for ``run_id``."""
    # Disposition per (policy_no, reason_code), derived from what actually
    # happened in the Remediate / Escalate nodes (not a re-gate).
    disposition_by_key: dict[tuple[str, str], str] = {}
    for p in state.informational:
        disposition_by_key[(p.policy_no, p.reason_code)] = "informational"
    for o in state.remediated:
        disposition_by_key[(o.policy_no, o.reason_code)] = (
            "escalated" if o.action == "escalation" else "remediated"
        )
    for o in state.skipped:
        disposition_by_key[(o.policy_no, o.reason_code)] = "below_threshold"
    for o in state.escalated:
        disposition_by_key[(o.policy_no, o.reason_code)] = "escalated"

    # The claimable amount + resolution snapshot already live on the plan items.
    plan_by_key = {
        (p.policy_no, p.reason_code): p for p in (*state.plan, *state.informational)
    }

    session = SessionLocal()
    try:
        recon = {
            r.policy_no: r
            for r in session.execute(select(ReconciliationResult)).scalars()
        }
        insurer = dict(
            session.execute(select(Policy.policy_no, Policy.insurer_name)).all()
        )
        for rec in state.findings:
            key = (rec.policy_no, rec.reason_code)
            pi = plan_by_key.get(key)
            r = recon.get(rec.policy_no)
            session.add(
                PersistedFinding(
                    run_id=run_id,
                    policy_no=rec.policy_no,
                    insurer=insurer.get(rec.policy_no),
                    reason_code=rec.reason_code,
                    severity=rec.severity,
                    explanation=rec.explanation,
                    detector=rec.detector,
                    score=rec.score,
                    amount=pi.amount if pi else _ZERO,
                    recon_status=r.status if r else None,
                    resolution_state=r.resolution_state if r else None,
                    disposition=disposition_by_key.get(key, "informational"),
                )
            )
        session.add(
            ReconciliationRun(
                id=run_id,
                finished_at=dt.datetime.now(dt.UTC),
                status="complete",
                summary=summary,
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Conditional routing out of Decide
# --------------------------------------------------------------------------- #
def decide_router(state: ReconciliationState) -> str:
    """Clean (no actionable leaks) → straight to Finalize; else run the action
    chain. Remediate and Escalate run in SEQUENCE (not as a parallel fan-out) so
    the append-only, hash-chained audit log has a single writer per run and its
    gap-free ``sequence_no`` never collides."""
    if not state.plan:
        return "finalize"
    return "remediate"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _to_actionable(item: PlanItem) -> ActionableFinding:
    return ActionableFinding(
        policy_no=item.policy_no,
        reason_code=item.reason_code,
        amount=item.amount,
        status=item.status,
        detail=item.detail,
    )


def _outcome(item: PlanItem, action: str, status: str, ref: str | None = None) -> OutcomeItem:
    return OutcomeItem(
        policy_no=item.policy_no,
        reason_code=item.reason_code,
        amount=item.amount,
        action=action,
        status=status,
        ref=ref,
    )


def _reset_action_tables() -> None:
    """Clear this run's claims + escalations so counts are deterministic.

    The append-only ``audit_log`` is intentionally NOT cleared.
    """
    from leaksentinel.actions.escalation import Escalation

    session = SessionLocal()
    try:
        session.query(CommissionClaim).delete()
        session.query(Escalation).delete()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Graph assembly
# --------------------------------------------------------------------------- #
def build_graph():
    """Compile and return the reconciliation graph."""
    g = StateGraph(ReconciliationState)
    g.add_node("intake", intake)
    g.add_node("reconcile", reconcile)
    g.add_node("detect", detect)
    g.add_node("decide", decide)
    g.add_node("remediate", remediate)
    g.add_node("escalate", escalate)
    g.add_node("finalize", finalize)

    g.add_edge(START, "intake")
    g.add_edge("intake", "reconcile")
    g.add_edge("reconcile", "detect")
    g.add_edge("detect", "decide")
    g.add_conditional_edges(
        "decide",
        decide_router,
        {"remediate": "remediate", "finalize": "finalize"},
    )
    # Sequential action chain: Remediate then Escalate (single audit writer).
    g.add_edge("remediate", "escalate")
    g.add_edge("escalate", "finalize")
    g.add_edge("finalize", END)

    return g.compile()
