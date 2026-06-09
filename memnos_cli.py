"""memnos — one cross-platform CLI for the whole platform (admin + data client + server).

    memnos setup                 # connect to YOUR Postgres, create schema + admin token
    memnos serve                 # run the server
    memnos token <principal>     # mint a bearer token
    memnos grant <p> <ns>        # grant namespace access
    memnos namespace add <ns>    # create a namespace
    memnos secret set <name>     # store an encrypted secret (vault)
    memnos secret rotate         # rotate the vault master key
    memnos remember/recall ...   # data client (talks to the server over HTTP)
    memnos stats|health|usage|audit|whoami|ns ...

Postgres is a PREREQUISITE (the installer never installs it). `setup` asks for the
connection and creates the database objects in it. Config lives in ~/.memnos/config.json.
"""
import argparse
import getpass
import json
import os
import sys
import urllib.request

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".memnos")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_DSN = "postgresql://memnos:memnos_core@localhost:5433/memnos"


# ---- config -----------------------------------------------------------------
def load_config():
    try:
        with open(CONFIG_PATH) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as fh:
        json.dump(cfg, fh, indent=2)
    try:
        os.chmod(CONFIG_PATH, 0o600)          # secret_key lives here
    except OSError:
        pass


def _apply_env(cfg):
    """Make config visible to Control/Vault/server (they read env)."""
    if cfg.get("dsn"):
        os.environ.setdefault("MEMNOS_DSN", cfg["dsn"])
    if cfg.get("secret_key"):
        os.environ.setdefault("MEMNOS_SECRET_KEY", cfg["secret_key"])
    if cfg.get("port"):
        os.environ.setdefault("MEMNOS_PORT", str(cfg["port"]))


def _dsn(cfg):
    return os.environ.get("MEMNOS_DSN") or cfg.get("dsn") or DEFAULT_DSN


def _conn(cfg):
    import psycopg
    from psycopg.rows import dict_row
    return psycopg.connect(_dsn(cfg), autocommit=True, row_factory=dict_row)


def _server_url(cfg):
    return os.environ.get("MEMNOS_URL") or cfg.get("url") or f"http://127.0.0.1:{cfg.get('port', 8900)}"


# ---- HTTP data client -------------------------------------------------------
def _post(cfg, path, payload, token):
    import urllib.request
    import urllib.error
    req = urllib.request.Request(_server_url(cfg) + path, method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=30).read() or b"{}")
    except urllib.error.HTTPError as e:
        sys.exit(f"server error {e.code}: {json.loads(e.read() or b'{}').get('error', '?')}")
    except urllib.error.URLError as e:
        sys.exit(f"cannot reach server at {_server_url(cfg)} — is it running? ({e})")


