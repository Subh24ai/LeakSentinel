# LeakSentinel — Codebase Audit & Gap Analysis

> Read-only audit. Date: 2026-06-13. Auditor: Claude (Opus 4.8).
> Method: full read of `src/`, `frontend/src/`, `scripts/`, `tests/`, `alembic/`,
> config; ran the test suite, `make ingest` / `reconcile` / `pipeline`, the
> frontend build, and `alembic check`; verified live API behaviour against the
> running Docker stack. **No code was modified.**

---

## 1. Executive summary

The project is in **strong, demo-ready shape** — substantially more complete and
more carefully built than a typical mid-flight codebase. It runs end-to-end
**right now**: `make pipeline` completes cleanly, `make reconcile` reports an
exact confusion matrix (zero FP/FN vs the synthetic ground-truth oracle), the
FastAPI backend and React frontend both build and serve, and the full Docker
stack comes up healthy. The test suite is **99 passing, 0 failing, 0 skipped**.
There is **no schema drift** (`alembic check` → "No new upgrade operations
detected") and **no stub/TODO/NotImplementedError** anywhere except one
*intentionally* mocked external-API seam (`actions/externalapi.py`).

**There is no single blocker** — nothing prevents the system from running. The
only items of note are housekeeping: (a) three **uncommitted working-tree
changes** that wire uploaded feeds into the reconcile pipeline and ship with a
now-stale docstring elsewhere; (b) a couple of **stale docstrings** left by past
refactors; and (c) the **document/vision-LLM extraction path is real but
standalone** — not wired into the LangGraph pipeline nor exposed via the API
(consistent with the README's dotted "supporting evidence" line, but worth a
conscious decision before a demo that claims it).

---

## 2. Module-by-module status

Legend: ✅ complete & verified · 🟡 complete but with a caveat · 🔵 intentional mock

| Module | README claim | Actual status | Notes |
|---|---|---|---|
| `ingestion/normalizer.py` | One `Normalizer` strategy per insurer; adding one is a class | ✅ | Registry of 4 insurers; 10 tests in `test_normalizer.py`. |
| `ingestion/loader.py` | Truncate+reload synthetic feeds | 🟡 **uncommitted change** | Now supports `source = synthetic\|uploaded\|all`; overlays uploads. Works (pipeline logged `source=all`, 250 rows). **Not committed.** |
| `ingestion/upload_processor.py` | Standalone CSV/PDF upload ingest | 🟡 | Complete (pdfplumber table extract → LLM fallback). **Docstring (L13–15) now stale**: says intake "still reads the canonical synthetic CSVs" — the uncommitted loader/workflow change contradicts this. |
| `reconciliation/engine.py` | Match on `policy_no`; classify; orphan pass | ✅ | Clean, dual SQL+ORM leak query kept on purpose. Exact confusion matrix. |
| `reconciliation/models.py` | Postgres tables for the domain | ✅ | 14 tables; all present in migrations; `alembic check` clean. |
| `reconciliation/schemas.py` | Canonical normalized view + tolerances | ✅ | `ReconciliationView`, `TOLERANCE`, resolution states. |
| `detection/rules.py` | 6 explainable rule detectors, shared reason vocab | ✅ | All 6 present; `DetectionReason` is the single vocabulary; engine maps onto it. |
| `detection/anomaly.py` | Isolation Forest, secondary, never overrides rules | ✅ | Excludes rule-covered policies; labels `ANOMALY_UNCLASSIFIED`. |
| `actions/remediation.py` | Gated, idempotent, audited; hash-chained log | ✅ | Production-grade: partial-unique-index race handling, savepoints, SHA-256 chain. 14 action tests. |
| `actions/escalation.py` | High-value / gate-fail → human queue | ✅ | No stubs; covered by `test_actions.py`. |
| `actions/externalapi.py` | Mocked insurer/CRM API, swappable | 🔵 | **Intentional** mock, clearly documented as the swappable seam. |
| `actions/mapping.py` | Finding → ActionableFinding bridge | ✅ | 4 tests in `test_mapping.py`. |
| `graph/workflow.py` | LangGraph Intake→…→Finalize, deterministic routing | 🟡 **uncommitted change** | Complete and correct; `intake` now picks `source=all` when uploads exist. Not committed. |
| `graph/run.py`, `state.py`, `tracing.py` | CLI entry + typed state + LangSmith | ✅ | Tracing degrades gracefully without `LANGSMITH_API_KEY`. |
| `documents/extractor.py` + `llm.py` | Vision-LLM PDF → JSON + confidence → validate | 🟡 | Real & tested (LLM mocked offline). **Not in the pipeline graph; no API endpoint.** Reachable only via `scripts/run_extraction.py` and as the PDF-upload fallback. |
| `api/main.py` + `service.py` + `schemas.py` | Async JWT FastAPI; leaks served from a table | 🟡 | Complete (auth, RBAC, async reconcile jobs, feed upload, user mgmt, self-signup). **`service.py` L8–11 docstring is stale** — says "Leaks are not a persisted table" but code reads the `findings` table. |
| `auth/` (core, users, __init__) | JWT, 3 roles, bootstrap admin | ✅ | Plus self-service `/auth/signup` (added this session). 22 API tests. |
| `db.py` / `config.py` | Engine/session; env-driven settings | ✅ | `config` centralizes env; `UPLOAD_DIR` is the one setting absent from `.env.example` (has a default). |
| `scripts/*` | gen-data, gen-docs, init_db, run_actions, run_extraction | ✅ | All present and wired to Make targets. |
| `frontend/` (React+Vite) | Typed client mirroring Pydantic; money as string | ✅ | Builds clean (`tsc` + `vite build`). Client paths all map to real routes. No mock data found. |

---

## 3. Test suite results

- **Command:** `.venv/bin/pytest` (after rebuilding the venv on Python 3.13).
- **Result: 99 passed, 0 failed, 0 skipped, 0 xfail** (6 deprecation warnings).
- 84 `def test_` functions; 99 collected (the delta is parametrized cases).
- The whole module is marked `pytest.mark.integration`; tests **skip** (don't fail)
  if Postgres is unreachable or unseeded — so a green run requires the DB up.

| Test file | tests | Covers |
|---|---|---|
| `test_api.py` | 22 | Auth/JWT, RBAC, signup, register, change-password gate, reconcile jobs, feed upload, leaks/metrics. |
| `test_actions.py` | 14 | Gate, idempotency, hash-chain audit, escalation routing. |
| `test_normalizer.py` | 10 | Per-insurer normalization + data-quality flags. |
| `test_engine.py` | 9 | Reconciliation classification + orphan pass. |
| `test_detection.py` | 8 | The 6 rule detectors + anomaly net. |
| `test_pipeline.py` | 6 | End-to-end LangGraph run + conservation. |
| `test_mapping.py` | 4 | Finding → ActionableFinding. |
| `test_smoke.py` | 4 | Imports / wiring. |
| `test_documents.py` | 3 | Vision-LLM extraction (LLM mocked) + validation. |
| `test_reconciliation_schemas.py` | 3 | Normalized view + tolerances. |
| `test_policy_unique.py` | 1 | `policies.policy_no` uniqueness. |

**Coverage gaps:** no test exercises the **document → reconciliation integration**
(because there is none — extraction is standalone). No frontend unit tests (only
the type-checked build). The anomaly detector is tested for shape, not for a
planted novel-pattern recall number.

---

## 4. Known issues / bugs found (ranked)

**None block the pipeline — it runs end-to-end today.** Ranked by impact:

1. **[Medium · process] Uncommitted working-tree changes.** `loader.py`,
   `graph/workflow.py`, `tests/test_api.py` (+ this session's signup feature and
   README edits) are modified but not committed. The loader/workflow change wires
   uploads into reconcile (`source=all`). Decide to **commit or revert** — right
   now the repo's behaviour differs from `git HEAD`, which will confuse a
   teammate or a clean redeploy.
2. **[Low · docs] Stale docstring in `upload_processor.py` L13–15** — claims
   intake "still reads the canonical synthetic CSVs", which the uncommitted change
   makes false.
3. **[Low · docs] Stale docstring in `api/service.py` L8–11** — "Leaks are not a
   persisted table: they're recomputed on demand." The code reads from the
   `findings` table (`PersistedFinding`); lines 52–57 of the same file correctly
   describe the table-backed approach. Leftover from the materialized-findings
   refactor.
4. **[Low · config] `UPLOAD_DIR` not in `.env.example`** — every other `Settings`
   field is documented there; this one only has the in-code default `./uploads`.
5. **[Low · scope] Document/vision-LLM path is not integrated.** Real and tested,
   but not a graph node and not an API endpoint. Fine *if* that's intended;
   risky if a demo narrative implies documents flow into detection automatically.
6. **[Trivial] Deprecation warnings:** `httpx`+`starlette.testclient`
   (suggests `httpx2`); PyMuPDF/SWIG `__module__` warnings. Cosmetic.

---

## 5. Environment & secrets — running from a clean checkout

**Required:**
- **Python 3.13** for the venv. ⚠️ The `Makefile` defaults to `PYTHON ?= python3.13`;
  if only 3.11 is available use `make install PYTHON=python3.11` (`requires-python`
  is `>=3.11`). *(During this audit the prior `.venv` was dead because Homebrew's
  `python@3.13` had been removed; it was reinstalled and the venv rebuilt.)*
- **Docker** — Postgres runs via `docker compose` (host port **5433**).
- **`.env`** — `cp .env.example .env`. `JWT_SECRET` and `FIRST_ADMIN_PASSWORD`
  ship with **working dev defaults**, so login works out of the box (change for
  production).

**Optional / feature-gated:**
- **`ANTHROPIC_API_KEY`** (or `GROQ_API_KEY` + `LLM_PROVIDER=groq`) — needed
  **only** for the document-extraction path (`make extract-doc`, or the PDF-upload
  LLM fallback). The **core reconciliation pipeline, detection, remediation, API,
  and the entire test suite run with no LLM key** — by design (no LLM in the money
  decision path).
- **`LANGSMITH_API_KEY`** — optional tracing; absence is logged and ignored.

**No exposed real secrets.** The only hardcoded credentials are clearly-labelled
dev defaults in `.env.example` / `docker-compose.yml`. `JWT_SECRET` has **no**
silent fallback in code — the API refuses to boot without it.

**Two ways to run:**
- *Full stack, one command:* `docker compose up -d --build` → frontend
  http://localhost:5173, API http://localhost:8000 (the worker seeds the DB once).
- *Local dev:* `make install` → `make db-up` → `make migrate` → `make gen-data`
  → `make ingest` → `make run` (+ `cd frontend && npm run dev`).

---

## 6. Gaps vs README claims

**Claimed-and-true (verified):** deterministic routing with no LLM in the decision
path; 6 explainable detectors + secondary Isolation Forest sharing one reason
vocabulary; gated/idempotent/audited remediation; hash-chained, gap-checked audit
log (`verify_audit_chain`); money as fixed-2dp strings; opposite-sign exposure
split (underpayment vs clawback). All present and exercised by tests.

**README slightly behind the code (code is *more* complete):**
- **Async reconcile jobs** (`reconciliation_jobs`, `GET /reconcile/{job_id}`),
  **feed upload API/UI** (CSV + PDF), **user management** (admin CRUD, roles,
  forced password change), and **self-service signup** all exist but the README's
  API table lists only a representative subset of endpoints (no `/feeds`,
  `/reconcile/jobs`, `/auth/signup`, `/auth/register`, `/auth/users`).
- The README **test count is stale**: it says **98**, actual is **99** (a signup
  test was added this session). *(For the record, the original pre-session README
  said 86.)*

**README ahead of the code (aspirational / not fully wired):**
- The architecture diagram shows **document intelligence feeding detection** as
  "supporting evidence" (dotted edge). In reality extraction is **standalone** —
  not a pipeline node, not an API endpoint. The dotted line is technically honest,
  but the integration a viewer might infer doesn't exist yet.

**Note on the audit brief:** the README has **no explicit "Done vs Planned"
section** (the task assumed one). Everything in it is described as built — and,
with the caveats above, it essentially *is*.

---

## 7. Recommended next steps (prioritized)

1. **Resolve the working tree (15 min).** Decide on the uncommitted upload→reconcile
   feature: either commit `ingestion/loader.py`, `graph/workflow.py`,
   `tests/test_api.py` (the test `test_uploaded_feed_survives_reconcile` already
   covers it and passes) **or** revert. Don't demo from a dirty tree.
2. **Fix the two stale docstrings (5 min).** `ingestion/upload_processor.py`
   L13–15 and `api/service.py` L8–11 — both now misdescribe behaviour. Cheap,
   high-signal for any reviewer reading the code.
3. **Decide the document-intelligence story (scoping).** Either (a) leave it
   standalone and say so plainly in the demo, or (b) integrate it: add a `documents`
   node to `graph/workflow.py` that emits `FIELD_MISMATCH` findings into the same
   `findings` table, and/or expose `POST /documents/extract` in `api/main.py`.
   This is the single biggest "make the README diagram fully true" item.
4. **Sync docs (5 min).** Add `UPLOAD_DIR` to `.env.example`; bump the README test
   count to 99 and round out the API endpoint table (`/feeds*`, `/reconcile/jobs`,
   `/auth/signup|register|users`).
5. **Silence deprecation noise (optional).** Pin/adjust the test client to remove
   the `httpx`/`starlette.testclient` warning so a clean run has zero warnings.
6. **Optional hardening for a "real" deployment:** self-signup currently grants
   anyone `viewer` access to financial data (first signup → admin). For production,
   gate signup behind email-domain allowlisting or admin approval.

**Bottom line:** this is a finish-and-polish situation, not a build-it situation.
The hard parts (deterministic engine, governed actions, audit chain, async jobs,
full-stack auth) are done and tested. The work remaining is committing in-flight
changes, three doc fixes, and one scoping decision on documents.
