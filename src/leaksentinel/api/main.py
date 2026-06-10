"""FastAPI application for LeakSentinel.

Async HTTP surface over the commission-reconciliation pipeline. Endpoints are
``async`` and offload the (synchronous) SQLAlchemy / LangGraph work to a
threadpool via :func:`fastapi.concurrency.run_in_threadpool`, so a slow pipeline
run never blocks the event loop. Every response is a validated pydantic v2 model
(see :mod:`leaksentinel.api.schemas`) shaped for a frontend to consume directly —
aggregates and human-readable explanations included, never raw ORM dumps.

CORS is enabled for the Vite dev server (http://localhost:5173).
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Query, status
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.orm import Session

from leaksentinel import __version__
from leaksentinel.api import service
from leaksentinel.api.schemas import (
    AuditItem,
    ClaimItem,
    EscalationItem,
    LeakDetail,
    LeakList,
    Metrics,
    ReconcileSummary,
    Token,
    UserOut,
)
from leaksentinel.auth import (
    authenticate_user,
    bootstrap_admin,
    create_access_token,
    get_current_user,
    require_jwt_secret,
    require_role,
)
from leaksentinel.config import get_settings
from leaksentinel.db import get_session

logger = logging.getLogger(__name__)

# Vite dev server the frontend will run on.
FRONTEND_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]

# Role bundles used across the endpoints.
_READ_ROLES = ("admin", "ops", "viewer")
_WRITE_ROLES = ("admin", "ops")

# Serializes pipeline runs so two concurrent POST /reconcile calls cannot race on
# the shared action tables (M6). A second caller while one is running gets 409.
_reconcile_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Fail closed on startup: no JWT secret => refuse to boot. Then bootstrap
    the first admin user if configured."""
    require_jwt_secret()  # raises if JWT_SECRET is unset — the service won't start
    try:
        bootstrap_admin()
    except Exception:  # pragma: no cover - depends on DB being migrated
        logger.exception("Admin bootstrap failed (is the schema migrated?)")
        raise
    yield

TAGS_METADATA = [
    {"name": "meta", "description": "Liveness and service metadata."},
    {"name": "auth", "description": "Token issuance and the current user."},
    {"name": "pipeline", "description": "Trigger and inspect reconciliation runs."},
    {"name": "leaks", "description": "Detected leaks with explanations and amounts."},
    {"name": "actions", "description": "Claims clawed back and the human escalation queue."},
    {"name": "audit", "description": "Immutable audit trail of every action and refusal."},
    {"name": "metrics", "description": "Dashboard aggregates."},
]

app = FastAPI(
    title="LeakSentinel API",
    description=(
        "Commission-reconciliation engine for two-wheeler insurance distribution. "
        "Detection and every financial decision are deterministic and audited; the "
        "LLM only reads documents. All data endpoints require a JWT bearer token."
    ),
    version=__version__,
    openapi_tags=TAGS_METADATA,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    # No cookies are used — auth is a bearer token in the Authorization header —
    # so credentialed CORS is unnecessary (L2).
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #
@app.get("/health", tags=["meta"], summary="Liveness probe")
async def health() -> dict[str, str]:
    """Return ``ok`` plus the service version."""
    return {"status": "ok", "version": __version__}


@app.get("/", tags=["meta"], summary="Service metadata")
async def root() -> dict[str, str]:
    """Service name, version, and the active LLM provider."""
    settings = get_settings()
    return {
        "service": "leaksentinel",
        "version": __version__,
        "llm_provider": settings.llm_provider.value,
    }


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
@app.post("/auth/token", tags=["auth"], summary="Obtain a JWT (OAuth2 password flow)")
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    session: Session = Depends(get_session),
) -> Token:
    """Exchange ``username`` (email) + ``password`` for a signed JWT. Public."""
    user = await run_in_threadpool(
        authenticate_user, session, form.username, form.password
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Token(access_token=create_access_token(user.id, user.role))


@app.get("/auth/me", tags=["auth"], summary="Current authenticated user")
async def me(user: dict = Depends(get_current_user)) -> UserOut:
    """Echo the authenticated principal (id, email, role). Requires any token."""
    return UserOut(**user)


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
@app.post(
    "/reconcile",
    tags=["pipeline"],
    summary="Run the full pipeline",
    dependencies=[Depends(require_role(*_WRITE_ROLES))],
)
async def reconcile() -> ReconcileSummary:
    """Trigger a full LangGraph pipeline run (Intake → Reconcile → Detect →
    Decide → Remediate | Escalate → Finalize) and return the end-to-end summary:
    policies processed, leaks by reason code, disposition counts, money at risk
    and claimed back, and a conservation check proving nothing was dropped.

    Requires the ``admin`` or ``ops`` role. Serialized: while one run is in
    flight, a concurrent call gets 409 rather than racing on the action tables.
    """
    if _reconcile_lock.locked():
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="reconciliation already running",
        )
    async with _reconcile_lock:
        return await run_in_threadpool(service.run_reconcile)


