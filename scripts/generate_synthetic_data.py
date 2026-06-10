#!/usr/bin/env python
"""Generate realistic synthetic data for LeakSentinel.

What this produces
------------------
* Clean, well-formed master data loaded straight into Postgres:
  ``dealers`` (real OEMs), ``sales`` (with the 1+1 renewal flags),
  ``policies`` (the *expected* side) and ``crm_records`` (our own booking).
* Four **insurer commission feeds** — one CSV per insurer — each in a
  DELIBERATELY DIFFERENT format (column names, date encodings, commission as
  absolute amount vs. rate-to-apply, exact vs. embedded policy references).
  These stay as raw CSVs in ``data/synthetic/``; they are ingested/normalized
  in the next step, not loaded clean.
* A ground-truth oracle (``data/synthetic/ground_truth.json``) recording which
  ``policy_no`` got which leakage scenario, so detection can be scored later.

Leakage scenarios (~15-20% of policies):
    MISSING_COMMISSION                 policy sold but absent from every feed
    UNDERPAID                          commission paid below expected rate
    DUPLICATE                          two commission entries (overpay risk)
    RENEWAL_1PLUS1_NOT_PROVISIONED     1+1 renewal owed but not provisioned
    ROUNDING_DELTA                     sub-rupee rounding mismatch

Run:
    .venv/bin/python scripts/generate_synthetic_data.py
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from leaksentinel.db import SessionLocal, reset_database
from leaksentinel.reconciliation.models import CrmRecord, Dealer, Policy, Sale

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
SEED = 42
N_POLICIES = 200
SCENARIO_FRACTION = 0.175  # ~17.5% of policies carry a leakage scenario

DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "synthetic"

INSURERS = ["ICICI Lombard", "Bajaj", "Digit", "Tata AIG"]

# Real OEM names -> a couple of dealers each.
DEALERS = [
    ("Hero MotoCorp Showroom", "Hero", "Mumbai"),
    ("Hero Velocity Motors", "Hero", "Jaipur"),
    ("Honda BigWing", "Honda", "Pune"),
    ("Honda Prestige Riders", "Honda", "Chennai"),
    ("TVS Apex Motors", "TVS", "Bengaluru"),
    ("TVS Sundaram Wheels", "TVS", "Coimbatore"),
    ("Bajaj Probiking Hub", "Bajaj", "Delhi"),
    ("Ather Space", "Ather", "Hyderabad"),
]

STATE_CODES = ["MH", "DL", "KA", "TS", "TN", "RJ"]
FIRST_NAMES = ["Aarav", "Vivaan", "Ananya", "Diya", "Kabir", "Ishaan",
               "Saanvi", "Aditya", "Riya", "Arjun", "Meera", "Rohan"]
LAST_NAMES = ["Sharma", "Verma", "Patel", "Reddy", "Iyer", "Nair",
              "Gupta", "Khan", "Das", "Mehta"]

SCENARIOS = [
    "MISSING_COMMISSION",
    "UNDERPAID",
    "DUPLICATE",
    "RENEWAL_1PLUS1_NOT_PROVISIONED",
    "ROUNDING_DELTA",
]

TWOPLACES = Decimal("0.01")


def money(x: float | Decimal) -> Decimal:
    """Round to 2 decimal places, half-up (currency)."""
    return Decimal(str(x)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


# --------------------------------------------------------------------------- #
# Synthetic record model (in-memory, before DB / CSV rendering)
# --------------------------------------------------------------------------- #
@dataclass
class PolicyRecord:
    policy_no: str
    insurer: str
    dealer_idx: int
    customer_name: str
    vehicle_reg: str
    state: str
    rsa_subscription_id: str
    sale_date: date
    issued_date: date
    paid_date: date
    premium: Decimal
    expected_rate: Decimal          # fraction, e.g. 0.15
    expected_commission: Decimal    # premium * expected_rate
    scenario: str = "CLEAN"
    renewal_eligible: bool = False
    renewal_provisioned: bool = False
    # Absolute commission amounts that should appear in the insurer feed.
    paid_entries: list[Decimal] = field(default_factory=list)
    raw_refs: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Per-insurer feed renderers — each intentionally different
# --------------------------------------------------------------------------- #
def _ref_icici(rec: PolicyRecord) -> str:
    # Exact match to policies.policy_no.
    return rec.policy_no


def _ref_bajaj(rec: PolicyRecord) -> str:
    # Embedded in a longer string: BAJAJ/<year>/<policy_no>/<state>.
    return f"BAJAJ/{rec.issued_date.year}/{rec.policy_no}/{rec.state}"


def _ref_digit(rec: PolicyRecord) -> str:
    # Embedded with prefix/suffix: DIGIT-<policy_no>-TW.
    return f"DIGIT-{rec.policy_no}-TW"


def _ref_tata(rec: PolicyRecord) -> str:
    # Exact match.
    return rec.policy_no


def render_icici(rec: PolicyRecord, paid: Decimal, rng: random.Random) -> dict:
    # Absolute amount, DD/MM/YYYY, exact ref.
    return {
        "Policy Reference": _ref_icici(rec),
        "Premium Collected": f"{rec.premium:.2f}",
        "Commission Payable": f"{paid:.2f}",
        "Payment Date": rec.paid_date.strftime("%d/%m/%Y"),
        "Settlement Status": rng.choice(["SETTLED", "PROCESSED", "SETTLED"]),
    }


def render_bajaj(rec: PolicyRecord, paid: Decimal, rng: random.Random) -> dict:
    # Rate-to-be-applied (percent), YYYY-MM-DD, embedded ref.
    rate_pct = (paid / rec.premium) * Decimal(100)
    return {
        "policy_ref": _ref_bajaj(rec),
        "gross_premium": f"{rec.premium:.2f}",
        "commission_rate_pct": f"{rate_pct:.6f}",
        "settled_on": rec.paid_date.isoformat(),
        "txn_status": rng.choice(["PAID", "Cleared", "paid"]),
    }


def render_digit(rec: PolicyRecord, paid: Decimal, rng: random.Random) -> dict:
    # Absolute amount, epoch seconds (UTC), embedded ref. Noon UTC keeps the
    # round-trip date stable for any reader timezone (M8).
    epoch = int(datetime.combine(rec.paid_date, time(12, 0), tzinfo=timezone.utc).timestamp())
    return {
        "ReferenceID": _ref_digit(rec),
        "PremiumAmount": f"{rec.premium:.2f}",
        "CommissionAmt": f"{paid:.2f}",
        "PaidEpoch": str(epoch),
        "Status": rng.choice(["Success", "SUCCESS", "DONE"]),
    }


def render_tata(rec: PolicyRecord, paid: Decimal, rng: random.Random) -> dict:
    # Rate-to-be-applied (percent), DD-Mon-YYYY, exact ref.
    rate_pct = (paid / rec.premium) * Decimal(100)
    return {
        "PolicyNumber": _ref_tata(rec),
        "Premium": f"{rec.premium:.2f}",
        "BrokeragePercent": f"{rate_pct:.6f}",
        "DateOfPayment": rec.paid_date.strftime("%d-%b-%Y"),
        "PaymentStatus": rng.choice(["Completed", "COMPLETED"]),
    }


# insurer -> (filename, ordered columns, renderer, ref-builder, commission_mode)
FEED_SPECS = {
    "ICICI Lombard": {
        "filename": "icici_lombard_feed.csv",
        "columns": ["Policy Reference", "Premium Collected",
                    "Commission Payable", "Payment Date", "Settlement Status"],
        "render": render_icici,
        "ref": _ref_icici,
        "commission_mode": "absolute",
        "date_format": "DD/MM/YYYY",
        "ref_mode": "exact",
    },
    "Bajaj": {
        "filename": "bajaj_feed.csv",
        "columns": ["policy_ref", "gross_premium", "commission_rate_pct",
                    "settled_on", "txn_status"],
        "render": render_bajaj,
        "ref": _ref_bajaj,
        "commission_mode": "rate_pct",
        "date_format": "YYYY-MM-DD",
        "ref_mode": "embedded",
    },
    "Digit": {
        "filename": "digit_feed.csv",
        "columns": ["ReferenceID", "PremiumAmount", "CommissionAmt",
                    "PaidEpoch", "Status"],
        "render": render_digit,
        "ref": _ref_digit,
        "commission_mode": "absolute",
        "date_format": "epoch_seconds",
        "ref_mode": "embedded",
    },
    "Tata AIG": {
        "filename": "tata_aig_feed.csv",
        "columns": ["PolicyNumber", "Premium", "BrokeragePercent",
                    "DateOfPayment", "PaymentStatus"],
        "render": render_tata,
        "ref": _ref_tata,
        "commission_mode": "rate_pct",
        "date_format": "DD-Mon-YYYY",
        "ref_mode": "exact",
    },
}

# Per-insurer expected commission-rate band (fractions).
RATE_BANDS = {
    "ICICI Lombard": (0.12, 0.18),
    "Bajaj": (0.14, 0.20),
    "Digit": (0.10, 0.16),
    "Tata AIG": (0.13, 0.19),
}

INSURER_PREFIX = {
    "ICICI Lombard": "IL",
    "Bajaj": "BJ",
    "Digit": "DG",
    "Tata AIG": "TA",
}


# --------------------------------------------------------------------------- #
# Generation
# --------------------------------------------------------------------------- #
def build_records(rng: random.Random) -> list[PolicyRecord]:
    records: list[PolicyRecord] = []
    start = date(2025, 6, 1)

    for i in range(N_POLICIES):
        insurer = INSURERS[i % len(INSURERS)]
        prefix = INSURER_PREFIX[insurer]
        policy_no = f"{prefix}-TW-{100001 + i}"

        dealer_idx = rng.randrange(len(DEALERS))
        state = rng.choice(STATE_CODES)
        customer = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        vehicle_reg = (
            f"{state}{rng.randint(1, 49):02d}"
            f"{rng.choice('ABCDEFGHJKLMNPQR')}{rng.choice('ABCDEFGHJKLMNPQR')}"
            f"{rng.randint(1000, 9999)}"
        )

        sale_dt = start + timedelta(days=rng.randint(0, 330))
        issued_dt = sale_dt + timedelta(days=rng.randint(0, 3))
        paid_dt = issued_dt + timedelta(days=rng.randint(12, 55))

        premium = money(rng.uniform(850, 2850))
        lo, hi = RATE_BANDS[insurer]
        rate = Decimal(str(round(rng.uniform(lo, hi), 4)))
        expected = money(premium * rate)

        records.append(
            PolicyRecord(
                policy_no=policy_no,
                insurer=insurer,
                dealer_idx=dealer_idx,
                customer_name=customer,
                vehicle_reg=vehicle_reg,
                state=state,
                rsa_subscription_id=f"RSA-{200001 + i}",
                sale_date=sale_dt,
                issued_date=issued_dt,
                paid_date=paid_dt,
                premium=premium,
                expected_rate=rate,
                expected_commission=expected,
            )
        )
    return records


def assign_scenarios(records: list[PolicyRecord], rng: random.Random) -> None:
    """Tag ~SCENARIO_FRACTION of records with a leakage scenario; set feed payouts."""
    n_scenario = round(SCENARIO_FRACTION * len(records))
    idx = list(range(len(records)))
    rng.shuffle(idx)
    chosen = idx[:n_scenario]

    # Distribute chosen indices round-robin across the five scenarios.
    for k, ridx in enumerate(chosen):
        records[ridx].scenario = SCENARIOS[k % len(SCENARIOS)]

    # A handful of *clean* sales are renewal-eligible AND provisioned, so the
    # not-provisioned cases aren't trivially separable by "eligible == True".
    clean_idx = [j for j in idx[n_scenario:]]
    rng.shuffle(clean_idx)
    for ridx in clean_idx[: max(8, n_scenario // 2)]:
        records[ridx].renewal_eligible = True
        records[ridx].renewal_provisioned = True

    # Now realize each record's feed payout(s) + renewal flags by scenario.
    for rec in records:
        exp = rec.expected_commission
        ref = FEED_SPECS[rec.insurer]["ref"](rec)

        if rec.scenario == "CLEAN":
            rec.paid_entries = [exp]
            rec.raw_refs = [ref]

        elif rec.scenario == "MISSING_COMMISSION":
            rec.paid_entries = []          # absent from every feed
            rec.raw_refs = []

        elif rec.scenario == "UNDERPAID":
            factor = Decimal(str(round(rng.uniform(0.60, 0.85), 3)))
            rec.paid_entries = [money(exp * factor)]
            rec.raw_refs = [ref]

        elif rec.scenario == "DUPLICATE":
            rec.paid_entries = [exp, exp]  # paid twice
            rec.raw_refs = [ref, ref]

        elif rec.scenario == "ROUNDING_DELTA":
            delta = Decimal(str(rng.choice([-0.50, -0.23, -0.07, 0.04, 0.11, 0.49])))
            rec.paid_entries = [money(exp + delta)]
            rec.raw_refs = [ref]

        elif rec.scenario == "RENEWAL_1PLUS1_NOT_PROVISIONED":
            # Base policy commission is paid normally; the leak is the un-booked
            # second-year renewal we owe the customer.
            rec.paid_entries = [exp]
            rec.raw_refs = [ref]
            rec.renewal_eligible = True
            rec.renewal_provisioned = False


# --------------------------------------------------------------------------- #
# Writers
# --------------------------------------------------------------------------- #
def write_feeds(records: list[PolicyRecord], rng: random.Random) -> dict[str, int]:
    """Write one CSV per insurer; return row counts per file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows_by_insurer: dict[str, list[dict]] = {ins: [] for ins in INSURERS}

    for rec in records:
        spec = FEED_SPECS[rec.insurer]
        for paid in rec.paid_entries:
            rows_by_insurer[rec.insurer].append(spec["render"](rec, paid, rng))

    counts: dict[str, int] = {}
    for insurer, rows in rows_by_insurer.items():
        spec = FEED_SPECS[insurer]
        path = DATA_DIR / spec["filename"]
        # Shuffle so the feed isn't conveniently ordered by policy.
        rng.shuffle(rows)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=spec["columns"])
            writer.writeheader()
            writer.writerows(rows)
        counts[spec["filename"]] = len(rows)
    return counts


