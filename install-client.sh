#!/usr/bin/env bash
# engram client installer
#
# Installs Claude Code automation hooks on the current machine.
# Works whether engram runs locally or on a remote server.
#
# Usage:
#   ./install-client.sh                              # prompts for server URL + key
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
    --server)   ARG_SERVER="$2"; shift 2 ;;
    --key)      ARG_KEY="$2";    shift 2 ;;
    --namespace|--ns) ARG_NS="$2"; shift 2 ;;
    *) warn "Unknown argument: $1"; shift ;;
  esac
done

# ─── Banner ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${BLUE}"
cat <<'BANNER'
   ___  ____   ___  ____   ____  __  __
  / __)(  _ \ / __)(  _ \ / _  \(  \/  )
 ( (__  )   /( (__  )   // /_\ / )    /
  \___)(_)\_) \___)(____/ \___/ (_/\/\_)
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
      # Possible MINGW/Git Bash on Windows
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
  # Claude Code settings location
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
  ENGRAM_SERVER="${ENGRAM_SERVER%/}"  # strip trailing slash

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
# engram hook config — edit to change server, API key, or default namespace.
ENGRAM_API=${ENGRAM_SERVER}
ENGRAM_KEY=${ENGRAM_API_KEY}
ENGRAM_DEFAULT_NS=${DEFAULT_NS}
ENGRAM_TOP_K=5
ENV
  success "Config: $CLAUDE_HOOKS_DIR/engram.env"

  # ── inject hook ────────────────────────────────────────────────────────────
  cat > "$CLAUDE_HOOKS_DIR/engram-inject.sh" <<'INJECT'
#!/usr/bin/env bash
# UserPromptSubmit hook — injects engram context before every Claude Code prompt.
# Namespace: .engram file in project root > ENGRAM_NS_OVERRIDE > ENGRAM_DEFAULT_NS in engram.env
set -euo pipefail
HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HOOKS_DIR/engram.env" ] && source "$HOOKS_DIR/engram.env"
ENGRAM_API="${ENGRAM_API:-http://localhost:8766}"
ENGRAM_KEY="${ENGRAM_KEY:-}"
ENGRAM_TOP_K="${ENGRAM_TOP_K:-5}"
INPUT=$(cat)
CWD=$(echo "$INPUT"    | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('cwd',''))" 2>/dev/null || echo "")
PROMPT=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('prompt',''))" 2>/dev/null || echo "")
if ! curl -sf --max-time 2 "$ENGRAM_API/api/v1/admin/health" -o /dev/null 2>/dev/null; then exit 0; fi
ENGRAM_NS="${ENGRAM_DEFAULT_NS:-personal:me}"
ENGRAM_NS="${ENGRAM_NS_OVERRIDE:-$ENGRAM_NS}"
REPO_ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null || echo "")
if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.engram" ]]; then
  FILE_NS=$(grep '^namespace=' "$REPO_ROOT/.engram" 2>/dev/null | cut -d= -f2 | tr -d ' ')
  [[ -n "$FILE_NS" ]] && ENGRAM_NS="$FILE_NS"
