#!/usr/bin/env bash
# engram client installer
#
# Installs Claude Code automation hooks on the current machine.
# Works whether engram runs locally or on a remote server.
#
# Usage:
#   ./install-client.sh
#   ./install-client.sh --server http://host:8766 --key engram-abc123
#   ./install-client.sh --server http://localhost:8766 --key engram-abc123 --namespace personal:me
#
# Supports: macOS, Linux, WSL (Windows Subsystem for Linux)
# For native Windows (PowerShell): use install-client.ps1 instead.

set -euo pipefail

# ─── Colors ───────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

info()    { echo -e "${CYAN}  -->${NC} $*"; }
success() { echo -e "${GREEN}  [ok]${NC} $*"; }
warn()    { echo -e "${YELLOW}  [!]${NC} $*"; }
error()   { echo -e "${RED}  [error]${NC} $*" >&2; }
die()     { error "$*"; exit 1; }
step()    { echo ""; echo -e "${BOLD}>>> $*${NC}"; }
ask() {
  local varname="$1" prompt="$2" default="${3:-}"
  if [ -n "$default" ]; then
    echo -ne "${CYAN}  ?${NC} ${prompt} ${DIM}[${default}]${NC}: "
  else
    echo -ne "${CYAN}  ?${NC} ${prompt}: "
  fi
  read -r input </dev/tty
  eval "$varname='${input:-$default}'"
}
ask_yn() {
  local varname="$1" prompt="$2" default="${3:-Y}"
  echo -ne "${CYAN}  ?${NC} ${prompt} ${DIM}[${default}]${NC}: "
  read -r input </dev/tty
  input="${input:-$default}"
  [[ "$input" =~ ^[Yy] ]] && eval "$varname=yes" || eval "$varname=no"
}

# ─── Parse arguments ──────────────────────────────────────────────────────────
ARG_SERVER="" ARG_KEY="" ARG_NS=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --server)         ARG_SERVER="$2"; shift 2 ;;
    --key)            ARG_KEY="$2";    shift 2 ;;
    --namespace|--ns) ARG_NS="$2";     shift 2 ;;
    *) warn "Unknown argument: $1"; shift ;;
  esac
done

