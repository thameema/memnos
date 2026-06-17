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
    """Portable repo key = normalized git remote origin URL: strip scheme (https://, or
    git@host: -> host/), trailing '.git', lowercase. So git@github.com:thameema/memnos.git
    and https://github.com/thameema/memnos both -> github.com/thameema/memnos. Returns
    None if there's no remote (caller falls back to a host-scoped path binding)."""
    try:
        url = subprocess.run(["git", "remote", "get-url", "origin"], cwd=cwd or os.getcwd(),
                             capture_output=True, text=True, timeout=3).stdout.strip()
    except Exception:
        return None
    if not url:
        return None
    return _normalize_remote(url)


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
