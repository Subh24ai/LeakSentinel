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
    # True until the user sets their own password (every new/bootstrapped account
    # starts with a temporary password they must change on first login).
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true", default=True
    )
    # Email of the admin who created this account (None for the bootstrap admin).
    created_by: Mapped[str | None] = mapped_column(String(255))
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
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


def create_user(
    session: Session,
    email: str,
    password: str,
    role: str,
    *,
    created_by: str | None = None,
    must_change_password: bool = True,
) -> User:
    """Create and flush a new user. Raises ``ValueError`` on an unknown role.

    New accounts start with ``must_change_password=True`` so the temporary
    password set by an admin (or the bootstrap) must be changed on first login.
    """
    if role not in VALID_ROLES:
        raise ValueError(f"unknown role {role!r}; must be one of {VALID_ROLES}")
    user = User(
        email=email.strip().lower(),
        hashed_password=hash_password(password),
        role=role,
        is_active=True,
        must_change_password=must_change_password,
        created_by=created_by,
    )
    session.add(user)
    session.flush()
    return user


def list_users(session: Session) -> list[User]:
    """All users, oldest first (for the admin user-management table)."""
    return list(session.execute(select(User).order_by(User.id)).scalars().all())


def update_user_fields(
    session: Session,
    user_id: int,
    *,
    role: str | None = None,
    is_active: bool | None = None,
) -> User | None:
    """Patch a user's role and/or active flag. Returns the user, or ``None`` if
    it doesn't exist. Raises ``ValueError`` on an unknown role."""
    user = session.get(User, user_id)
    if user is None:
        return None
    if role is not None:
        if role not in VALID_ROLES:
            raise ValueError(f"unknown role {role!r}; must be one of {VALID_ROLES}")
        user.role = role
    if is_active is not None:
        user.is_active = is_active
    session.flush()
    return user


def soft_delete_user(session: Session, user_id: int) -> User | None:
    """Deactivate a user (preserves the row, and any audit/created_by trail)."""
    user = session.get(User, user_id)
    if user is None:
        return None
    user.is_active = False
    session.flush()
    return user


def set_password(session: Session, user: User, new_password: str) -> None:
    """Replace a user's password hash and clear the must-change flag."""
    user.hashed_password = hash_password(new_password)
    user.must_change_password = False
    session.flush()


def touch_last_login(session: Session, user: User) -> None:
    """Stamp the user's last successful login time."""
    user.last_login_at = dt.datetime.now(dt.UTC)
    session.flush()