# ─── Banner ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${BLUE}"
cat <<'BANNER'
   ___    _  _    ___   ___     _     __  __
  | __|  | \| |  / __| | _ \   /_\   |  \/  |
  | _|   | .` | | (_ | |   /  / _ \  | |\/| |
  |___|  |_|\_| \____| |_|\_\ /_/ \_ |_|  |_|
  Client Installer  (Claude Code hooks)
BANNER
echo -e "${NC}"

# ─── OS detection ─────────────────────────────────────────────────────────────
detect_os() {
  case "$(uname -s)" in
    Darwin) OS="macos" ;;
    Linux)
      if grep -qi microsoft /proc/version 2>/dev/null; then OS="wsl"
      else OS="linux"; fi ;;
    *)
      if [[ "$(uname -s)" == MINGW* ]] || [[ "$(uname -s)" == CYGWIN* ]]; then
        OS="gitbash"
      else
        die "Unsupported shell environment. On Windows, use install-client.ps1 or Git Bash."
      fi ;;
  esac
  info "Detected: ${OS}"
}

# ─── Check Claude Code is installed ───────────────────────────────────────────
check_claude_code() {
  step "Checking Claude Code"
  if [ -f "$HOME/.claude/settings.json" ]; then
    CLAUDE_SETTINGS="$HOME/.claude/settings.json"
    success "Claude Code found: $CLAUDE_SETTINGS"
  else
    warn "~/.claude/settings.json not found."
    warn "Install Claude Code first: https://claude.ai/code"
    warn "Continuing anyway — hooks will be installed but not registered."
    CLAUDE_SETTINGS=""
  fi
  CLAUDE_HOOKS_DIR="$HOME/.claude/hooks"
  CLAUDE_COMMANDS_DIR="$HOME/.claude/commands"
}

# ─── Collect config ───────────────────────────────────────────────────────────
collect_config() {
  step "engram connection"

  if [ -n "$ARG_SERVER" ]; then
    ENGRAM_SERVER="$ARG_SERVER"
    info "Server: $ENGRAM_SERVER (from --server)"
  else
    ask ENGRAM_SERVER "engram server URL" "http://localhost:8766"
  fi
  ENGRAM_SERVER="${ENGRAM_SERVER%/}"

  if [ -n "$ARG_KEY" ]; then
    ENGRAM_API_KEY="$ARG_KEY"
    info "API key: provided via --key"
  else
    ask ENGRAM_API_KEY "engram API key" ""
    [ -z "$ENGRAM_API_KEY" ] && die "API key required. Get it from the server's .env file or admin."
  fi

  if [ -n "$ARG_NS" ]; then
    DEFAULT_NS="$ARG_NS"
  else
    ask DEFAULT_NS "Default namespace" "personal:me"
  fi
}

# ─── Test server connectivity ─────────────────────────────────────────────────
test_connection() {
  step "Testing server connection"
  if curl -sf --max-time 5 "${ENGRAM_SERVER}/api/v1/admin/health" \
    -H "X-API-Key: ${ENGRAM_API_KEY}" -o /dev/null 2>/dev/null; then
    success "Connected to engram at ${ENGRAM_SERVER}"
  else
    warn "Could not reach ${ENGRAM_SERVER} — hooks will still be installed."
    warn "Hooks fail silently when the server is unreachable, so this is safe."
  fi
}

# ─── Install hooks ─────────────────────────────────────────────────────────────
install_hooks() {
  step "Installing Claude Code hooks"

  mkdir -p "$CLAUDE_HOOKS_DIR" "$CLAUDE_COMMANDS_DIR"

  # ── engram.env config file ─────────────────────────────────────────────────
  cat > "$CLAUDE_HOOKS_DIR/engram.env" <<ENV
# engram hook config — edit to change server, API key, namespace, or tuning.
ENGRAM_API=${ENGRAM_SERVER}
ENGRAM_KEY=${ENGRAM_API_KEY}
ENGRAM_DEFAULT_NS=${DEFAULT_NS}
ENGRAM_TOP_K=8
ENGRAM_MIN_SCORE=0.50
ENGRAM_AUTOSAVE_MINUTES=10
ENGRAM_HEARTBEAT_MINUTES=10
# LLM summaries use claude --print (no API key needed)
ENV
  success "Config: $CLAUDE_HOOKS_DIR/engram.env"

  # ── inject hook (UserPromptSubmit) ─────────────────────────────────────────
  cat > "$CLAUDE_HOOKS_DIR/engram-inject.sh" <<'INJECT'
#!/usr/bin/env bash
# UserPromptSubmit hook — injects relevant engram memories before every prompt.
# Searches ns=all so results come from every namespace the key can access.
set -euo pipefail

HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HOOKS_DIR/engram.env" ] && source "$HOOKS_DIR/engram.env"

# Launch the background heartbeat daemon (cross-platform: Mac/Linux/Windows)
# It runs once per machine, handles abrupt exits via transcript scanning.
python3 "$HOOKS_DIR/engram-heartbeat.py" 2>/dev/null &

ENGRAM_API="${ENGRAM_API:-http://localhost:8766}"
ENGRAM_KEY="${ENGRAM_KEY:-}"
ENGRAM_TOP_K="${ENGRAM_TOP_K:-8}"
ENGRAM_MIN_SCORE="${ENGRAM_MIN_SCORE:-0.50}"

INPUT=$(cat)
PROMPT=$(echo "$INPUT" | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print(d.get('prompt',''))" 2>/dev/null || echo "")

# Skip trivially short prompts — nothing useful to retrieve
PROMPT_LEN=${#PROMPT}
[[ $PROMPT_LEN -lt 15 ]] && exit 0

# ── Secret pattern detection ──────────────────────────────────────────────────
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
    print(f'[vault-alert] Potential secret in prompt ({types}) — save to engram vault before use: vault_secret_set(key_name=\"<name>\", value=\"<value>\", namespace=\"...\")')
" 2>/dev/null || echo "")

QUERY=$(echo "$PROMPT" | head -c 200 | python3 -c \
  "import sys,urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip()))" 2>/dev/null || echo "")
[[ -z "$QUERY" ]] && exit 0

# Single call — ns=all: server searches every accessible namespace. 3s hard timeout.
RESPONSE=$(curl -sf --max-time 3 \
  "$ENGRAM_API/api/v1/memory/search?q=$QUERY&ns=all&top_k=$ENGRAM_TOP_K" \
  -H "X-API-Key: $ENGRAM_KEY" 2>/dev/null || echo "[]")

CONTEXT=$(echo "$RESPONSE" | python3 -c "
import sys, json
MIN_SCORE = float('$ENGRAM_MIN_SCORE')
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
results = data if isinstance(data, list) else data.get('results', [])
results = [r for r in results if isinstance(r.get('score'), float) and r['score'] >= MIN_SCORE]
if not results:
    sys.exit(0)
lines = ['[engram context]']
for r in results:
    mem = r.get('memory', r)
    mtype = mem.get('memory_type', 'fact')
    content = mem.get('content', '').strip()
    score = r.get('score', 0)
    if content:
        lines.append(f'[{mtype} {score:.2f}] {content[:150]}')
if len(lines) <= 1:
    sys.exit(0)
print('\n'.join(lines))
" 2>/dev/null || echo "")

# Merge engram context and vault alert — either or both may be present
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
INJECT
  chmod +x "$CLAUDE_HOOKS_DIR/engram-inject.sh"
  success "Inject hook: $CLAUDE_HOOKS_DIR/engram-inject.sh"

  # ── heartbeat daemon (cross-platform Python) ───────────────────────────────
  cat > "$CLAUDE_HOOKS_DIR/engram-heartbeat.py" <<'HEARTBEAT'
#!/usr/bin/env python3
"""
engram-heartbeat.py — cross-platform background daemon (Mac / Linux / Windows).

Launched once per machine by the UserPromptSubmit hook. Uses a PID file so only
one instance ever runs. Every 10 minutes it scans all Claude Code transcript files
modified recently, generates a session summary via Claude Haiku, and writes it to
engram. This is the safety net for Ctrl+C, power loss, kill -9, and abrupt exits —
the transcript is always on disk even when the session dies, so this daemon catches
everything the in-process hooks miss.
"""

import json
import os
import pathlib
import platform
import signal
import sys
import time
import urllib.request

# ── Config (read from engram.env if present) ─────────────────────────────────
def load_env():
    env = {}
    env_path = pathlib.Path(__file__).parent / "engram.env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    return env

cfg = load_env()
ENGRAM_API = cfg.get("ENGRAM_API",        os.environ.get("ENGRAM_API",        "http://localhost:8766"))
ENGRAM_KEY = cfg.get("ENGRAM_KEY",        os.environ.get("ENGRAM_KEY",        ""))
DEFAULT_NS = cfg.get("ENGRAM_DEFAULT_NS", os.environ.get("ENGRAM_DEFAULT_NS", "personal:me"))
INTERVAL   = int(cfg.get("ENGRAM_HEARTBEAT_MINUTES", "10")) * 60

# ── PID file — one daemon per machine ────────────────────────────────────────
TMP       = pathlib.Path(os.environ.get("TEMP", "/tmp"))
PID_FILE  = TMP / "engram_heartbeat.pid"
MARK_FILE = TMP / "engram_heartbeat_marker"

def already_running() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        if platform.system() == "Windows":
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        else:
            os.kill(pid, 0)
            return True
    except (ProcessLookupError, ValueError, OSError):
        return False

def write_pid():
    PID_FILE.write_text(str(os.getpid()))

def cleanup(*_):
    PID_FILE.unlink(missing_ok=True)
    sys.exit(0)

# ── Transcript discovery ──────────────────────────────────────────────────────
def find_active_transcripts(since_seconds: int = 900) -> list:
    projects_dir = pathlib.Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return []
    cutoff = time.time() - since_seconds
    return [
        p for p in projects_dir.rglob("*.jsonl")
        if p.stat().st_mtime > cutoff
    ]

# ── Extract recent turns from transcript ─────────────────────────────────────
def extract_turns(transcript: pathlib.Path, max_turns: int = 12) -> list:
    turns = []
    try:
        for line in transcript.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                role = d.get("type", "")
                if role not in ("user", "assistant"):
                    continue
                msg = d.get("message", {})
                content = msg.get("content", "")
                text = ""
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            text += c.get("text", "")
                text = text.strip()
                if len(text) > 20:
                    turns.append(f"{role.upper()}: {text[:500]}")
            except Exception:
                continue
    except Exception:
        pass
    return turns[-max_turns:]

# ── Generate summary via claude --print (no API key needed) ──────────────────
def summarise(turns: list, project: str, branch: str) -> str:
    import shutil, subprocess
    if shutil.which("claude") is None or len(turns) < 2:
        return ""
    prompt = (
        f"Project: {project}" + (f"  branch: {branch}" if branch else "") +
        "\n\n[HEARTBEAT — session may have ended abruptly]\n\n" +
        "\n\n".join(turns) +
        '\n\nCapture this session for recovery. Write a dense, specific summary: '
        "what was being worked on, current status, any in-progress changes, last known state. "
        "Name tickets, files, functions. Be concise (max 180 words). "
        'End with "STATUS: <in-progress|blocked|complete|unknown>".'
    )
    try:
        result = subprocess.run(
            ["claude", "--print", "--no-session-persistence", "--strict-mcp-config", "--tools", ""],
            input=prompt, capture_output=True, text=True, timeout=60
        )
        return result.stdout.strip()[:800]
    except Exception:
        return ""

# ── Write memory to engram ────────────────────────────────────────────────────
def write_memory(content: str, namespace: str, project: str, session_id: str):
    if not ENGRAM_KEY:
        return
    payload = json.dumps({
        "content": content,
        "namespace": namespace,
        "memory_type": "session",
        "tags": ["session-summary", "heartbeat", "auto", project],
        "metadata": {"session_id": session_id, "project": project, "source": "heartbeat-daemon"},
        "provenance": {"tool": "engram-heartbeat-daemon", "agent_id": session_id},
    }).encode()
    req = urllib.request.Request(
        f"{ENGRAM_API}/api/v1/memory/",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-API-Key": ENGRAM_KEY,
            "X-Engram-Tool": "heartbeat-daemon",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=5)
    except Exception:
        pass

# ── Per-session last-save tracking ───────────────────────────────────────────
last_saved: dict = {}

def process_transcript(transcript: pathlib.Path):
    session_id = transcript.stem
    now = time.time()
    if now - last_saved.get(session_id, 0) < 480:
        return

    slug = transcript.parent.name
    cwd  = slug.replace("-", "/", 1) if slug.startswith("-") else slug
    project = pathlib.Path(cwd).name or "unknown"
    branch  = ""
    try:
        import subprocess
        branch = subprocess.check_output(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        pass

    ns = DEFAULT_NS
    engram_file = pathlib.Path(cwd) / ".engram"
    if engram_file.exists():
        for line in engram_file.read_text().splitlines():
            if line.startswith("namespace="):
                ns = line.split("=", 1)[1].strip()
                break

    turns = extract_turns(transcript)
    summary = summarise(turns, project, branch)
    if not summary:
        return

    content = f"[heartbeat] {project}" + (f" | {branch}" if branch else "") + f" — {summary}"
    write_memory(content, ns, project, session_id)
    last_saved[session_id] = now

def main():
    if already_running():
        sys.exit(0)

    if platform.system() != "Windows":
        try:
            if os.fork() > 0:
                sys.exit(0)
        except AttributeError:
            pass

    write_pid()
    signal.signal(signal.SIGTERM, cleanup)
    try:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    except AttributeError:
        pass

    MARK_FILE.touch()

    while True:
        try:
            transcripts = find_active_transcripts(since_seconds=INTERVAL + 300)
            for t in transcripts:
                try:
                    process_transcript(t)
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
HEARTBEAT
  chmod +x "$CLAUDE_HOOKS_DIR/engram-heartbeat.py"
  success "Heartbeat daemon: $CLAUDE_HOOKS_DIR/engram-heartbeat.py"

  # ── git-write hook (PostToolUse) ───────────────────────────────────────────
  cat > "$CLAUDE_HOOKS_DIR/engram-git-write.sh" <<'GITWRITE'
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
      -H "X-API-Key: $ENGRAM_KEY" \
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
          -H "X-API-Key: $ENGRAM_KEY" \
          -H "X-Engram-Tool: periodic-autosave" \
          -d @- -o /dev/null 2>/dev/null || true
    ) &
    disown
  fi
fi

exit 0
GITWRITE
  chmod +x "$CLAUDE_HOOKS_DIR/engram-git-write.sh"
  success "Git+periodic hook: $CLAUDE_HOOKS_DIR/engram-git-write.sh"

  # ── precompact hook (PreCompact) ───────────────────────────────────────────
  cat > "$CLAUDE_HOOKS_DIR/engram-precompact.sh" <<'PRECOMPACT'
#!/usr/bin/env bash
# PreCompact hook — saves session state to engram before Claude Code compacts context.
# Uses `claude --print` for LLM summarization — no separate API key required.
set -euo pipefail

HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HOOKS_DIR/engram.env" ] && source "$HOOKS_DIR/engram.env"

ENGRAM_API="${ENGRAM_API:-http://localhost:8766}"
ENGRAM_KEY="${ENGRAM_KEY:-}"

INPUT=$(cat)
CWD=$(echo        "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('cwd',''))"          2>/dev/null || echo "")
SESSION=$(echo    "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id',''))"   2>/dev/null || echo "")
TRANSCRIPT=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('transcript_path',''))" 2>/dev/null || echo "")

[[ -z "$SESSION" ]] && exit 0

# claude CLI required for summaries
command -v claude &>/dev/null || exit 0

# Namespace resolution
ENGRAM_NS="${ENGRAM_DEFAULT_NS:-personal:me}"
REPO_ROOT=$(git -C "${CWD:-.}" rev-parse --show-toplevel 2>/dev/null || echo "")
if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.engram" ]]; then
  FILE_NS=$(grep '^namespace=' "$REPO_ROOT/.engram" 2>/dev/null | cut -d= -f2 | tr -d ' ')
  [[ -n "$FILE_NS" ]] && ENGRAM_NS="$FILE_NS"
fi

PROJECT=$(basename "${CWD:-unknown}")
BRANCH=$(git -C "${CWD:-.}" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")

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
print('\n\n'.join(turns[-12:]))
PYEOF
)

[[ -z "$TURNS_TEXT" ]] && exit 0

PROMPT="Project: $PROJECT${BRANCH:+  branch: $BRANCH}

[PRE-COMPACT — context window approaching limit]

$TURNS_TEXT

Capture this in-progress dev session before context is compacted. Write a dense, specific summary: what has been done, what is currently in progress, decisions made, errors encountered, exact current state. Name tickets, files, functions. Be concise (max 200 words). End with \"STATUS: <in-progress|blocked|complete>\".
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

CONTENT="[pre-compact] $PROJECT${BRANCH:+ | $BRANCH} — $SUMMARY"

python3 -c "
import json, sys
content, ns, project, session = sys.argv[1:5]
print(json.dumps({
    'content':     content,
    'namespace':   ns,
    'memory_type': 'session',
    'tags':        ['session-summary', 'auto-compact', 'real-time', project],
    'metadata':    {'session_id': session, 'project': project, 'source': 'pre-compact-hook'},
    'provenance':  {'tool': 'engram-precompact-hook', 'agent_id': session},
}))
" "$CONTENT" "$ENGRAM_NS" "$PROJECT" "$SESSION" 2>/dev/null \
| curl -sf --max-time 8 -X POST "$ENGRAM_API/api/v1/memory/" \
    -H "Content-Type: application/json" \
    -H "X-API-Key: $ENGRAM_KEY" \
    -H "X-Engram-Tool: precompact-hook" \
    -d @- -o /dev/null 2>/dev/null || true

exit 0
PRECOMPACT
  chmod +x "$CLAUDE_HOOKS_DIR/engram-precompact.sh"
  success "PreCompact hook: $CLAUDE_HOOKS_DIR/engram-precompact.sh"

  # ── session-write hook (Stop) ──────────────────────────────────────────────
  cat > "$CLAUDE_HOOKS_DIR/engram-session-write.sh" <<'SESSION'
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
    -H "X-API-Key: $ENGRAM_KEY" \
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
    -H "X-API-Key: $ENGRAM_KEY" \
    -H "X-Engram-Tool: stop-hook-rich" \
    -d @- -o /dev/null 2>/dev/null || true

exit 0
SESSION
  chmod +x "$CLAUDE_HOOKS_DIR/engram-session-write.sh"
  success "Session hook: $CLAUDE_HOOKS_DIR/engram-session-write.sh"

  # ── slash command ──────────────────────────────────────────────────────────
  cat > "$CLAUDE_COMMANDS_DIR/engram.md" <<'CMD'
# /engram [save|status|ns:<namespace>]

---

## /engram status

Run these bash commands, then format the results as shown below.

```bash
# 1. All namespaces
curl -sf "$(grep '^ENGRAM_API=' ~/.claude/hooks/engram.env | cut -d= -f2)/api/v1/admin/namespaces" \
  -H "X-API-Key: $(grep '^ENGRAM_KEY=' ~/.claude/hooks/engram.env | cut -d= -f2)" \
  2>/dev/null || echo "[]"