fi
QUERY=$(echo "$PROMPT" | head -c 200 | python3 -c "import sys,urllib.parse; print(urllib.parse.quote(sys.stdin.read().strip()))" 2>/dev/null || echo "")
[[ -z "$QUERY" ]] && exit 0
RESPONSE=$(curl -sf --max-time 5 "$ENGRAM_API/api/v1/memory/search?q=$QUERY&ns=$ENGRAM_NS&top_k=$ENGRAM_TOP_K" -H "X-API-Key: $ENGRAM_KEY" 2>/dev/null || echo "[]")
CONTEXT=$(echo "$RESPONSE" | python3 -c "
import sys, json
try: data = json.load(sys.stdin)
except: sys.exit(0)
results = data if isinstance(data, list) else data.get('results', [])
if not results: sys.exit(0)
lines = ['[engram: relevant past context]']
for r in results:
    mem = r.get('memory', r)
    mtype = mem.get('memory_type', 'fact')
    content = mem.get('content', '').strip()
    score = r.get('score', '')
    score_str = f'  (similarity: {score:.2f})' if isinstance(score, float) else ''
    if content: lines.append(f'[{mtype}]{score_str} {content[:280]}')
if len(lines) <= 1: sys.exit(0)
print('\n'.join(lines))
" 2>/dev/null || echo "")
[[ -z "$CONTEXT" ]] && exit 0
python3 -c "import json,sys; print(json.dumps({'hookSpecificOutput':{'hookEventName':'UserPromptSubmit','additionalContext':sys.argv[1]}}))" "$CONTEXT"
INJECT
  chmod +x "$CLAUDE_HOOKS_DIR/engram-inject.sh"
  success "Inject hook: $CLAUDE_HOOKS_DIR/engram-inject.sh"

  # ── session-write hook ─────────────────────────────────────────────────────
  cat > "$CLAUDE_HOOKS_DIR/engram-session-write.sh" <<'SESSION'
#!/usr/bin/env bash
# Stop hook — writes session state to engram after every Claude Code turn.
set -euo pipefail
HOOKS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$HOOKS_DIR/engram.env" ] && source "$HOOKS_DIR/engram.env"
ENGRAM_API="${ENGRAM_API:-http://localhost:8766}"
ENGRAM_KEY="${ENGRAM_KEY:-}"
INPUT=$(cat)
CWD=$(echo "$INPUT"        | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('cwd',''))" 2>/dev/null || echo "")
SESSION_ID=$(echo "$INPUT" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('session_id',''))" 2>/dev/null || echo "")
[[ -z "$CWD" ]] && exit 0
if ! curl -sf --max-time 2 "$ENGRAM_API/api/v1/admin/health" -o /dev/null 2>/dev/null; then exit 0; fi
ENGRAM_NS="${ENGRAM_DEFAULT_NS:-personal:me}"
ENGRAM_NS="${ENGRAM_NS_OVERRIDE:-$ENGRAM_NS}"
REPO_ROOT=$(git -C "$CWD" rev-parse --show-toplevel 2>/dev/null || echo "")
if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.engram" ]]; then
  FILE_NS=$(grep '^namespace=' "$REPO_ROOT/.engram" 2>/dev/null | cut -d= -f2 | tr -d ' ')
  [[ -n "$FILE_NS" ]] && ENGRAM_NS="$FILE_NS"
fi
PROJECT=$(basename "$CWD")
BRANCH="" RECENT_COMMITS="" UNCOMMITTED=0
if git -C "$CWD" rev-parse --git-dir >/dev/null 2>&1; then
  BRANCH=$(git -C "$CWD" rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")
  RECENT_COMMITS=$(git -C "$CWD" log --oneline -5 --no-decorate 2>/dev/null || echo "")
  UNCOMMITTED=$(git -C "$CWD" status --short 2>/dev/null | wc -l | tr -d ' ')
fi
if [[ -n "$RECENT_COMMITS" ]]; then
  CONTENT="session ended | project: $PROJECT | dir: $CWD${BRANCH:+ | branch: $BRANCH} | uncommitted: $UNCOMMITTED
Recent commits:
$RECENT_COMMITS"
else
  CONTENT="session ended | project: $PROJECT | dir: $CWD"
fi
PAYLOAD=$(python3 -c "
import json, sys
print(json.dumps({'content':sys.argv[1],'namespace':sys.argv[2],'memory_type':'fact','tags':['session-log','auto',sys.argv[3]],'metadata':{'session_id':sys.argv[4],'project':sys.argv[3],'source':'claude-code-stop-hook'}}))
" "$CONTENT" "$ENGRAM_NS" "$PROJECT" "$SESSION_ID" 2>/dev/null)
curl -sf --max-time 5 -X POST "$ENGRAM_API/api/v1/memory/" -H "Content-Type: application/json" -H "X-API-Key: $ENGRAM_KEY" -d "$PAYLOAD" -o /dev/null 2>/dev/null || true
exit 0
SESSION
  chmod +x "$CLAUDE_HOOKS_DIR/engram-session-write.sh"
  success "Session hook: $CLAUDE_HOOKS_DIR/engram-session-write.sh"

  # ── slash command ──────────────────────────────────────────────────────────
  cat > "$CLAUDE_COMMANDS_DIR/engram.md" <<'CMD'
Run these bash commands immediately (no confirmation needed), then format results as shown.

```bash
curl -sf "${ENGRAM_API:-http://localhost:8766}/api/v1/admin/namespaces" \
  -H "X-API-Key: $(grep ENGRAM_KEY ~/.claude/hooks/engram.env | cut -d= -f2 | tr -d ' ')" \
  2>/dev/null || echo "[]"
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo "")
if [[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.engram" ]]; then
  echo "source:file"; grep '^namespace=' "$REPO_ROOT/.engram" | cut -d= -f2
else
  echo "source:default"; grep ENGRAM_DEFAULT_NS ~/.claude/hooks/engram.env | cut -d= -f2 | tr -d ' '
fi
NS=$(grep ENGRAM_DEFAULT_NS ~/.claude/hooks/engram.env | cut -d= -f2 | tr -d ' ')
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || echo "")
[[ -n "$REPO_ROOT" && -f "$REPO_ROOT/.engram" ]] && NS=$(grep '^namespace=' "$REPO_ROOT/.engram" | cut -d= -f2 | tr -d ' ')
curl -sf "${ENGRAM_API:-http://localhost:8766}/api/v1/memory/search?q=session+commit+work&ns=$NS&top_k=5" \
  -H "X-API-Key: $(grep ENGRAM_KEY ~/.claude/hooks/engram.env | cut -d= -f2 | tr -d ' ')" 2>/dev/null || echo "[]"
```

Show:
**engram status**
- **Namespaces** — bullet list of all namespace names
- **Active namespace** — name + how it was resolved (.engram file / default)
  If $ARGUMENTS contains `ns:something`, show: `echo 'namespace=something' > .engram`
- **Recent memories** — up to 5 as: `[type] score — first 120 chars`
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
# Memory type: feat/refactor → decision | fix → incident | else → fact
# Per-repo override: create .git/hooks/post-commit.local
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
msg,ns,mtype,commit,branch,author,files,repo=sys.argv[1:9]
print(json.dumps({'content':msg,'namespace':ns,'memory_type':mtype,'author':author,'tags':['git-commit','auto',repo,mtype],'metadata':{'commit_hash':commit,'repo':repo,'branch':branch,'source':'post-commit-hook'}}))
" "$CONTENT" "$ENGRAM_NS" "$MEMORY_TYPE" "$COMMIT_FULL" "$BRANCH" "$COMMIT_AUTHOR" "$CHANGED_FILES" "$REPO_NAME" 2>/dev/null)
curl -sf --max-time 5 -X POST "$ENGRAM_API/api/v1/memory/" -H "Content-Type: application/json" -H "X-API-Key: $ENGRAM_KEY" -d "$PAYLOAD" -o /dev/null 2>/dev/null || true
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
  local session_cmd="$CLAUDE_HOOKS_DIR/engram-session-write.sh"

  python3 - <<PYEOF
import json, os, sys

settings_file = "$CLAUDE_SETTINGS"
inject_cmd    = "$inject_cmd"
session_cmd   = "$session_cmd"

try:
    with open(settings_file) as f:
        settings = json.load(f)
except Exception as e:
    print(f"  [warn] Could not read {settings_file}: {e}")
    sys.exit(0)

settings.setdefault("hooks", {})

# UserPromptSubmit — add inject if not already registered
ups = settings["hooks"].setdefault("UserPromptSubmit", [{"hooks": []}])
if not any(h.get("command","") == inject_cmd
           for entry in ups for h in entry.get("hooks",[])):
    ups[0]["hooks"].insert(0, {"type":"command","command":inject_cmd,"timeout":8})

# Stop — add session-write if not already registered
stops = settings["hooks"].setdefault("Stop", [{"hooks": []}])
if not any(h.get("command","") == session_cmd
           for entry in stops for h in entry.get("hooks",[])):
    stops[0]["hooks"].append({"type":"command","command":session_cmd,"timeout":8,"async":True})

with open(settings_file, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print(f"  [ok] Hooks registered in {settings_file}")
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
  echo -e "    ${DIM}~/.claude/hooks/engram.env${NC}            — config (server URL, key, namespace)"
  echo -e "    ${DIM}~/.claude/hooks/engram-inject.sh${NC}      — context injection on every prompt"
  echo -e "    ${DIM}~/.claude/hooks/engram-session-write.sh${NC} — session state on every turn end"
  echo -e "    ${DIM}~/.git-hooks/post-commit${NC}              — commit memory on every git commit"
  echo -e "    ${DIM}~/.claude/commands/engram.md${NC}          — /engram slash command"
  echo ""
  echo -e "  ${BOLD}Server${NC} : ${ENGRAM_SERVER}"
  echo -e "  ${BOLD}Namespace${NC}: ${DEFAULT_NS}"
  echo ""
  echo -e "  ${BOLD}To set a per-project namespace:${NC}"
  echo -e "    echo 'namespace=project:myproject' > /path/to/repo/${BOLD}.engram${NC}"
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
