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

from sqlalchemy import select

from leaksentinel.db import SessionLocal
from leaksentinel.ingestion.normalizer import get_normalizer, registered_insurers
from leaksentinel.reconciliation.models import InsurerCommissionFeed, InsurerFeedUpload
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


def _load_synthetic(session, data_dir: Path) -> dict[str, dict[str, int]]:
    """Truncate insurer_commission_feeds and reload from the synthetic CSVs."""
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
        flagged = sum(1 for v in views if v.normalization_notes)
        for view in views:
            session.add(_to_feed_row(view))

        summary[insurer] = {"loaded": len(views), "flagged": flagged}

    return summary


def _uploaded_views(session) -> list[ReconciliationView]:
    """Normalized rows from the LATEST processed upload per insurer (a later
    upload for an insurer supersedes earlier ones)."""
    uploads = (
        session.execute(
            select(InsurerFeedUpload)
            .where(InsurerFeedUpload.status == "processed")
            .order_by(InsurerFeedUpload.uploaded_at, InsurerFeedUpload.id)
        )
        .scalars()
        .all()
    )
    latest: dict[str, InsurerFeedUpload] = {}
    for up in uploads:
        latest[up.insurer_name] = up  # last one wins

    views: list[ReconciliationView] = []
    for up in latest.values():
        path = Path(up.storage_path)
        if not path.exists():
            continue
        try:
            normalizer = get_normalizer(up.insurer_name)
        except KeyError:
            continue
        if up.file_type == "csv":
            with path.open(newline="", encoding="utf-8") as fh:
                rows = list(csv.DictReader(fh))
            views.extend(normalizer.normalize_feed(rows))
        elif up.file_type == "pdf":
            # Lazy import: upload_processor imports this module, so importing it at
            # module load would form a cycle.
            from leaksentinel.ingestion.upload_processor import _normalize

            views.extend(_normalize(str(path), up.insurer_name, "pdf"))
    return views


def _overlay_uploaded(session) -> dict[str, dict[str, int]]:
    """Upsert uploaded rows onto insurer_commission_feeds — uploaded wins.

    Deduplicates on (policy_no, insurer_name): every existing row for a key that
    appears in the uploads is removed and replaced by the uploaded rows for that
    key (so an uploaded feed, including its duplicate-payment rows, supersedes the
    synthetic rows for those policies, while policies absent from the uploads keep
    their existing rows). Does NOT truncate.
    """
    views = [v for v in _uploaded_views(session) if v.policy_no]
    keys = {(v.policy_no, v.insurer_name) for v in views}
    for policy_no, insurer in keys:
        session.query(InsurerCommissionFeed).filter_by(
            policy_no=policy_no, insurer_name=insurer
        ).delete()
    for view in views:
        session.add(_to_feed_row(view))
    return {"_uploaded": {"loaded": len(views), "insurers": len({k[1] for k in keys})}}


def load_feeds(
    session, data_dir: Path = DEFAULT_DATA_DIR, source: str = "synthetic"
) -> dict[str, dict[str, int]]:
    """Load insurer feeds from ``source`` (mutated + flushed, NOT committed):

    * ``"synthetic"`` — truncate + reload from the synthetic CSVs (default; used
      by ``make ingest`` and the precision/recall tests).
    * ``"uploaded"``  — overlay only processed uploads; existing rows are NOT
      truncated.
    * ``"all"``       — synthetic first, then overlay processed uploads (uploads
      win for the same policy + insurer).
    """
    if source not in ("synthetic", "uploaded", "all"):
        raise ValueError(f"unknown feed source {source!r}")

    summary: dict[str, dict[str, int]] = {}
    if source in ("synthetic", "all"):
        summary.update(_load_synthetic(session, data_dir))
        session.flush()  # make synthetic rows visible to the overlay's delete-by-key
    if source in ("uploaded", "all"):
        summary.update(_overlay_uploaded(session))

    session.flush()
    return summary


def run(
    source: str = "synthetic", data_dir: Path = DEFAULT_DATA_DIR
) -> dict[str, dict[str, int]]:
    """Open a session, load feeds from ``source``, commit, return the summary."""
    session = SessionLocal()
    try:
        summary = load_feeds(session, data_dir, source=source)
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
