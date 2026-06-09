# LeakSentinel

Commission-reconciliation engine for two-wheeler insurance distribution.

> ⚠️ **Synthetic data only.** Everything in this repo runs on programmatically
> generated data. The insurer names (ICICI Lombard, Bajaj, Digit, Tata AIG) and
> OEM names (Hero, Honda, TVS, Bajaj, Ather) are real brands used purely to make
> the scenario realistic. **No real insurer commission statements, and no
> SureDrive internal systems or data, are involved.** See `scripts/generate_synthetic_data.py`.

## Problem

A distributor sells roadside-assistance (RSA) subscriptions and insurance
policies through OEM dealers, on behalf of several insurers, and earns
commission on each policy. The money owed back is easy to lose track of:
each insurer reports what it actually paid in its own statement format, those
statements rarely line up cleanly with the policies on our books, and the
gap between *what we should have been paid* and *what we were paid* is where
revenue silently leaks. Reconciliation is hard precisely because the "actual"
side is heterogeneous — different column names, date encodings (DD/MM/YYYY vs
epoch seconds vs …), commission expressed sometimes as an absolute amount and
sometimes as a rate to apply, and policy references that are sometimes exact
and sometimes buried inside a longer string — so before anything can be matched
on `policy_no`, every feed has to be normalized into one canonical shape.

## Current status

Built end-to-end from ingestion through the gated-actions layer:
**ingestion → reconciliation → detection → document intelligence →
remediation / escalation**, all backed by tests. Only the agent orchestration,
the full API surface, and a frontend remain **not built yet**; these are clearly
marked below.

### Done

- **Schema** (`reconciliation/models.py`) — `dealers`, `sales` (with the 1+1
  renewal flags), `policies` (`policy_no` UNIQUE), `insurer_commission_feeds`,
  `crm_records`, `reconciliation_results`, `audit_log`. Single one-directional
  `policies.sale_id → sales.id` link. Create/reset via `db.py` + `scripts/init_db.py`.
- **Canonical schema** (`reconciliation/schemas.py`) — `ReconciliationView`
  plus `ReconStatus` / `ReasonCode` / `ResolutionState` / `PaymentStatus` enums.
- **Synthetic data generator** (`scripts/generate_synthetic_data.py`) — 200
  policies across 4 insurers, four deliberately different feed formats, ~17.5%
  injected leakage, and a **ground-truth oracle** (`data/synthetic/ground_truth.json`)
  recording which `policy_no` got which scenario.
- **Normalization layer** (`ingestion/normalizer.py`) — registry/strategy
  pattern: one `Normalizer` subclass per insurer, registered by name; adding a
  5th insurer is one class, no dispatcher edits.
- **Ingestion loader** (`ingestion/loader.py`, `make ingest`) — runs each
  normalizer and persists into `insurer_commission_feeds`; flagged/unparseable
  rows are persisted *with* their `normalization_notes` (nothing dropped).
- **Reconciliation engine** (`reconciliation/engine.py`, `make reconcile`) —
  joins feeds ↔ `policies` ↔ `crm_records` on `policy_no` and classifies each
  policy as `MATCHED` / `MISSING_COMMISSION` / `UNDERPAID` / `OVERPAID_DUPLICATE`
  / `CRM_MISMATCH`, persisting results with a status and preliminary reason code.
  The core leak query is kept in **both** raw SQL and SQLAlchemy form.
- **Precision/recall tests** — the engine's output is asserted to match the
  oracle exactly (any invented or missed leak fails the build).
- **Detection layer** (`detection/rules.py` + `detection/anomaly.py`) —
  explainable rule detectors (missing commission, underpaid-below-rate,
  duplicate payment, 1+1 renewal-not-provisioned, rounding-delta) emitting
  `reason_code` + `severity` + a plain-English explanation, plus a secondary
  Isolation Forest anomaly net for novel patterns that never overrides a rule.
  Asserted against the oracle in `test_detection.py`.
- **Document intelligence** (`documents/extractor.py`,
  `scripts/generate_documents.py`, `make gen-docs` / `make extract-doc`) —
  generate synthetic insurer PDFs (one with a planted premium mismatch), extract
  structured fields with the LLM, and validate them against ground truth
  (`test_documents.py`).
- **Gated actions** (`actions/`, `scripts/run_actions.py`) — remediation on
  confirmed leaks behind three tested safety properties: **gated**
  (`validate_action` blocks unless the finding is a confirmed open leak with a
  real reason code above `MIN_CLAIM_THRESHOLD`), **idempotent** (SHA-256 key of
  `policy_no|reason_code|amount`, so a retry returns the existing claim and never
  double-pays), and **audited** (every action *and* every block appends a hashed
  `audit_log` row). High-value or non-trivially-blocked findings route to a human
  `escalations` queue; the insurer/CRM call sits behind a swappable
  `ExternalClaimsAPI` stub. Covered by `test_actions.py`. The whole suite is 64
  tests.

### Planned (NOT built)

- **LangGraph orchestration** — tie detection + documents + actions into an agent.
- **API** — FastAPI surface (only a health/metadata stub exists today).
- **Frontend** — none.

## Pipeline (as built)

