"""Ingestion loader: raw insurer CSVs -> normalized ``insurer_commission_feeds``.

For each registered insurer normalizer, this reads its CSV from
``data/synthetic/``, normalizes every row into a ``ReconciliationView`` and
persists it. Rows that fail to parse cleanly are **still persisted** with their
``normalization_notes`` attached — nothing is silently dropped.

Run via ``make ingest`` (``python -m leaksentinel.ingestion.loader``).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from leaksentinel.db import SessionLocal
from leaksentinel.ingestion.normalizer import get_normalizer, registered_insurers
from leaksentinel.reconciliation.models import InsurerCommissionFeed
from leaksentinel.reconciliation.schemas import ReconciliationView

# repo_root/data/synthetic (loader.py is src/leaksentinel/ingestion/loader.py)
DEFAULT_DATA_DIR = Path(__file__).resolve().parents[3] / "data" / "synthetic"


def _to_feed_row(view: ReconciliationView) -> InsurerCommissionFeed:
    """Map a canonical view onto the ORM row (flags preserved as JSON)."""
    return InsurerCommissionFeed(
        insurer_name=view.insurer_name,
        raw_policy_ref=view.source_ref,
        policy_no=view.policy_no,
        premium=view.premium,
        commission_amount=view.actual_commission,
        paid_date=view.paid_date,
        status=view.raw_status,
        payment_status=view.payment_status.value if view.payment_status else None,
        normalization_notes=(
            json.dumps(view.normalization_notes) if view.normalization_notes else None
        ),
    )


def load_feeds(session, data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, dict[str, int]]:
    """Truncate + reload every insurer feed. Returns per-insurer counts.

    The session is mutated and flushed but NOT committed — the caller commits.
    """
    # Idempotent: clear existing feeds so re-running doesn't duplicate.
    session.query(InsurerCommissionFeed).delete()

    summary: dict[str, dict[str, int]] = {}
    for insurer in registered_insurers():
        normalizer = get_normalizer(insurer)
        path = data_dir / normalizer.source_file
        if not path.exists():
            summary[insurer] = {"loaded": 0, "flagged": 0, "missing_file": 1}
            continue

        with path.open(newline="", encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh))

        views = normalizer.normalize_feed(rows)
        flagged = 0
        for view in views:
            if view.normalization_notes:
                flagged += 1
            session.add(_to_feed_row(view))

        summary[insurer] = {"loaded": len(views), "flagged": flagged}

    session.flush()
    return summary


def run(data_dir: Path = DEFAULT_DATA_DIR) -> dict[str, dict[str, int]]:
    """Open a session, load all feeds, commit, return the summary."""
    session = SessionLocal()
    try:
        summary = load_feeds(session, data_dir)
        session.commit()
        return summary
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _print_summary(summary: dict[str, dict[str, int]]) -> None:
    print("\n=== Ingestion summary (insurer_commission_feeds) ===")
    print(f"{'Insurer':<18}{'Loaded':>8}{'Flagged':>9}")
    print("-" * 35)
    total_loaded = total_flagged = 0
    for insurer, counts in summary.items():
        if counts.get("missing_file"):
            print(f"{insurer:<18}{'-':>8}{'(CSV not found)':>20}")
            continue
        loaded, flagged = counts["loaded"], counts["flagged"]
        total_loaded += loaded
        total_flagged += flagged
        print(f"{insurer:<18}{loaded:>8}{flagged:>9}")
    print("-" * 35)
    print(f"{'TOTAL':<18}{total_loaded:>8}{total_flagged:>9}")
    if total_flagged:
        print(f"\n{total_flagged} row(s) carried data-quality flags "
              "(persisted with normalization_notes).")
    else:
        print("\nNo data-quality flags — all rows normalized cleanly.")


def main() -> None:
    summary = run()
    _print_summary(summary)


if __name__ == "__main__":
    main()
