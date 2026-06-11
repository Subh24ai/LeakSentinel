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

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    BackgroundTasks,
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.orm import Session

from leaksentinel import __version__
from leaksentinel.api import service
from leaksentinel.api.schemas import (
    AuditItem,
    ChangePasswordRequest,
    ClaimItem,
    EscalationItem,
    FeedUploadOut,
    LeakDetail,
    LeakList,
    Metrics,
    ReconcileAccepted,
    ReconcileJobOut,
    RegisterRequest,
    Token,
    UserAdminOut,
    UserOut,
    UserPatchRequest,
)
from leaksentinel.auth import (
    User,
    authenticate_user,
    bootstrap_admin,
    create_access_token,
    create_user,
    get_current_user,
    get_user_by_email,
    list_users,
    require_jwt_secret,
    require_password_changed,
    require_role,
    set_password,
    soft_delete_user,
    touch_last_login,
    update_user_fields,
    verify_password,
)
from leaksentinel.config import get_settings
from leaksentinel.db import SessionLocal, get_session
from leaksentinel.ingestion.normalizer import get_registered_insurers
from leaksentinel.ingestion.upload_processor import process_feed_upload
from leaksentinel.reconciliation.models import InsurerFeedUpload, ReconciliationJob

logger = logging.getLogger(__name__)

# Vite dev server the frontend will run on.
FRONTEND_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]

# Role bundles used across the endpoints.
_READ_ROLES = ("admin", "ops", "viewer")
_WRITE_ROLES = ("admin", "ops")

# Reusable role-gate dependencies (defining them once keeps them out of argument
# defaults, where a fresh call would trip flake8-bugbear B008).
_require_admin = require_role("admin")
_require_write = require_role(*_WRITE_ROLES)
_require_read = require_role(*_READ_ROLES)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Fail closed on startup: no JWT secret => refuse to boot. Ensure the upload
    directory exists, then bootstrap the first admin user if configured."""
    require_jwt_secret()  # raises if JWT_SECRET is unset — the service won't start
    Path(get_settings().upload_dir).mkdir(parents=True, exist_ok=True)
    try:
        bootstrap_admin()
    except Exception:  # pragma: no cover - depends on DB being migrated
        logger.exception("Admin bootstrap failed (is the schema migrated?)")
        raise
    yield

TAGS_METADATA = [
    {"name": "meta", "description": "Liveness and service metadata."},
    {"name": "auth", "description": "Token issuance and the current user."},
    {"name": "feeds", "description": "Upload and track insurer feed files."},
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
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
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
    """Exchange ``username`` (email) + ``password`` for a signed JWT. Public.

    Stamps ``last_login_at`` and echoes ``must_change_password`` so the client
    can route a temporary-password user to the change-password screen.
    """
    user = await run_in_threadpool(
        authenticate_user, session, form.username, form.password
    )
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )
    must_change = user.must_change_password
    token = create_access_token(user.id, user.role, must_change)
    await run_in_threadpool(_record_login, session, user)
    return Token(access_token=token, must_change_password=must_change)


@app.get("/auth/me", tags=["auth"], summary="Current authenticated user")
async def me(user: dict = Depends(require_password_changed)) -> UserOut:
    """Echo the authenticated principal (id, email, role, must_change_password)."""
    return UserOut(**user)


@app.post(
    "/auth/change-password",
    tags=["auth"],
    summary="Change your own password",
)
async def change_password(
    body: ChangePasswordRequest,
    current: dict = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    """Verify the current password and set a new one (>= 8 chars), clearing the
    must-change flag. Requires a valid token but NOT a changed password (this is
    the one endpoint a temporary-password user can reach besides login)."""
    await run_in_threadpool(
        _change_password, session, current["id"], body.current_password, body.new_password
    )
    return {"status": "password_changed"}


# --------------------------------------------------------------------------- #
# User management (admin)
# --------------------------------------------------------------------------- #
@app.post(
    "/auth/register",
    tags=["auth"],
    summary="Create a user (admin)",
    status_code=status.HTTP_201_CREATED,
)
async def register(
    body: RegisterRequest,
    current: dict = Depends(_require_admin),
    session: Session = Depends(get_session),
) -> UserAdminOut:
    """Create a user with a temporary password (must_change_password=True),
    stamped with the creating admin's email. 409 if the email already exists."""
    user = await run_in_threadpool(
        _register_user,
        session,
        body.email,
        body.password,
        body.role,
        current["email"],
    )
    return UserAdminOut.model_validate(user)


