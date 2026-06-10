"""User persistence and password hashing for LeakSentinel auth.

Passwords are hashed with **bcrypt directly** (not via passlib): the installed
``passlib`` 1.7.x cannot read bcrypt >= 4.1's version metadata and raises on
every hash, so we use the ``bcrypt`` package's own API, which is stable and
handles its 72-byte input limit explicitly.
"""

from __future__ import annotations

import datetime as dt

import bcrypt
from sqlalchemy import Boolean, DateTime, String, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from leaksentinel.db import Base

# The three roles, most- to least-privileged. ``require_role`` takes an explicit
# allowed set per endpoint, so there is no implicit hierarchy to get wrong.
VALID_ROLES: tuple[str, ...] = ("admin", "ops", "viewer")

# bcrypt hashes at most the first 72 bytes of input; we truncate explicitly so a
# long passphrase fails closed (consistently) rather than raising.
_BCRYPT_MAX_BYTES = 72


class User(Base):
    """An authenticated operator. ``role`` is one of :data:`VALID_ROLES`."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="viewer")
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# --------------------------------------------------------------------------- #
# Password hashing
# --------------------------------------------------------------------------- #
def hash_password(plain: str) -> str:
    """Return a bcrypt hash of ``plain`` (first 72 bytes), as a str."""
    pw = plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]
    return bcrypt.hashpw(pw, bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time check of ``plain`` against a stored bcrypt ``hashed``."""
    try:
        return bcrypt.checkpw(
            plain.encode("utf-8")[:_BCRYPT_MAX_BYTES], hashed.encode("utf-8")
        )
    except (ValueError, TypeError):
        return False


# --------------------------------------------------------------------------- #
# Queries / mutations
# --------------------------------------------------------------------------- #
def get_user_by_email(session: Session, email: str) -> User | None:
    """Return the user with this email (case-insensitive), or ``None``."""
    return session.execute(
        select(User).where(func.lower(User.email) == email.strip().lower())
    ).scalar_one_or_none()


def create_user(session: Session, email: str, password: str, role: str) -> User:
    """Create and flush a new user. Raises ``ValueError`` on an unknown role."""
    if role not in VALID_ROLES:
        raise ValueError(f"unknown role {role!r}; must be one of {VALID_ROLES}")
    user = User(
        email=email.strip().lower(),
        hashed_password=hash_password(password),
        role=role,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user