def write_ground_truth(records: list[PolicyRecord], feed_counts: dict[str, int]) -> Path:
    counts: dict[str, int] = {s: 0 for s in SCENARIOS}
    counts["CLEAN"] = 0
    policies: dict[str, dict] = {}

    for rec in records:
        counts[rec.scenario] += 1
        policies[rec.policy_no] = {
            "scenario": rec.scenario,
            "insurer": rec.insurer,
            "premium": float(rec.premium),
            "expected_rate": float(rec.expected_rate),
            "expected_commission": float(rec.expected_commission),
            "paid_entries": [float(p) for p in rec.paid_entries],
            "raw_refs": rec.raw_refs,
            "issued_date": rec.issued_date.isoformat(),
            "paid_date": rec.paid_date.isoformat(),
            "renewal_eligible": rec.renewal_eligible,
            "renewal_provisioned": rec.renewal_provisioned,
            "dealer": DEALERS[rec.dealer_idx][0],
        }

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "seed": SEED,
        "n_policies": len(records),
        "scenario_counts": counts,
        "feed_row_counts": feed_counts,
        "feed_formats": {
            ins: {
                "file": FEED_SPECS[ins]["filename"],
                "commission_mode": FEED_SPECS[ins]["commission_mode"],
                "date_format": FEED_SPECS[ins]["date_format"],
                "ref_mode": FEED_SPECS[ins]["ref_mode"],
            }
            for ins in INSURERS
        },
        "policies": policies,
    }

    path = DATA_DIR / "ground_truth.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# DB load
