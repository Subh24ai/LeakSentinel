"""Service layer for the API — all the (synchronous) database and pipeline work.

The HTTP layer (:mod:`leaksentinel.api.main`) is thin and async; it offloads
every function here to a threadpool. Keeping the blocking SQLAlchemy / LangGraph
work in one place means the endpoints stay declarative and the business logic is
unit-testable without a web server.

Leaks are served from the ``findings`` table, materialised once per pipeline run
(see :func:`leaksentinel.graph.workflow.finalize`): reads filter to the latest
completed ``run_id`` rather than re-running the rule detectors and the Isolation
Forest on every request. The API therefore reflects the same explainable findings
the rest of the system uses — human-readable explanations and rupee amounts
included — without re-fitting the model per call.
"""

from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal

from sqlalchemy import select

from leaksentinel.actions.escalation import Escalation
from leaksentinel.actions.remediation import CommissionClaim
from leaksentinel.api.schemas import (
    ActionTaken,
    AuditItem,
    ClaimItem,
    ConservationCheck,
    DispositionCounts,
    EscalationItem,
    LeakDetail,
    LeakItem,
    LeakList,
    Metrics,
    ReconcileSummary,
    ReconView,
)
from leaksentinel.db import SessionLocal
from leaksentinel.detection.rules import CLAWBACK_REASONS, UNDERPAYMENT_REASONS
from leaksentinel.graph.run import run_pipeline
from leaksentinel.reconciliation.models import (
    AuditLog,
    PersistedFinding,
    Policy,
    ReconciliationResult,
    ReconciliationRun,
)

_ZERO = Decimal("0.00")


