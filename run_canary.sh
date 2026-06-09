#!/bin/bash
# memnos nightly accuracy canary — small LoCoMo probe to catch retrieval regressions.
cd /Users/thameema/git/memnos
set -a; . /Users/thameema/git/memnos/.env 2>/dev/null; set +a
export MEMNOS_DSN="${MEMNOS_DSN:-postgresql://memnos:REDACTED_LOCAL_DB_PASSWORD@localhost:5432/memnos}"
exec /Users/thameema/.local/pipx/venvs/memnos/bin/python memnos_eval.py