# --------------------------------------------------------------------------- #
def load_into_db(records: list[PolicyRecord]) -> None:
    """Reset the schema and load dealers/sales/policies/crm (feeds stay as CSV)."""
    reset_database()
    session = SessionLocal()
    try:
        # Dealers
        dealer_rows = [Dealer(name=n, oem=o, city=c) for (n, o, c) in DEALERS]
        session.add_all(dealer_rows)
        session.flush()  # assign dealer ids

        for rec in records:
            sale = Sale(
                dealer_id=dealer_rows[rec.dealer_idx].id,
                customer_name=rec.customer_name,
                vehicle_reg=rec.vehicle_reg,
                sale_date=rec.sale_date,
                rsa_subscription_id=rec.rsa_subscription_id,
                renewal_1plus1_eligible=rec.renewal_eligible,
                renewal_provisioned=rec.renewal_provisioned,
            )
            session.add(sale)
            session.flush()  # assign sale id

            session.add(
                Policy(
                    insurer_name=rec.insurer,
                    policy_no=rec.policy_no,
                    premium=rec.premium,
                    expected_commission_rate=rec.expected_rate,
                    sale_id=sale.id,
                    issued_date=rec.issued_date,
                )
            )
            # CRM books the *expected* commission (our own source of truth).
            session.add(
                CrmRecord(
                    policy_no=rec.policy_no,
                    recorded_commission=rec.expected_commission,
                    recorded_date=rec.issued_date + timedelta(days=2),
                )
            )

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Validation + reporting
# --------------------------------------------------------------------------- #
def print_summary(records: list[PolicyRecord], feed_counts: dict[str, int]) -> None:
    counts: dict[str, int] = {s: 0 for s in SCENARIOS}
    counts["CLEAN"] = 0
    for rec in records:
        counts[rec.scenario] += 1

    total = len(records)
    leak_total = total - counts["CLEAN"]

    print("\n=== Injected scenario summary ===")
    print(f"{'Scenario':<34}{'Count':>7}{'% of total':>12}")
    print("-" * 53)
    for s in SCENARIOS:
        print(f"{s:<34}{counts[s]:>7}{counts[s] / total * 100:>11.1f}%")
    print("-" * 53)
    print(f"{'LEAKAGE SUBTOTAL':<34}{leak_total:>7}{leak_total / total * 100:>11.1f}%")
    print(f"{'CLEAN':<34}{counts['CLEAN']:>7}{counts['CLEAN'] / total * 100:>11.1f}%")
    print(f"{'TOTAL POLICIES':<34}{total:>7}{100.0:>11.1f}%")

    print("\n=== Insurer feed CSVs (raw, not loaded) ===")
    for ins in INSURERS:
        spec = FEED_SPECS[ins]
        print(f"  {spec['filename']:<26} rows={feed_counts[spec['filename']]:>4}  "
              f"[{spec['commission_mode']}, {spec['date_format']}, ref={spec['ref_mode']}]")


