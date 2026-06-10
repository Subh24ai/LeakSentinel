"""Document extractor tests — fully offline (the LLM call is mocked).

Rasterization runs for real against a generated PDF, but ``call_vision_llm`` is
monkeypatched to return fixed JSON, so these are deterministic and need no API
key. Asserts that validation flags the planted premium mismatch and that a
low-confidence field routes the extraction to HUMAN_REVIEW.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from leaksentinel.db import SessionLocal
from leaksentinel.db import engine as db_engine
from leaksentinel.documents import extractor
from leaksentinel.documents.extractor import (
    FIELD_MISMATCH,
    extract_document,
    validate,
)
from leaksentinel.reconciliation.models import Policy

DOCS_DIR = Path(__file__).resolve().parent.parent / "data" / "synthetic" / "docs"
MANIFEST = DOCS_DIR / "docs_manifest.json"


@pytest.fixture(scope="module")
def manifest() -> list[dict]:
    if not MANIFEST.exists():
        pytest.skip("docs not generated; run scripts/generate_documents.py")
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def db_ready():
    try:
        db_engine.connect().close()
    except OperationalError:
        pytest.skip("Postgres not reachable")
    session = SessionLocal()
    try:
        if session.query(Policy).count() == 0:
            pytest.skip("No policies; run scripts/generate_synthetic_data.py first")
    finally:
        session.close()


def _tampered_doc(manifest: list[dict]) -> dict:
    doc = next((d for d in manifest if d["tampered_field"] == "premium"), None)
    assert doc, "expected one tampered document in the manifest"
    return doc


def test_validation_flags_planted_premium_mismatch(manifest, db_ready, monkeypatch):
    """A correct extraction of the TAMPERED doc must fail validation on premium."""
    doc = _tampered_doc(manifest)

    # The vision model "reads" exactly what's printed (the wrong premium), with
    # high confidence on every field.
    fixed = {
        "policy_no": doc["policy_no"],
        "insurer_name": doc["insurer_name"],
        "premium": doc["printed_premium"],          # wrong vs DB true_premium
        "vehicle_reg": doc["vehicle_reg"],
        "customer_name": doc["customer_name"],
        "confidence": {
            "policy_no": 0.99, "insurer_name": 0.98, "premium": 0.97,
            "vehicle_reg": 0.96, "customer_name": 0.95,
        },
    }
    monkeypatch.setattr(extractor, "call_vision_llm", lambda *a, **k: dict(fixed))

    result = extract_document(DOCS_DIR / doc["file"])
    assert result.status == "OK"  # all confidences high

    session = SessionLocal()
    try:
        validation = validate(result, session)
    finally:
        session.close()

    assert validation.matched_policy is True
    assert validation.status == FIELD_MISMATCH
    fields = {m.field for m in validation.mismatches}
    assert fields == {"premium"}, f"expected only premium mismatch, got {fields}"

    mismatch = validation.mismatches[0]
    assert mismatch.reason_code == FIELD_MISMATCH
    assert float(mismatch.extracted) == doc["printed_premium"]
    assert float(mismatch.recorded) == doc["true_premium"]


def test_low_confidence_routes_to_human_review(manifest, db_ready, monkeypatch):
    """A field below the confidence threshold must mark the doc HUMAN_REVIEW."""
    doc = manifest[0]
    fixed = {
        "policy_no": doc["policy_no"],
        "insurer_name": doc["insurer_name"],
        "premium": doc["printed_premium"],
        "vehicle_reg": doc["vehicle_reg"],
        "customer_name": doc["customer_name"],
        "confidence": {
            "policy_no": 0.99, "insurer_name": 0.98, "premium": 0.95,
            "vehicle_reg": 0.93, "customer_name": 0.40,   # below 0.70 threshold
        },
    }
    monkeypatch.setattr(extractor, "call_vision_llm", lambda *a, **k: dict(fixed))

    result = extract_document(DOCS_DIR / doc["file"])
    assert result.status == "HUMAN_REVIEW"
    assert "customer_name" in result.low_confidence_fields


def test_clean_doc_validates(manifest, db_ready, monkeypatch):
    """A faithful extraction of an UNtampered doc must validate cleanly."""
    doc = next(d for d in manifest if d["tampered_field"] is None)
    fixed = {
        "policy_no": doc["policy_no"],
        "insurer_name": doc["insurer_name"],
        "premium": doc["true_premium"],
        "vehicle_reg": doc["vehicle_reg"],
        "customer_name": doc["customer_name"],
        "confidence": {f: 0.99 for f in
                       ("policy_no", "insurer_name", "premium",
                        "vehicle_reg", "customer_name")},
    }
    monkeypatch.setattr(extractor, "call_vision_llm", lambda *a, **k: dict(fixed))

    result = extract_document(DOCS_DIR / doc["file"])
    session = SessionLocal()
    try:
        validation = validate(result, session)
    finally:
        session.close()

    assert result.status == "OK"
    assert validation.status == "VALIDATED"
    assert validation.mismatches == []
