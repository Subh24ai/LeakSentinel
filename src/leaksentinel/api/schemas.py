"""Pydantic v2 response models for the LeakSentinel API.

Every endpoint returns a typed, validated model — financial payloads are never
raw ORM dumps. Money is carried internally as :class:`~decimal.Decimal` and
serialised to a fixed-2dp **string** in JSON (``Money``), so a frontend never has
to reason about float rounding on rupee amounts.

The shapes are deliberately frontend-ready: amounts, human-readable explanations,
and pre-computed aggregates are included so a dashboard can render straight off
the response.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

# A rupee amount: Decimal in Python, "1234.56" string in JSON.
Money = Annotated[
    Decimal,
    PlainSerializer(lambda v: f"{Decimal(v):.2f}", return_type=str, when_used="json"),
]

Disposition = Literal["remediated", "escalated", "below_threshold", "informational"]


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
Role = Literal["admin", "ops", "viewer"]


class Token(BaseModel):
    """An issued JWT bearer token (OAuth2 password-flow response).

    ``must_change_password`` tells the client to route the user to the
    change-password screen before letting them into the app.
    """

    access_token: str
    token_type: str = "bearer"
    must_change_password: bool = False


class UserOut(BaseModel):
    """The authenticated principal, as returned by ``GET /auth/me``."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    role: str
    must_change_password: bool = False


class UserAdminOut(BaseModel):
    """Full user record for the admin user-management views. Never the hash."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    role: str
    is_active: bool
    must_change_password: bool
    created_by: str | None = None
    last_login_at: dt.datetime | None = None
    created_at: dt.datetime


class RegisterRequest(BaseModel):
    """Admin-created account: a temporary password the user must change."""

    email: str = Field(min_length=3)
    password: str = Field(min_length=8)
    role: Role


class ChangePasswordRequest(BaseModel):
    # new_password length is validated in the handler (returns 400, per spec),
    # not here (which would surface as a 422).
    current_password: str
    new_password: str


class UserPatchRequest(BaseModel):
    """Any subset of the admin-mutable fields."""

    role: Role | None = None
    is_active: bool | None = None


# --------------------------------------------------------------------------- #
# Leaks
# --------------------------------------------------------------------------- #
class LeakItem(BaseModel):
    """One detected leak, enriched for display (not the raw recon row)."""

    model_config = ConfigDict(from_attributes=True)

    policy_no: str
    insurer: str | None = None
    recon_status: str | None = Field(
        None, description="Reconciliation class, e.g. MISSING_COMMISSION / UNDERPAID."
    )
    reason_code: str = Field(description="Detector reason code (the leak type).")
    severity: str
    explanation: str = Field(description="Plain-English, human-readable reason.")
    amount: Money = Field(description="Rupee amount at risk for this policy.")
    resolution_state: str | None = None
    disposition: Disposition = Field(
        description="What the governed pipeline would do with this leak."
    )
    detector: str
    score: float | None = None


class LeakList(BaseModel):
    """A filtered list of leaks plus the echoed filters and a count."""

    count: int
    filters: dict[str, str | None]
    items: list[LeakItem]


class ReconView(BaseModel):
    """The reconciliation row for one policy (expected vs. actual)."""

    model_config = ConfigDict(from_attributes=True)

    status: str | None
    reason_code: str | None
    expected: Money | None = None
    actual: Money | None = None
    delta: Money | None = None
    resolution_state: str | None = None


class ActionTaken(BaseModel):
    """A concrete action recorded against a policy (claim or escalation)."""

    kind: Literal["claim", "escalation"]
    id: int
    status: str
    amount: Money | None = None
    reason: str | None = Field(None, description="reason_code or escalation_reason.")
    ref: str | None = Field(None, description="External ref / idempotency key / queue id.")
    created_at: dt.datetime | None = None


class LeakDetail(BaseModel):
    """Full picture for a single policy: recon + finding + actions taken."""

    policy_no: str
    insurer: str | None = None
    recon: ReconView | None = None
    finding: LeakItem | None = Field(
        None, description="The detector finding, if this policy is a leak."
    )
    actions: list[ActionTaken] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Claims / escalations / audit
# --------------------------------------------------------------------------- #
class ClaimItem(BaseModel):
    """A commission_claims row — money we are clawing back from the insurer."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    policy_no: str
    claim_amount: Money
    reason_code: str
    idempotency_key: str
    status: str
    external_ref: str | None = None
    created_at: dt.datetime


class EscalationItem(BaseModel):
    """An escalations row — one finding parked for human review, full context."""

    id: int
    policy_no: str
    reason_code: str
    escalation_reason: str
    amount: Money | None = None
    status: str
    finding_context: dict = Field(default_factory=dict)
    created_at: dt.datetime


class AuditItem(BaseModel):
    """An audit_log row — the append-only, hash-chained trail (every action AND
    every refusal). ``payload_sha256`` chains from ``prev_hash``; ``sequence_no``
    is gap-free, so tampering is detectable."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    sequence_no: int
    action: str
    payload_sha256: str | None = None
    prev_hash: str | None = None
    actor: str | None = None
    detail: str | None = None
    created_at: dt.datetime


# --------------------------------------------------------------------------- #
# Reconcile run + dashboard metrics
# --------------------------------------------------------------------------- #
class ConservationCheck(BaseModel):
    """Proof that nothing detected was silently dropped."""

    accounted: int
    total: int
    ok: bool


class ReconcileSummary(BaseModel):
    """The end-to-end result of a full pipeline run."""

    policies_processed: int
    leaks_by_reason: dict[str, int]
    auto_remediated: int
    escalated: int
    below_threshold: int
    informational: int
    underpayment_exposure: Money = Field(
        description="Commission owed TO us (missing/underpaid/unprovisioned/orphan/anomaly)."
    )
    clawback_exposure: Money = Field(
        description="Money we received but owe BACK (duplicate payments) — opposite sign."
    )
    total_claimed: Money = Field(
        description="Sum of claims lodged with the insurer. Not confirmed received."
    )
    conservation: ConservationCheck


class DispositionCounts(BaseModel):
    auto_remediated: int
    escalated: int
    below_threshold: int
    informational: int


class Metrics(BaseModel):
    """Aggregates shaped for a dashboard."""

    policies_processed: int
    policies_with_leaks: int
    underpayment_exposure: Money = Field(
        description="Commission owed TO us (missing/underpaid/unprovisioned/orphan/anomaly)."
    )
    clawback_exposure: Money = Field(
        description="Money we received but owe BACK (duplicate payments) — opposite sign."
    )
    total_claimed: Money = Field(
        description="Sum of claims lodged with the insurer. Not confirmed received."
    )
    claims_count: int
    leaks_by_insurer: dict[str, int]
    leaks_by_reason_code: dict[str, int]
    disposition: DispositionCounts