# ---- setup wizard -----------------------------------------------------------
def cmd_setup(args, cfg):
    print("=== memnos setup (Postgres is a prerequisite — this only creates objects in it) ===")
    dsn = args.dsn or os.environ.get("MEMNOS_DSN")
    if not dsn:
        host = input("Postgres host [localhost]: ").strip() or "localhost"
        port = input("Postgres port [5432]: ").strip() or "5432"
        db = input("Database name [memnos]: ").strip() or "memnos"
        user = input("User [memnos]: ").strip() or "memnos"
        pw = getpass.getpass("Password: ")
        dsn = f"postgresql://{user}:{pw}@{host}:{port}/{db}"
    os.environ["MEMNOS_DSN"] = dsn
    import psycopg
    from psycopg.rows import dict_row
    try:
        conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    except Exception as e:
        # friction-free: if only the DATABASE is missing, create it (connect to 'postgres')
        if "does not exist" in str(e).lower():
            try:
                from urllib.parse import urlsplit
                u = urlsplit(dsn)
                dbname = u.path.lstrip("/") or "memnos"
                admin_dsn = dsn.rsplit("/", 1)[0] + "/postgres"
                ac = psycopg.connect(admin_dsn, autocommit=True)
                with ac.cursor() as c:
                    c.execute(f'CREATE DATABASE "{dbname}"')
                ac.close()
                print(f"[memnos] created database '{dbname}'.")
                conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
            except Exception as e2:
                sys.exit(f"could not connect or create the database: {e2}\n"
                         f"(create it manually: createdb {dbname})")
        else:
            sys.exit(f"could not connect to Postgres: {e}")
    from memnos_brain.store import BrainStore
    from memnos_brain.control import Control
    from memnos_brain.vault import Vault
    with conn.cursor() as c:
        try:
            c.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception as e:
            sys.exit(f"pgvector extension missing/insufficient privilege — install pgvector and grant CREATE: {e}")
    dim = 1536 if os.environ.get("OPENAI_API_KEY") else 384
    BrainStore(conn=conn).create_schema("memnos", dim=dim)
    Control.init(conn)
    secret_key = os.environ.get("MEMNOS_SECRET_KEY") or cfg.get("secret_key") or Vault.keygen()
    pid = Control.create_principal(conn, "admin", "service")
    Control.grant(conn, pid, "*")
    os.environ["MEMNOS_SECRET_KEY"] = secret_key
    tok = Control.mint_token(conn, pid, "console")
    cfg.update({"dsn": dsn, "port": cfg.get("port", 8900), "secret_key": secret_key})
    save_config(cfg)
    print(f"\n✓ schema + control plane created (embedding dim {dim})")
    print(f"✓ config written to {CONFIG_PATH}")
    print(f"\nADMIN TOKEN (shown once — paste into the /admin console or `--token`):\n  {tok}")
    print("\nNext:  memnos serve   then open  http://127.0.0.1:8900/admin")

    # friction-free: if Claude Code is installed, wire it up (MCP + hooks + /memnos + CLAUDE.md)
    if os.path.isdir(os.path.join(os.path.expanduser("~"), ".claude")):
        ans = "y" if (args.dsn or os.environ.get("MEMNOS_CI")) else \
            (input("\nClaude Code detected — wire up memnos memory (MCP + hooks + /memnos)? [Y/n]: ").strip().lower() or "y")
        if ans.startswith("y"):
            cfg = load_config()
            cmd_claude_setup(argparse.Namespace(namespace=None, force=False), cfg)


def cmd_serve(args, cfg):
    _apply_env(cfg)
    import memnos_server
    memnos_server.serve(port=args.port)


def cmd_mcp(args, cfg):
    # stdio MCP adapter for Claude Code / Cursor / Windsurf / any MCP client.
    # MEMNOS_URL / MEMNOS_TOKEN / MEMNOS_NS come from the client's env block;
    # fall back to the local config so `memnos mcp` works out of the box.
    os.environ.setdefault("MEMNOS_URL", f"http://127.0.0.1:{cfg.get('port', 8900)}")
    if cfg.get("admin_token"):
        os.environ.setdefault("MEMNOS_TOKEN", cfg["admin_token"])
    if args.namespace:
        os.environ["MEMNOS_NS"] = args.namespace
    import memnos_mcp
    memnos_mcp.mcp.run()


# ---- admin / control --------------------------------------------------------
def _principal_id(conn, name):
    with conn.cursor() as c:
        c.execute("SELECT id FROM memnos_control.principals WHERE name=%s", (name,))
        r = c.fetchone()
    if not r:
        sys.exit(f"no principal '{name}'")
    return r["id"]


def cmd_admin(args, cfg):
    from memnos_brain.control import Control
    conn = _conn(cfg)
    Control.init(conn)
    pid = Control.create_principal(conn, "admin", "service")
    Control.grant(conn, pid, "*")
    print("ADMIN TOKEN (shown once):\n  " + Control.mint_token(conn, pid, "console"))


def cmd_principal(args, cfg):
    from memnos_brain.control import Control
    pid = Control.create_principal(_conn(cfg), args.name, args.kind)
    print(f"principal '{args.name}' id={pid}")


