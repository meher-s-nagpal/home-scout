#!/usr/bin/env bash
# Home Scout daily runner. Usage: ./run.sh [--dry-run] [--no-email]
set -euo pipefail
cd "$(dirname "$0")"
export TZ=America/Los_Angeles
mkdir -p logs
# optional secrets for email (never committed): see .env.example
[ -f .env ] && set -a && . ./.env && set +a
[ -x .venv/bin/python ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
echo "=== $(date '+%F %T %Z') run start ===" >> logs/cron.log
rc=0; .venv/bin/python scout.py "$@" >> logs/cron.log 2>&1 || rc=$?
echo "=== $(date '+%F %T %Z') run end (exit $rc) ===" >> logs/cron.log
exit $rc
