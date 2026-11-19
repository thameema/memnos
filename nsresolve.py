"""Namespace resolution for memnos Claude-Code integration — PACKAGED so it works after a
`pipx`/`uv tool install` (no repo paths). Used by `memnos hook recall|remember`,
`memnos mcp`, and the /memnos slash command.

Issue #20, Part A: routing is now a SERVER-MANAGED, host-aware, portable binding registry
scoped to the principal — not a per-machine local file the server never sees. The map
follows the user across machines + reinstalls.

Two-phase design (NEVER fetch-on-resolve):
  - resolve(data) reads ONLY the local cache `~/.memnos/bindings_cache.json` — fast,
    offline-safe; network must never sit in the resolve hot path.
  - refresh() pulls the principal's bindings from the server (GET /bindings) + registers
    this host (POST /hosts), writing the cache. Best-effort: any network/auth failure
    leaves the stale cache in place and returns quietly.

resolve() order (highest first):
  1. explicit data['namespace'] / arg.
  2. cache repo-key match           (key_type='repo', key == repo_key)            — host-agnostic, follows the project.
  3. cache host-scoped:             (key_type='host_repo', host_id==machine_id, key==repo_key)
                                    then (key_type='host_path', host_id==machine_id, key==abspath).
  4. legacy ~/.memnos/ns_overrides.json (offline / migration fallback).
  5. MEMNOS_NS env (if not 'auto').
  6. derived 'proj:<repo-basename or cwd-basename>'.
"""
import json
import os
import re
import socket
import subprocess
import tempfile
import time

_DIR = os.path.join(os.path.expanduser("~"), ".memnos")
_OVR = os.path.join(_DIR, "ns_overrides.json")
_CACHE = os.path.join(_DIR, "bindings_cache.json")
_MID = os.path.join(_DIR, "machine_id")


def _git_root(cwd=None):
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd or os.getcwd(),
                           capture_output=True, text=True, timeout=3).stdout.strip()
        return r or None
    except Exception:
        return None


def machine_id():
    """sanitize(hostname): lowercase, non-alphanumeric -> '-', collapse repeats, strip
    leading/trailing '-'. Re-derivable from the hostname (no opaque UUID). Cached in
    ~/.memnos/machine_id but ALWAYS recomputable — the cache is a convenience, not truth."""
    host = socket.gethostname() or "unknown"
    mid = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", host.lower())).strip("-") or "unknown"
    try:
        os.makedirs(_DIR, exist_ok=True)
        with open(_MID, "w") as f:
            f.write(mid)
    except Exception:
        pass
    return mid


