#!/bin/bash
# memnos nightly consolidation ("sleep pass") — distills new facts into entity dossiers.
# Dirty-only: idle namespaces are skipped, so this is cheap when nothing changed.
cd /Users/thameema/git/memnos
set -a; . /Users/thameema/git/memnos/.env 2>/dev/null; . /Users/thameema/git/memnos/.env 2>/dev/null; set +a
exec /Users/thameema/git/memnos/.venv/bin/python -u memnos_consolidate.py