```mermaid
%%{init: {'theme':'base','themeVariables':{'fontSize':'16px'},'flowchart':{'nodeSpacing':45,'rankSpacing':50,'curve':'basis'}}}%%
flowchart TB
    classDef proc    fill:#e8f0fe,stroke:#1a73e8,stroke-width:1.5px,color:#0b2545;
    classDef store   fill:#fff4e5,stroke:#f59e0b,stroke-width:1.5px,color:#5b3b00;
    classDef gate    fill:#fde8e8,stroke:#d93025,stroke-width:1.5px,color:#5c0a06;
    classDef planned fill:#f1f3f4,stroke:#9aa0a6,stroke-width:1.5px,stroke-dasharray:5 4,color:#5f6368;

    CSV["Insurer CSVs — 4 formats"]:::proc
    NORM["Normalizers — registry / strategy"]:::proc
    FEEDS[("insurer_commission_feeds")]:::store
    MASTER[("policies · sales · crm")]:::store
    ENG["Reconciliation engine — match on policy_no"]:::proc
    RES[("reconciliation_results")]:::store
    DOCS["Document intelligence — LLM extract + validate"]:::proc
    DET["Detection — rule detectors + anomaly net"]:::proc
    GATE{"validate_action — confirmed? · real reason? · above threshold?"}:::gate
    ACT["Remediation — claim / rebilling (idempotent)"]:::proc
    CLAIMS[("commission_claims")]:::store
    ESC[("escalations — human queue")]:::store
    AUDIT[("audit_log — SHA-256 of every action")]:::store

    CSV --> NORM --> FEEDS
    MASTER --> ENG
    FEEDS --> ENG
    ENG --> RES --> DET
    DOCS -. supporting evidence .-> DET
    DET --> GATE
    GATE -- pass --> ACT --> CLAIMS
    GATE -- "high-value / blocked" --> ESC
    ACT --> AUDIT
    GATE --> AUDIT

    subgraph PLANNED [" Planned — not built "]
        direction TB
        GRAPH["LangGraph orchestration"]:::planned
        API["FastAPI surface — stub today"]:::planned
        UI["Frontend"]:::planned
        GRAPH --> API --> UI
    end

    CLAIMS -.-> GRAPH
    ESC -.-> GRAPH
```

**Legend** — 🟦 process · 🟧 data store · 🟥 decision gate · ⬜ planned (dashed
border). Read top → bottom; solid arrows are built, dashed arrows reach into the
planned layers.

## The core problem, in one table

Each insurer's commission feed is in a different format. Normalizing all four
into one `ReconciliationView` is the precondition for any matching:

| Insurer | Sample columns | Date format | Commission | Policy ref |
| --- | --- | --- | --- | --- |
| ICICI Lombard | `Policy Reference`, `Premium Collected`, `Commission Payable`, `Payment Date`, `Settlement Status` | `DD/MM/YYYY` | absolute amount | exact (`IL-TW-100021`) |
| Bajaj | `policy_ref`, `gross_premium`, `commission_rate_pct`, `settled_on`, `txn_status` | `YYYY-MM-DD` | **rate %** → × premium | embedded (`BAJAJ/2025/BJ-TW-100118/DL`) |
| Digit | `ReferenceID`, `PremiumAmount`, `CommissionAmt`, `PaidEpoch`, `Status` | epoch seconds | absolute amount | embedded (`DIGIT-DG-TW-100075-TW`) |
| Tata AIG | `PolicyNumber`, `Premium`, `BrokeragePercent`, `DateOfPayment`, `PaymentStatus` | `DD-Mon-YYYY` | **rate %** → × premium | exact (`TA-TW-100136`) |

Free-text statuses (`SETTLED` / `paid` / `SUCCESS` / `Completed` / …) are also
normalized to a single `PaymentStatus` enum.

## Tech stack

Python 3.11+, SQLAlchemy 2 + psycopg (Postgres), pydantic v2, pandas,
scikit-learn, pytest. FastAPI is scaffolded; LangGraph is a declared dependency
but not yet used.

## Run what exists

```bash
# 0. Install (editable, with dev extras) into a Python 3.11+ venv
make install

# 1. Start Postgres (host port 5433 — 5432 is often already taken)
docker compose up -d postgres

# 2. Create the schema
make db-recreate

# 3. Generate synthetic data + load dealers/sales/policies/crm into Postgres,
#    write the 4 insurer CSVs and ground_truth.json
#    (NOTE: this also resets the schema, so it's safe to run on its own)
make gen-data

# 4. Normalize the 4 insurer CSVs into insurer_commission_feeds
make ingest

# 5. Reconcile and print the confusion summary
make reconcile

# 6. Run the test suite (includes the precision/recall backbone)
make test
```

`make reconcile` prints planted (oracle) vs. detected (engine) per class:

```
=== Confusion summary: planted (oracle) vs. detected (engine) ===
Class                  Planted  Detected    TP    FP    FN
----------------------------------------------------------
MATCHED                    179       179   179     0     0
MISSING_COMMISSION           7         7     7     0     0
UNDERPAID                    7         7     7     0     0
OVERPAID_DUPLICATE           7         7     7     0     0
CRM_MISMATCH                 0         0     0     0     0
----------------------------------------------------------
TOTAL                      200       200

EXACT MATCH — zero false positives/negatives.
```

`ROUNDING_DELTA` and `RENEWAL_1PLUS1_NOT_PROVISIONED` policies are planted in
the data but read as `MATCHED` here on purpose — they are not commission-amount
mismatches on a single feed line, and belong to the (planned) detection layer.

## Make targets

`make help` lists everything (install, db-up/down/reset, db-init/recreate,
gen-data, ingest, reconcile, run, test, lint, clean).