def repo_key(cwd=None):
    """Portable repo key = normalized git remote URL (checks origin, github, upstream): strip
    scheme (https://, or git@host: -> host/), trailing '.git', lowercase. So
    git@github.com:thameema/memnos.git and https://github.com/thameema/memnos both ->
    github.com/thameema/memnos. Returns None if there's no remote."""
    target_cwd = cwd or os.getcwd()
    for remote in ("origin", "github", "upstream"):
        try:
            url = subprocess.run(
                ["git", "remote", "get-url", remote],
                cwd=target_cwd,
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout.strip()
        except Exception:
            url = ""
        if url:
            norm = _normalize_remote(url)
            if norm:
                return norm
    return None


def _normalize_remote(url):
    u = url.strip()
    if u.startswith("git@"):                       # git@github.com:thameema/memnos.git
        u = u[len("git@"):].replace(":", "/", 1)
    else:                                          # https:// | http:// | ssh:// | git://
        u = re.sub(r"^[a-z0-9+.\-]+://", "", u, flags=re.I)
        if "@" in u.split("/", 1)[0]:              # strip user@ credentials in host part
            u = u.split("@", 1)[1]
    if u.endswith(".git"):
        u = u[:-len(".git")]
    return u.rstrip("/").lower() or None


def _load(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def refresh(url=None, token=None, timeout=4):
    """Pull the principal's bindings from the server and cache them; register this host.
    BEST-EFFORT: any failure (no URL/token, network down, auth) leaves the existing cache
    untouched and returns quietly (never raises). NOT called from resolve()."""
    import time as _t
    import urllib.request
    url = (url or os.environ.get("MEMNOS_URL", "")).rstrip("/")
    token = token or os.environ.get("MEMNOS_TOKEN", "")
    if not url or not token:
        return False
    hdr = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    # register / check-in this host (best-effort, independent of the GET)
    try:
        req = urllib.request.Request(url + "/hosts", method="POST",
                                     data=json.dumps({"machine_id": machine_id()}).encode(), headers=hdr)
        urllib.request.urlopen(req, timeout=timeout).read()
    except Exception:
        pass
    try:
        req = urllib.request.Request(url + "/bindings", method="GET", headers=hdr)
        data = json.loads(urllib.request.urlopen(req, timeout=timeout).read() or b"{}")
    except Exception:
        return False                               # keep stale cache
    try:
        os.makedirs(_DIR, exist_ok=True)
        payload = {"fetched_at": _t.time(), "machine_id": machine_id(),
                   "bindings": data.get("bindings", []), "hosts": data.get("hosts", [])}
        with open(_CACHE, "w") as f:
            json.dump(payload, f, indent=2)
    except Exception:
        return False
    return True


def resolve(data=None):
    """Resolve a namespace from the LOCAL CACHE only — NEVER makes a network call.
    Back-compat wrapper: returns just the namespace. Callers that need to know WHETHER
    the namespace came from a real binding vs a default fallback use resolve_with_source."""
    return resolve_with_source(data)[0]


def resolve_with_source(data=None):
    """Like resolve() but ALSO returns WHERE the namespace came from, so the write-side
    (hook/MCP/CLI) can say "writing to <ns>" and warn on a default fallback (issue #20,
    Part B). Returns (namespace, source) where source is one of:
        explicit          — caller passed data['namespace'] / arg
        binding_repo      — host-agnostic cache binding (key_type='repo')
        binding_host_repo — host-scoped repo binding for THIS host
        binding_host_path — host-scoped path binding for THIS host
        legacy            — ~/.memnos/ns_overrides.json (offline / pre-#20 fallback)
        env               — MEMNOS_NS
        default           — derived proj:<repo|cwd> fallback (NO binding for this repo)
    NEVER makes a network call. 'default' is the signal that no binding exists yet."""
    cwd = (data or {}).get("cwd") or os.getcwd()

    # 1. explicit arg
    explicit = (data or {}).get("namespace")
    if explicit and str(explicit).strip().lower() != "auto":
        return str(explicit).strip(), "explicit"

    root = _git_root(cwd)
    rkey = repo_key(cwd)
    mid = machine_id()
    cache = _load(_CACHE) or {}
    binds = cache.get("bindings") or []

    # 2. cache repo-key match (host-agnostic — same on every machine)
    if rkey:
        for b in binds:
            if b.get("key_type") == "repo" and (b.get("key") or "").lower() == rkey:
                return b["namespace"], "binding_repo"

    # 3. cache host-scoped: host_repo (this host + repo) then host_path (this host + abspath)
    if rkey:
        for b in binds:
            if (b.get("key_type") == "host_repo" and b.get("host_id") == mid
                    and (b.get("key") or "").lower() == rkey):
                return b["namespace"], "binding_host_repo"
    abspath = os.path.realpath(cwd)
    for b in binds:
        if (b.get("key_type") == "host_path" and b.get("host_id") == mid
                and os.path.realpath(b.get("key") or "") == abspath):
            return b["namespace"], "binding_host_path"

    # 4. legacy local override file (offline / pre-#20 migration fallback)
    m = _load(_OVR) or {}
    for k in (cwd, os.path.realpath(cwd), root):
        if k and m.get(k):
            return m[k], "legacy"

    # 5. env default
    env = os.environ.get("MEMNOS_NS", "").strip()
    if env and env.lower() != "auto":
        return env, "env"

    # 6. derived default (NO binding for this repo — write-side warns + offers to bind)
    return ("proj:" + (os.path.basename(root) if root else
                       (os.path.basename(cwd.rstrip("/")) or "default")), "default")


# --- write-side surfacing helpers (issue #20, Part B) -----------------------
# A binding exists for this location iff the source is one of these.
BOUND_SOURCES = ("explicit", "binding_repo", "binding_host_repo", "binding_host_path", "legacy")


def bind_key_for(data=None):
    """The key a user would `memnos bind` to persist THIS location's routing — the
    portable repo key if there's a remote, else this host's abspath. Used by the
    default-fallback hint so the offer is copy-pasteable."""
    cwd = (data or {}).get("cwd") or os.getcwd()
    return repo_key(cwd) or os.path.realpath(cwd)


def default_fallback_hint(ns, data=None):
    """One-line warning + bind offer for a default-fallback write (source == 'default').
    SUGGEST, never auto-route: we don't create the binding, we make the fix one step away."""
    key = bind_key_for(data)
    return (f"no namespace bound for this repo — writing to default '{ns}'. "
            f"Bind future writes here: memnos bind {key} {ns}")


def session_first_time(session_id, ns, kind="dest"):
    """Per-session/ns dedupe so the hook surfaces the destination line ONCE per session
    and again only when the namespace CHANGES — never every turn. Returns True the first
    time it's called for a given (session_id, ns, kind), False afterwards. Marker files
    live under ~/.memnos/sessmark/; best-effort (any FS error -> treat as first time so
    the user still sees it). A missing session_id falls back to 'nosess' (still deduped
    within a process run via the same marker)."""
    sid = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id or "nosess"))[:120]
    nsk = re.sub(r"[^A-Za-z0-9_.:-]", "_", str(ns or "?"))[:120]
    d = os.path.join(_DIR, "sessmark")
    marker = os.path.join(d, f"{sid}.{kind}.{nsk}")
    try:
        if os.path.exists(marker):
            return False
        os.makedirs(d, exist_ok=True)
        # a CHANGE = a different ns for this session+kind: clear this session's other
        # markers of the same kind so the new destination surfaces.
        for f in os.listdir(d):
            if f.startswith(f"{sid}.{kind}.") and f != os.path.basename(marker):
                try:
                    os.remove(os.path.join(d, f))
                except Exception:
                    pass
        open(marker, "w").close()
        return True
    except Exception:
        return True