@app.get(
    "/auth/users",
    tags=["auth"],
    summary="List all users (admin)",
    dependencies=[Depends(_require_admin)],
)
async def list_users_endpoint(
    session: Session = Depends(get_session),
) -> list[UserAdminOut]:
    """Every user (no password hashes)."""
    users = await run_in_threadpool(list_users, session)
    return [UserAdminOut.model_validate(u) for u in users]


@app.patch(
    "/auth/users/{user_id}",
    tags=["auth"],
    summary="Update a user's role / active state (admin)",
)
async def patch_user(
    user_id: int,
    body: UserPatchRequest,
    current: dict = Depends(_require_admin),
    session: Session = Depends(get_session),
) -> UserAdminOut:
    """Change ``role`` and/or ``is_active``. You may not deactivate yourself or
    change your own role (guards against privilege accidents)."""
    if user_id == current["id"]:
        if body.is_active is False:
            raise HTTPException(status_code=400, detail="You cannot deactivate yourself.")
        if body.role is not None and body.role != current["role"]:
            raise HTTPException(status_code=400, detail="You cannot change your own role.")
    user = await run_in_threadpool(_patch_user, session, user_id, body.role, body.is_active)
    if user is None:
        raise HTTPException(status_code=404, detail=f"Unknown user {user_id}")
    return UserAdminOut.model_validate(user)


@app.delete(
    "/auth/users/{user_id}",
    tags=["auth"],
    summary="Deactivate a user (admin, soft delete)",
)
async def delete_user(
    user_id: int,
    current: dict = Depends(_require_admin),
    session: Session = Depends(get_session),
) -> UserAdminOut:
    """Soft delete: sets ``is_active=False`` (keeps the row so the created_by /
    audit trail is preserved). You cannot delete yourself."""
    if user_id == current["id"]:
        raise HTTPException(status_code=400, detail="You cannot delete yourself.")
    user = await run_in_threadpool(_soft_delete, session, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail=f"Unknown user {user_id}")
    return UserAdminOut.model_validate(user)


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #
@app.post(
    "/reconcile",
    tags=["pipeline"],
    summary="Enqueue a reconciliation run",
    status_code=status.HTTP_202_ACCEPTED,
)
async def reconcile(
    background: BackgroundTasks,
    current: dict = Depends(_require_write),
) -> ReconcileAccepted:
    """Enqueue a full pipeline run and return immediately (202). The run executes
    in a background thread; poll ``GET /reconcile/{job_id}`` for status/summary.
    Requires the ``admin`` or ``ops`` role.

    Single-flight: if a job is already queued or running, its id is returned
    instead of starting a second run (concurrent pipeline runs would race on the
    shared reconciliation tables).
    """
    job_id, created = await run_in_threadpool(_create_job, current["email"])
    if created:
        background.add_task(_run_reconcile_job, job_id)
    return ReconcileAccepted(job_id=job_id, status="queued")


@app.get(
    "/reconcile/jobs",
    tags=["pipeline"],
    summary="Recent reconciliation jobs",
    dependencies=[Depends(_require_write)],
)
async def list_reconcile_jobs() -> list[ReconcileJobOut]:
    """The last 20 jobs, newest first. Requires the ``admin`` or ``ops`` role."""
    jobs = await run_in_threadpool(_list_jobs)
    return [ReconcileJobOut.model_validate(j) for j in jobs]


