"""Async httpx tests for the FastAPI surface.

Exercises the app in-process via ``httpx.ASGITransport`` (no network, no uvicorn)
against the live Postgres. Covers POST /reconcile, GET /leaks with a filter,
GET /leaks/{policy_no}, and GET /metrics. Skipped if Postgres or the synthetic
data isn't present, like the other DB-backed tests.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError

from leaksentinel.api.main import app
from leaksentinel.auth import create_user, get_user_by_email, hash_password
from leaksentinel.db import SessionLocal, create_all
from leaksentinel.db import engine as db_engine
from leaksentinel.reconciliation.models import Policy

pytestmark = pytest.mark.integration

ADMIN_EMAIL = "ci-admin@leaksentinel.local"
ADMIN_PW = "ci-admin-pw-123"


def _db_ready() -> bool:
    try:
        db_engine.connect().close()
    except OperationalError:
        return False
    session = SessionLocal()
    try:
        return session.query(Policy).count() > 0
    finally:
        session.close()


def _ensure_admin() -> None:
    """Make sure a usable admin exists. Force a known password and clear the
    must-change flag so the data tests aren't password-gated, regardless of any
    prior run that may have changed/locked the account."""
    create_all()  # ensure users + action tables exist
    session = SessionLocal()
    try:
        user = get_user_by_email(session, ADMIN_EMAIL)
        if user is None:
            create_user(session, ADMIN_EMAIL, ADMIN_PW, "admin", must_change_password=False)
        else:
            user.hashed_password = hash_password(ADMIN_PW)
            user.must_change_password = False
            user.is_active = True
        session.commit()
    finally:
        session.close()


@pytest_asyncio.fixture(scope="module")
async def client():
    if not _db_ready():
        pytest.skip("Postgres not reachable or no synthetic data loaded")
    _ensure_admin()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # Authenticate through the real OAuth2 token endpoint, then attach the
        # bearer token to every subsequent request from this client.
        resp = await c.post(
            "/auth/token", data={"username": ADMIN_EMAIL, "password": ADMIN_PW}
        )
        assert resp.status_code == 200, resp.text
        token = resp.json()["access_token"]
        assert resp.json()["token_type"] == "bearer"
        c.headers.update({"Authorization": f"Bearer {token}"})
        yield c


@pytest_asyncio.fixture(scope="module")
async def reconciled(client):
    """Run the pipeline once so the action tables are populated for read tests."""
    resp = await client.post("/reconcile")
    assert resp.status_code == 200
    return resp.json()


@pytest.mark.asyncio
async def test_reconcile_returns_summary(reconciled):
    body = reconciled
    assert body["policies_processed"] == 200
    assert body["leaks_by_reason"]["MISSING_COMMISSION"] == 7
    assert body["conservation"]["ok"] is True
    # Money is serialised as a fixed-2dp string, not a float.
    assert isinstance(body["total_claimed"], str)
    assert Decimal(body["total_claimed"]) > 0
    # Exposure is split by sign — underpayment (owed to us) vs clawback (we owe).
    assert Decimal(body["underpayment_exposure"]) > 0
    assert isinstance(body["clawback_exposure"], str)


@pytest.mark.asyncio
async def test_leaks_filter_by_reason_code(client, reconciled):
    resp = await client.get("/leaks", params={"reason_code": "MISSING_COMMISSION"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["filters"]["reason_code"] == "MISSING_COMMISSION"
    assert body["count"] == 7
    assert len(body["items"]) == 7
    for item in body["items"]:
        assert item["reason_code"] == "MISSING_COMMISSION"
        assert item["explanation"]                    # human-readable text present
        assert Decimal(item["amount"]) > 0            # rupee amount present
        assert item["disposition"] == "remediated"    # all 7 clear the threshold


@pytest.mark.asyncio
async def test_leak_detail_includes_finding_and_action(client, reconciled):
    # Pick a known MISSING policy from the filtered list.
    listing = (await client.get("/leaks", params={"reason_code": "MISSING_COMMISSION"})).json()
    policy_no = listing["items"][0]["policy_no"]

    resp = await client.get(f"/leaks/{policy_no}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["policy_no"] == policy_no
    assert body["recon"]["status"] == "MISSING_COMMISSION"
    assert body["finding"]["reason_code"] == "MISSING_COMMISSION"
    assert body["finding"]["explanation"]
    # A claim was lodged for this policy and shows up under actions.
    kinds = {a["kind"] for a in body["actions"]}
    assert "claim" in kinds


@pytest.mark.asyncio
async def test_leak_detail_unknown_policy_404(client):
    resp = await client.get("/leaks/NOPE-DOES-NOT-EXIST")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reconcile_requires_auth():
    """Every data endpoint is closed: POST /reconcile with no token is 401."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post("/reconcile")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_metrics_requires_auth():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.get("/metrics")
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_token_then_authenticated_call(client):
    """Step 6/7: a valid token unlocks the protected endpoints; /auth/me echoes
    the principal."""
    resp = await client.get("/auth/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == ADMIN_EMAIL
    assert body["role"] == "admin"


@pytest.mark.asyncio
async def test_bad_credentials_rejected():
    _ensure_admin()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        resp = await c.post(
            "/auth/token", data={"username": ADMIN_EMAIL, "password": "wrong"}
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
async def test_metrics_shape_and_values(client, reconciled):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    m = resp.json()
    assert m["policies_processed"] == 200
    assert m["leaks_by_reason_code"]["MISSING_COMMISSION"] == 7
    assert m["leaks_by_insurer"]                       # grouped by insurer
    assert Decimal(m["underpayment_exposure"]) > 0
    assert Decimal(m["total_claimed"]) > 0
    assert m["claims_count"] >= 7
    d = m["disposition"]
    assert d["auto_remediated"] + d["escalated"] + d["below_threshold"] > 0
    # Disposition must reflect ACTUAL outcomes and agree with the pipeline summary
    # (a rebilled duplicate must count as remediated, not re-gated to escalated).
    assert d["auto_remediated"] == reconciled["auto_remediated"]
    assert d["escalated"] == reconciled["escalated"]
    assert d["below_threshold"] == reconciled["below_threshold"]
    assert m["total_claimed"] == reconciled["total_claimed"]
    # The two exposures agree between the metrics view and the run summary.
    assert m["underpayment_exposure"] == reconciled["underpayment_exposure"]
    assert m["clawback_exposure"] == reconciled["clawback_exposure"]


# --------------------------------------------------------------------------- #
# User management + password-change gate
# --------------------------------------------------------------------------- #
def _email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@leaksentinel.test"


@pytest.mark.asyncio
async def test_register_creates_must_change_user_and_409_on_dup(client):
    email = _email("ops")
    resp = await client.post(
        "/auth/register", json={"email": email, "password": "TempPass1", "role": "ops"}
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == email
    assert body["role"] == "ops"
    assert body["must_change_password"] is True
    assert body["created_by"] == ADMIN_EMAIL
    assert "hashed_password" not in body and "password" not in body
    # Duplicate email -> 409.
    dup = await client.post(
        "/auth/register", json={"email": email, "password": "TempPass1", "role": "ops"}
    )
    assert dup.status_code == 409


@pytest.mark.asyncio
async def test_temp_password_user_is_gated_until_change(client):
    email = _email("gate")
    assert (
        await client.post(
            "/auth/register",
            json={"email": email, "password": "TempPass1", "role": "viewer"},
        )
    ).status_code == 201

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        tok = (
            await c.post("/auth/token", data={"username": email, "password": "TempPass1"})
        ).json()
        assert tok["must_change_password"] is True
        c.headers.update({"Authorization": f"Bearer {tok['access_token']}"})

        # Gated everywhere except login/change-password.
        gated = await c.get("/metrics")
        assert gated.status_code == 403
        assert gated.json()["detail"] == "password_change_required"

        # Wrong current password / too-short new password -> 400.
        assert (
            await c.post(
                "/auth/change-password",
                json={"current_password": "wrong", "new_password": "NewPass12"},
            )
        ).status_code == 400
        assert (
            await c.post(
                "/auth/change-password",
                json={"current_password": "TempPass1", "new_password": "short"},
            )
        ).status_code == 400

        # Success -> the SAME token now passes the gate (it reads the live DB).
        ok = await c.post(
            "/auth/change-password",
            json={"current_password": "TempPass1", "new_password": "NewPass12"},
        )
        assert ok.status_code == 200
        assert (await c.get("/metrics")).status_code == 200


@pytest.mark.asyncio
async def test_viewer_cannot_manage_users(client):
    email = _email("vw")
    await client.post(
        "/auth/register", json={"email": email, "password": "TempPass1", "role": "viewer"}
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        tok = (
            await c.post("/auth/token", data={"username": email, "password": "TempPass1"})
        ).json()["access_token"]
        c.headers.update({"Authorization": f"Bearer {tok}"})
        await c.post(
            "/auth/change-password",
            json={"current_password": "TempPass1", "new_password": "ViewerPass1"},
        )
        assert (await c.get("/auth/users")).status_code == 403
        assert (
            await c.post(
                "/auth/register",
                json={"email": _email("x"), "password": "TempPass1", "role": "ops"},
            )
        ).status_code == 403


@pytest.mark.asyncio
async def test_admin_self_protection(client):
    users = (await client.get("/auth/users")).json()
    admin = next(u for u in users if u["email"] == ADMIN_EMAIL)
    assert (
        await client.patch(f"/auth/users/{admin['id']}", json={"role": "ops"})
    ).status_code == 400
    assert (
        await client.patch(f"/auth/users/{admin['id']}", json={"is_active": False})
    ).status_code == 400
    assert (await client.delete(f"/auth/users/{admin['id']}")).status_code == 400


@pytest.mark.asyncio
async def test_admin_patch_and_soft_delete_other(client):
    email = _email("pat")
    uid = (
        await client.post(
            "/auth/register",
            json={"email": email, "password": "TempPass1", "role": "viewer"},
        )
    ).json()["id"]

    promote = await client.patch(f"/auth/users/{uid}", json={"role": "ops"})
    assert promote.status_code == 200 and promote.json()["role"] == "ops"

    deleted = await client.delete(f"/auth/users/{uid}")
    assert deleted.status_code == 200 and deleted.json()["is_active"] is False

    # A soft-deleted (inactive) user can no longer authenticate.
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        login = await c.post("/auth/token", data={"username": email, "password": "TempPass1"})
        assert login.status_code == 401
