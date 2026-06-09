# LeakSentinel

Agentic **commission-reconciliation engine** for two-wheeler insurance distribution.

LeakSentinel is built for a distributor that sells RSA (Roadside Assistance)
subscriptions and insurance policies through OEM dealers, on behalf of multiple
insurers. It ingests policy/subscription records and insurer commission
statements, reconciles what *should* have been paid against what *was* paid,
detects revenue leakage, and drives agentic follow-up actions.

> **Status:** Skeleton only. No business logic is implemented yet — this commit
> establishes the project structure, packaging, and local Postgres.

## Architecture

The package lives under `src/leaksentinel/` and is split into focused modules:

| Module           | Responsibility                                                        |
| ---------------- | --------------------------------------------------------------------- |
| `ingestion`      | Load dealer/policy data and insurer commission statements             |
| `reconciliation` | Match expected vs. actual commissions, compute deltas                 |
| `detection`      | Leakage detection (rules + ML anomaly scoring)                        |
| `documents`      | Parse statements, generate dispute / reconciliation documents         |
| `actions`        | Agentic follow-up actions (notify, dispute, escalate)                 |
| `graph`          | LangGraph orchestration tying the agent workflow together             |
| `api`            | FastAPI application exposing the engine                               |

## Tech stack

Python 3.11+, FastAPI, SQLAlchemy 2 + psycopg (Postgres), LangGraph, pandas,
scikit-learn, pydantic v2, pytest. LLM via Anthropic Claude (default) or Groq.

## Quickstart

```bash
# 1. Create venv + install (editable, with dev extras)
make install

# 2. Configure environment
cp .env.example .env        # then fill in API keys

# 3. Bring up Postgres
make db-up

# 4. Run the API
make run                    # http://127.0.0.1:8000/docs
```

Without `make`:

```bash
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
docker compose up -d postgres
uvicorn leaksentinel.api.main:app --reload
```

## LLM provider

Set `LLM_PROVIDER` in `.env` to `anthropic` (default) or `groq`, and provide the
matching API key (`ANTHROPIC_API_KEY` or `GROQ_API_KEY`). See
`leaksentinel.config.Settings`.

## Make targets

Run `make help` for the full list (install, db-up, db-down, db-reset, run, test,
lint, clean).