def cmd_token(args, cfg):
    from memnos_brain.control import Control
    conn = _conn(cfg)
    tok = Control.mint_token(conn, _principal_id(conn, args.principal), args.label, args.ttl_days)
    print("TOKEN (shown once):\n  " + tok)


def cmd_grant(args, cfg):
    from memnos_brain.control import Control
    conn = _conn(cfg)
    Control.grant(conn, _principal_id(conn, args.principal), args.namespace,
                  can_read=True, can_write=not args.read_only)
    print(f"granted {args.principal} -> {args.namespace} ({'read' if args.read_only else 'read+write'})")


def cmd_namespace(args, cfg):
    from memnos_brain.control import Control
    conn = _conn(cfg)
    if args.action == "add":
        created_by = None
        try:
            created_by = _principal_id(conn, "admin")
        except SystemExit:
            pass
        Control.create_namespace(conn, args.name, created_by=created_by, description=args.desc)
        print(f"namespace '{args.name}' created")
    elif args.action == "rm":
        Control.delete_namespace(conn, args.name, purge_data=args.purge)
        print(f"namespace '{args.name}' deleted")
    elif args.action in ("copy", "move"):
        from memnos_brain.store import BrainStore
        if not args.name or not args.to:
            sys.exit("usage: memnos namespace copy|move <src> --to <dst> [--like X]")
        out = BrainStore(conn=conn).migrate_namespace("tenant_memnos", args.name, args.to,
                                                      mode=args.action, like=args.like)
        print(f"{out['mode']}d {out['facts']} facts + {out['raw_turns']} turns "
              f"from {args.name} -> {args.to}")
    else:  # ls
        for n in Control.list_namespaces(conn):
            print(f"  {n['name']:<28} turns={n['turns']} facts={n['facts']}  {n['description'] or ''}")


def cmd_secret(args, cfg):
    _apply_env(cfg)
    from memnos_brain.vault import Vault, VaultLocked
    if args.action == "keygen":
        print("MEMNOS_SECRET_KEY=" + Vault.keygen()); return
    conn = _conn(cfg)
    try:
        if args.action == "set":
            val = args.value or getpass.getpass(f"value for '{args.name}': ")
            Vault.set(conn, args.name, val, args.desc); print(f"secret '{args.name}' stored (encrypted)")
        elif args.action == "ls":
            for s in Vault.list(conn):
                print(f"  {s['name']:<24} {s['description'] or ''}")
        elif args.action == "rm":
            Vault.delete(conn, args.name); print(f"secret '{args.name}' deleted")
        elif args.action == "rotate":
            old = os.environ.get("MEMNOS_SECRET_KEY") or cfg.get("secret_key")
            if not old:
                sys.exit("no current MEMNOS_SECRET_KEY to rotate from")
            new = Vault.keygen()
            n = Vault.rotate_key(conn, old, new)
            cfg["secret_key"] = new; save_config(cfg)
            print(f"rotated {n} secret(s). New key saved to config; update .env if you use it:\n  MEMNOS_SECRET_KEY={new}")
    except VaultLocked as e:
        sys.exit(f"vault locked: {e}")


# ---- observability ----------------------------------------------------------
def cmd_stats(args, cfg):
    from memnos_brain.control import Control
    for r in Control.stats(_conn(cfg), 24):
        print(f"  {r['action']:<12} calls={r['calls']} err%={r['error_pct'] or 0} "
              f"p50={r['p50_ms'] or '-'} p95={r['p95_ms'] or '-'}")


def cmd_health(args, cfg):
    from memnos_brain.control import Control
    rows = Control.health(_conn(cfg), 24)
    print("OK — no findings" if not rows else "")
    for level, msg in rows:
        print(f"  [{level}] {msg}")


def cmd_whoami(args, cfg):
    from memnos_brain.control import Control
    conn = _conn(cfg)
    pid = Control.authenticate(conn, args.token)
    if pid is None:
        print("auth: FAIL"); return
    print(f"auth OK principal_id={pid}")
    print("grants:", [(g["namespace"], g["can_read"], g["can_write"])
                      for g in Control.authorized_namespaces(conn, pid)])