@app.get(
    "/reconcile/{job_id}",
    tags=["pipeline"],
    summary="Reconciliation job status",
    dependencies=[Depends(_require_read)],
)
async def get_reconcile_job(job_id: str) -> ReconcileJobOut:
    """Status + summary for one job. 404 if unknown."""
    job = await run_in_threadpool(_get_job, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown job {job_id}")
    return ReconcileJobOut.model_validate(job)


# --------------------------------------------------------------------------- #
# Feeds
# --------------------------------------------------------------------------- #
@app.post(
    "/feeds/upload",
    tags=["feeds"],
    summary="Upload an insurer feed (CSV or PDF)",
    status_code=status.HTTP_201_CREATED,
)
async def upload_feed(
    file: UploadFile = File(...),
    insurer_name: str = Form(...),
    current: dict = Depends(_require_write),
) -> FeedUploadOut:
    """Upload a CSV/PDF feed for a known insurer, persist it, and process it
    synchronously (normalize + load into insurer_commission_feeds). Requires the
    ``admin`` or ``ops`` role."""
    if insurer_name not in get_registered_insurers():
        raise HTTPException(
            status_code=400,
            detail=f"Unknown insurer {insurer_name!r}. Allowed: {get_registered_insurers()}",
        )
    ext = Path(file.filename or "").suffix.lower()
    if ext not in (".csv", ".pdf"):
        raise HTTPException(
            status_code=400, detail="Only .csv and .pdf files are accepted."
        )
    content = await file.read()
    upload = await run_in_threadpool(
        _handle_upload, current["email"], file.filename or "upload", insurer_name,
        ext.lstrip("."), content,
    )
    return FeedUploadOut.model_validate(upload)


@app.get(
    "/feeds",
    tags=["feeds"],
    summary="List feed uploads",
    dependencies=[Depends(_require_write)],
)
async def list_feeds() -> list[FeedUploadOut]:
    """All uploads, newest first. Requires the ``admin`` or ``ops`` role."""
    rows = await run_in_threadpool(_list_uploads)
    return [FeedUploadOut.model_validate(r) for r in rows]


@app.get(
    "/feeds/{upload_id}",
    tags=["feeds"],
    summary="One feed upload",
    dependencies=[Depends(_require_write)],
)
async def get_feed(upload_id: int) -> FeedUploadOut:
    """A single upload record. 404 if unknown."""
    row = await run_in_threadpool(_get_upload, upload_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"Unknown upload {upload_id}")
    return FeedUploadOut.model_validate(row)


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


# --------------------------------------------------------------------------- #
# Blocking DB helpers for the auth endpoints (run in a threadpool). Each owns its
# commit; HTTPExceptions raised here propagate to FastAPI's handler as normal.
# --------------------------------------------------------------------------- #
def _record_login(session: Session, user: User) -> None:
    touch_last_login(session, user)
    session.commit()


def _change_password(
    session: Session, user_id: int, current_password: str, new_password: str
) -> None:
    if len(new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be at least 8 characters.")
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found.")
    if not verify_password(current_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Current password is incorrect.")
    set_password(session, user, new_password)
    session.commit()


def _register_user(
    session: Session, email: str, password: str, role: str, created_by: str
) -> User:
    if get_user_by_email(session, email) is not None:
        raise HTTPException(status_code=409, detail=f"A user with email {email!r} already exists.")
    user = create_user(
        session, email, password, role, created_by=created_by, must_change_password=True
    )
    session.commit()
    return user


def _patch_user(
    session: Session, user_id: int, role: str | None, is_active: bool | None
) -> User | None:
    user = update_user_fields(session, user_id, role=role, is_active=is_active)
    if user is not None:
        session.commit()
    return user


def _soft_delete(session: Session, user_id: int) -> User | None:
    user = soft_delete_user(session, user_id)
    if user is not None:
        session.commit()
    return user


# --------------------------------------------------------------------------- #
# Feed upload helpers (threadpool)
# --------------------------------------------------------------------------- #
def _handle_upload(
    email: str, filename: str, insurer_name: str, file_type: str, content: bytes
) -> InsurerFeedUpload:
    """Save the file, record the upload row, and process it synchronously."""
    upload_dir = Path(get_settings().upload_dir)
    upload_dir.mkdir(parents=True, exist_ok=True)
    dest = upload_dir / f"{uuid.uuid4().hex}_{Path(filename).name}"
    dest.write_bytes(content)

    session = SessionLocal()
    try:
        upload = InsurerFeedUpload(
            filename=filename,
            insurer_name=insurer_name,
            file_type=file_type,
            file_size_bytes=len(content),
            storage_path=str(dest),
            status="uploaded",
            uploaded_by=email,
        )
        session.add(upload)
        session.commit()
        upload_id = upload.id
    finally:
        session.close()

    process_feed_upload(upload_id)  # owns its own session + commits final status

    session = SessionLocal()
    try:
        return session.get(InsurerFeedUpload, upload_id)
    finally:
        session.close()


def _list_uploads() -> list[InsurerFeedUpload]:
    session = SessionLocal()
    try:
        return list(
            session.execute(
                select(InsurerFeedUpload).order_by(
                    InsurerFeedUpload.uploaded_at.desc(), InsurerFeedUpload.id.desc()
                )
            )
            .scalars()
            .all()
        )
    finally:
        session.close()


def _get_upload(upload_id: int) -> InsurerFeedUpload | None:
    session = SessionLocal()
    try:
        return session.get(InsurerFeedUpload, upload_id)
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Async reconcile job helpers (each owns a fresh session)
# --------------------------------------------------------------------------- #
def _create_job(email: str) -> tuple[str, bool]:
    """Return ``(job_id, created)``. If a job is already queued/running, reuse it
    (created=False) so we never start two concurrent pipeline runs."""
    session = SessionLocal()
    try:
        active = session.execute(
            select(ReconciliationJob.id)
            .where(ReconciliationJob.status.in_(("queued", "running")))
            .order_by(ReconciliationJob.queued_at.desc())
            .limit(1)
        ).scalar_one_or_none()
        if active is not None:
            return active, False
        job_id = str(uuid.uuid4())
        session.add(ReconciliationJob(id=job_id, status="queued", triggered_by=email))
        session.commit()
        return job_id, True
    finally:
        session.close()


def _run_reconcile_job(job_id: str) -> None:
    """Background task: run the pipeline and record the outcome. Uses fresh
    sessions (the request session is long gone by now)."""
    session = SessionLocal()
    try:
        job = session.get(ReconciliationJob, job_id)
        if job is None:
            return
        job.status = "running"
        job.started_at = _utcnow()
        session.commit()
    finally:
        session.close()

    try:
        summary = service.run_reconcile()
        _finish_job(job_id, "complete", summary=summary.model_dump(mode="json"))
    except Exception as exc:  # noqa: BLE001 - record any failure on the job
        logger.exception("Reconcile job %s failed.", job_id)
        _finish_job(job_id, "failed", error=str(exc))


def _finish_job(
    job_id: str, status_: str, *, summary: dict | None = None, error: str | None = None
) -> None:
    session = SessionLocal()
    try:
        job = session.get(ReconciliationJob, job_id)
        if job is None:
            return
        job.status = status_
        job.finished_at = _utcnow()
        if summary is not None:
            job.summary = summary
        if error is not None:
            job.error_message = error[:2000]
        session.commit()
    finally:
        session.close()


def _get_job(job_id: str) -> ReconciliationJob | None:
    session = SessionLocal()
    try:
        return session.get(ReconciliationJob, job_id)
    finally:
        session.close()


def _list_jobs() -> list[ReconciliationJob]:
    session = SessionLocal()
    try:
        return list(
            session.execute(
                select(ReconciliationJob)
                .order_by(ReconciliationJob.queued_at.desc())
                .limit(20)
            )
            .scalars()
            .all()
        )
    finally:
        session.close()


def _utcnow():
    import datetime as _dt

    return _dt.datetime.now(_dt.UTC)


def run() -> None:
    """Console-script entrypoint: ``leaksentinel``."""
    import uvicorn

    uvicorn.run("leaksentinel.api.main:app", host="127.0.0.1", port=8000, reload=True)
