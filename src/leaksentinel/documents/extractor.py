"""Document intelligence: extract structured fields from insurance/RC PDFs.

Pipeline:
    PDF -> rasterize page to PNG -> vision LLM -> structured JSON (+ per-field
    confidence) -> ExtractionResult -> validate() against the policies table.

The LLM is reached through :func:`leaksentinel.llm.get_vision_chat_model`, which
respects ``LLM_PROVIDER`` and falls back to Anthropic Claude (with a logged
message) when the configured provider can't do vision.

Low-confidence extractions are routed to ``HUMAN_REVIEW``. ``validate()`` joins
the extracted ``policy_no`` to ``policies``/``sales`` and flags any differing
field with a ``FIELD_MISMATCH`` reason code, naming the specific field.
"""

from __future__ import annotations

import base64
import json
import logging
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import select

from leaksentinel.config import Settings, get_settings
from leaksentinel.llm import get_vision_chat_model
from leaksentinel.reconciliation.models import Policy, Sale

logger = logging.getLogger(__name__)

# Fields we extract and confidence-score.
EXTRACTION_FIELDS = ("policy_no", "insurer_name", "premium", "vehicle_reg", "customer_name")

CONFIDENCE_THRESHOLD = 0.70          # below this on any field => HUMAN_REVIEW
PREMIUM_TOLERANCE = Decimal("0.50")  # currency rounding slack for premium compare

FIELD_MISMATCH = "FIELD_MISMATCH"

_PROMPT = (
    "You are reading a two-wheeler insurance certificate / vehicle RC document. "
    "Extract these fields exactly as printed: policy_no, insurer_name, premium "
    "(numeric, no currency symbol or commas), vehicle_reg, customer_name. "
    "Return ONLY a JSON object with those five keys plus a 'confidence' object "
    "mapping each field name to your confidence from 0.0 to 1.0. If a field is "
    "absent or unreadable, set it to null with confidence 0.0. No prose, JSON only."
)


# --------------------------------------------------------------------------- #
# Result models
# --------------------------------------------------------------------------- #
class ExtractionResult(BaseModel):
    policy_no: str | None = None
    insurer_name: str | None = None
    premium: Decimal | None = None
    vehicle_reg: str | None = None
    customer_name: str | None = None
    field_confidence: dict[str, float] = Field(default_factory=dict)
    low_confidence_fields: list[str] = Field(default_factory=list)
    status: str = "OK"           # OK | HUMAN_REVIEW
    provider: str | None = None  # which LLM provider actually ran
    source_doc: str | None = None


class FieldMismatch(BaseModel):
    field: str
    reason_code: str = FIELD_MISMATCH
    extracted: str | None = None
    recorded: str | None = None


class ValidationResult(BaseModel):
    policy_no: str | None = None
    matched_policy: bool = False
    status: str = "VALIDATED"     # VALIDATED | FIELD_MISMATCH | NO_POLICY
    mismatches: list[FieldMismatch] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Rasterization + LLM call
# --------------------------------------------------------------------------- #
def rasterize_pdf(pdf_path: str | Path, page_index: int = 0, dpi: int = 150) -> bytes:
    """Render one PDF page to PNG bytes (via PyMuPDF — no system poppler needed)."""
    import fitz  # PyMuPDF

    with fitz.open(str(pdf_path)) as doc:
        page = doc[page_index]
        pix = page.get_pixmap(dpi=dpi)
        return pix.tobytes("png")


def _coerce_json(content) -> dict:
    """Pull a JSON object out of the model's response content."""
    if isinstance(content, list):  # some providers return content blocks
        content = "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    text = str(content).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in model response: {text[:200]!r}")
    return json.loads(text[start : end + 1])