# --------------------------------------------------------------------------- #
# Core: read the enriched leak list from the persisted findings of the latest run
# --------------------------------------------------------------------------- #
# Leaks are materialised once per pipeline run (see graph.workflow.finalize), so
# reads are a plain table query — the rule detectors and the Isolation Forest are
# NOT re-run per request (H4).
def _latest_run_id(session) -> str | None:
    """The id of the most recent successfully completed pipeline run, if any."""
    return session.execute(
        select(ReconciliationRun.id)
        .where(ReconciliationRun.status == "complete")
        .order_by(ReconciliationRun.finished_at.desc(), ReconciliationRun.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def _finding_to_item(f: PersistedFinding) -> LeakItem:
    return LeakItem(
        policy_no=f.policy_no,
        insurer=f.insurer,
        recon_status=f.recon_status,
        reason_code=f.reason_code,
        severity=f.severity,
        explanation=f.explanation,
        amount=f.amount,
        resolution_state=f.resolution_state,
        disposition=f.disposition,
        detector=f.detector,
        score=f.score,
    )


def _leak_items(session) -> list[LeakItem]:
    """All persisted findings for the latest completed run (empty before any run)."""
    run_id = _latest_run_id(session)
    if run_id is None:
        return []
    rows = (
        session.execute(
            select(PersistedFinding)
            .where(PersistedFinding.run_id == run_id)
            .order_by(PersistedFinding.policy_no)
        )
        .scalars()
        .all()
    )
    return [_finding_to_item(f) for f in rows]


# --------------------------------------------------------------------------- #
# Endpoint services
# --------------------------------------------------------------------------- #
def list_leaks(
    status: str | None = None,
    insurer: str | None = None,
    reason_code: str | None = None,
    severity: str | None = None,
) -> LeakList:
    """Enriched, filterable list of detected leaks."""
    session = SessionLocal()
    try:
        items = _leak_items(session)
    finally:
        session.close()

    def keep(it: LeakItem) -> bool:
        if status and (it.recon_status or "").upper() != status.upper():
            return False
        if insurer and (it.insurer or "").lower() != insurer.lower():
            return False
        if reason_code and it.reason_code.upper() != reason_code.upper():
            return False
        if severity and it.severity.lower() != severity.lower():
            return False
        return True

    filtered = [it for it in items if keep(it)]
    return LeakList(
        count=len(filtered),
        filters={
            "status": status,
            "insurer": insurer,
            "reason_code": reason_code,
            "severity": severity,
        },
        items=filtered,
    )


def leak_detail(policy_no: str) -> LeakDetail | None:
    """Full detail for one policy: recon row, finding, and actions taken.

    Returns ``None`` if the policy doesn't exist (the route turns that into 404).
    """
    session = SessionLocal()
    try:
        policy = session.execute(
            select(Policy).where(Policy.policy_no == policy_no)
        ).scalar_one_or_none()
        if policy is None:
            return None

        recon = session.execute(
            select(ReconciliationResult).where(
                ReconciliationResult.policy_no == policy_no
            )
        ).scalar_one_or_none()

        run_id = _latest_run_id(session)
        finding_row = (
            session.execute(
                select(PersistedFinding).where(
                    PersistedFinding.run_id == run_id,
                    PersistedFinding.policy_no == policy_no,
                )
            )
            .scalars()
            .first()
            if run_id is not None
            else None
        )
        finding_item = _finding_to_item(finding_row) if finding_row else None

        claims = session.execute(
            select(CommissionClaim).where(CommissionClaim.policy_no == policy_no)
        ).scalars().all()
        escs = session.execute(
            select(Escalation).where(Escalation.policy_no == policy_no)
        ).scalars().all()
    finally:
        session.close()

    actions: list[ActionTaken] = []
    for c in claims:
        actions.append(
            ActionTaken(
                kind="claim", id=c.id, status=c.status, amount=c.claim_amount,
                reason=c.reason_code, ref=c.external_ref, created_at=c.created_at,
            )
        )
    for e in escs:
        actions.append(
            ActionTaken(
                kind="escalation", id=e.id, status=e.status, amount=e.amount,
                reason=e.escalation_reason, ref=str(e.id), created_at=e.created_at,
            )
        )

    return LeakDetail(
        policy_no=policy_no,
        insurer=policy.insurer_name,
        recon=ReconView.model_validate(recon) if recon else None,
        finding=finding_item,
        actions=actions,
    )


def list_claims() -> list[ClaimItem]:
    session = SessionLocal()
    try:
        rows = session.execute(
            select(CommissionClaim).order_by(CommissionClaim.id)
        ).scalars().all()
        return [ClaimItem.model_validate(r) for r in rows]
    finally:
        session.close()


def list_escalations() -> list[EscalationItem]:
    session = SessionLocal()
    try:
        rows = session.execute(select(Escalation).order_by(Escalation.id)).scalars().all()
        return [
            EscalationItem(
                id=e.id,
                policy_no=e.policy_no,
                reason_code=e.reason_code,
                escalation_reason=e.escalation_reason,
                amount=e.amount,
                status=e.status,
                finding_context=_safe_json(e.finding_context),
                created_at=e.created_at,
            )
            for e in rows
        ]
    finally:
        session.close()


def list_audit(limit: int = 100) -> list[AuditItem]:
    session = SessionLocal()
    try:
        rows = session.execute(
            select(AuditLog).order_by(AuditLog.id.desc()).limit(limit)
        ).scalars().all()
        return [AuditItem.model_validate(r) for r in rows]
    finally:
        session.close()


def metrics() -> Metrics:
    """Dashboard aggregates derived from current DB state."""
    session = SessionLocal()
    try:
        items = _leak_items(session)
        n_policies = session.query(ReconciliationResult).count()
        claims = session.execute(select(CommissionClaim)).scalars().all()
    finally:
        session.close()

    actionable = [it for it in items if it.disposition != "informational"]
    by_insurer = Counter(it.insurer or "unknown" for it in actionable)
    by_reason = Counter(it.reason_code for it in actionable)
    disp = Counter(it.disposition for it in items)
    # Underpayment (owed to us) and clawback (we owe back) have opposite sign and
    # are reported separately — never as one mixed "at risk" figure (M3).
    underpayment_exposure = sum(
        (it.amount for it in actionable if it.reason_code in UNDERPAYMENT_REASONS), _ZERO
    )
    clawback_exposure = sum(
        (it.amount for it in actionable if it.reason_code in CLAWBACK_REASONS), _ZERO
    )
    total_claimed = sum((c.claim_amount for c in claims), _ZERO)

    return Metrics(
        policies_processed=n_policies,
        policies_with_leaks=len({it.policy_no for it in actionable}),
        underpayment_exposure=underpayment_exposure,
        clawback_exposure=clawback_exposure,
        total_claimed=total_claimed,
        claims_count=len(claims),
        leaks_by_insurer=dict(sorted(by_insurer.items())),
        leaks_by_reason_code=dict(sorted(by_reason.items())),
        disposition=DispositionCounts(
            auto_remediated=disp.get("remediated", 0),
            escalated=disp.get("escalated", 0),
            below_threshold=disp.get("below_threshold", 0),
            informational=disp.get("informational", 0),
        ),
    )


def run_reconcile() -> ReconcileSummary:
    """Run the full LangGraph pipeline and return the typed summary."""
    state = run_pipeline(reset_actions=True)
    s = state.summary
    accounted = s["auto_remediated"] + s["escalated"] + s["below_threshold"]
    total = len(state.plan)
    return ReconcileSummary(
        policies_processed=s["policies_processed"],
        leaks_by_reason=s["leaks_by_reason"],
        auto_remediated=s["auto_remediated"],
        escalated=s["escalated"],
        below_threshold=s["below_threshold"],
        informational=s["informational"],
        underpayment_exposure=Decimal(s["underpayment_exposure"]),
        clawback_exposure=Decimal(s["clawback_exposure"]),
        total_claimed=Decimal(s["total_claimed"]),
        conservation=ConservationCheck(
            accounted=accounted, total=total, ok=(accounted == total)
        ),
    )


def _safe_json(blob: str | None) -> dict:
    if not blob:
        return {}
    try:
        data = json.loads(blob)
        return data if isinstance(data, dict) else {"value": data}
    except (ValueError, TypeError):
        return {}