def cmd_ns(args, cfg):
    import nsresolve
    if getattr(args, "value", None) is not None:        # `memnos ns set <X>` / `ns clear`
        print(nsresolve.set_override(args.value))
    else:
        print(nsresolve.resolve())


# ---- data client ------------------------------------------------------------
def cmd_remember(args, cfg):
    import nsresolve
    ns = args.namespace if args.namespace and args.namespace != "auto" else nsresolve.resolve()
    out = _post(cfg, "/remember", {"namespace": ns, "text": args.text}, args.token or cfg.get("admin_token"))
    print(json.dumps(out))


def cmd_recall(args, cfg):
    import nsresolve
    ns = args.namespace if args.namespace and args.namespace != "auto" else nsresolve.resolve()
    body = {"namespace": ns, "query": args.query}
    if getattr(args, "scope", None) in ("all", "wide"):
        body["scope"] = "all"
    out = _post(cfg, "/recall", body, args.token or cfg.get("admin_token"))
    if out.get("namespaces_searched"):
        print(f"[searched: {', '.join(out['namespaces_searched'])}]")
    print(out.get("context", json.dumps(out)))


_SLASH_CMD = """---
description: memnos memory — recall, set folder namespace (ns=...), show (ns) or list (ns list)
allowed-tools: Bash(memnos:*)
---

!`A="$ARGUMENTS"; case "$A" in ns=*) memnos ns "${A#ns=}";; "ns clear") memnos ns clear;; "ns list"|"list"|"ls") memnos namespace ls;; ""|"ns") memnos ns;; *) memnos recall "$A" --namespace auto;; esac`

Instructions:
- `/memnos ns=proj:x` pins this folder's namespace; `/memnos ns` shows it; `/memnos ns list` lists namespaces; `/memnos ns clear` reverts.
- Otherwise, use the recalled memories above to answer: $ARGUMENTS
"""

_CLAUDE_MD = """## memnos — long-term memory (auto)
memnos gives you persistent, governed memory across sessions (local server, namespace-scoped).
- Memory **auto-injects** before each prompt and **auto-saves** after (hooks). The
  "## Relevant memories (memnos)" block in your context comes from memnos.
- Use the memnos MCP tools for recall instead of ad-hoc file search: `recall` (current
  namespace), `recall_wide` (across all namespaces your key may read), `remember`,
  `get_entity`, `get_provenance`.
- **Staleness check (important):** before answering from a local note / CLAUDE.md fact on a
  key project detail, call `reconcile_claim(statement, subject, predicate)`. If it returns
  `stale`, tell the user their local note is out of date and give the newer memnos value + its
  date. (Local notes are authoritative for the user; memnos catches when they drift.)
- This folder's namespace is set with `/memnos ns=<namespace>`.
"""


def _backup(path):
    if os.path.exists(path):
        import shutil, time
        shutil.copy2(path, f"{path}.memnos-bak")


def _ensure_claude_token(cfg):
    """A principal+token for the Claude integration: default namespace user:<user> plus a
    proj:* wildcard so per-project + widened recall work. (Grants, not namespace creation.)"""
    from memnos_brain.control import Control
    conn = _conn(cfg)
    Control.init(conn)
    name = (os.environ.get("USER") or "me").split()[0]
    try:
        pid = _principal_id(conn, name)
    except SystemExit:
        pid = Control.create_principal(conn, name, "user")
    Control.grant(conn, pid, f"user:{name}")
    Control.grant(conn, pid, "proj:*")
    return Control.mint_token(conn, pid, "claude-code"), f"user:{name}"