def call_vision_llm(image_png: bytes, settings: Settings | None = None) -> dict:
    """Send the page image to the vision LLM and return the parsed JSON dict.

    This is the single seam tests mock so they run offline & deterministically.
    """
    settings = settings or get_settings()
    model, provider = get_vision_chat_model(settings, temperature=0)
    logger.info("Document extraction via provider=%s", provider)

    from langchain_core.messages import HumanMessage

    b64 = base64.b64encode(image_png).decode("ascii")
    message = HumanMessage(
        content=[
            {"type": "text", "text": _PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
        ]
    )
    response = model.invoke([message])
    data = _coerce_json(response.content)
    data.setdefault("_provider", provider)
    return data


# --------------------------------------------------------------------------- #
# Extraction
# --------------------------------------------------------------------------- #
def _to_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None


def _build_result(
    raw: dict, source_doc: str, threshold: float
) -> ExtractionResult:
    conf_in = raw.get("confidence") or {}
    confidence = {f: float(conf_in.get(f, 0.0) or 0.0) for f in EXTRACTION_FIELDS}
    low = [f for f in EXTRACTION_FIELDS if confidence[f] < threshold]

    return ExtractionResult(
        policy_no=raw.get("policy_no"),
        insurer_name=raw.get("insurer_name"),
        premium=_to_decimal(raw.get("premium")),
        vehicle_reg=raw.get("vehicle_reg"),
        customer_name=raw.get("customer_name"),
        field_confidence=confidence,
        low_confidence_fields=low,
        status="HUMAN_REVIEW" if low else "OK",
        provider=raw.get("_provider"),
        source_doc=source_doc,
    )


def extract_document(
    pdf_path: str | Path,
    settings: Settings | None = None,
    threshold: float = CONFIDENCE_THRESHOLD,
) -> ExtractionResult:
    """Rasterize the PDF, run vision extraction, and return a scored result."""
    image = rasterize_pdf(pdf_path)
    raw = call_vision_llm(image, settings)
    return _build_result(raw, source_doc=str(pdf_path), threshold=threshold)


# --------------------------------------------------------------------------- #
# Validation against the policies table
# --------------------------------------------------------------------------- #
def _norm(value: str | None) -> str:
    return "".join(str(value).split()).lower() if value is not None else ""


def validate(extraction: ExtractionResult, session) -> ValidationResult:
    """Compare extracted fields to the recorded policy; flag FIELD_MISMATCHes."""
    if not extraction.policy_no:
        return ValidationResult(policy_no=None, matched_policy=False, status="NO_POLICY")

    row = session.execute(
        select(
            Policy.policy_no,
            Policy.insurer_name,
            Policy.premium,
            Sale.vehicle_reg,
            Sale.customer_name,
        )
        .join(Sale, Policy.sale_id == Sale.id)
        .where(Policy.policy_no == extraction.policy_no)
    ).first()

    if row is None:
        return ValidationResult(
            policy_no=extraction.policy_no, matched_policy=False, status="NO_POLICY"
        )

    recorded = {
        "insurer_name": row.insurer_name,
        "premium": row.premium,
        "vehicle_reg": row.vehicle_reg,
        "customer_name": row.customer_name,
    }
    mismatches: list[FieldMismatch] = []

    # Numeric premium compare with tolerance.
    if extraction.premium is not None and recorded["premium"] is not None:
        if abs(extraction.premium - recorded["premium"]) > PREMIUM_TOLERANCE:
            mismatches.append(
                FieldMismatch(
                    field="premium",
                    extracted=f"{extraction.premium}",
                    recorded=f"{recorded['premium']}",
                )
            )

    # Whitespace/case-insensitive compare for the string fields.
    for field in ("insurer_name", "vehicle_reg", "customer_name"):
        extracted_val = getattr(extraction, field)
        recorded_val = recorded[field]
        if extracted_val and recorded_val and _norm(extracted_val) != _norm(recorded_val):
            mismatches.append(
                FieldMismatch(field=field, extracted=str(extracted_val),
                              recorded=str(recorded_val))
            )

    return ValidationResult(
        policy_no=extraction.policy_no,
        matched_policy=True,
        status=FIELD_MISMATCH if mismatches else "VALIDATED",
        mismatches=mismatches,
    )
