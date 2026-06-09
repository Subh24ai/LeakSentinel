"""SQLAlchemy engine / session plumbing.

Skeleton only — defines the engine, session factory, and declarative base.
Models will be added under their respective modules later.
"""

from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from leaksentinel.config import get_settings


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


_settings = get_settings()
engine = create_engine(_settings.database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a transactional session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _load_models() -> None:
    """Import all model modules so they register on ``Base.metadata``.

    Imported lazily (inside the lifecycle functions) to avoid a circular
    import: model modules import ``Base`` from here.
    """
    from leaksentinel.reconciliation import models  # noqa: F401


def create_all() -> None:
    """Create every table that does not already exist."""
    _load_models()
    Base.metadata.create_all(bind=engine)


def drop_all() -> None:
    """Drop every known table (destructive)."""
    _load_models()
    Base.metadata.drop_all(bind=engine)


def reset_database() -> None:
    """Drop and recreate every table — a clean slate."""
    drop_all()
    create_all()