def cmd_claude_setup(args, cfg):
    """Auto-wire Claude Code (MCP + hooks + /memnos + CLAUDE.md) — friction-free. Idempotent;
    backs up files it edits. Detects ~/.claude; safe to re-run."""
    home = os.path.expanduser("~")
    claude_dir = os.path.join(home, ".claude")
    if not os.path.isdir(claude_dir):
        if args.force:
            os.makedirs(os.path.join(claude_dir, "commands"), exist_ok=True)
        else:
            print("Claude Code not detected (~/.claude missing). Re-run with --force to set it up anyway.")
            return
    os.makedirs(os.path.join(claude_dir, "commands"), exist_ok=True)
    url = os.environ.get("MEMNOS_URL") or f"http://127.0.0.1:{cfg.get('port', 8900)}"
    token, default_ns = _ensure_claude_token(cfg)
    ns = args.namespace or default_ns

    # 1. MCP server -> ~/.claude.json (the file Claude Code reads for MCP)
    cj = os.path.join(home, ".claude.json")
    try:
        d = json.load(open(cj)) if os.path.exists(cj) else {}
    except Exception:
        d = {}
    d.setdefault("mcpServers", {})["memnos"] = {
        "command": "memnos", "args": ["mcp"],
        "env": {"MEMNOS_URL": url, "MEMNOS_TOKEN": token, "MEMNOS_NS": ns}}
    _backup(cj); json.dump(d, open(cj, "w"), indent=2)

    # 2. hooks -> ~/.claude/settings.json (recall before prompt, remember after)
    sj = os.path.join(claude_dir, "settings.json")
    try:
        s = json.load(open(sj)) if os.path.exists(sj) else {}
    except Exception:
        s = {}
    hooks = s.setdefault("hooks", {})
    env = f"MEMNOS_URL={url} MEMNOS_TOKEN={token} MEMNOS_NS={ns}"

    def wire(event, cmd):
        groups = [g for g in hooks.get(event, []) if "memnos hook" not in json.dumps(g)]
        groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 15}]})
        hooks[event] = groups
    wire("UserPromptSubmit", f"{env} memnos hook recall")
    wire("Stop", f"{env} memnos hook remember")
    _backup(sj); json.dump(s, open(sj, "w"), indent=2)

    # 3. /memnos slash command
    open(os.path.join(claude_dir, "commands", "memnos.md"), "w").write(_SLASH_CMD)

    # 4. CLAUDE.md memnos section (append once)
    cm = os.path.join(claude_dir, "CLAUDE.md")
    existing = open(cm).read() if os.path.exists(cm) else ""
    if "## memnos — long-term memory" not in existing:
        if existing:
            _backup(cm)
        with open(cm, "a") as f:
            f.write(("\n\n" if existing else "") + _CLAUDE_MD)

    print("[memnos] Claude Code wired:")
    print(f"  • MCP server      -> ~/.claude.json (memnos, ns={ns})")
    print("  • hooks           -> ~/.claude/settings.json (auto recall + save)")
    print("  • /memnos command -> ~/.claude/commands/memnos.md")
    print("  • CLAUDE.md       -> memnos usage + staleness-check instructions")
    print("\n  Restart Claude Code to load the MCP tools. Verify with /mcp.")


_AGENT_MD = """## memnos — long-term memory (MCP tools)
memnos gives you persistent, governed memory across sessions (namespace-scoped). Call its
MCP tools explicitly (this agent has no auto-inject hooks):
- `recall` / `recall_wide` — fetch relevant past decisions, facts, people, and project state
  before answering (recall_wide searches every namespace your key may read).
- `remember` — save a durable fact/decision worth keeping.
- **Staleness check:** before answering from a local note on a key project detail, call
  `reconcile_claim(statement, subject, predicate)`. If it returns `stale`, tell the user their
  local note is out of date and give the newer memnos value + its date.
"""

# MCP-capable agents: where each keeps its MCP server config, and the format.
_AGENTS = {
    "codex":          {"path": "~/.codex/config.toml",                         "fmt": "toml", "agents_md": "~/.codex/AGENTS.md"},
    "cursor":         {"path": "~/.cursor/mcp.json",                           "fmt": "json"},
    "windsurf":       {"path": "~/.codeium/windsurf/mcp_config.json",          "fmt": "json"},
    "claude-desktop": {"path": "~/Library/Application Support/Claude/claude_desktop_config.json", "fmt": "json"},
}


