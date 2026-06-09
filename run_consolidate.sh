#!/bin/bash
# memnos nightly consolidation ("sleep pass") — distills new facts into entity dossiers.
# Dirty-only: idle namespaces are skipped, so this is cheap when nothing changed.
cd /Users/thameema/git/memnos
set -a; . /Users/thameema/git/memnos/.env 2>/dev/null; set +a
export MEMNOS_DSN="${MEMNOS_DSN:-postgresql://memnos:REDACTED_LOCAL_DB_PASSWORD@localhost:5432/memnos}"
exec /Users/thameema/.local/pipx/venvs/memnos/bin/python -u memnos_consolidate.py
