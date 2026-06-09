#!/usr/bin/env python
"""Create or reset the LeakSentinel database schema.

Usage:
    python scripts/init_db.py            # create missing tables
    python scripts/init_db.py --reset    # drop everything, then recreate

Reads DATABASE_URL from the environment / .env (see leaksentinel.config).
"""

from __future__ import annotations

import argparse

from sqlalchemy import inspect

from leaksentinel.config import get_settings
from leaksentinel.db import create_all, engine, reset_database


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize the LeakSentinel schema.")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Drop all tables before recreating them (destructive).",
    )
    args = parser.parse_args()

    url = get_settings().database_url
    # Mask credentials when echoing the target.
    safe_url = url.split("@")[-1] if "@" in url else url
    print(f"Target database: ...@{safe_url}")

    if args.reset:
        print("Resetting (drop + create all)...")
        reset_database()
    else:
        print("Creating tables (if not present)...")
        create_all()

    tables = sorted(inspect(engine).get_table_names())
    print(f"Done. {len(tables)} tables present:")
    for name in tables:
        print(f"  - {name}")


if __name__ == "__main__":
    main()