def cmd_agent_setup(args, cfg):
    """Wire memnos into another MCP-capable agent (codex/cursor/windsurf/claude-desktop).
    Writes its MCP server config (+ an AGENTS.md instruction for codex). Idempotent; backs up."""
    spec = _AGENTS.get(args.agent)
    if not spec:
        sys.exit(f"unknown agent '{args.agent}' — choose: {', '.join(_AGENTS)}")
    url = os.environ.get("MEMNOS_URL") or f"http://127.0.0.1:{cfg.get('port', 8900)}"
    token, default_ns = _ensure_claude_token(cfg)
    ns = args.namespace or default_ns
    path = os.path.expanduser(spec["path"])
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if spec["fmt"] == "json":
        try:
            d = json.load(open(path)) if os.path.exists(path) else {}
        except Exception:
            d = {}
        d.setdefault("mcpServers", {})["memnos"] = {
            "command": "memnos", "args": ["mcp"],
            "env": {"MEMNOS_URL": url, "MEMNOS_TOKEN": token, "MEMNOS_NS": ns}}
        _backup(path); json.dump(d, open(path, "w"), indent=2)
    else:  # toml (codex)
        existing = open(path).read() if os.path.exists(path) else ""
        block = (f'\n[mcp_servers.memnos]\ncommand = "memnos"\nargs = ["mcp"]\n\n'
                 f'[mcp_servers.memnos.env]\nMEMNOS_URL = "{url}"\n'
                 f'MEMNOS_TOKEN = "{token}"\nMEMNOS_NS = "{ns}"\n')
        if "[mcp_servers.memnos]" in existing:
            print(f"[memnos] {args.agent}: MCP entry already present in {spec['path']} (left as-is).")
        else:
            _backup(path)
            with open(path, "a") as f:
                f.write(("\n" if existing and not existing.endswith("\n") else "") + block)
        am = os.path.expanduser(spec["agents_md"])
        cur = open(am).read() if os.path.exists(am) else ""
        if "## memnos — long-term memory" not in cur:
            if cur:
                _backup(am)
            with open(am, "a") as f:
                f.write(("\n\n" if cur else "") + _AGENT_MD)

    print(f"[memnos] {args.agent} wired -> {spec['path']} (MCP server 'memnos', ns={ns}).")
    if spec.get("agents_md"):
        print(f"          + instructions -> {spec['agents_md']}")
    print("  Note: this agent uses the memnos MCP *tools* (recall/remember/reconcile_claim) — "
          "no auto inject/save hooks (those are Claude Code only). Restart the agent to load it.")


# ---- Claude Code hook entry (`memnos hook recall|remember`) ------------------
def cmd_hook(args, cfg):
    """Stdin-driven Claude Code hooks, packaged so they work after a pipx install with no
    repo paths. recall -> inject memory before the prompt; remember -> save the turn after."""
    import nsresolve
    url = os.environ.get("MEMNOS_URL") or f"http://127.0.0.1:{cfg.get('port', 8900)}"
    token = os.environ.get("MEMNOS_TOKEN") or cfg.get("admin_token", "")
    hdr = {"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})}
    try:
        data = json.load(sys.stdin)
    except Exception:
        return
    ns = nsresolve.resolve(data)

    if args.which == "recall":
        prompt = (data.get("prompt") or "").strip()
        if not prompt:
            return
        try:
            req = urllib.request.Request(f"{url}/recall", method="POST",
                data=json.dumps({"namespace": ns, "query": prompt}).encode(), headers=hdr)
            ctx = json.load(urllib.request.urlopen(req, timeout=12)).get("context", "")
        except Exception:
            return
        if ctx.strip():
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                  "additionalContext": "## Relevant memories (memnos)\n" + ctx}}))
        return

    # remember (Stop): prefer the last user message from the transcript
    text = data.get("prompt", "")
    tp = data.get("transcript_path", "")
    if tp and os.path.exists(tp):
        try:
            last = ""
            with open(tp) as f:
                for line in f:
                    ev = json.loads(line); c = ev.get("message", {}).get("content")
                    if ev.get("type") == "user" and isinstance(c, str):
                        last = c
            text = last or text
        except Exception:
            pass
    text = (text or "").strip()
    low = text.lower()
    if not text or low.startswith("<") or text.startswith("# ") or "<<autonomous-loop" in low \
            or low.startswith("# autonomous loop") or "</task-notification" in low \
            or "this is an automated background-task event" in low \
            or "reference answer:" in low or "reply with only" in low or low.startswith("question:") \
            or len(text) < 15 or len(text.split()) < 3:
        return
    try:
        req = urllib.request.Request(f"{url}/remember", method="POST",
            data=json.dumps({"namespace": ns, "text": text, "speaker": "user"}).encode(), headers=hdr)
        urllib.request.urlopen(req, timeout=12).read()
    except Exception:
        pass