def validate_against_db(records: list[PolicyRecord]) -> bool:
    """Confirm ground_truth.json agrees with what landed in Postgres."""
    gt = json.loads((DATA_DIR / "ground_truth.json").read_text(encoding="utf-8"))
    gt_policy_nos = set(gt["policies"].keys())

    session = SessionLocal()
    try:
        db_policy_nos = {p for (p,) in session.query(Policy.policy_no).all()}
        db_policy_count = session.query(Policy).count()
        db_sale_count = session.query(Sale).count()
        db_crm_count = session.query(CrmRecord).count()
        db_dealer_count = session.query(Dealer).count()
        # Sales that are renewal-eligible but not provisioned.
        db_renewal_leak = (
            session.query(Sale)
            .filter(Sale.renewal_1plus1_eligible.is_(True))
            .filter(Sale.renewal_provisioned.is_(False))
            .count()
        )
    finally:
        session.close()

    gt_renewal_leak = gt["scenario_counts"]["RENEWAL_1PLUS1_NOT_PROVISIONED"]

    checks = [
        ("policy_no sets match (ground truth <-> DB)", gt_policy_nos == db_policy_nos),
        ("policy count matches", db_policy_count == len(records)),
        ("sale count matches policy count", db_sale_count == len(records)),
        ("crm count matches policy count", db_crm_count == len(records)),
        ("dealer count matches", db_dealer_count == len(DEALERS)),
        ("renewal-not-provisioned count matches GT", db_renewal_leak == gt_renewal_leak),
    ]

    print("\n=== Validation: ground_truth.json vs. Postgres ===")
    all_ok = True
    for label, ok in checks:
        print(f"  [{'OK' if ok else 'FAIL'}] {label}")
        all_ok = all_ok and ok

    if all_ok:
        print(f"\nAll checks passed. DB rows: dealers={db_dealer_count}, "
              f"sales={db_sale_count}, policies={db_policy_count}, crm={db_crm_count}.")
    else:
        print("\nVALIDATION FAILED — see failed checks above.")
    return all_ok


def main() -> None:
    rng = random.Random(SEED)

    records = build_records(rng)
    assign_scenarios(records, rng)
    feed_counts = write_feeds(records, rng)
    gt_path = write_ground_truth(records, feed_counts)
    load_into_db(records)

    print_summary(records, feed_counts)
    print(f"\nGround truth written to: {gt_path}")
    ok = validate_against_db(records)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
