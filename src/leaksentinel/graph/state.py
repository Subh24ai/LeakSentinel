"""Typed state for the LeakSentinel reconciliation graph.

A single pydantic ``ReconciliationState`` flows through every node. Nodes return
partial updates that LangGraph merges in. Keeping it typed (and free of ORM
objects) means the state stays inspectable, serialisable, and easy to assert on
in tests — the graph's contract is data, not live database handles.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class FindingRec(BaseModel):
    """A detector finding, flattened for the state (serialisable, no ORM)."""

    policy_no: str
    reason_code: str
    severity: str
    explanation: str
    detector: str
    score: float | None = None


class PlanItem(BaseModel):
    """One detected leak, already mapped to its gateable shape + routing target.

    ``target`` is decided by the Decide node using the *same* predicates the
    actions layer's gate uses, so the node a finding is sent to always agrees
    with what ``route()`` will actually do.
    """

    policy_no: str
    reason_code: str
    severity: str
    amount: Decimal
    status: str                 # reconciliation resolution_state ('open' = confirmed)
    detail: str
    target: Literal["remediate", "escalate", "informational"]


class OutcomeItem(BaseModel):
    """What an action did to one finding (for the final summary + audit cross-check)."""

    policy_no: str
    reason_code: str
    amount: Decimal
    action: str                 # 'claim' | 'rebill' | 'escalation' | 'blocked'
    status: str                 # outcome status / escalation reason / gate code
    ref: str | None = None      # claim id, escalation id, or external ref


class ReconciliationState(BaseModel):
    """The state object threaded through Intake → Reconcile → Detect → Decide →
    Remediate/Escalate → Finalize."""

    # --- inputs ---------------------------------------------------------- #
    # Clear this run's commission_claims / escalations first so a re-run is
    # deterministic (the audit_log stays append-only and is never cleared).
    reset_actions: bool = True

    # --- produced by Intake / Reconcile / Detect ------------------------- #
    ingest_summary: dict[str, dict[str, int]] = Field(default_factory=dict)
    detected: dict[str, list[str]] = Field(default_factory=dict)  # class -> policy_nos
    policies_processed: int = 0
    findings: list[FindingRec] = Field(default_factory=list)      # raw detector output
    informational: list[PlanItem] = Field(default_factory=list)   # INFO-only (rounding)

    # --- produced by Decide ---------------------------------------------- #
    plan: list[PlanItem] = Field(default_factory=list)            # actionable leaks

    # --- produced by Remediate / Escalate (disjoint keys, run in parallel) #
    remediated: list[OutcomeItem] = Field(default_factory=list)
    escalated: list[OutcomeItem] = Field(default_factory=list)
    skipped: list[OutcomeItem] = Field(default_factory=list)      # gated-out (too small)

    # --- produced by Finalize -------------------------------------------- #
    summary: dict = Field(default_factory=dict)
