"""Integration test: policies.policy_no must be UNIQUE.

Runs against the configured Postgres (DATABASE_URL). Skipped automatically if
the database is not reachable, so it never breaks DB-less environments.
"""

import uuid

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from leaksentinel.db import SessionLocal, create_all, engine
from leaksentinel.reconciliation.models import Policy


@pytest.fixture(scope="module")
def session():
    try:
        engine.connect().close()
    except OperationalError:
        pytest.skip("Postgres not reachable; skipping DB integration test")
    create_all()
    sess = SessionLocal()
    yield sess
    sess.rollback()
    sess.close()


def test_duplicate_policy_no_raises(session):
    policy_no = f"TEST-{uuid.uuid4().hex[:8]}"
    try:
        session.add(Policy(insurer_name="ACME", policy_no=policy_no))
        session.commit()

        session.add(Policy(insurer_name="ACME", policy_no=policy_no))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    finally:
        # Clean up the row we inserted so reruns stay green.
        session.query(Policy).filter(Policy.policy_no == policy_no).delete()
        session.commit()
