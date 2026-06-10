# Backend image: FastAPI app + Alembic migrations + seed scripts.
FROM python:3.11-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# pyproject reads README.md (readme = "README.md") and discovers packages under
# src/, so both must be present before the editable install. (This differs from a
# naive copy-pyproject-then-install ordering, which would fail to find either.)
COPY pyproject.toml README.md ./
COPY src/ src/
RUN pip install --upgrade pip && pip install -e ".[dev]"

# The rest only changes when migrations / scripts change — copied after the
# (slow) dependency install so it doesn't bust that layer.
COPY alembic/ alembic/
COPY alembic.ini ./
COPY scripts/ scripts/

EXPOSE 8000

# Default command; docker-compose overrides this for the backend (migrate +
# serve) and the worker (seed).
CMD ["uvicorn", "leaksentinel.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