def set_override(namespace, cwd=None):
    """Local/offline override (legacy ns_overrides.json). Kept for offline use + as the
    migration source; server bindings (resolve order 2-3) take precedence over it."""
    cwd = cwd or os.getcwd()
    key = _git_root(cwd) or cwd
    os.makedirs(_DIR, exist_ok=True)
    m = _load(_OVR) or {}
    if not namespace or namespace.lower() in ("clear", "none", "auto"):
        m.pop(key, None)
        action = f"namespace override cleared for {key}"
    else:
        m[key] = namespace
        action = f"namespace for {key} set to {namespace}"
    with open(_OVR, "w") as f:
        json.dump(m, f, indent=2)
    return action


# --- active named-session binding per workspace (issue #231) --------------------------
def _active_path():
    # resolved per call (not import time) so MEMNOS_CONFIG_DIR / HOME overrides apply
    return os.path.join(os.environ.get("MEMNOS_CONFIG_DIR") or os.path.expanduser("~/.memnos"),
                        "active_sessions.json")


def _write_active(m):
    p = _active_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p), suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(m, f, indent=2)
    os.replace(tmp, p)


def _parent_tty_and_harness():
    """Walk the parent PID chain (up to 6 hops) to discover `(tty, harness, agent_pid, cmd_name)`
    even when invoked inside a non-TTY hook or tool subprocess."""
    harness = ""
    agent_pid = os.getppid()
    tty_str = ""
    cmd_name = ""
    if os.environ.get("ANTIGRAVITY_CLI") or os.environ.get("AGY_SESSION_ID"):
        harness = "antigravity"
    elif os.environ.get("CLAUDE_CODE") or os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_PLUGIN_ROOT") or os.environ.get("CLAUDE_PROJECT_DIR"):
        harness = "claude-code"
    elif os.environ.get("CURSOR_VERSION") or os.environ.get("CURSOR_TRACE_ID"):
        harness = "cursor"
    elif os.environ.get("GEMINI_CLI"):
        harness = "gemini"
    if os.name == "nt":
        tty_str = f"win-{agent_pid}"
    else:
        cur = os.getppid()
        for _ in range(6):
            if not cur or cur <= 1:
                break
            try:
                out = subprocess.run(
                    ["ps", "-o", "pid=,ppid=,tty=,comm=,args=", "-p", str(cur)],
                    capture_output=True, text=True, timeout=2,
                ).stdout.strip()
            except Exception:
                break
            if not out:
                break
            parts = out.split(None, 4)
            if len(parts) < 4:
                break
            p_pid = int(parts[0])
            p_ppid = int(parts[1])
            p_tty = parts[2] if parts[2] not in ("??", "?", "-") else ""
            p_comm = parts[3].lower()
            p_base = os.path.basename(p_comm)
            p_args = parts[4] if len(parts) > 4 else parts[3]
            if p_tty and not tty_str:
                tty_str = p_tty
            low_args = p_args.lower()
            if not harness:
                if "claude.app" in low_args and "disclaimer" not in p_base:
                    harness = "claude-desktop"
                    agent_pid = p_pid
                elif p_base == "claude" or re.match(r"^(?:\S+/)?claude(?:\s|$)", low_args):
                    harness = "claude-code"
                    agent_pid = p_pid
                    m = re.search(r"--name\s+(.+?)(?:\s+--|\s*$)", p_args)
                    if m:
                        cmd_name = _slugify_session_title(m.group(1).strip("\"' "))
                elif p_base in ("agy", "antigravity") or re.match(r"^(?:\S+/)?(?:agy|antigravity)(?:\s|$)", low_args):
                    harness = "antigravity"
                    agent_pid = p_pid
                elif p_base == "gemini" or re.match(r"^(?:\S+/)?gemini(?:\s|$)", low_args):
                    harness = "gemini"
                    agent_pid = p_pid
                elif p_base == "cursor":
                    harness = "cursor"
                    agent_pid = p_pid
            elif harness == "claude-code" and (p_base == "claude" or re.match(r"^(?:\S+/)?claude(?:\s|$)", low_args)):
                agent_pid = p_pid
                m = re.search(r"--name\s+(.+?)(?:\s+--|\s*$)", p_args)
                if m and not cmd_name:
                    cmd_name = _slugify_session_title(m.group(1).strip("\"' "))
            elif harness == "antigravity" and (p_base in ("agy", "antigravity") or re.match(r"^(?:\S+/)?(?:agy|antigravity)(?:\s|$)", low_args)):
                agent_pid = p_pid
            cur = p_ppid
    if not harness:
        if os.environ.get("CLAUDE_PLUGIN_ROOT") or os.environ.get("CLAUDE_PROJECT_DIR"):
            harness = "claude-code"
        elif os.environ.get("ANTIGRAVITY_CLI") or os.environ.get("AGY_SESSION_ID"):
            harness = "antigravity"
        elif os.environ.get("CURSOR_VERSION"):
            harness = "cursor"
    if not tty_str and os.name != "nt":
        try:
            if os.isatty(0):
                tty_str = os.path.basename(os.ttyname(0))
        except Exception:
            pass
    return tty_str, harness, agent_pid, cmd_name


