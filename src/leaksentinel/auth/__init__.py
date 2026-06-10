"""Authentication & authorization for the LeakSentinel API.

JWT bearer tokens (:mod:`leaksentinel.auth.core`) over a ``users`` table
(:mod:`leaksentinel.auth.users`), with three roles: ``admin``, ``ops``,
``viewer``. The first admin is bootstrapped from settings on startup.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select

from leaksentinel.auth.core import (
    authenticate_user,
    create_access_token,
    decode_token,
    get_current_user,
    require_jwt_secret,
    require_role,
)
from leaksentinel.auth.users import (
    VALID_ROLES,
    User,
    create_user,
    get_user_by_email,
    hash_password,
    verify_password,
)
from leaksentinel.config import Settings, get_settings
from leaksentinel.db import SessionLocal

logger = logging.getLogger(__name__)

__all__ = [
    "User",
    "VALID_ROLES",
    "authenticate_user",
    "bootstrap_admin",
    "create_access_token",
    "create_user",
    "decode_token",
    "get_current_user",
    "get_user_by_email",
    "hash_password",
    "require_jwt_secret",
    "require_role",
    "verify_password",
]


def bootstrap_admin(settings: Settings | None = None) -> User | None:
    """Create the first admin user if the table is empty and a password is set.

    Idempotent: does nothing if any user already exists. Returns the created
    user, or ``None`` if bootstrap was skipped.
    """
    settings = settings or get_settings()
    if not settings.first_admin_password:
        logger.info("No FIRST_ADMIN_PASSWORD configured; skipping admin bootstrap.")
        return None

    session = SessionLocal()
    try:
        if session.execute(select(func.count(User.id))).scalar_one():
            return None  # users already exist — never silently reset
        user = create_user(
            session,
            settings.first_admin_email,
            settings.first_admin_password,
            "admin",
        )
        session.commit()
        logger.info("Bootstrapped first admin user %s", settings.first_admin_email)
        return user
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
