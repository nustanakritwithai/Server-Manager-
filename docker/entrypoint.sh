#!/bin/sh
# Migrate (and optionally seed) only when starting the API process.
set -eu

if [ "${1:-}" = "uvicorn" ]; then
  alembic upgrade head
  if [ "${SIMCORE_SEED_ON_START:-0}" = "1" ]; then
    python -m simcore.seed
  fi
fi

exec "$@"
