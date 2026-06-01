#!/usr/bin/env bash
# ~/.claude/hooks/memnos-inject.sh
# UserPromptSubmit hook — injects relevant memnos memories before every prompt.
# Uses ns=all: memnos finds the best match across all accessible namespaces.
# No namespace routing needed here — the server handles it.

set -euo pipefail

HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HOOKS_DIR/memnos.env" ] && source "$HOOKS_DIR/memnos.env"

# Launch heartbeat daemon if not already running (cross-platform: Mac/Linux/Windows)
# The daemon runs in the background and handles abrupt exits — cron-free, OS-agnostic.
python3 "$HOOKS_DIR/memnos-heartbeat.py" 2>/dev/null &

MEMNOS_API="${MEMNOS_API:-http://localhost:8766}"
MEMNOS_KEY="${MEMNOS_KEY:-}"
MEMNOS_TOP_K="${MEMNOS_TOP_K:-3}"
MEMNOS_MIN_SCORE="${MEMNOS_MIN_SCORE:-0.50}"

INPUT=$(cat)
PROMPT=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('prompt',''))" 2>/dev/null || echo "")

# Skip trivially short prompts (greetings, single words) — nothing useful to retrieve
PROMPT_LEN=${#PROMPT}
[[ $PROMPT_LEN -lt 15 ]] && exit 0

# ── Secret pattern detection ──────────────────────────────────────────────────
# Scan the prompt for credential patterns and warn Claude to vault them.
VAULT_ALERT=$(echo "$PROMPT" | python3 -c "
import sys, re
PATTERNS = [
    (r'sk-ant-api[0-9A-Za-z\-]{20,}',                                                  'Anthropic API key'),
    (r'\bsk-[0-9A-Za-z]{40,}',                                                         'API key (sk-)'),
    (r'\bghp_[0-9A-Za-z]{36,}',                                                        'GitHub personal token'),
    (r'\bghs_[0-9A-Za-z]{36,}',                                                        'GitHub service token'),
    (r'\bAKIA[0-9A-Z]{16}\b',                                                          'AWS access key'),
    (r'-----BEGIN [A-Z ]+ PRIVATE KEY-----',                                            'private key PEM'),
    (r'ey[A-Za-z0-9_-]{20,}\.ey[A-Za-z0-9_-]{20,}',                                  'JWT token'),
    (r'(?i)(?:password|api[_-]?key|access[_-]?key|auth[_-]?token|client[_-]?secret)\s*[=:]\s*[\"\']+(?!\s)[^\s\"\']{16,}', 'credential assignment'),
]
text = sys.stdin.read()
found = []
for pattern, label in PATTERNS:
    if re.search(pattern, text):
        found.append(label)
if found:
    types = ', '.join(found)
    print(f'[vault-alert] Potential secret in prompt ({types}) — save to memnos vault before use: vault_secret_set(key_name=\"<name>\", value=\"<value>\", namespace=\"...\")')
" 2>/dev/null || echo "")

QUERY=$(echo "$PROMPT" | head -c 200 | python3 -c \
  "import sys,urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip()))" 2>/dev/null || echo "")
[[ -z "$QUERY" ]] && exit 0

# Single call — no health pre-check, no namespace routing. 3s hard timeout.
RESPONSE=$(curl -sf --max-time 3 \
  "$MEMNOS_API/api/v1/memory/search?q=$QUERY&ns=all&top_k=$MEMNOS_TOP_K" \
  -H "Authorization: Bearer $MEMNOS_KEY" 2>/dev/null || echo "[]")

CONTEXT=$(echo "$RESPONSE" | python3 -c "
import sys, json
MIN_SCORE = float('$MEMNOS_MIN_SCORE')
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
results = data if isinstance(data, list) else data.get('results', [])
# Filter below minimum score — don't inject noise
results = [r for r in results if isinstance(r.get('score'), float) and r['score'] >= MIN_SCORE]
if not results:
    sys.exit(0)
lines = ['[memnos context]']
for r in results:
    mem = r.get('memory', r)
    mtype = mem.get('memory_type', 'fact')
    content = mem.get('content', '').strip()
    score = r.get('score', 0)
    ns = mem.get('namespace', '')
    if content:
        lines.append(f'[{mtype} {score:.2f}] {content[:150]}')
if len(lines) <= 1:
    sys.exit(0)
print('\n'.join(lines))
" 2>/dev/null || echo "")

# Merge memnos context and vault alert — either or both may be present
if [[ -n "$CONTEXT" && -n "$VAULT_ALERT" ]]; then
  FULL_CONTEXT="$CONTEXT
$VAULT_ALERT"
elif [[ -n "$CONTEXT" ]]; then
  FULL_CONTEXT="$CONTEXT"
elif [[ -n "$VAULT_ALERT" ]]; then
  FULL_CONTEXT="$VAULT_ALERT"
else
  exit 0
fi

python3 -c "
import json, sys
print(json.dumps({'hookSpecificOutput':{'hookEventName':'UserPromptSubmit','additionalContext':sys.argv[1]}}))
" "$FULL_CONTEXT"
