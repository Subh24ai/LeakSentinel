#!/usr/bin/env python
"""Create or reset the LeakSentinel schema — DEVELOPMENT ONLY.

Production schema is managed by Alembic; run ``alembic upgrade head`` instead.
This script bypasses migrations via ``create_all`` and is therefore gated behind
an explicit ``--dev`` flag so it can never be run by accident against a real
database.

Usage:
    python scripts/init_db.py --dev            # create missing tables (dev)
    python scripts/init_db.py --dev --reset    # drop everything, then recreate

Reads DATABASE_URL from the environment / .env (see leaksentinel.config).
"""

from __future__ import annotations

import argparse

from sqlalchemy import inspect

from leaksentinel.config import get_settings
from leaksentinel.db import create_all, engine, reset_database


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the LeakSentinel schema (dev only).")
    parser.add_argument(
        "--dev",
        action="store_true",
        help="Confirm this is a throwaway dev database (required).",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop all tables before recreating them (destructive; requires --dev).",
    )
    args = parser.parse_args()

    if not args.dev:
        print("Refusing to create tables directly without --dev.")
        print("Production schema is managed by Alembic. Run:\n")
        print("    alembic upgrade head\n")
        print("For a throwaway DEV database, re-run with --dev (optionally --reset).")
        raise SystemExit(1)

    url = get_settings().database_url
    # Mask credentials when echoing the target.
    safe_url = url.split("@")[-1] if "@" in url else url
    print(f"[DEV] Target database: ...@{safe_url}")

    if args.reset:
        print("[DEV] Resetting (drop + create all)...")
        reset_database()
    else:
        print("[DEV] Creating tables (if not present)...")
        create_all()

    tables = sorted(inspect(engine).get_table_names())
    print(f"Done. {len(tables)} tables present:")
    for name in tables:
        print(f"  - {name}")


if __name__ == "__main__":
    main()