# 2. Current namespace for this project
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo "")
if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.engram" ]]; then
  echo "source:file"; grep '^namespace=' "$REPO_ROOT/.engram" | cut -d= -f2
else
  echo "source:default"; grep '^ENGRAM_DEFAULT_NS=' ~/.claude/hooks/engram.env | cut -d= -f2 | tr -d ' '
fi

# 3. Recent memories (last 5)
NS=$(grep '^ENGRAM_DEFAULT_NS=' ~/.claude/hooks/engram.env | cut -d= -f2 | tr -d ' ')
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo "")
[[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.engram" ]] && NS=$(grep '^namespace=' "$REPO_ROOT/.engram" | cut -d= -f2 | tr -d ' ')
API=$(grep '^ENGRAM_API=' ~/.claude/hooks/engram.env | cut -d= -f2)
KEY=$(grep '^ENGRAM_KEY=' ~/.claude/hooks/engram.env | cut -d= -f2)
curl -sf "$API/api/v1/memory/search?q=session+commit+work&ns=$NS&top_k=5" \
  -H "X-API-Key: $KEY" 2>/dev/null || echo "[]"
```

**engram status**

**Namespaces** — bullet list of all namespace names

**Current namespace** — name + how resolved (.engram file / env default).
If $ARGUMENTS contains `ns:something`, show: `echo 'namespace=something' > .engram`

**Recent memories** — up to 5 as: `[type] score — first 120 chars`

---

## /engram save

Persist this entire session to engram as raw, searchable chunks.

**Use the conversation in your current context window. Do NOT read transcript files.**
Write content as-is — do NOT summarize or compress. Cover the full session chronologically.
Do NOT stop early — every task, finding, decision, error, and fix must be captured.

### Steps

**1. Read config:**
```bash
KEY=$(grep '^ENGRAM_KEY=' ~/.claude/hooks/engram.env | cut -d= -f2)
API=$(grep '^ENGRAM_API=' ~/.claude/hooks/engram.env | cut -d= -f2)
NS=$(grep '^ENGRAM_DEFAULT_NS=' ~/.claude/hooks/engram.env | cut -d= -f2)
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo "")
[[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.engram" ]] && NS=$(grep '^namespace=' "$REPO_ROOT/.engram" | cut -d= -f2 | tr -d ' ')
PROJECT=$(basename "${REPO_ROOT:-$(pwd)}")
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
echo "NS=$NS  PROJECT=$PROJECT  BRANCH=$BRANCH"
```

**2. Split the session into ~300-word raw chunks** covering the full conversation. For each chunk, write it using this Python (handles escaping safely):

```python
import json, urllib.request, os, subprocess

def cfg(key, default=''):
    try:
        for line in open(os.path.expanduser('~/.claude/hooks/engram.env')).read().splitlines():
            if line.startswith(key + '='): return line.split('=', 1)[1].strip()
    except: pass
    return default

api  = cfg('ENGRAM_API',        'http://localhost:8766')
akey = cfg('ENGRAM_KEY',        '')
ns   = cfg('ENGRAM_DEFAULT_NS', 'personal:me')
try:
    root = subprocess.check_output(['git','rev-parse','--show-toplevel'],
        stderr=subprocess.DEVNULL, text=True).strip()
    for line in open(f'{root}/.engram').read().splitlines():
        if line.startswith('namespace='): ns = line.split('=',1)[1].strip()
except: pass
project = os.path.basename(
    subprocess.run(['git','rev-parse','--show-toplevel'],
        capture_output=True, text=True).stdout.strip() or os.getcwd())

# ── FILL IN: one entry per ~300-word raw segment of the session ──────────────
chunks = [
    "CHUNK_1_CONTENT_HERE",
    "CHUNK_2_CONTENT_HERE",
    # ...
]
# ─────────────────────────────────────────────────────────────────────────────

for i, chunk in enumerate(chunks, 1):
    payload = json.dumps({
        'content':     f'[chunk {i}/{len(chunks)}] {chunk}',
        'namespace':   ns,
        'memory_type': 'session',
        'tags':        ['session-chunk', 'manual-save', project],
        'metadata':    {'project': project, 'chunk': i, 'total': len(chunks), 'source': 'save-command'},
    }).encode()
    req = urllib.request.Request(f'{api}/api/v1/memory/', data=payload,
        headers={'Content-Type':'application/json','X-API-Key':akey}, method='POST')
    r = json.loads(urllib.request.urlopen(req, timeout=5).read())
    print(f'  chunk {i}: {r.get("id","?")[:8]}')

print(f'Written {len(chunks)} chunks to {ns}')
```

**3. Write a session index memory** (replace PROJECT, BRANCH, SUMMARY, N with actual values):
```python
import json, urllib.request, os, subprocess

def cfg(key, default=''):
    try:
        for line in open(os.path.expanduser('~/.claude/hooks/engram.env')).read().splitlines():
            if line.startswith(key + '='): return line.split('=', 1)[1].strip()
    except: pass
    return default

api  = cfg('ENGRAM_API',        'http://localhost:8766')
akey = cfg('ENGRAM_KEY',        '')
ns   = cfg('ENGRAM_DEFAULT_NS', 'personal:me')
project = os.path.basename(subprocess.run(['git','rev-parse','--show-toplevel'],
    capture_output=True,text=True).stdout.strip() or os.getcwd())
branch  = subprocess.run(['git','rev-parse','--abbrev-ref','HEAD'],
    capture_output=True,text=True).stdout.strip()

# ── FILL IN ──────────────────────────────────────────────────────────────────
one_line_summary = "BRIEF_SUMMARY_OF_FULL_SESSION"
n_chunks = N
# ─────────────────────────────────────────────────────────────────────────────

content = f'[session-index] {project}' + (f' | {branch}' if branch else '') + \
          f' — {one_line_summary} | chunks: {n_chunks}'
payload = json.dumps({'content': content, 'namespace': ns, 'memory_type': 'session',
    'tags': ['session-index', 'manual-save', project]}).encode()
req = urllib.request.Request(f'{api}/api/v1/memory/', data=payload,
    headers={'Content-Type':'application/json','X-API-Key':akey}, method='POST')
print('Index:', json.loads(urllib.request.urlopen(req,timeout=5).read()).get('id','?')[:8])
```

**4. Report:** `Saved N chunks + 1 index to <namespace>`

---

## /engram ns:<namespace>

To set a permanent namespace for a project:
```bash
echo 'namespace=<namespace>' > .engram
```
Then confirm with `/engram status` that the new namespace is active.
CMD
  success "Slash command /engram: $CLAUDE_COMMANDS_DIR/engram.md"
}

# ─── Install global git hook ───────────────────────────────────────────────────
install_git_hook() {
  step "Installing global git post-commit hook"

  if ! command -v git &>/dev/null; then
    warn "git not found — skipping git hook installation."
    return 0
  fi

  local git_hooks_dir="$HOME/.git-hooks"
  mkdir -p "$git_hooks_dir"

  cat > "$git_hooks_dir/post-commit" <<'GITHOOK'
#!/usr/bin/env bash
# Global git post-commit hook — writes every commit to engram.
set -euo pipefail
HOOKS_CONFIG="$HOME/.claude/hooks/engram.env"
[ -f "$HOOKS_CONFIG" ] && source "$HOOKS_CONFIG"
ENGRAM_API="${ENGRAM_API:-http://localhost:8766}"
ENGRAM_KEY="${ENGRAM_KEY:-}"
if ! curl -sf --max-time 2 "$ENGRAM_API/api/v1/admin/health" -o /dev/null 2>/dev/null; then exit 0; fi
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo "")
REPO_NAME=$(basename "$REPO_ROOT")
ENGRAM_NS="${ENGRAM_DEFAULT_NS:-personal:me}"
ENGRAM_NS="${ENGRAM_NS_OVERRIDE:-$ENGRAM_NS}"
if [[ -f "$REPO_ROOT/.engram" ]]; then
  FILE_NS=$(grep '^namespace=' "$REPO_ROOT/.engram" 2>/dev/null | cut -d= -f2 | tr -d ' ')
  [[ -n "$FILE_NS" ]] && ENGRAM_NS="$FILE_NS"
fi
COMMIT_HASH=$(git rev-parse --short HEAD)
COMMIT_FULL=$(git rev-parse HEAD)
COMMIT_MSG=$(git log -1 --pretty=%B | head -5)
COMMIT_AUTHOR=$(git log -1 --pretty=%an)
CHANGED_FILES=$(git diff-tree --no-commit-id -r --name-only HEAD | head -20 | tr '\n' ' ')
BRANCH=$(git rev-parse --abbrev-ref HEAD)
MEMORY_TYPE="fact"
echo "$COMMIT_MSG" | grep -qiE '^(feat|feature|refactor|arch):' && MEMORY_TYPE="decision"
echo "$COMMIT_MSG" | grep -qiE '^(fix|hotfix|bug):' && MEMORY_TYPE="incident" || true
CONTENT="[engram-commit] $COMMIT_MSG
repo: $REPO_NAME | commit: $COMMIT_HASH | branch: $BRANCH | author: $COMMIT_AUTHOR
files: $CHANGED_FILES"
PAYLOAD=$(python3 -c "
import json,sys
msg,ns,mtype,commit_full,commit_short,branch,author,files,repo=sys.argv[1:10]
print(json.dumps({'content':msg,'namespace':ns,'memory_type':mtype,'author':author,'tags':['git-commit','auto',repo,mtype],'metadata':{'commit_hash':commit_full,'commit_short':commit_short,'repo':repo,'branch':branch,'author':author,'changed_files':files,'source':'post-commit-hook'},'provenance':{'tool':'engram-git','git_commit':commit_short,'user_id':author,'agent_id':f'git:{repo}:{commit_short}'}}))
" "$CONTENT" "$ENGRAM_NS" "$MEMORY_TYPE" "$COMMIT_FULL" "$COMMIT_HASH" "$BRANCH" "$COMMIT_AUTHOR" "$CHANGED_FILES" "$REPO_NAME" 2>/dev/null)
curl -sf --max-time 5 -X POST "$ENGRAM_API/api/v1/memory/" \
  -H "Content-Type: application/json" -H "X-API-Key: $ENGRAM_KEY" \
  -H "X-Engram-Tool: engram-git" \
  -d "$PAYLOAD" -o /dev/null 2>/dev/null || true
LOCAL_HOOK="$(git rev-parse --git-dir 2>/dev/null)/hooks/post-commit.local"
[[ -x "$LOCAL_HOOK" ]] && exec "$LOCAL_HOOK" "$@"
exit 0
GITHOOK
  chmod +x "$git_hooks_dir/post-commit"
  git config --global core.hooksPath "$git_hooks_dir"
  success "Git hook: $git_hooks_dir/post-commit"
  success "git config --global core.hooksPath $git_hooks_dir"
}

# ─── Patch Claude Code settings.json ─────────────────────────────────────────
patch_settings() {
  step "Registering hooks in Claude Code"
  [ -z "$CLAUDE_SETTINGS" ] && warn "settings.json not found — skipping." && return 0

  local inject_cmd="$CLAUDE_HOOKS_DIR/engram-inject.sh"
  local precompact_cmd="$CLAUDE_HOOKS_DIR/engram-precompact.sh"
  local gitwrite_cmd="$CLAUDE_HOOKS_DIR/engram-git-write.sh"
  local session_cmd="$CLAUDE_HOOKS_DIR/engram-session-write.sh"

  python3 - <<PYEOF
import json, os, sys

settings_file  = "$CLAUDE_SETTINGS"
inject_cmd     = "$inject_cmd"
precompact_cmd = "$precompact_cmd"
gitwrite_cmd   = "$gitwrite_cmd"
session_cmd    = "$session_cmd"

try:
    with open(settings_file) as f:
        settings = json.load(f)
except Exception as e:
    print(f"  [warn] Could not read {settings_file}: {e}")
    sys.exit(0)

settings.setdefault("hooks", {})

def cmd_registered(hook_list, cmd):
    return any(h.get("command","") == cmd
               for entry in hook_list for h in entry.get("hooks",[]))

# UserPromptSubmit — inject
ups = settings["hooks"].setdefault("UserPromptSubmit", [{"hooks": []}])
if not cmd_registered(ups, inject_cmd):
    ups[0]["hooks"].insert(0, {"type":"command","command":inject_cmd,"timeout":8})

# PreCompact — precompact (async so it never delays the compact)
pcs = settings["hooks"].setdefault("PreCompact", [{"hooks": []}])
if not cmd_registered(pcs, precompact_cmd):
    pcs[0]["hooks"].append({"type":"command","command":precompact_cmd,"timeout":30,"async":True})

# PostToolUse — git-write (async, fires on every tool call)
ptus = settings["hooks"].setdefault("PostToolUse", [{"hooks": []}])
if not cmd_registered(ptus, gitwrite_cmd):
    ptus[0]["hooks"].append({"type":"command","command":gitwrite_cmd,"timeout":6,"async":True})

# Stop — session-write
stops = settings["hooks"].setdefault("Stop", [{"hooks": []}])
if not cmd_registered(stops, session_cmd):
    stops[0]["hooks"].append({"type":"command","command":session_cmd,"timeout":8,"async":True})

with open(settings_file, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print(f"  [ok] 4 hooks registered in {settings_file}")
PYEOF
}

# ─── Success ──────────────────────────────────────────────────────────────────
print_success() {
  echo ""
  echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${BOLD}${GREEN}  engram client hooks installed!${NC}"
  echo -e "${BOLD}${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo ""
  echo -e "  ${BOLD}What was installed:${NC}"
  echo -e "    ${DIM}~/.claude/hooks/engram.env${NC}               — config (server, key, namespace)"
  echo -e "    ${DIM}~/.claude/hooks/engram-inject.sh${NC}         — context injection (UserPromptSubmit)"
  echo -e "    ${DIM}~/.claude/hooks/engram-heartbeat.py${NC}      — background daemon (abrupt-exit safety)"
  echo -e "    ${DIM}~/.claude/hooks/engram-git-write.sh${NC}      — git commits + periodic save (PostToolUse)"
  echo -e "    ${DIM}~/.claude/hooks/engram-precompact.sh${NC}     — save before context compact (PreCompact)"
  echo -e "    ${DIM}~/.claude/hooks/engram-session-write.sh${NC}  — session summary on exit (Stop)"
  echo -e "    ${DIM}~/.git-hooks/post-commit${NC}                 — commit memory on every git commit"
  echo -e "    ${DIM}~/.claude/commands/engram.md${NC}             — /engram slash command"
  echo ""
  echo -e "  ${BOLD}Hook pipeline:${NC}"
  echo -e "    UserPromptSubmit → inject context"
  echo -e "    PostToolUse      → capture git commits, periodic auto-save (every ${DIM}${ENGRAM_AUTOSAVE_MINUTES:-10}min${NC})"
  echo -e "    PreCompact       → save before context window compact"
  echo -e "    Stop             → full session summary on exit"
  echo -e "    Heartbeat daemon → safety net for Ctrl+C / power loss (every ${DIM}${ENGRAM_HEARTBEAT_MINUTES:-10}min${NC})"
  echo ""
  echo -e "  ${BOLD}Server${NC}    : ${ENGRAM_SERVER}"
  echo -e "  ${BOLD}Namespace${NC} : ${DEFAULT_NS}"
  echo -e "  ${BOLD}LLM summaries${NC}: via claude --print (built-in)"
  echo ""
  echo -e "  ${BOLD}Per-project namespace:${NC}  echo 'namespace=project:myname' > /path/to/repo/.engram"
  echo -e "  ${BOLD}Manual save:${NC}            /engram save  (in Claude Code)"
  echo ""
  echo -e "  ${BOLD}Restart Claude Code${NC} (quit and reopen) to activate the hooks."
  echo ""
}

main() {
  detect_os
  check_claude_code
  collect_config
  test_connection
  install_hooks
  install_git_hook
  patch_settings
  print_success
}

main "$@"
