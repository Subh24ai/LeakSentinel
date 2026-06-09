#!/usr/bin/env python
"""Generate synthetic two-wheeler insurance certificate PDFs.

Pulls a handful of real policies from the DB (so policy_no / customer / vehicle
match the ``policies``/``sales`` tables) and renders each as a visually-plausible
"Certificate cum Policy Schedule" PDF into ``data/synthetic/docs/``.

ONE document is deliberately printed with a WRONG premium so the extractor's
validation step has a real mismatch to catch. A manifest records the true vs.
printed values and which field was tampered.

Run: .venv/bin/python scripts/generate_documents.py
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas
from sqlalchemy import select

from leaksentinel.db import SessionLocal
from leaksentinel.reconciliation.models import Policy, Sale

DOCS_DIR = Path(__file__).resolve().parent.parent / "data" / "synthetic" / "docs"

# Per-insurer header colour, to make documents look distinct.
INSURER_COLORS = {
    "ICICI Lombard": colors.HexColor("#B22222"),
    "Bajaj": colors.HexColor("#1F4E79"),
    "Digit": colors.HexColor("#0B7A5B"),
    "Tata AIG": colors.HexColor("#3B2F8F"),
}

PAGE_W, PAGE_H = A4


def _one_year_later(iso_date: str) -> str:
    """Return the ISO date one year on (policy end date)."""
    y, m, d = iso_date.split("-")
    return f"{int(y) + 1}-{m}-{d}"


def _draw_certificate(path: Path, data: dict, printed_premium: Decimal) -> None:
    c = canvas.Canvas(str(path), pagesize=A4)
    accent = INSURER_COLORS.get(data["insurer_name"], colors.HexColor("#333333"))

    # --- Header band ------------------------------------------------------- #
    c.setFillColor(accent)
    c.rect(0, PAGE_H - 32 * mm, PAGE_W, 32 * mm, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(18 * mm, PAGE_H - 17 * mm, data["insurer_name"])
    c.setFont("Helvetica", 11)
    c.drawString(18 * mm, PAGE_H - 25 * mm,
                 "Two-Wheeler Package Policy — Certificate cum Schedule")

    # --- Policy number strip ---------------------------------------------- #
    c.setFillColor(colors.HexColor("#F2F2F2"))
    c.rect(18 * mm, PAGE_H - 46 * mm, PAGE_W - 36 * mm, 10 * mm, fill=1, stroke=0)
    c.setFillColor(colors.black)
    c.setFont("Helvetica-Bold", 12)
    c.drawString(22 * mm, PAGE_H - 43 * mm, f"Policy No: {data['policy_no']}")
    c.setFont("Helvetica", 10)
    c.drawRightString(PAGE_W - 22 * mm, PAGE_H - 43 * mm,
                      f"Issued: {data['issued_date']}")

    # --- Detail table ------------------------------------------------------ #
    rows = [
        ("Insured Name", data["customer_name"]),
        ("Vehicle Registration No.", data["vehicle_reg"]),
        ("Make / Type", "Two-Wheeler"),
        ("Insurer", data["insurer_name"]),
        ("Total Premium (INR)", f"{printed_premium:,.2f}"),
        ("Policy Period",
         f"{data['issued_date']} to {_one_year_later(data['issued_date'])}"),
    ]
    top = PAGE_H - 60 * mm
    row_h = 11 * mm
    label_x, value_x = 22 * mm, 85 * mm
    table_w = PAGE_W - 36 * mm

    c.setStrokeColor(colors.HexColor("#CCCCCC"))
    for i, (label, value) in enumerate(rows):
        y = top - i * row_h
        if i % 2 == 0:
            c.setFillColor(colors.HexColor("#FAFAFA"))
            c.rect(18 * mm, y - row_h + 3 * mm, table_w, row_h, fill=1, stroke=0)
        c.setFillColor(colors.HexColor("#555555"))
        c.setFont("Helvetica", 10)
        c.drawString(label_x, y - 4 * mm, label)
        c.setFillColor(colors.black)
        c.setFont("Helvetica-Bold", 11)
        c.drawString(value_x, y - 4 * mm, str(value))

    # --- Footer ------------------------------------------------------------ #
    c.setStrokeColor(accent)
    c.setLineWidth(1)
    c.line(18 * mm, 40 * mm, PAGE_W - 18 * mm, 40 * mm)
    c.setFillColor(colors.HexColor("#777777"))
    c.setFont("Helvetica-Oblique", 8)
    c.drawString(18 * mm, 33 * mm,
                 "This is a computer-generated certificate. Subject to terms, "
                 "conditions and exclusions of the policy.")
    c.drawString(18 * mm, 28 * mm,
                 "SYNTHETIC DOCUMENT — generated for testing. Not a real policy.")
    c.showPage()
    c.save()


def main() -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    session = SessionLocal()
    try:
        rows = session.execute(
            select(
                Policy.policy_no,
                Policy.insurer_name,
                Policy.premium,
                Policy.issued_date,
                Sale.customer_name,
                Sale.vehicle_reg,
            ).join(Sale, Policy.sale_id == Sale.id)
        ).all()
    finally:
        session.close()

    if not rows:
        raise SystemExit("No policies in DB; run scripts/generate_synthetic_data.py first.")

    # One policy per insurer, in a stable order.
    by_insurer: dict[str, dict] = {}
    for policy_no, insurer, premium, issued, customer, vehicle in rows:
        if insurer not in by_insurer:
            by_insurer[insurer] = {
                "policy_no": policy_no,
                "insurer_name": insurer,
                "premium": premium,
                "issued_date": issued.isoformat(),
                "customer_name": customer,
                "vehicle_reg": vehicle,
            }
    picked = list(by_insurer.values())[:4]

    # Tamper exactly one document's printed premium.
    tamper_index = len(picked) - 1
    manifest = []
    for i, data in enumerate(picked):
        true_premium = Decimal(data["premium"])
        if i == tamper_index:
            printed_premium = (true_premium + Decimal("500.00")).quantize(Decimal("0.01"))
            tampered = "premium"
        else:
            printed_premium = true_premium
            tampered = None

        fname = f"{data['insurer_name'].replace(' ', '_').lower()}_{data['policy_no']}.pdf"
        path = DOCS_DIR / fname
        _draw_certificate(path, data, printed_premium)

        manifest.append({
            "file": fname,
            "policy_no": data["policy_no"],
            "insurer_name": data["insurer_name"],
            "customer_name": data["customer_name"],
            "vehicle_reg": data["vehicle_reg"],
            "true_premium": float(true_premium),
            "printed_premium": float(printed_premium),
            "tampered_field": tampered,
        })
        flag = f"  <-- TAMPERED premium ({true_premium} -> {printed_premium})" if tampered else ""
        print(f"  wrote {fname}{flag}")

    manifest_path = DOCS_DIR / "docs_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"\n{len(picked)} documents + manifest written to {DOCS_DIR}")


if __name__ == "__main__":
    main()
