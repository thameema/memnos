#!/usr/bin/env bash
# PostToolUse hook — two jobs:
# 1. Git commits: written to engram immediately (real-time cross-session visibility)
# 2. Periodic auto-save: every 10 minutes of tool activity, background session save
#    Uses `claude --print` for summaries — no separate API key required.
set -euo pipefail

HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HOOKS_DIR/engram.env" ] && source "$HOOKS_DIR/engram.env"

ENGRAM_API="${ENGRAM_API:-http://localhost:8766}"
ENGRAM_KEY="${ENGRAM_KEY:-}"

INPUT=$(cat)

TOOL=$(echo    "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_name',''))"                    2>/dev/null || echo "")
CMD=$(echo     "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_input',{}).get('command',''))" 2>/dev/null || echo "")
OUTPUT=$(echo  "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('tool_response',''))"                2>/dev/null || echo "")
CWD=$(echo     "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('cwd',''))"                         2>/dev/null || echo "")
SESSION=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id',''))"                  2>/dev/null || echo "")

[[ -z "$SESSION" ]] && exit 0

# ── Namespace resolution ──────────────────────────────────────────────────────
ENGRAM_NS="${ENGRAM_DEFAULT_NS:-personal:me}"
if [[ -n "$CWD" ]]; then
  REPO_ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null || echo "")
  if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.engram" ]]; then
    FILE_NS=$(grep '^namespace=' "$REPO_ROOT/.engram" 2>/dev/null | cut -d= -f2 | tr -d ' ')
    [[ -n "$FILE_NS" ]] && ENGRAM_NS="$FILE_NS"
  fi
fi

PROJECT=$(basename "${CWD:-unknown}")
BRANCH=$(git -C "${CWD:-.}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")

# ── Job 1: git commit capture ─────────────────────────────────────────────────
if [[ "$TOOL" == "Bash" ]] && echo "$CMD" | grep -q 'git commit'; then
  COMMIT_LINE=$(echo "$OUTPUT" | grep -E '^\[' | head -1 || echo "")
  [[ -z "$COMMIT_LINE" ]] && COMMIT_LINE=$(echo "$CMD" | grep -oE '"[^"]{10,}"' | head -1 | tr -d '"' || echo "")
  CONTENT="$PROJECT${BRANCH:+ | $BRANCH} — committed: $COMMIT_LINE"

  python3 -c "
import json, sys
content, ns, project, session = sys.argv[1:5]
print(json.dumps({
    'content':     content,
    'namespace':   ns,
    'memory_type': 'fact',
    'tags':        ['git-commit', 'real-time', 'auto', project],
    'metadata':    {'session_id': session, 'project': project, 'source': 'post-tool-hook'},
    'provenance':  {'tool': 'claude-code-post-tool-hook', 'agent_id': session},
}))
" "$CONTENT" "$ENGRAM_NS" "$PROJECT" "$SESSION" 2>/dev/null \
  | curl -sf --max-time 4 -X POST "$ENGRAM_API/api/v1/memory/" \
      -H "Content-Type: application/json" \
      -H "Authorization: Bearer $ENGRAM_KEY" \
      -H "X-Engram-Tool: post-tool-hook" \
      -d @- -o /dev/null 2>/dev/null || true
fi

# ── Job 2: time-based auto-save every N minutes ───────────────────────────────
COUNTER_FILE="/tmp/engram_counter_${SESSION}"
COUNT=$(cat "$COUNTER_FILE" 2>/dev/null || echo 0)
COUNT=$((COUNT + 1))
echo "$COUNT" > "$COUNTER_FILE"

SAVE_INTERVAL_MINUTES="${ENGRAM_AUTOSAVE_MINUTES:-10}"
LAST_SAVE_FILE="/tmp/engram_lastsave_${SESSION}"
LAST_SAVE=$(cat "$LAST_SAVE_FILE" 2>/dev/null || echo 0)
NOW=$(date +%s)
ELAPSED=$(( NOW - LAST_SAVE ))

if [[ $ELAPSED -ge $(( SAVE_INTERVAL_MINUTES * 60 )) ]]; then
  echo "$NOW" > "$LAST_SAVE_FILE"

  # Require claude CLI for summaries
  command -v claude &>/dev/null || exit 0

  TRANSCRIPT=$(echo "$INPUT" | python3 -c \
    "import sys,json; d=json.load(sys.stdin); print(d.get('transcript_path',''))" 2>/dev/null || echo "")
  if [[ -z "$TRANSCRIPT" || ! -f "$TRANSCRIPT" ]]; then
    SLUG=$(python3 -c "print('${CWD}'.replace('/', '-'))" 2>/dev/null || echo "")
    TRANSCRIPT="$HOME/.claude/projects/$SLUG/$SESSION.jsonl"
  fi

  if [[ -f "$TRANSCRIPT" ]]; then
    # Run in background so it never blocks the tool response
    (
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
print('\n\n'.join(turns[-12:]))
PYEOF
      )

      [[ -z "$TURNS_TEXT" ]] && exit 0

      PROMPT="Project: $PROJECT${BRANCH:+  branch: $BRANCH}  [auto-save #$COUNT]

$TURNS_TEXT

Capture this in-progress dev session for another agent to resume. Write a dense, specific summary: what has been done, what is currently being worked on, decisions made, errors seen, current status. Name specific tickets, files, functions. Be concise (max 200 words). End with \"STATUS: <in-progress|blocked|complete>\".
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

      CONTENT="[auto-save #$COUNT] $PROJECT${BRANCH:+ | $BRANCH} — $SUMMARY"

      python3 -c "
import json, sys
content, ns, project, session, count = sys.argv[1:6]
print(json.dumps({
    'content':     content,
    'namespace':   ns,
    'memory_type': 'session',
    'tags':        ['session-summary', 'auto-periodic', 'real-time', project],
    'metadata':    {'session_id': session, 'project': project, 'elapsed_minutes': int(count), 'source': 'periodic-autosave'},
    'provenance':  {'tool': 'periodic-autosave', 'agent_id': session},
}))
" "$CONTENT" "$ENGRAM_NS" "$PROJECT" "$SESSION" "$COUNT" 2>/dev/null \
      | curl -sf --max-time 5 -X POST "$ENGRAM_API/api/v1/memory/" \
          -H "Content-Type: application/json" \
          -H "Authorization: Bearer $ENGRAM_KEY" \
          -H "X-Engram-Tool: periodic-autosave" \
          -d @- -o /dev/null 2>/dev/null || true
    ) &
    disown
  fi
fi

exit 0
