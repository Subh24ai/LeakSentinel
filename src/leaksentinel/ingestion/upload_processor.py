"""Process an uploaded insurer feed file into ``insurer_commission_feeds``.

Flow (see :func:`process_feed_upload`):

* CSV  → run the insurer's :class:`Normalizer` over the rows.
* PDF  → try ``pdfplumber`` table extraction; if it yields a table with the
         insurer's expected columns, normalize it like a CSV. Otherwise fall
         back to the vision-LLM document extractor.

Only rows that normalize to a usable ``policy_no`` are loaded — the gap between
``rows_extracted`` and ``rows_loaded`` reflects rows whose columns didn't match
the selected insurer (e.g. uploading a Bajaj file as "Digit").

This is a standalone ingest path; the reconciliation pipeline's intake still
reads the canonical synthetic CSVs.
"""

from __future__ import annotations

import csv
import datetime as dt
import logging

from leaksentinel.db import SessionLocal
from leaksentinel.ingestion.loader import _to_feed_row
from leaksentinel.ingestion.normalizer import get_normalizer
from leaksentinel.reconciliation.models import InsurerFeedUpload
from leaksentinel.reconciliation.schemas import ReconciliationView

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Extraction helpers
# --------------------------------------------------------------------------- #
def _read_csv_rows(path: str) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def extract_from_pdf(path: str, insurer_name: str) -> list[dict[str, str]]:
    """Extract tabular rows from a PDF with ``pdfplumber``.

    Returns a list of ``{column: value}`` dicts if a table whose header contains
    the insurer's reference column is found, else ``[]`` (signalling the caller
    to fall back to LLM extraction).
    """
    import pdfplumber  # local import: only PDF uploads need it

    normalizer = get_normalizer(insurer_name)
    ref_col = normalizer.COL_REF
    rows: list[dict[str, str]] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            for table in page.extract_tables() or []:
                if not table or len(table) < 2:
                    continue
                header = [(c or "").strip() for c in table[0]]
                if ref_col not in header:
                    continue  # not this insurer's table — let the LLM try
                for raw in table[1:]:
                    rows.append(
                        {h: (v or "").strip() for h, v in zip(header, raw, strict=False)}
                    )
    return rows


def _llm_extract(path: str, insurer_name: str) -> list[ReconciliationView]:
    """Last-resort PDF extraction via the vision-LLM document extractor."""
    from leaksentinel.documents.extractor import extract_document

    result = extract_document(path)
    if not result.policy_no:
        return []
    return [
        ReconciliationView(
            policy_no=result.policy_no,
            insurer_name=insurer_name,
            premium=result.premium,
            source_ref=str(path),
            source_system="pdf_llm",
        )
    ]


def _normalize(path: str, insurer_name: str, file_type: str) -> list[ReconciliationView]:
    """Return normalized views for the file (CSV or PDF)."""
    if file_type == "csv":
        return get_normalizer(insurer_name).normalize_feed(_read_csv_rows(path))
    if file_type == "pdf":
        table_rows = extract_from_pdf(path, insurer_name)
        if table_rows:
            return get_normalizer(insurer_name).normalize_feed(table_rows)
        return _llm_extract(path, insurer_name)
    raise ValueError(f"unsupported file type {file_type!r}")


# --------------------------------------------------------------------------- #
# Processor
# --------------------------------------------------------------------------- #
def process_feed_upload(upload_id: int) -> InsurerFeedUpload:
    """Normalize and load an uploaded feed; record status + row counts.

    Always commits the final status (processed or failed); never raises.
    """
    session = SessionLocal()
    try:
        upload = session.get(InsurerFeedUpload, upload_id)
        if upload is None:
            raise ValueError(f"upload {upload_id} not found")

        upload.status = "processing"
        session.commit()

        try:
            views = _normalize(upload.storage_path, upload.insurer_name, upload.file_type)
            rows_extracted = len(views)
            # Only persist rows that normalized to a usable policy_no.
            loaded = 0
            for view in views:
                if view.policy_no:
                    session.add(_to_feed_row(view))
                    loaded += 1
            session.flush()
            upload.rows_extracted = rows_extracted
            upload.rows_loaded = loaded
            upload.status = "processed"
            upload.processed_at = dt.datetime.now(dt.UTC)
            session.commit()
            logger.info(
                "Upload %d processed: %d extracted, %d loaded.",
                upload_id,
                rows_extracted,
                loaded,
            )
        except Exception as exc:  # noqa: BLE001 - record any failure on the row
            session.rollback()
            upload = session.get(InsurerFeedUpload, upload_id)
            if upload is not None:
                upload.status = "failed"
                upload.error_message = str(exc)[:2000]
                upload.processed_at = dt.datetime.now(dt.UTC)
                session.commit()
            logger.exception("Upload %d failed.", upload_id)

        return upload
    finally:
        session.close()
