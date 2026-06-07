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

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".memnos")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_DSN = "postgresql://memnos:memnos_poc@localhost:5433/memnos"


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
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "integrations", "claude-code"))
    import memnos_ns
    print(memnos_ns.resolve())


# ---- data client ------------------------------------------------------------
def cmd_remember(args, cfg):
    out = _post(cfg, "/remember", {"namespace": args.namespace, "text": args.text}, args.token or cfg.get("admin_token"))
    print(json.dumps(out))


def cmd_recall(args, cfg):
    out = _post(cfg, "/recall", {"namespace": args.namespace, "query": args.query}, args.token or cfg.get("admin_token"))
    print(out.get("context", json.dumps(out)))


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
    p = sub.add_parser("namespace"); p.add_argument("action", choices=["add", "ls", "rm"]); p.add_argument("name", nargs="?"); p.add_argument("--desc"); p.add_argument("--purge", action="store_true"); p.set_defaults(fn=cmd_namespace)
    p = sub.add_parser("secret"); p.add_argument("action", choices=["set", "ls", "rm", "keygen", "rotate"]); p.add_argument("name", nargs="?"); p.add_argument("--value"); p.add_argument("--desc"); p.set_defaults(fn=cmd_secret)
    p = sub.add_parser("stats"); p.set_defaults(fn=cmd_stats)
    p = sub.add_parser("health"); p.set_defaults(fn=cmd_health)
    p = sub.add_parser("whoami"); p.add_argument("token"); p.set_defaults(fn=cmd_whoami)
    p = sub.add_parser("ns"); p.set_defaults(fn=cmd_ns)
    p = sub.add_parser("remember"); p.add_argument("text"); p.add_argument("--namespace", required=True); p.add_argument("--token"); p.set_defaults(fn=cmd_remember)
    p = sub.add_parser("recall"); p.add_argument("query"); p.add_argument("--namespace", required=True); p.add_argument("--token"); p.set_defaults(fn=cmd_recall)

    args = ap.parse_args()
    args.fn(args, cfg)


if __name__ == "__main__":
    main()
