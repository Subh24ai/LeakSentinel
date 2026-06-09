#!/usr/bin/env python
"""Run a REAL document extraction against the configured LLM and validate it.

Requires a working LLM key in the environment / .env (ANTHROPIC_API_KEY, or
GROQ_API_KEY with a vision-capable Groq model — otherwise it falls back to
Anthropic). By default it runs the TAMPERED document, so you can see both the
extraction and validation catching the planted premium mismatch.

Usage:
    .venv/bin/python scripts/run_extraction.py [doc_filename]
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from leaksentinel.config import get_settings
from leaksentinel.db import SessionLocal
from leaksentinel.documents.extractor import extract_document, validate

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

DOCS_DIR = Path(__file__).resolve().parent.parent / "data" / "synthetic" / "docs"


def main() -> None:
    settings = get_settings()
    if not settings.anthropic_api_key and not settings.groq_api_key:
        raise SystemExit(
            "No LLM key configured. Set ANTHROPIC_API_KEY (or GROQ_API_KEY) in "
            ".env or the environment, e.g.:\n"
            "  ANTHROPIC_API_KEY=sk-... .venv/bin/python scripts/run_extraction.py"
        )

    manifest = json.loads((DOCS_DIR / "docs_manifest.json").read_text())
    if len(sys.argv) > 1:
        doc = next(d for d in manifest if d["file"] == sys.argv[1])
    else:
        doc = next((d for d in manifest if d["tampered_field"]), manifest[0])

    path = DOCS_DIR / doc["file"]
    print(f"\nExtracting: {path.name}  (policy {doc['policy_no']})")
    print(f"  manifest: printed_premium={doc['printed_premium']} "
          f"true_premium={doc['true_premium']} tampered={doc['tampered_field']}\n")

    result = extract_document(path)
    print("=== Extraction result ===")
    print(json.dumps(result.model_dump(mode="json"), indent=2))

    session = SessionLocal()
    try:
        validation = validate(result, session)
    finally:
        session.close()

    print("\n=== Validation result ===")
    print(json.dumps(validation.model_dump(mode="json"), indent=2))

    print(f"\nDoc status: {result.status}   Validation: {validation.status}")
    for m in validation.mismatches:
        print(f"  {m.reason_code}: {m.field} extracted={m.extracted} recorded={m.recorded}")


if __name__ == "__main__":
    main()
