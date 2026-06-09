#!/bin/bash
cd /Users/thameema/git/memnos
set -a; . /Users/thameema/git/memnos/.env 2>/dev/null; set +a
exec /Users/thameema/git/memnos/.venv/bin/python memnos_eval.py