def main():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    cfg = load_config()
    ap = argparse.ArgumentParser(prog="memnos", description="memnos memory platform CLI")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("setup"); p.add_argument("--dsn"); p.set_defaults(fn=cmd_setup)
    p = sub.add_parser("serve"); p.add_argument("--port", type=int); p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("mcp"); p.add_argument("--namespace"); p.set_defaults(fn=cmd_mcp)
    p = sub.add_parser("admin"); p.set_defaults(fn=cmd_admin)
    p = sub.add_parser("principal"); p.add_argument("name"); p.add_argument("--kind", default="user"); p.set_defaults(fn=cmd_principal)
    p = sub.add_parser("token"); p.add_argument("principal"); p.add_argument("--label"); p.add_argument("--ttl-days", type=int); p.set_defaults(fn=cmd_token)
    p = sub.add_parser("grant"); p.add_argument("principal"); p.add_argument("namespace"); p.add_argument("--read-only", action="store_true"); p.set_defaults(fn=cmd_grant)
    p = sub.add_parser("namespace"); p.add_argument("action", choices=["add", "ls", "rm", "copy", "move"]); p.add_argument("name", nargs="?"); p.add_argument("--to"); p.add_argument("--like"); p.add_argument("--desc"); p.add_argument("--purge", action="store_true"); p.set_defaults(fn=cmd_namespace)
    p = sub.add_parser("secret"); p.add_argument("action", choices=["set", "ls", "rm", "keygen", "rotate"]); p.add_argument("name", nargs="?"); p.add_argument("--value"); p.add_argument("--desc"); p.set_defaults(fn=cmd_secret)
    p = sub.add_parser("stats"); p.set_defaults(fn=cmd_stats)
    p = sub.add_parser("health"); p.set_defaults(fn=cmd_health)
    p = sub.add_parser("whoami"); p.add_argument("token"); p.set_defaults(fn=cmd_whoami)
    p = sub.add_parser("ns"); p.add_argument("value", nargs="?"); p.set_defaults(fn=cmd_ns)
    p = sub.add_parser("remember"); p.add_argument("text"); p.add_argument("--namespace", default="auto"); p.add_argument("--token"); p.set_defaults(fn=cmd_remember)
    p = sub.add_parser("recall"); p.add_argument("query"); p.add_argument("--namespace", default="auto"); p.add_argument("--scope", choices=["all", "wide"]); p.add_argument("--token"); p.set_defaults(fn=cmd_recall)
    p = sub.add_parser("hook"); p.add_argument("which", choices=["recall", "remember"]); p.set_defaults(fn=cmd_hook)
    p = sub.add_parser("claude-setup"); p.add_argument("--namespace"); p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_claude_setup)
    p = sub.add_parser("agent-setup"); p.add_argument("agent", choices=list(_AGENTS)); p.add_argument("--namespace"); p.set_defaults(fn=cmd_agent_setup)

    args = ap.parse_args()
    args.fn(args, cfg)


if __name__ == "__main__":
    main()
