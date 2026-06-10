"""JWT authentication and role-based authorization.

Tokens are signed with ``settings.jwt_secret`` (HS256 by default). There is no
hardcoded fallback secret: :func:`require_jwt_secret` raises if it is unset, and
the API's startup hook calls it so the service refuses to boot without one.

Usage in routes::

    @app.get("/leaks", dependencies=[Depends(require_role("admin", "ops", "viewer"))])
    async def list_leaks(...): ...

    @app.get("/auth/me")
    async def me(user: dict = Depends(get_current_user)): return user
"""

from __future__ import annotations

import datetime as dt

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from leaksentinel.auth.users import User, get_user_by_email, verify_password
from leaksentinel.config import Settings, get_settings
from leaksentinel.db import get_session

# tokenUrl is the path clients POST credentials to (also what Swagger's Authorize
# dialog uses). Must match the route registered in api.main.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/token", auto_error=False)

_CREDENTIALS_EXC = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def require_jwt_secret(settings: Settings | None = None) -> str:
    """Return the configured JWT secret, or raise if it is unset.

    Fail-closed: a missing secret is a misconfiguration, never a silent default.
    """
    settings = settings or get_settings()
    if not settings.jwt_secret:
        raise RuntimeError(
            "JWT_SECRET is not configured. Set it in the environment / .env; "
            "the API will not start without it."
        )
    return settings.jwt_secret


# --------------------------------------------------------------------------- #
# Token issuance / decoding
# --------------------------------------------------------------------------- #
def create_access_token(
    user_id: int | str, role: str, settings: Settings | None = None
) -> str:
    """Mint a signed JWT carrying the subject (user id) and role, with expiry."""
    settings = settings or get_settings()
    secret = require_jwt_secret(settings)
    now = dt.datetime.now(dt.UTC)
    payload = {
        "sub": str(user_id),
        "role": role,
        "iat": int(now.timestamp()),
        "exp": now + dt.timedelta(hours=settings.jwt_expiry_hours),
    }
    return jwt.encode(payload, secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str, settings: Settings | None = None) -> dict:
    """Decode and verify a JWT. Raises 401 on any problem (bad sig / expired)."""
    settings = settings or get_settings()
    secret = require_jwt_secret(settings)
    try:
        return jwt.decode(token, secret, algorithms=[settings.jwt_algorithm])
    except JWTError as exc:
        raise _CREDENTIALS_EXC from exc


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def authenticate_user(session: Session, email: str, password: str) -> User | None:
    """Return the active user if email+password match, else ``None``."""
    user = get_user_by_email(session, email)
    if user is None or not user.is_active:
        return None
    if not verify_password(password, user.hashed_password):
        return None
    return user


def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    session: Session = Depends(get_session),
) -> dict:
    """Resolve the bearer token to the authenticated, active user.

    Raises 401 if the token is missing, invalid, expired, or the user no longer
    exists / is deactivated.
    """
    if not token:
        raise _CREDENTIALS_EXC
    payload = decode_token(token)
    sub = payload.get("sub")
    if sub is None:
        raise _CREDENTIALS_EXC
    try:
        user_id = int(sub)
    except (TypeError, ValueError) as exc:
        raise _CREDENTIALS_EXC from exc
    user = session.get(User, user_id)
    if user is None or not user.is_active:
        raise _CREDENTIALS_EXC
    return {"id": user.id, "email": user.email, "role": user.role}


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #
def require_role(*roles: str):
    """Dependency factory: allow only users whose role is in ``roles``.

    401 if unauthenticated; 403 if authenticated but not permitted.
    """
    allowed = frozenset(roles)

    def _dependency(user: dict = Depends(get_current_user)) -> dict:
        if user["role"] not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=(
                    f"role '{user['role']}' is not permitted here; "
                    f"requires one of {sorted(allowed)}"
                ),
            )
        return user

    return _dependency
