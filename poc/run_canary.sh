#!/bin/bash
cd /Users/thameema/git/memnos/poc
set -a; . /Users/thameema/git/memnos/.env 2>/dev/null; set +a
exec /Users/thameema/git/memnos/poc/.venv/bin/python memnos_eval.py