# --------------------------------------------------------------------------- #
# Leaks
# --------------------------------------------------------------------------- #
@app.get(
    "/leaks",
    tags=["leaks"],
    summary="List detected leaks",
    dependencies=[Depends(require_role(*_READ_ROLES))],
)
async def list_leaks(
    status: str | None = Query(None, description="Reconciliation class, e.g. MISSING_COMMISSION."),
    insurer: str | None = Query(None, description="Insurer name."),
    reason_code: str | None = Query(None, description="Detector reason code (leak type)."),
    severity: str | None = Query(None, description="info | low | medium | high."),
) -> LeakList:
    """Enriched, filterable list of leaks. Each item carries the human-readable
    explanation, the rupee amount at risk, and the intended disposition — not
    just raw reconciliation columns.
    """
    return await run_in_threadpool(
        service.list_leaks, status, insurer, reason_code, severity
    )


@app.get(
    "/leaks/{policy_no}",
    tags=["leaks"],
    summary="Leak detail for one policy",
    dependencies=[Depends(require_role(*_READ_ROLES))],
)
async def leak_detail(policy_no: str) -> LeakDetail:
    """Full picture for one policy: the reconciliation result, the detector
    finding (reason code + explanation), and any action taken (claim or
    escalation). 404 if the policy is unknown.
    """
    detail = await run_in_threadpool(service.leak_detail, policy_no)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"Unknown policy_no {policy_no!r}")
    return detail


# --------------------------------------------------------------------------- #
# Actions
# --------------------------------------------------------------------------- #
@app.get(
    "/claims",
    tags=["actions"],
    summary="Commission claims clawed back",
    dependencies=[Depends(require_role(*_READ_ROLES))],
)
async def list_claims() -> list[ClaimItem]:
    """Every ``commission_claims`` row — the shortfalls we are claiming back."""
    return await run_in_threadpool(service.list_claims)


@app.get(
    "/escalations",
    tags=["actions"],
    summary="Human review queue",
    dependencies=[Depends(require_role(*_READ_ROLES))],
)
async def list_escalations() -> list[EscalationItem]:
    """The escalation queue, each item carrying the full finding context."""
    return await run_in_threadpool(service.list_escalations)


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
@app.get(
    "/audit",
    tags=["audit"],
    summary="Audit log",
    dependencies=[Depends(require_role(*_READ_ROLES))],
)
async def list_audit(
    limit: int = Query(100, ge=1, le=1000, description="Most recent N rows."),
) -> list[AuditItem]:
    """The append-only, hash-chained audit trail (most recent first): every
    action AND every blocked attempt, each with a chained SHA-256 and a gap-free
    sequence_no so tampering is detectable.
    """
    return await run_in_threadpool(service.list_audit, limit)


# --------------------------------------------------------------------------- #
# Metrics
# --------------------------------------------------------------------------- #
@app.get(
    "/metrics",
    tags=["metrics"],
    summary="Dashboard aggregates",
    dependencies=[Depends(require_role(*_READ_ROLES))],
)
async def metrics() -> Metrics:
    """Aggregates for a dashboard: total ₹ at risk, ₹ recovered, leaks by insurer
    and by reason code, and disposition counts.
    """
    return await run_in_threadpool(service.metrics)


def run() -> None:
    """Console-script entrypoint: ``leaksentinel``."""
    import uvicorn

    uvicorn.run("leaksentinel.api.main:app", host="127.0.0.1", port=8000, reload=True)
