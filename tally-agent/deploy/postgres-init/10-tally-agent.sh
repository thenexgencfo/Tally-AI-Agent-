#!/bin/bash
# Runs once, on first start with an empty data volume (postgres entrypoint).
set -euo pipefail
PGSUPER="psql -U postgres" \
RO_PASS="$RO_PASS" OWNER_PASS="$OWNER_PASS" DB_NAME="${DB_NAME:-tally}" SEED="${SEED:-0}" \
  bash /opt/tally-agent/scripts/setup_db.sh
