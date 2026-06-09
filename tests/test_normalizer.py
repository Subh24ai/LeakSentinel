"""Tests for the insurer-feed normalizers.

These normalize each real synthetic CSV and assert against the ground-truth
oracle: parsed policy_no values, rate->amount conversions, and all four date
formats landing on the right canonical date. Also covers the data-quality
note path (unparseable ref / ambiguous status / missing amount).
"""

from __future__ import annotations

import csv
import datetime as dt
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path

import pytest

from leaksentinel.ingestion.normalizer import (
    Normalizer,
    get_normalizer,
    normalize_status,
    registered_insurers,
)
from leaksentinel.reconciliation.schemas import PaymentStatus

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "synthetic"
GROUND_TRUTH = DATA_DIR / "ground_truth.json"

INSURERS = ["ICICI Lombard", "Bajaj", "Digit", "Tata AIG"]


@pytest.fixture(scope="module")
def ground_truth() -> dict:
    if not GROUND_TRUTH.exists():
        pytest.skip("ground_truth.json missing; run scripts/generate_synthetic_data.py")
    return json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _normalize_insurer(insurer: str):
    normalizer = get_normalizer(insurer)
    path = DATA_DIR / normalizer.source_file
    if not path.exists():
        pytest.skip(f"{path.name} missing; run the generator")
    return normalizer.normalize_feed(_read_csv(path))


def test_all_four_insurers_registered():
    for insurer in INSURERS:
        assert insurer in registered_insurers()
        assert isinstance(get_normalizer(insurer), Normalizer)


@pytest.mark.parametrize("insurer", INSURERS)
def test_parsed_policy_no_matches_ground_truth(insurer, ground_truth):
    """Every parsed policy_no must match the feed multiset implied by the oracle."""
    views = _normalize_insurer(insurer)

    # Expected: each policy of this insurer appears once per paid_entry.
    expected = Counter()
    for policy_no, info in ground_truth["policies"].items():
        if info["insurer"] == insurer:
            expected[policy_no] += len(info["paid_entries"])

    got = Counter(v.policy_no for v in views)

    # No row should fail to parse.
    assert None not in got, f"{insurer}: some refs failed to parse"
    assert got == expected, f"{insurer}: parsed policy_no multiset mismatch"


@pytest.mark.parametrize("insurer", INSURERS)
def test_no_normalization_notes_on_clean_feed(insurer):
    """The synthetic feeds are format-messy but value-clean: zero DQ flags."""
    views = _normalize_insurer(insurer)
    offenders = [(v.source_ref, v.normalization_notes) for v in views if v.normalization_notes]
    assert not offenders, f"{insurer}: unexpected notes {offenders[:3]}"


@pytest.mark.parametrize("insurer", INSURERS)
def test_commission_amount_matches_ground_truth(insurer, ground_truth):
    """Absolute amounts match exactly; rate->amount conversions within ₹0.01."""
    views = _normalize_insurer(insurer)

    got: dict[str, list[Decimal]] = {}
    for v in views:
        got.setdefault(v.policy_no, []).append(v.actual_commission)

    for policy_no, amounts in got.items():
        info = ground_truth["policies"][policy_no]
        expected = sorted(Decimal(str(p)) for p in info["paid_entries"])
        actual = sorted(amounts)
        assert len(actual) == len(expected)
        for a, e in zip(actual, expected):
            assert abs(a - e) <= Decimal("0.01"), (
                f"{insurer} {policy_no}: got {a}, expected {e}"
            )


@pytest.mark.parametrize("insurer", INSURERS)
def test_dates_match_ground_truth(insurer, ground_truth):
    """All four date formats decode to the canonical paid_date in the oracle."""
    views = _normalize_insurer(insurer)
    for v in views:
        expected = dt.date.fromisoformat(ground_truth["policies"][v.policy_no]["paid_date"])
        assert v.paid_date == expected, f"{insurer} {v.policy_no}: {v.paid_date} != {expected}"


@pytest.mark.parametrize("insurer", INSURERS)
def test_status_canonicalized_to_paid(insurer):
    """Every synthetic feed status is a 'paid' synonym -> PaymentStatus.PAID."""
    views = _normalize_insurer(insurer)
    assert views, "no rows normalized"
    assert all(v.payment_status is PaymentStatus.PAID for v in views)


# --------------------------------------------------------------------------- #
# Data-quality note path (does not depend on generated data)
# --------------------------------------------------------------------------- #
def test_status_normalization_unit():
    assert normalize_status("SETTLED")[0] is PaymentStatus.PAID
    assert normalize_status("paid")[0] is PaymentStatus.PAID
    assert normalize_status("SUCCESS")[0] is PaymentStatus.PAID
    assert normalize_status("Completed")[0] is PaymentStatus.PAID
    status, note = normalize_status("weird-thing")
    assert status is PaymentStatus.UNKNOWN and note and note.startswith("ambiguous_status")
    status, note = normalize_status("")
    assert status is PaymentStatus.UNKNOWN and note == "missing_status"


def test_bajaj_unparseable_ref_and_missing_amount_flagged():
    n = get_normalizer("Bajaj")
    view = n.normalize_row(
        {
            "policy_ref": "NOT-A-VALID-REF",
            "gross_premium": "",          # missing premium
            "commission_rate_pct": "",    # missing rate
            "settled_on": "2026-13-99",   # bad date
            "txn_status": "huh?",         # ambiguous
        }
    )
    notes = " ".join(view.normalization_notes)
    assert view.policy_no is None
    assert "unparseable_policy_ref" in notes
    assert "missing_or_invalid_premium" in notes
    assert "missing_commission_rate" in notes
    assert "unparseable_date" in notes
    assert "ambiguous_status" in notes


def test_digit_embedded_ref_parsing():
    n = get_normalizer("Digit")
    pol, note = n.parse_policy_no("DIGIT-DG-TW-100075-TW")
    assert pol == "DG-TW-100075" and note is None


def test_bajaj_embedded_ref_parsing():
    n = get_normalizer("Bajaj")
    pol, note = n.parse_policy_no("BAJAJ/2025/BJ-TW-100118/DL")
    assert pol == "BJ-TW-100118" and note is None
