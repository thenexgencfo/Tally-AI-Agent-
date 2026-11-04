#!/usr/bin/env bash
# Rebuild the demo database and run the full suite.
set -euo pipefail
cd "$(dirname "$0")/.."
sudo -u postgres psql -q -d postgres -c "DROP DATABASE IF EXISTS tally;" >/dev/null
ALLOW_WEAK_PASSWORDS=1 RO_PASS=devro OWNER_PASS=devowner SEED=1 ./scripts/setup_db.sh >/dev/null 2>&1
exec ./.venv/bin/python -m pytest "$@"
