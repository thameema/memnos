#!/usr/bin/env bash
# Stop hook — writes session state to engram at the end of every Claude Code session.
# Stage A: sparse git metadata (always runs, fast).
# Stage B: LLM summary via `claude --print` (no API key — uses existing Claude Code auth).
set -euo pipefail

HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HOOKS_DIR/engram.env" ] && source "$HOOKS_DIR/engram.env"

ENGRAM_API="${ENGRAM_API:-http://localhost:8766}"
ENGRAM_KEY="${ENGRAM_KEY:-}"

INPUT=$(cat)
CWD=$(echo        "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('cwd',''))"          2>/dev/null || echo "")
SESSION=$(echo    "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id',''))"   2>/dev/null || echo "")
TRANSCRIPT=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('transcript_path',''))" 2>/dev/null || echo "")

[[ -z "$CWD" ]] && exit 0

# Namespace resolution
ENGRAM_NS="${ENGRAM_DEFAULT_NS:-personal:me}"
REPO_ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null || echo "")
if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.engram" ]]; then
  FILE_NS=$(grep '^namespace=' "$REPO_ROOT/.engram" 2>/dev/null | cut -d= -f2 | tr -d ' ')
  [[ -n "$FILE_NS" ]] && ENGRAM_NS="$FILE_NS"
fi

PROJECT=$(basename "$CWD")
BRANCH="" RECENT_COMMITS="" UNCOMMITTED=0
if git -C "$CWD" rev-parse --git-dir >/dev/null 2>&1; then
  BRANCH=$(git -C "$CWD" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
  RECENT_COMMITS=$(git -C "$CWD" log --oneline -3 --no-decorate 2>/dev/null || echo "")
  UNCOMMITTED=$(git -C "$CWD" status --short 2>/dev/null | wc -l | tr -d ' ')
fi

# ── Stage A: sparse git metadata ─────────────────────────────────────────────
if [[ -n "$RECENT_COMMITS" ]]; then
  GIT_CONTENT="session ended | project: $PROJECT | dir: $CWD${BRANCH:+ | branch: $BRANCH} | uncommitted: $UNCOMMITTED
Recent commits:
$RECENT_COMMITS"
else
  GIT_CONTENT="session ended | project: $PROJECT | dir: $CWD"
fi

python3 -c "
import json, sys
content, ns, project, session_id = sys.argv[1:5]
print(json.dumps({
    'content':     content,
    'namespace':   ns,
    'memory_type': 'fact',
    'tags':        ['session-log', 'auto', project],
    'metadata':    {'session_id': session_id, 'project': project, 'source': 'stop-hook'},
    'provenance':  {'tool': 'claude-code-stop-hook', 'agent_id': session_id},
}))
" "$GIT_CONTENT" "$ENGRAM_NS" "$PROJECT" "$SESSION" 2>/dev/null \
| curl -sf --max-time 5 -X POST "$ENGRAM_API/api/v1/memory/" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $ENGRAM_KEY" \
    -H "X-Engram-Tool: stop-hook" \
    -d @- -o /dev/null 2>/dev/null || true

# ── Stage B: LLM summary via claude --print (no separate API key needed) ──────
command -v claude &>/dev/null || exit 0

# Find transcript
if [[ -z "$TRANSCRIPT" || ! -f "$TRANSCRIPT" ]]; then
  SLUG=$(python3 -c "print('${CWD}'.replace('/', '-'))" 2>/dev/null || echo "")
  TRANSCRIPT="$HOME/.claude/projects/$SLUG/$SESSION.jsonl"
fi
[[ ! -f "$TRANSCRIPT" ]] && exit 0

TURNS_TEXT=$(python3 - "$TRANSCRIPT" <<'PYEOF'
import json, sys
turns = []
try:
    with open(sys.argv[1]) as f:
        for line in f:
            try:
                d = json.loads(line.strip())
                role = d.get('type', '')
                if role not in ('user', 'assistant'): continue
                msg = d.get('message', {})
                content = msg.get('content', '')
                text = ''
                if isinstance(content, str): text = content
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get('type') == 'text':
                            text += c.get('text', '')
                text = text.strip()
                if len(text) > 20:
                    turns.append(f"{role.upper()}: {text[:500]}")
            except: continue
except: pass
print('\n\n'.join(turns[-8:]))
PYEOF
)

[[ -z "$TURNS_TEXT" ]] && exit 0

PROMPT="Project: $PROJECT${BRANCH:+  branch: $BRANCH}

[SESSION END]

$TURNS_TEXT

Write a dense session summary for future reference. Cover: what was accomplished, decisions made, files changed, errors fixed. Name specific tickets, files, and functions. Be concise (max 180 words). End with \"STATUS: <complete|in-progress|blocked>\".
IMPORTANT: respond with PLAIN TEXT ONLY. Do not generate any tool calls, <function_calls> XML, or <invoke> tags."

SUMMARY=$(echo "$PROMPT" | claude --print --no-session-persistence --strict-mcp-config --tools "" 2>/dev/null \
  | python3 -c "
import re, sys
t = sys.stdin.read()
t = re.sub(r'<function_calls>.*?</function_calls>', '', t, flags=re.DOTALL)
t = re.sub(r'<tool_call>.*?</tool_call>', '', t, flags=re.DOTALL)
t = re.sub(r'\n{3,}', '\n\n', t)
print(t.strip()[:1000])
" 2>/dev/null)
[[ -z "$SUMMARY" ]] && exit 0

RICH_CONTENT="[session-end] $PROJECT${BRANCH:+ | $BRANCH} — $SUMMARY"

python3 -c "
import json, sys
content, ns, project, session = sys.argv[1:5]
print(json.dumps({
    'content':     content,
    'namespace':   ns,
    'memory_type': 'session',
    'tags':        ['session-summary', 'auto', 'rich', project],
    'metadata':    {'session_id': session, 'project': project, 'source': 'stop-hook-rich'},
    'provenance':  {'tool': 'claude-code-stop-hook', 'agent_id': session},
}))
" "$RICH_CONTENT" "$ENGRAM_NS" "$PROJECT" "$SESSION" 2>/dev/null \
| curl -sf --max-time 8 -X POST "$ENGRAM_API/api/v1/memory/" \
    -H "Content-Type: application/json" \
    -H "Authorization: Bearer $ENGRAM_KEY" \
    -H "X-Engram-Tool: stop-hook-rich" \
    -d @- -o /dev/null 2>/dev/null || true

exit 0
