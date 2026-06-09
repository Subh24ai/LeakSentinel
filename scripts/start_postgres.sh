#!/usr/bin/env bash
# Start the local Postgres container and wait until it is healthy.
# Usage: ./scripts/start_postgres.sh
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Starting Postgres via docker compose..."
docker compose up -d postgres

echo -n "Waiting for Postgres to become healthy"
for _ in $(seq 1 60); do
  status="$(docker inspect -f '{{.State.Health.Status}}' leaksentinel-postgres 2>/dev/null || echo starting)"
  if [ "$status" = "healthy" ]; then
    echo " — ready."
    docker compose ps postgres
    exit 0
  fi
  echo -n "."
  sleep 1
done

echo
echo "ERROR: Postgres did not become healthy in time." >&2
docker compose logs postgres >&2
exit 1