def save_active_session(name, cwd=None, namespace=None):
    """Bind a session name to this workspace scoped by TTY and harness so two agents
    (e.g. Claude Code and Antigravity) working in the same repo never collide."""
    m = _load(_active_path()) or {}
    key = os.path.realpath(cwd or os.getcwd())
    clean = name.strip().lstrip("@")
    tty_str, harness, _, _ = _parent_tty_and_harness()
    entry = {"name": clean, "namespace": namespace or "", "tty": tty_str, "harness": harness, "updated_at": time.time()}
    if tty_str:
        m[f"{key}::{tty_str}"] = entry
    if harness:
        m[f"{key}::harness:{harness}"] = entry
    else:
        m[key] = entry
    _write_active(m)


def clear_active_session(name=None, cwd=None):
    """Drop the binding for this cwd/tty/harness (and/or any binding with this name)."""
    m = _load(_active_path()) or {}
    key = os.path.realpath(cwd or os.getcwd())
    tty_str, harness, _, _ = _parent_tty_and_harness()
    name = (name or "").strip().lstrip("@")
    target_keys = {key, f"{key}::{tty_str}", f"{key}::harness:{harness}"}
    keep = {k: v for k, v in m.items()
            if not ((k in target_keys and (not name or v.get("name") == name)) or (name and not cwd and v.get("name") == name))}
    if keep != m:
        _write_active(keep)


def _slugify_session_title(title):
    """Turn a session title like 'Claude | Backend' or 'Agent | WebApp | 37beb4cb'
    into a valid session slug ('claude-backend', 'agent-webapp')."""
    t = re.sub(r"\|\s*[0-9a-f]{8}\s*$", "", (title or "").strip(), flags=re.IGNORECASE)
    s = re.sub(r"[^a-z0-9_.:-]+", "-", t.lower()).strip("-._:")
    return s[:64]


def _transcript_session_name(transcript_path):
    """Extract agentName / customTitle from a Claude Code .jsonl transcript if present."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return ""
    title = ""
    try:
        with open(transcript_path, encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i > 64:
                    break
                if "agentName" not in line and "customTitle" not in line:
                    continue
                obj = json.loads(line)
                t = obj.get("agentName") or obj.get("customTitle") or ""
                if t:
                    title = t
    except Exception:
        return ""
    return _slugify_session_title(title)


def resolve_active_session(cwd=None, data=None):
    """Priority: (1) $MEMNOS_SESSION, (2) Claude transcript agentName/customTitle,
    (3) <cwd>::<tty>, (4) <cwd>::harness:<harness>, (5) matching-harness bare <cwd>."""
    env = os.environ.get("MEMNOS_SESSION", "").strip().lstrip("@")
    if env:
        return env
    tp = (data or {}).get("transcript_path") or (data or {}).get("transcriptPath") or ""
    from_tp = _transcript_session_name(tp)
    if from_tp:
        return from_tp
    key = os.path.realpath((data or {}).get("cwd") or cwd or os.getcwd())
    tty_str, harness, _, cmd_name = _parent_tty_and_harness()
    if cmd_name:
        return cmd_name
    m = _load(_active_path()) or {}
    if tty_str:
        b_tty = (m.get(f"{key}::{tty_str}") or {}).get("name", "")
        if b_tty:
            return b_tty
    if harness:
        b_h = (m.get(f"{key}::harness:{harness}") or {}).get("name", "")
        if b_h:
            return b_h
    bare = m.get(key) or {}
    bare_name = bare.get("name", "")
    bare_h = bare.get("harness", "")
    if bare_name:
        if (not bare_h or not harness or bare_h == harness) and not (
            (harness == "claude-code" and bare_name.startswith("agy-"))
            or (harness == "antigravity" and bare_name.startswith("tommy-"))
        ):
            return bare_name
    return ""


def _git_branch(cwd=None) -> str:
    try:
        r = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd or os.getcwd(),
                           capture_output=True, text=True, timeout=2).stdout.strip()
        return r if r and r != "HEAD" else ""
    except Exception:
        return ""


def is_local_pid_alive(pid) -> bool:
    """Return True if integer `pid` is an existing OS process on THIS machine (macOS, Linux, Windows)."""
    if not pid:
        return False
    try:
        p = int(pid)
        if p <= 1:
            return False
        if os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x1000, False, p)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == 259  # STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        os.kill(p, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def infer_topic_channel(cwd=None, task: str = "", topic: str = "") -> str:
    """Derive a clean, human-readable `#<topic>` channel name related to the topic discussed,
    current git branch, or project context."""
    raw = (topic or task or "").strip()
    if raw:
        slug = re.sub(r"[^a-z0-9]+", "-", raw.lower().lstrip("#")).strip("-")
        # strip filler words at start if derived from a natural-language task
        if not topic and len(slug) > 28:
            words = [w for w in slug.split("-") if w not in ("the", "and", "for", "with", "please", "check", "fix", "update", "build", "test")]
            slug = "-".join(words[:4])
        slug = slug[:48].strip("-")
        if slug:
            return "#" + slug
    real_cwd = os.path.realpath(cwd or os.getcwd())
    root = _git_root(real_cwd) or real_cwd
    repo_slug = re.sub(r"[^a-z0-9]+", "-", os.path.basename(root.rstrip("/\\")).lower()).strip("-")[:24] or "general"
    branch = _git_branch(real_cwd)
    branch_slug = ""
    if branch and branch.lower() not in ("main", "master", "develop", "dev", "head", "trunk"):
        branch_slug = re.sub(r"^(?:feat|feature|fix|bugfix|chore|hotfix|release)[/-]+", "", branch.lower())
        branch_slug = re.sub(r"[^a-z0-9]+", "-", branch_slug).strip("-")[:24]
    if branch_slug and branch_slug != repo_slug:
        return f"#{repo_slug}-{branch_slug}"[:60]
    return f"#{repo_slug}"


def detect_terminal_session(cwd=None, data=None) -> dict:
    """Inspect the parent process chain (`os.getppid()`) across macOS, Linux, and Windows to
    identify the active terminal agent (`claude`, `agy`/`antigravity`, `gemini`, `cursor`,
    `claude-desktop`), its OS PID, TTY/console, `machine_id`, and a human-readable, meaningful
    session name (e.g. `claude-memnos`, `agy-webapp`, `claude-webapp-auth-redesign`)."""
    real_cwd = os.path.realpath((data or {}).get("cwd") or cwd or os.getcwd())
    explicit_name = resolve_active_session(cwd=real_cwd, data=data)
    harness = ""
    agent_pid = os.getppid()
    tty_str = ""
    cmd_name = ""

    if os.name == "nt":
        # Windows: check environment & parent PID via PowerShell/WMIC/tasklist fallback
        if os.environ.get("ANTIGRAVITY_CLI") or os.environ.get("AGY_SESSION_ID"):
            harness = "antigravity"
        elif os.environ.get("CLAUDE_CODE") or os.environ.get("CLAUDE_PLUGIN_ROOT") or os.environ.get("CLAUDE_PROJECT_DIR"):
            harness = "claude-code"
        elif os.environ.get("CURSOR_VERSION") or os.environ.get("CURSOR_TRACE_ID"):
            harness = "cursor"
        elif os.environ.get("GEMINI_CLI"):
            harness = "gemini"
        tty_str = f"win-{agent_pid}"
    else:
        cur = os.getppid()
        for _ in range(5):
            if not cur or cur <= 1:
                break
            try:
                out = subprocess.run(
                    ["ps", "-o", "pid=,ppid=,tty=,comm=,args=", "-p", str(cur)],
                    capture_output=True, text=True, timeout=2,
                ).stdout.strip()
            except Exception:
                break
            if not out:
                break
            parts = out.split(None, 4)
            if len(parts) < 4:
                break
            p_pid = int(parts[0])
            p_ppid = int(parts[1])
            p_tty = parts[2] if parts[2] not in ("??", "?", "-") else ""
            p_comm = parts[3].lower()
            p_args = parts[4] if len(parts) > 4 else parts[3]
            if p_tty and not tty_str:
                tty_str = p_tty
            low_args = p_args.lower()
            if not harness:
                if "claude.app" in low_args:
                    harness = "claude-desktop"
                    agent_pid = p_pid
                elif re.search(r"(?:^|/|\s)claude(?:\s|$)", low_args) and "disclaimer" not in p_comm:
                    harness = "claude-code"
                    agent_pid = p_pid
                    m = re.search(r"--name\s+(.+?)(?:\s+--|\s*$)", p_args)
                    if m:
                        cmd_name = _slugify_session_title(m.group(1).strip("\"' "))
                elif re.search(r"(?:^|/|\s)(?:agy|antigravity)(?:\s|$)", low_args):
                    harness = "antigravity"
                    agent_pid = p_pid
                elif re.search(r"(?:^|/|\s)gemini(?:\s|$)", low_args):
                    harness = "gemini"
                    agent_pid = p_pid
                elif "cursor" in low_args:
                    harness = "cursor"
                    agent_pid = p_pid
            cur = p_ppid

    if not harness:
        if os.environ.get("CLAUDE_PLUGIN_ROOT") or os.environ.get("CLAUDE_PROJECT_DIR"):
            harness = "claude-code"
        elif os.environ.get("CURSOR_VERSION"):
            harness = "cursor"

    if not tty_str and os.name != "nt":
        try:
            if os.isatty(0):
                tty_str = os.path.basename(os.ttyname(0))
        except Exception:
            pass

    root = _git_root(real_cwd) or real_cwd
    repo_slug = re.sub(r"[^a-z0-9]+", "-", os.path.basename(root.rstrip("/\\")).lower()).strip("-")[:22] or "workspace"
    branch = _git_branch(real_cwd)
    branch_slug = ""
    if branch and branch.lower() not in ("main", "master", "develop", "dev", "head", "trunk"):
        branch_slug = re.sub(r"^(?:feat|feature|fix|bugfix|chore|hotfix|release)[/-]+", "", branch.lower())
        branch_slug = re.sub(r"[^a-z0-9]+", "-", branch_slug).strip("-")[:18]

    rel_sub = ""
    if real_cwd != root and real_cwd.startswith(root):
        sub = os.path.relpath(real_cwd, root).split(os.sep)[0]
        if sub and sub not in (".", "..", "src", "lib", "app", "apps", "packages"):
            rel_sub = re.sub(r"[^a-z0-9]+", "-", sub.lower()).strip("-")[:14]

    h_prefix = {
        "claude-code": "claude",
        "claude-desktop": "claude-app",
        "antigravity": "agy",
        "gemini": "gemini",
        "cursor": "cursor",
    }.get(harness, "agent")
    h_title = {
        "claude-code": "Claude Code",
        "claude-desktop": "Claude Desktop",
        "antigravity": "Antigravity",
        "gemini": "Gemini CLI",
        "cursor": "Cursor",
    }.get(harness, "Agent")

    name = explicit_name or cmd_name
    auto_named = False
    if not name:
        auto_named = True
        context_slug = branch_slug or rel_sub
        base_name = f"{h_prefix}-{repo_slug}-{context_slug}" if (context_slug and context_slug != repo_slug) else f"{h_prefix}-{repo_slug}"
        name = re.sub(r"[^a-z0-9_.-]+", "-", base_name.lower()).strip("-")[:64]

    tty_slug = re.sub(r"[^a-z0-9]+", "", tty_str.lower().replace("/dev/", "").replace("ttys", "s").replace("pts", "p"))
    topic_channel = infer_topic_channel(cwd=real_cwd)
    display_label = f"{h_title} · {repo_slug}" + (f" ({branch})" if branch else "") + (f" [{tty_str}]" if tty_str else "")

    return {
        "name": name,
        "fallback_suffix": tty_slug or f"p{agent_pid % 10000}",
        "display_label": display_label,
        "suggested_topic": topic_channel,
        "harness": harness or "cli",
        "pid": agent_pid,
        "tty": tty_str,
        "machine_id": machine_id(),
        "host": socket.gethostname() or "localhost",
        "cwd": real_cwd,
        "auto_named": auto_named,
    }
