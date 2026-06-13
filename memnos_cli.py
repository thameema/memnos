"""memnos — one cross-platform CLI for the whole platform (admin + data client + server).

    memnos setup                 # connect to YOUR Postgres, create schema + admin token
    memnos serve                 # run the server
    memnos token <principal>     # mint a bearer token
    memnos grant <p> <ns>        # grant namespace access
    memnos namespace add <ns>    # create a namespace
    memnos namespace set <ns> --kind knowledge   # mark as a knowledge namespace
    memnos namespace link <src> <dst>            # ground recall on src in dst
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
import re
import sys
import urllib.request

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".memnos")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "server.log")
PID_PATH = os.path.join(CONFIG_DIR, "server.pid")
DEFAULT_DSN = "postgresql://memnos:memnos@localhost:5432/memnos"


def _installed_version():
    try:
        import importlib.metadata as _m
        return _m.version("memnos")
    except Exception:
        return None


def _version():
    v = _installed_version()
    return ("v" + v) if v else "(dev)"


def _vparts(v):
    out = []
    for p in (v or "0").split(".")[:4]:
        n = "".join(c for c in p if c.isdigit())
        out.append(int(n) if n else 0)
    return tuple(out)


def _latest_pypi_version(timeout=4):
    import urllib.request
    import json as _json
    try:
        with urllib.request.urlopen("https://pypi.org/pypi/memnos/json", timeout=timeout) as r:
            return _json.load(r)["info"]["version"]
    except Exception:
        return None


def _upgrade_cmd():
    """Pick the right upgrade command for however memnos was installed (uv tool / pipx / pip)."""
    import shutil
    import subprocess
    if shutil.which("uv"):
        out = subprocess.run(["uv", "tool", "list"], capture_output=True, text=True).stdout
        if "memnos" in out:
            return ["uv", "tool", "upgrade", "memnos"]
    if shutil.which("pipx"):
        out = subprocess.run(["pipx", "list", "--short"], capture_output=True, text=True).stdout
        if "memnos" in out:
            return ["pipx", "upgrade", "memnos"]
    return [sys.executable, "-m", "pip", "install", "-U", "memnos"]


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
def _post(cfg, path, payload, token, timeout=120):
    import urllib.request
    import urllib.error
    req = urllib.request.Request(_server_url(cfg) + path, method="POST",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read() or b"{}")
    except urllib.error.HTTPError as e:
        try:                                   # error body may be truncated / non-JSON
            msg = json.loads(e.read() or b"{}").get("error", "?")
        except Exception:
            msg = "?"
        hint = "  (no/invalid token — pass --token, or re-run `memnos setup`)" if e.code == 401 else ""
        sys.exit(f"server error {e.code}: {msg}{hint}")
    except Exception as e:                     # URLError, TimeoutError (3.11+), reset, bad JSON
        sys.exit(f"cannot reach server at {_server_url(cfg)} — is it running? "
                 f"({type(e).__name__}: {e})\nstart it with:  memnos start")


# ---- setup wizard -----------------------------------------------------------
MIN_PG_MAJOR = 13          # generated STORED columns need 12; 13 is our tested floor
# pgvector floor. 0.6.0 (what Debian/Ubuntu apt ships) is enough: memnos feature-detects the
# version and uses full-precision `vector` columns on 0.6, half-precision `halfvec` on >= 0.7.
# A clean `apt install postgresql-N-pgvector` therefore works with no source build.
MIN_PGVECTOR = (0, 6, 0)
HALFVEC_PGVECTOR = (0, 7, 0)   # halfvec storage optimization needs >= 0.7

DOCKER_PG_CONTAINER = "memnos-pg"
DOCKER_PG_IMAGE = "pgvector/pgvector:pg16"   # Postgres + pgvector pre-baked, version-matched


def _ensure_docker_pg():
    """Provision (or reuse) a pgvector Postgres in Docker and return a DSN to it. The image
    ships pgvector pre-installed for its PG version, so there's no version-matching to do."""
    import shutil
    import subprocess
    import secrets
    import time
    if not shutil.which("docker"):
        sys.exit("--docker needs Docker, which isn't installed.\n"
                 "  Install Docker Desktop (https://docker.com), or run `memnos setup --dsn ...` "
                 "against your own Postgres.")
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True)
    except Exception:
        sys.exit("Docker is installed but not running — start Docker Desktop and re-run "
                 "`memnos setup --docker`.")
    name, image, user, db = DOCKER_PG_CONTAINER, DOCKER_PG_IMAGE, "memnos", "memnos"

    def _port():  # host port mapped to the container's 5432
        out = subprocess.run(["docker", "port", name, "5432/tcp"], capture_output=True, text=True).stdout.strip()
        return out.rsplit(":", 1)[-1] if out else None

    def _env(key):
        out = subprocess.run(["docker", "inspect", "--format",
                              "{{range .Config.Env}}{{println .}}{{end}}", name],
                             capture_output=True, text=True).stdout
        for line in out.splitlines():
            if line.startswith(key + "="):
                return line.split("=", 1)[1]
        return None

    running = subprocess.run(["docker", "ps", "-q", "-f", f"name=^{name}$"],
                             capture_output=True, text=True).stdout.strip()
    exists = subprocess.run(["docker", "ps", "-aq", "-f", f"name=^{name}$"],
                            capture_output=True, text=True).stdout.strip()
    if not running and exists:
        subprocess.run(["docker", "start", name], capture_output=True, check=True)
        running = exists
    if running:
        port, pw = _port(), _env("POSTGRES_PASSWORD")
        if port and pw:
            dsn = f"postgresql://{user}:{pw}@localhost:{port}/{db}"
            print(f"[memnos] reusing pgvector container '{name}' on localhost:{port}")
            _wait_dsn(dsn)
            return dsn
        subprocess.run(["docker", "rm", "-f", name], capture_output=True)   # malformed — recreate

    port = _free_port([5432, 5433, 5434])
    pw = secrets.token_hex(12)
    print(f"[memnos] starting pgvector container '{name}' ({image}) on localhost:{port} ...")
    subprocess.run(["docker", "run", "-d", "--name", name, "-p", f"{port}:5432",
                    "-e", f"POSTGRES_USER={user}", "-e", f"POSTGRES_PASSWORD={pw}",
                    "-e", f"POSTGRES_DB={db}", image], capture_output=True, check=True)
    dsn = f"postgresql://{user}:{pw}@localhost:{port}/{db}"
    _wait_dsn(dsn)
    print(f"[memnos] ✓ pgvector Postgres ready on localhost:{port}")
    return dsn


def _wait_dsn(dsn, tries=90):
    """Wait until a real host-side connection succeeds — the official PG image flaps the TCP
    listener during first-boot init, so pg_isready isn't enough; only a clean connect is."""
    import time
    import psycopg
    last = None
    for _ in range(tries):
        try:
            psycopg.connect(dsn, connect_timeout=3).close()
            return
        except Exception as e:
            last = e
            time.sleep(1)
    sys.exit(f"Postgres container didn't accept connections in time: {last}\n"
             f"Check `docker logs {DOCKER_PG_CONTAINER}`.")


def _free_port(prefer):
    import socket
    for p in prefer:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:   # nothing listening -> free
                return p
    # fall back to an ephemeral free port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _vtuple(v):
    """'0.8.2' -> (0,8,2); tolerant of junk."""
    parts = []
    for p in (v or "").split(".")[:3]:
        n = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(n) if n else 0)
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def _pgvector_install_hint(pg_major=None):
    maj = pg_major or "16"
    if sys.platform == "darwin":
        return (f"  macOS (Homebrew):  brew install pgvector  &&  brew services restart postgresql@{maj}\n"
                "    ⚠ `brew install pgvector` builds for ONE Postgres version. If you already\n"
                "      installed it but still see this, it was built for a DIFFERENT version than\n"
                f"      the @{maj} you're connecting to. Build it for this one:\n"
                f"        cd /tmp && git clone --branch v0.8.2 https://github.com/pgvector/pgvector\n"
                f"        cd pgvector && make install PG_CONFIG=$(brew --prefix postgresql@{maj})/bin/pg_config\n"
                f"        brew services restart postgresql@{maj}")
    if sys.platform.startswith("linux"):
        return (f"  Debian/Ubuntu:  sudo apt install postgresql-{maj}-pgvector\n"
                f"  RHEL/Fedora:    sudo dnf install pgvector_{maj}")
    return "  See https://github.com/pgvector/pgvector#installation"


def _pg_not_reachable_hint(host, port):
    base = (f"Couldn't reach PostgreSQL at {host}:{port}. memnos does not install Postgres — "
            "it connects to yours.\n")
    if sys.platform == "darwin":
        return base + ("  Is it running?   brew services start postgresql@16\n"
                       "  Not installed?   brew install postgresql@16 && brew install pgvector\n"
                       "  (Alternative: memnos setup --docker runs a pgvector Postgres for you.)")
    if sys.platform.startswith("linux"):
        return base + ("  Is it running?   sudo systemctl start postgresql\n"
                       "  Not installed?   sudo apt install postgresql postgresql-16-pgvector")
    return base + "  Start your PostgreSQL server (needs the pgvector >= 0.6 extension) and re-run."


def _preflight_postgres(conn):
    """Detect + validate the server: PG version, pgvector availability, enable it, verify the
    pgvector version supports halfvec. Exits with an actionable message on any problem."""
    with conn.cursor() as c:
        c.execute("SHOW server_version")
        pgver = c.fetchone()["server_version"]
        c.execute("SELECT current_setting('server_version_num')::int AS n")
        pgnum = c.fetchone()["n"]
        if pgnum < MIN_PG_MAJOR * 10000:
            sys.exit(f"PostgreSQL {pgver} found — memnos needs >= {MIN_PG_MAJOR}. Please upgrade.")
        pg_major = str(pgnum // 10000)
        print(f"[memnos] ✓ PostgreSQL {pgver}")

        c.execute("SELECT installed_version FROM pg_available_extensions WHERE name = 'vector'")
        avail = c.fetchone()
        if not avail:
            sys.exit("pgvector (the 'vector' extension, >= 0.6) is NOT available to THIS Postgres server.\n"
                     "Install it for this server, then re-run `memnos setup`:\n"
                     + _pgvector_install_hint(pg_major) +
                     "\n\n  (Alternative — let memnos run a pre-configured pgvector Postgres in Docker:"
                     "  memnos setup --docker)")
        try:
            c.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception as e:
            sys.exit("pgvector is available but couldn't be enabled — creating an extension "
                     "needs a superuser role.\n"
                     f"  {str(e).strip()}\n"
                     "Have a superuser run:  CREATE EXTENSION vector;  (in this database), "
                     "then re-run `memnos setup` — or run setup as a superuser role.")
        c.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ver = c.fetchone()["extversion"]
        if _vtuple(ver) < MIN_PGVECTOR:
            sys.exit(f"pgvector {ver} is enabled, but memnos needs >= "
                     f"{'.'.join(map(str, MIN_PGVECTOR))}. Upgrade pgvector:\n"
                     + _pgvector_install_hint(pg_major))
        if _vtuple(ver) < HALFVEC_PGVECTOR:
            print(f"[memnos] ✓ pgvector {ver} enabled "
                  f"(< {'.'.join(map(str, HALFVEC_PGVECTOR))}: using full-precision `vector` "
                  f"columns — halfvec storage optimization is skipped, no functional difference)")
        else:
            print(f"[memnos] ✓ pgvector {ver} enabled (halfvec)")


def _openai_key_ok(key):
    """Live-validate an OpenAI key (free call, no tokens). True = usable."""
    try:
        from openai import OpenAI, AuthenticationError
        OpenAI(api_key=key, timeout=15, max_retries=1).models.list()
        return True
    except Exception as e:
        name = type(e).__name__
        if name == "AuthenticationError" or "401" in str(e) or "invalid_api_key" in str(e).lower():
            print("  ✗ OpenAI rejected this key (invalid or revoked) — check it and try again.")
            return False
        # network / proxy / outage — can't verify, let the user decide
        print(f"  ⚠ could not reach the OpenAI API to validate ({name}: {e})")
        return input("    accept the key UNVERIFIED anyway? [y/N]: ").strip().lower() in ("y", "yes")


def cmd_setup(args, cfg):
    print("=== memnos setup (Postgres is a prerequisite — this only creates objects in it) ===")
    dsn = args.dsn or os.environ.get("MEMNOS_DSN")
    if getattr(args, "docker", False) and not dsn:
        dsn = _ensure_docker_pg()       # memnos provisions a pgvector Postgres for you
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
    from urllib.parse import urlsplit
    u = urlsplit(dsn)
    host, port = (u.hostname or "localhost"), (u.port or 5432)
    try:
        conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    except Exception as e:
        # friction-free: if only the DATABASE is missing, create it (connect to 'postgres')
        if "does not exist" in str(e).lower() and "database" in str(e).lower():
            try:
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
        elif any(k in str(e).lower() for k in ("could not connect", "connection refused",
                                               "could not translate", "timeout", "no route")):
            sys.exit(_pg_not_reachable_hint(host, port))
        else:
            sys.exit(f"could not connect to Postgres: {e}")

    # detect + validate PostgreSQL version, then verify + enable pgvector (>= 0.6; halfvec on >= 0.7)
    _preflight_postgres(conn)

    from core.store import BrainStore
    from core.control import Control
    from core.vault import Vault
    secret_key = os.environ.get("MEMNOS_SECRET_KEY") or cfg.get("secret_key") or Vault.keygen()
    os.environ["MEMNOS_SECRET_KEY"] = secret_key

    # OpenAI key — optional; enables 1536-d embeddings + bi-temporal fact extraction. Without
    # it, memnos runs free local 384-d mode (embeddings only). Stored ENCRYPTED in the vault.
    openai_key = None
    if not os.environ.get("OPENAI_API_KEY") and not (args.dsn or os.environ.get("MEMNOS_CI")):
        print("\n── Embeddings: choose now (this is PERMANENT for this database) ──────────────")
        print("  With an OpenAI key:  1536-d OpenAI embeddings + LLM fact extraction →")
        print("                       stronger recall + structured bi-temporal facts. Costs")
        print("                       OpenAI usage per write; your key is encrypted in the vault.")
        print("  Without a key:       free LOCAL 384-d embeddings, no extraction, no cost, fully")
        print("                       private (nothing leaves your machine).")
        print("  ⚠ The embedding dimension (1536 vs 384) is baked into the schema. Switching")
        print("    later means re-embedding EVERY stored memory (`memnos migrate-embeddings`)")
        print("    — it works, but choose right the first time.")
        try:                                  # drop buffered type-ahead/paste so a stray leading
            import termios                    # newline can't be read as "blank = local mode"
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
        except Exception:
            pass
        while True:
            entered = getpass.getpass(
                "\n  OpenAI API key (input is HIDDEN as you paste — leave blank for free local mode): ")
            entered = "".join(entered.split())  # keys never contain whitespace — scrub any the paste added
            if entered:
                masked = (entered[:6] + "…" + entered[-4:]) if len(entered) > 12 else "•" * len(entered)
                if not entered.startswith("sk-") or len(entered) < 40:
                    print(f"  ✗ that doesn't look like an OpenAI key ({masked}, {len(entered)} chars — "
                          "expected sk-…, 40+ chars). The paste may have been cut off — try again.")
                    continue
                print(f"  · validating key against the OpenAI API ({masked}, {len(entered)} chars) …")
                if not _openai_key_ok(entered):
                    continue
                openai_key = entered
                os.environ["OPENAI_API_KEY"] = entered  # so the schema is built at 1536-d
                print("  ✓ key VALID — will be stored encrypted in the vault.")
                break
            # blank could be a stray newline from a paste — confirm before locking in 384-d
            ans = input("  No key entered — confirm FREE LOCAL 384-d mode? [Y/n] (n = re-enter key): ").strip().lower()
            if ans in ("", "y", "yes"):
                print("  → using free local 384-d mode (embeddings only, no extraction).")
                break

    dim = 1536 if os.environ.get("OPENAI_API_KEY") else 384
    BrainStore(conn=conn).create_schema("memnos", dim=dim)
    Control.init(conn)
    if openai_key:
        Vault.set(conn, "openai", openai_key, "OpenAI API key (embeddings + extraction)")
        cfg["openai"] = "secret://openai"               # server resolves this from the vault
    pid = Control.create_principal(conn, "admin", "service")
    Control.grant(conn, pid, "*")
    tok = Control.mint_token(conn, pid, "console")
    # persist the admin token (config is already 0600 and holds the vault master key) so
    # `memnos recall/remember` work out of the box — without it every CLI call 401s
    cfg.update({"dsn": dsn, "port": cfg.get("port", 8900), "secret_key": secret_key,
                "admin_token": tok})
    save_config(cfg)
    print(f"\n✓ schema + control plane created (embedding dim {dim}"
          f"{' — OpenAI key stored in the encrypted vault' if openai_key else ''})")
    print(f"✓ config written to {CONFIG_PATH}")
    print(f"\nADMIN TOKEN (shown once — paste into the /admin console or `--token`):\n  {tok}")

    # friction-free: if Claude Code is installed, wire it up (MCP + hooks + /memnos + CLAUDE.md)
    if os.path.isdir(os.path.join(os.path.expanduser("~"), ".claude")):
        ans = "y" if (args.dsn or os.environ.get("MEMNOS_CI")) else \
            (input("\nClaude Code detected — wire up memnos memory (MCP + hooks + /memnos)? [Y/n]: ").strip().lower() or "y")
        if ans.startswith("y"):
            cfg = load_config()
            cmd_claude_setup(argparse.Namespace(namespace=None, force=False), cfg)

    # offer autostart (login service) — the #1 cause of "my agent has no memory" is a
    # server that was never started after a reboot
    if sys.platform in ("darwin",) or sys.platform.startswith("linux"):
        if not (args.dsn or os.environ.get("MEMNOS_CI")) and not _autostart_installed():
            ans = input("\nStart memnos automatically at login (recommended — survives reboots, "
                        "waits for Postgres)? [Y/n]: ").strip().lower() or "y"
            if ans.startswith("y"):
                cmd_autostart(argparse.Namespace(remove=False), cfg)

    port = cfg.get("port", 8900)
    print("\n" + "═" * 70)
    if _autostart_installed():
        print("  ✓ Setup complete — the server is starting via autostart")
        print("")
        print("      memnos status         # check it (first start downloads ~1 GB of models)")
    else:
        print("  ✓ Setup complete — but ONE more step: START THE SERVER")
        print("")
        print("      memnos start          # starts the server in the background")
    print("")
    print("  Nothing works until the server is running — recall, the MCP tools, and any")
    print(f"  agent memory you just wired all talk to it. Then open  http://127.0.0.1:{port}/admin")
    print("  Manage it with:  memnos status · memnos restart · memnos stop · memnos autostart")
    print("═" * 70)


def _preflight_pg(cfg):
    """Fail FAST with a clear message if Postgres is down — never let the user discover
    it via a hanging server. Returns silently when reachable."""
    import psycopg
    from urllib.parse import urlparse
    dsn = _dsn(cfg)
    try:
        psycopg.connect(dsn, connect_timeout=5).close()
    except Exception:
        u = urlparse(dsn if "//" in dsn else "postgresql://" + dsn)
        sys.exit(_pg_not_reachable_hint(u.hostname or "localhost", u.port or 5432) +
                 "\n\nmemnos was NOT started — once Postgres is up, run `memnos start` again.\n"
                 "(Tip: `memnos autostart` installs a login service that keeps retrying until "
                 "Postgres is up.)")


def cmd_start(args, cfg):
    """Start the server in the background (the usual way to run it)."""
    import subprocess
    import time
    _apply_env(cfg)
    port = args.port or int(os.environ.get("MEMNOS_PORT", cfg.get("port", 8900)))
    _preflight_pg(cfg)
    svc = _autostart_installed()
    if svc:                                   # managed by launchd/systemd — start through it
        kind, path = svc
        url = f"http://127.0.0.1:{port}"
        if _server_up(url):
            sys.exit(f"a memnos server is already running at {url} (autostart service).")
        if kind == "launchd":
            subprocess.run(["launchctl", "load", path], capture_output=True)
            subprocess.run(["launchctl", "kickstart", f"gui/{os.getuid()}/com.memnos.server"], capture_output=True)
        else:
            subprocess.run(["systemctl", "--user", "start", "memnos"], capture_output=True)
        print(f"[memnos] starting via the autostart service ({kind}) ...")
        for _ in range(240):
            if _server_up(url):
                print(f"[memnos] ✓ server running at {url}   ·   logs: {LOG_PATH}")
                return
            time.sleep(1.5)
        sys.exit(f"server still not up — check:  tail {LOG_PATH}")
    _serve_background(port)


def cmd_restart(args, cfg):
    import subprocess
    import time
    _apply_env(cfg)
    port = args.port or int(os.environ.get("MEMNOS_PORT", cfg.get("port", 8900)))
    _preflight_pg(cfg)
    svc = _autostart_installed()
    if svc:
        kind, path = svc
        if kind == "launchd":
            subprocess.run(["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/com.memnos.server"],
                           capture_output=True)
        else:
            subprocess.run(["systemctl", "--user", "restart", "memnos"], capture_output=True)
        url = f"http://127.0.0.1:{port}"
        print(f"[memnos] restarting via the autostart service ({kind}) ...")
        for _ in range(240):
            if _server_up(url):
                print(f"[memnos] ✓ server running at {url}")
                return
            time.sleep(1.5)
        sys.exit(f"server still not up — check:  tail {LOG_PATH}")
    _stop_quiet()
    url = f"http://127.0.0.1:{port}"
    went_down = False
    for _ in range(20):                      # wait for the old server to release the port
        if not _server_up(url):
            went_down = True
            break
        time.sleep(0.5)
    if not went_down:
        # a restart that doesn't restart is a LIE (field: leaked process survived every
        # "restart" because an unmanaged service owned it) — fail loudly instead
        sys.exit(f"restart FAILED: a memnos server is still running at {url} that this CLI "
                 "does not manage (no/stale pid file). It may be an old launchd/systemd "
                 "service or a foreground `memnos serve`.\n"
                 "  find it:   launchctl list | grep -i memnos     (macOS)\n"
                 "             systemctl --user list-units | grep -i memnos   (Linux)\n"
                 "  or:        lsof -i :%d   then kill that pid, and use `memnos autostart` "
                 "going forward." % port)
    _serve_background(port)


def cmd_serve(args, cfg):
    """Run the server in the FOREGROUND — for process managers (systemd/launchd), Docker, or
    debugging. Most users want `memnos start` (background)."""
    _apply_env(cfg)
    port = args.port or int(os.environ.get("MEMNOS_PORT", cfg.get("port", 8900)))
    print(f"[memnos] server (foreground) on http://127.0.0.1:{port}  —  Ctrl-C to stop "
          f"(or use `memnos start` to run it in the background)")
    import memnos_server
    memnos_server.serve(port=args.port)


def _serve_background(port):
    import subprocess
    import shutil
    import time
    import urllib.request
    os.makedirs(CONFIG_DIR, exist_ok=True)
    url = f"http://127.0.0.1:{port}"
    if _server_up(url):
        sys.exit(f"a memnos server is already running at {url} (stop it with `memnos stop`).")
    exe = shutil.which("memnos")
    cmd = ([exe, "serve"] if exe else [sys.executable, os.path.abspath(__file__), "serve"]) + ["--port", str(port)]
    _rotate_log()
    log = open(LOG_PATH, "a")
    proc = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            start_new_session=True, env=dict(os.environ))
    with open(PID_PATH, "w") as f:
        f.write(str(proc.pid))
    print(f"[memnos] starting server in the background (pid {proc.pid}) ...")
    last_line = ""
    for i in range(240):                      # up to ~6 min — first start downloads models
        if _server_up(url):
            print(f"[memnos] ✓ server running at {url}   ·   console: {url}/admin")
            print(f"         logs:  {LOG_PATH}")
            print(f"         stop:  memnos stop")
            return
        if proc.poll() is not None:
            tail = ""
            try:
                tail = "".join(open(LOG_PATH).readlines()[-12:])
            except Exception:
                pass
            sys.exit(f"server exited on startup — last log lines:\n{tail}\n(full log: {LOG_PATH})")
        if i == 4:
            print("  · still starting — a FIRST start downloads the local embedding/reranker")
            print(f"    models (~1 GB), which can take a few minutes. Watching {LOG_PATH}:")
        if i >= 4:                            # surface log progress so it never looks hung
            try:
                cur = open(LOG_PATH).readlines()[-1].replace("\r", " ").strip()
                if cur and cur != last_line:
                    print(f"    · {cur[:110]}")
                    last_line = cur
            except Exception:
                pass
        time.sleep(1.5)
    sys.exit(f"[memnos] server still not up after ~6 min — check `memnos status` and:  tail {LOG_PATH}")


def _rotate_log(max_bytes=10 * 1024 * 1024):
    """Keep ~/.memnos/server.log bounded: at >10MB roll to server.log.1 (one generation)."""
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > max_bytes:
            os.replace(LOG_PATH, LOG_PATH + ".1")
    except OSError:
        pass


def _server_up(url, timeout=2):
    import urllib.request
    try:
        urllib.request.urlopen(url + "/healthz", timeout=timeout).read()
        return True
    except Exception:
        return False


def _pidfile_pid():
    """Read PID_PATH and return ('alive', pid) / ('dead', pid) / ('none', None).
    Only `memnos start` writes this file; an autostart-managed server writes none."""
    if not os.path.exists(PID_PATH):
        return ("none", None)
    try:
        pid = int(open(PID_PATH).read().strip())
    except (ValueError, OSError):
        return ("dead", None)
    try:
        os.kill(pid, 0)
        return ("alive", pid)
    except PermissionError:        # exists but owned by another user → treat as alive
        return ("alive", pid)
    except (ProcessLookupError, OSError):
        return ("dead", pid)


def _background_status(running: bool, svc, pidstate) -> dict:
    """Pure decision for the `background:` line of `memnos status`, reconciling the pidfile
    with ACTUAL liveness (healthz). Pure so it is unit-testable without launchd/systemd or a
    real server (issue: status falsely reported "STALE pid file" for autostart-managed
    servers, which write no pidfile).

      running   = healthz responded (server is live on its port)
      svc       = _autostart_installed() result (truthy → ('launchd'|'systemd', path))
      pidstate  = _pidfile_pid() result: ('alive'|'dead'|'none', pid)

    Returns {lines:[...], stale_warning:bool, managed:str}. RULE: if the server RESPONDS it
    is RUNNING; the only genuine "stale pid file" warning is a DEAD pid AND nothing serving.
    """
    state, pid = pidstate
    lines, stale_warning, managed = [], False, "none"
    svc_kind = svc[0] if svc else None

    if state == "alive":
        lines.append(f"  background: pid {pid}   ·   logs: {LOG_PATH}")
        managed = "start"                       # owned by `memnos start`
    elif state == "dead":
        if running and svc:
            lines.append(f"  background: running (autostart-managed, {svc_kind})   ·   logs: {LOG_PATH}")
            lines.append("             (stale pid file from a prior `memnos start` ignored — "
                         "the live server is the autostart service)")
            managed = "autostart"
        elif running:
            lines.append("  background: running, but NOT managed by `memnos start` (stale pid "
                         "file; the live server is from another manager)   ·   logs: " + LOG_PATH)
            managed = "other"
        else:                                   # dead pid AND nothing serving → genuinely stale
            lines.append("  background: STALE pid file (that process is gone) — the running "
                         "server, if any, is NOT managed by `memnos start`")
            stale_warning = True
    else:                                       # no pidfile
        if running and svc:
            lines.append(f"  background: running (autostart-managed, {svc_kind})   ·   logs: {LOG_PATH}")
            managed = "autostart"

    # `stop/restart` can't control a live server this CLI doesn't own and isn't autostart.
    if running and managed not in ("start", "autostart"):
        lines.append("  ⚠ server is running but unmanaged (foreground shell, an old launchd/systemd "
                     "service, or another manager) — `memnos stop/restart` may not control it.")
    return {"lines": lines, "stale_warning": stale_warning, "managed": managed}


def cmd_status(args, cfg):
    port = int(os.environ.get("MEMNOS_PORT", cfg.get("port", 8900)))
    url = f"http://127.0.0.1:{port}"
    print(f"memnos {_version()}")
    have_cfg = os.path.exists(CONFIG_PATH)
    print(f"  config:    {CONFIG_PATH}  ({'ok' if have_cfg else 'missing — run: memnos setup'})")
    dsn = cfg.get("dsn")
    if dsn:
        import re
        redacted = re.sub(r"://([^:]+):[^@]*@", r"://\1:****@", dsn)
        print(f"  database:  {redacted}")
    else:
        print("  database:  not configured  (run: memnos setup)")
    running = _server_up(url)
    # Prefer the live server's truth (handles local-LLM extraction via MEMNOS_EXTRACT_BASE_URL,
    # which the static config doesn't reflect). Fall back to config-based guess if unavailable.
    prov = None
    if running:
        tok = os.environ.get("MEMNOS_TOKEN") or cfg.get("admin_token")
        if tok:
            try:
                import urllib.request
                req = urllib.request.Request(url + "/admin/api/provider",
                                             headers={"Authorization": "Bearer " + tok})
                prov = json.load(urllib.request.urlopen(req, timeout=2))
            except Exception:
                prov = None
    if prov:
        emb = "OpenAI 1536-d" if prov.get("mode") == "openai" else f"local {prov.get('dim', 384)}-d (free)"
        if prov.get("extraction"):
            base = prov.get("extract_base_url")
            model = prov.get("extract_model") or "?"
            ex = (f"local LLM extraction ({model} @ {base})" if base
                  else f"OpenAI extraction ({model})")
        else:
            ex = "no extraction"
        mode = f"{emb} · {ex}"
    else:
        mode = "OpenAI 1536-d + extraction" if (cfg.get("openai") or os.environ.get("OPENAI_API_KEY")) \
            else "local 384-d (free, no extraction)"
    print(f"  embeddings: {mode}")
    if running:
        print(f"  server:    RUNNING at {url}   ·   console: {url}/admin")
    else:
        print(f"  server:    not running   (run: memnos start)")
    svc = _autostart_installed()
    verdict = _background_status(running, svc, _pidfile_pid())
    for line in verdict["lines"]:
        print(line)
    print(f"  autostart: {'installed (' + svc[0] + ') — starts at login, restarts on failure' if svc else 'not installed   (run: memnos autostart)'}")
    if cfg.get("proxy_token"):                    # capture proxy configured on this machine
        pport = (cfg.get("proxy") or {}).get("port", 8910)
        purl = f"http://127.0.0.1:{pport}"
        try:
            import urllib.request
            h = json.load(urllib.request.urlopen(purl + "/healthz", timeout=2))
            s = h.get("stats", {})
            print(f"  proxy:     RUNNING at {purl}   ·   captured {s.get('captured', 0)} · "
                  f"skipped {s.get('skipped', 0)} · errors {s.get('errors', 0) + s.get('relay_errors', 0)}")
            if s.get("last_error"):
                print(f"             last error: {s['last_error']}")
        except Exception:
            print(f"  proxy:     not running   (run: memnos proxy — clients pointed at :{pport} "
                  "will FAIL until it's up)")


# ---- autostart (login service: launchd on macOS, systemd --user on Linux) ----------
_LAUNCHD_PLIST = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", "com.memnos.server.plist")
_SYSTEMD_UNIT = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user", "memnos.service")
_LAUNCHD_PROXY = os.path.join(os.path.expanduser("~"), "Library", "LaunchAgents", "com.memnos.proxy.plist")
_SYSTEMD_PROXY = os.path.join(os.path.expanduser("~"), ".config", "systemd", "user", "memnos-proxy.service")


def _autostart_installed():
    if sys.platform == "darwin":
        return ("launchd", _LAUNCHD_PLIST) if os.path.exists(_LAUNCHD_PLIST) else None
    if sys.platform.startswith("linux"):
        return ("systemd", _SYSTEMD_UNIT) if os.path.exists(_SYSTEMD_UNIT) else None
    return None


def cmd_autostart(args, cfg):
    """Install (or --remove) a login service so the memnos server starts automatically and
    keeps retrying until Postgres is up — no more 'Claude has no memory because I forgot
    to start the server'."""
    import shutil
    import subprocess
    exe = shutil.which("memnos") or os.path.abspath(__file__)

    def _plist(label, prog_args, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        argxml = "".join(f"<string>{a}</string>" for a in prog_args)
        with open(path, "w") as f:
            f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>{argxml}</array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>{LOG_PATH}</string>
  <key>StandardErrorPath</key><string>{LOG_PATH}</string>
</dict></plist>
""")
        subprocess.run(["launchctl", "unload", path], capture_output=True)   # reload if present
        r = subprocess.run(["launchctl", "load", path], capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"launchctl load failed for {label}: {r.stderr.strip()}")

    if sys.platform == "darwin":
        if args.remove:
            for p in (_LAUNCHD_PLIST, _LAUNCHD_PROXY):
                subprocess.run(["launchctl", "unload", p], capture_output=True)
                try:
                    os.remove(p)
                except OSError:
                    pass
            print("[memnos] autostart removed (launchd services unloaded + plists deleted).")
            return
        _rotate_log()
        _plist("com.memnos.server", [exe, "serve"], _LAUNCHD_PLIST)
        print(f"[memnos] ✓ autostart installed (launchd) — the server now starts at login,")
        print(f"  restarts if it dies, and waits for Postgres if it isn't up yet.")
        if getattr(args, "proxy", False):
            _plist("com.memnos.proxy", [exe, "proxy"], _LAUNCHD_PROXY)
            print("  ✓ capture proxy autostart installed too (com.memnos.proxy) — clients")
            print("    pointed at the proxy keep working after every reboot.")
        print(f"  service: {_LAUNCHD_PLIST}\n  logs:    {LOG_PATH}\n  remove:  memnos autostart --remove")
    elif sys.platform.startswith("linux"):
        def _unit(name, desc, cmdline, path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(f"[Unit]\nDescription={desc}\nAfter=network.target\n\n"
                        f"[Service]\nExecStart={cmdline}\nRestart=always\nRestartSec=10\n"
                        f"StandardOutput=append:{LOG_PATH}\nStandardError=append:{LOG_PATH}\n\n"
                        f"[Install]\nWantedBy=default.target\n")
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            r = subprocess.run(["systemctl", "--user", "enable", "--now", name], capture_output=True, text=True)
            if r.returncode != 0:
                sys.exit(f"systemctl enable failed for {name}: {r.stderr.strip()}")

        if args.remove:
            for name, path in (("memnos", _SYSTEMD_UNIT), ("memnos-proxy", _SYSTEMD_PROXY)):
                subprocess.run(["systemctl", "--user", "disable", "--now", name], capture_output=True)
                try:
                    os.remove(path)
                except OSError:
                    pass
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
            print("[memnos] autostart removed (systemd user units disabled + deleted).")
            return
        _rotate_log()
        _unit("memnos", "memnos memory server", f"{exe} serve", _SYSTEMD_UNIT)
        print("[memnos] ✓ autostart installed (systemd --user) — starts at login, restarts on failure,")
        print(f"  waits for Postgres.\n  unit: {_SYSTEMD_UNIT}\n  logs: {LOG_PATH}\n  remove: memnos autostart --remove")
        if getattr(args, "proxy", False):
            _unit("memnos-proxy", "memnos LLM capture proxy", f"{exe} proxy", _SYSTEMD_PROXY)
            print("  ✓ capture proxy autostart installed too (memnos-proxy.service).")
    else:
        print("[memnos] Windows: create a logon task that runs `memnos serve`:\n"
              f'  schtasks /create /tn memnos /tr "{exe} serve" /sc onlogon\n'
              "  (remove with: schtasks /delete /tn memnos)")


def _stop_quiet():
    """Best-effort stop of the background server; returns the pid it killed, or None."""
    import signal
    if not os.path.exists(PID_PATH):
        return None
    pid = None
    try:
        pid = int(open(PID_PATH).read().strip())
        os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, ValueError):
        pid = None
    except Exception:
        pass
    try:
        os.remove(PID_PATH)
    except OSError:
        pass
    return pid


def cmd_stop(args, cfg):
    import subprocess
    svc = _autostart_installed()
    if svc:                                   # managed by launchd/systemd — stop through it
        kind, path = svc                      # (killing the pid would just get auto-restarted)
        if kind == "launchd":
            subprocess.run(["launchctl", "unload", path], capture_output=True)
        else:
            subprocess.run(["systemctl", "--user", "stop", "memnos"], capture_output=True)
        _stop_quiet()                         # clean up any stray manually-started copy too
        print(f"[memnos] stopped ({kind} service unloaded — it returns at next login; "
              "remove permanently with `memnos autostart --remove`)")
        return
    if not os.path.exists(PID_PATH):
        sys.exit("no background memnos server recorded (no pid file). If it's in the foreground, Ctrl-C it.")
    pid = _stop_quiet()
    print(f"[memnos] stopped background server (pid {pid})" if pid else
          "[memnos] server wasn't running (cleaned up stale pid file)")


def cmd_upgrade(args, cfg):
    cur = _installed_version()
    print(f"[memnos] checking for updates (installed v{cur or '?'}) ...")
    latest = _latest_pypi_version()
    if not latest:
        sys.exit("couldn't reach PyPI to check for updates (offline?). Try later, or run "
                 "`uv tool upgrade memnos` manually.")
    cfg["latest_known"] = latest
    save_config(cfg)
    if cur and _vparts(latest) <= _vparts(cur):
        print(f"[memnos] ✓ you're on the latest version (v{cur}).")
        return
    print(f"[memnos] update available: v{cur or '?'} → v{latest}")
    if getattr(args, "check", False):
        print("  run `memnos upgrade` to install it.")
        return
    import subprocess
    cmd = _upgrade_cmd()
    print(f"  upgrading ... ({' '.join(cmd)})")
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        sys.exit(f"upgrade failed (exit {rc}). Try manually:  uv tool upgrade memnos  "
                 "(or: pip install -U memnos)")
    print(f"[memnos] ✓ upgraded to v{latest}.")
    _refresh_integrations()
    print("  restart the server to run the new code:  memnos restart")


def _refresh_integrations():
    """After an upgrade, re-wire previously-installed integrations so new hooks/skills
    actually reach the agents — upgrading the package alone never touches their config."""
    import shutil
    import subprocess
    exe = shutil.which("memnos")
    if not exe:
        return
    home = os.path.expanduser("~")
    sj = os.path.join(home, ".claude", "settings.json")
    try:
        if os.path.exists(sj) and "memnos hook" in open(sj).read():
            print("  refreshing Claude Code wiring (hooks/MCP/skill may have changed) ...")
            r = subprocess.run([exe, "agent-setup", "claude-code"], capture_output=True, text=True)
            print("  ✓ Claude Code re-wired" if r.returncode == 0 else
                  "  ⚠ re-wiring failed — run manually:  memnos agent-setup claude-code")
    except Exception:
        pass
    # other agents: configs are static MCP entries — only nudge if present
    cd = os.path.join(home, "Library", "Application Support", "Claude", "claude_desktop_config.json") \
        if sys.platform == "darwin" else os.path.join(home, ".config", "Claude", "claude_desktop_config.json")
    try:
        if os.path.exists(cd) and "memnos" in open(cd).read():
            print("  tip: refresh the Desktop skill too:  memnos agent-setup claude-desktop")
    except Exception:
        pass


# tables whose `embedding` column is derived from a stored text column (+ its HNSW index)
_EMB_TABLES = [("raw_turns", "text", "raw_hnsw"), ("episodic", "text", "epi_hnsw"),
               ("semantic", "statement", "sem_hnsw"), ("entities", "name", "ent_hnsw")]


def _emb_dim(conn, schema, table="raw_turns"):
    import re
    with conn.cursor() as c:
        c.execute("SELECT format_type(a.atttypid, a.atttypmod) AS t FROM pg_attribute a "
                  "JOIN pg_class c ON a.attrelid=c.oid JOIN pg_namespace n ON c.relnamespace=n.oid "
                  "WHERE n.nspname=%s AND c.relname=%s AND a.attname='embedding'", (schema, table))
        row = c.fetchone()
    m = re.search(r"\((\d+)\)", row["t"]) if row else None
    return int(m.group(1)) if m else None


def _embedder_for(target, conn):
    """Return a callable text->list[float] for the target dim, or None if unavailable."""
    if target == 384:
        from core import local_models
        return local_models.embed
    from core.vault import Vault
    key = os.environ.get("OPENAI_API_KEY", "")
    if key.startswith("secret://"):
        try:
            key = Vault.resolve(conn, key)
        except Exception:
            key = ""
    if not key:                                # fall back to a vault-stored key
        try:                                   # (`memnos secret set openai`)
            key = Vault.get(conn, "openai") or ""
        except Exception:
            key = ""
    if not key or key.startswith("secret://"):
        return None
    os.environ["OPENAI_API_KEY"] = key
    from openai import OpenAI
    from core.embed import CachedEmbedder
    from core.usage import TSCostMeter
    return CachedEmbedder(OpenAI(timeout=60, max_retries=3), TSCostMeter())


def cmd_migrate_embeddings(args, cfg):
    import psycopg
    from psycopg.rows import dict_row
    from core.store import vlit, detect_vector_type
    _apply_env(cfg)
    dsn = cfg.get("dsn") or os.environ.get("MEMNOS_DSN")
    if not dsn:
        sys.exit("not configured — run `memnos setup` first.")
    schema = "tenant_memnos"
    conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
    vtype = detect_vector_type(conn)
    vops = "halfvec_cosine_ops" if vtype == "halfvec" else "vector_cosine_ops"
    cur = _emb_dim(conn, schema)
    if cur is None:
        sys.exit("no memnos schema found in this database (run `memnos setup` first).")
    target = int(args.to) if args.to else (1536 if (cfg.get("openai") or os.environ.get("OPENAI_API_KEY")) else 384)
    if target not in (384, 1536):
        sys.exit("--to must be 384 (local) or 1536 (OpenAI).")
    if cur == target:
        print(f"[memnos] embeddings are already {cur}-d — nothing to migrate.")
        return
    embed = _embedder_for(target, conn)
    if embed is None:
        sys.exit("migrating to 1536-d needs an OpenAI key. Set it first:  memnos secret set openai")

    total = 0
    with conn.cursor() as c:
        for tbl, _, _ in _EMB_TABLES:
            c.execute(f'SELECT count(*) AS n FROM "{schema}"."{tbl}"')
            total += c.fetchone()["n"]
    print(f"[memnos] migrate embeddings  {cur}-d → {target}-d   ·   ~{total} rows to re-embed")
    if target == 1536:
        print("  ⚠ this calls the OpenAI embeddings API for every row (incurs usage cost).")
    if _server_up(f"http://127.0.0.1:{cfg.get('port', 8900)}"):
        print("  ⚠ the server is RUNNING — stop it first (`memnos stop`) so nothing writes at the old dim.")
    if not args.yes and (input("  proceed? [y/N]: ").strip().lower() not in ("y", "yes")):
        return

    fn = embed.embed if hasattr(embed, "embed") else embed
    for tbl, txtcol, idx in _EMB_TABLES:
        with conn.cursor() as c:
            c.execute(f'DROP INDEX IF EXISTS "{schema}"."{idx}"')
            c.execute(f'ALTER TABLE "{schema}"."{tbl}" ALTER COLUMN embedding TYPE {vtype}({target}) USING NULL')
            c.execute(f'SELECT id, {txtcol} AS t FROM "{schema}"."{tbl}" WHERE {txtcol} IS NOT NULL')
            rows = c.fetchall()
        if hasattr(embed, "prime"):
            embed.prime([r["t"] for r in rows])          # batch the OpenAI calls
        with conn.cursor() as c:
            for r in rows:
                c.execute(f'UPDATE "{schema}"."{tbl}" SET embedding=%s::{vtype} WHERE id=%s', (vlit(fn(r["t"])), r["id"]))
            c.execute(f'CREATE INDEX IF NOT EXISTS "{idx}" ON "{schema}"."{tbl}" '
                      f'USING hnsw (embedding {vops})')
        print(f"    ✓ {tbl}: re-embedded {len(rows)} rows")

    # keep the server's embedding mode consistent with the new dim
    if target == 1536:
        cfg["openai"] = "secret://openai"
    else:
        cfg.pop("openai", None)
    save_config(cfg)
    print(f"\n✓ migration complete — embeddings are now {target}-d. Restart the server:  memnos restart")


def _ensure_proxy_token(cfg):
    """A dedicated, audited, revocable principal for proxy capture (mint once, persist)."""
    if cfg.get("proxy_token"):
        return cfg["proxy_token"]
    from core.control import Control
    conn = _conn(cfg)
    Control.init(conn)
    try:
        pid = _principal_id(conn, "proxy")
    except SystemExit:
        pid = Control.create_principal(conn, "proxy", "service")
    name = (os.environ.get("USER") or "me").split()[0]
    Control.grant(conn, pid, f"user:{name}")
    Control.grant(conn, pid, "proj:*")
    tok = Control.mint_token(conn, pid, "capture-proxy")
    cfg["proxy_token"] = tok
    save_config(cfg)
    return tok


def cmd_proxy(args, cfg):
    """Run the LLM-API capture proxy (foreground). Point any OpenAI/Anthropic-compatible
    client's base URL at it and full conversations (both speakers) are captured."""
    _apply_env(cfg)
    os.environ.setdefault("MEMNOS_URL", f"http://127.0.0.1:{cfg.get('port', 8900)}")
    try:
        os.environ.setdefault("MEMNOS_TOKEN", _ensure_proxy_token(cfg))
    except Exception as e:
        print(f"[memnos] WARN: could not mint a proxy token ({e}) — capture will fail "
              "until the database is reachable. Relay still works.")
    if args.namespace:
        os.environ["MEMNOS_NS"] = args.namespace
    import memnos_proxy
    memnos_proxy.CFG = memnos_proxy._load_cfg()         # re-read with env applied
    if args.no_capture:
        memnos_proxy.CFG["capture"] = False
    memnos_proxy.serve(port=args.port)


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
    from core.control import Control
    conn = _conn(cfg)
    Control.init(conn)
    pid = Control.create_principal(conn, "admin", "service")
    Control.grant(conn, pid, "*")
    print("ADMIN TOKEN (shown once):\n  " + Control.mint_token(conn, pid, "console"))


def cmd_principal(args, cfg):
    from core.control import Control
    pid = Control.create_principal(_conn(cfg), args.name, args.kind)
    print(f"principal '{args.name}' id={pid}")


def cmd_principal_ls(args, cfg):
    from core.control import Control
    for p in Control.list_principals(_conn(cfg)):
        print(f"  {p['id']:<5} {p['name']:<24} {p['kind']:<8} active_tokens={p['active_tokens']}")


def cmd_token(args, cfg):
    from core.control import Control
    conn = _conn(cfg)
    tok = Control.mint_token(conn, _principal_id(conn, args.principal), args.label, args.ttl_days)
    print("TOKEN (shown once):\n  " + tok)


def cmd_token_ls(args, cfg):
    from core.control import Control
    conn = _conn(cfg)
    for t in Control.list_tokens(conn, _principal_id(conn, args.principal)):
        state = "revoked" if t["revoked"] else "active"
        print(f"  {t['id']:<5} {t['hint'] or '—':<16} {t['label'] or '':<16} {state:<8} "
              f"expires={t['expires_at'] or 'never'}")


def cmd_token_revoke(args, cfg):
    from core.control import Control
    Control.revoke_token_by_id(_conn(cfg), args.id)
    print(f"token {args.id} revoked")


def cmd_grant(args, cfg):
    from core.control import Control
    conn = _conn(cfg)
    Control.grant(conn, _principal_id(conn, args.principal), args.namespace,
                  can_read=True, can_write=not args.read_only)
    print(f"granted {args.principal} -> {args.namespace} ({'read' if args.read_only else 'read+write'})")


def cmd_grant_ls(args, cfg):
    from core.control import Control
    conn = _conn(cfg)
    for g in Control.authorized_namespaces(conn, _principal_id(conn, args.principal)):
        mode = ("read" if g["can_read"] else "") + ("+write" if g["can_write"] else "")
        print(f"  {g['namespace']:<32} {mode.lstrip('+') or 'none'}")


def cmd_grant_rm(args, cfg):
    from core.control import Control
    conn = _conn(cfg)
    Control.revoke_grant(conn, _principal_id(conn, args.principal), args.namespace)
    print(f"revoked {args.principal} -> {args.namespace}")


def cmd_namespace(args, cfg):
    from core.control import Control
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
        from core.store import BrainStore
        if not args.name or not args.to:
            sys.exit("usage: memnos namespace copy|move <src> --to <dst> [--like X]")
        out = BrainStore(conn=conn).migrate_namespace("tenant_memnos", args.name, args.to,
                                                      mode=args.action, like=args.like)
        print(f"{out['mode']}d {out['facts']} facts + {out['raw_turns']} turns "
              f"from {args.name} -> {args.to}")
    elif args.action == "set":
        if not args.name or not args.kind:
            sys.exit("usage: memnos namespace set <ns> --kind memory|knowledge")
        Control.set_namespace_kind(conn, args.name, args.kind)
        print(f"namespace '{args.name}' kind set to '{args.kind}'")
    elif args.action == "link":
        if not args.name or not args.dst:
            sys.exit("usage: memnos namespace link <src> <dst>")
        created_by = None
        try:
            created_by = _principal_id(conn, "admin")
        except SystemExit:
            pass
        Control.link_namespaces(conn, args.name, args.dst, created_by=created_by)
        print(f"linked {args.name} -> {args.dst} (recall on '{args.name}' will also "
              f"ground in '{args.dst}' for callers with a read grant on it)")
    elif args.action == "unlink":
        if not args.name or not args.dst:
            sys.exit("usage: memnos namespace unlink <src> <dst>")
        removed = Control.unlink_namespaces(conn, args.name, args.dst)
        print(f"unlinked {args.name} -> {args.dst}" if removed else "no such link")
    elif args.action == "reconcile":
        # BACKFILL pre-fix contradiction debt (issue #10 residual C): apply the
        # deterministic write-time supersession logic (dedupe + SPO + negation
        # close-out) pairwise over the namespace's LIVE facts, newest-first.
        # Embedding-only (stored vectors), NO LLM. Direct-DB admin path (same _conn
        # DSN trust as every other `memnos namespace` verb). Dry-run rolls back the
        # SAME mutations, so its counts are exact, not estimates.
        if not args.name:
            sys.exit("usage: memnos namespace reconcile <ns> [--dry-run] [--limit N]")
        import psycopg
        from psycopg.rows import dict_row
        from core.store import BrainStore
        from core.service import reconcile_namespace
        conn.close()                       # reconcile owns its own (non-autocommit) txn
        rconn = psycopg.connect(_dsn(cfg), autocommit=False, row_factory=dict_row)
        try:
            res = reconcile_namespace(BrainStore(conn=rconn), args.name, limit=args.limit)
            rconn.rollback() if args.dry_run else rconn.commit()
        finally:
            rconn.close()
        mode = "dry-run — no changes written" if args.dry_run else "applied"
        print(f"namespace reconcile '{args.name}' ({mode})")
        print(f"  {'facts walked':<14} {res['facts_scanned']}")
        print(f"  {'would-close' if args.dry_run else 'closed':<14} {res['closed']}")
        print(f"  {'would-dedupe' if args.dry_run else 'deduped':<14} {res['deduped']}")
    elif args.action == "links":
        rows = Control.list_links(conn, args.name)
        if not rows:
            print("no links" + (f" from '{args.name}'" if args.name else ""))
        for l in rows:
            print(f"  {l['src_ns']} -> {l['dst_ns']}  (by {l['created_by'] or '?'}, {l['created_at']:%Y-%m-%d})")
    else:  # ls
        for n in Control.list_namespaces(conn):
            kind = " [knowledge]" if n.get("kind") == "knowledge" else ""
            print(f"  {n['name']:<28} turns={n['turns']} facts={n['facts']}{kind}  {n['description'] or ''}")


def cmd_secret(args, cfg):
    _apply_env(cfg)
    from core.vault import Vault, VaultLocked
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
    from core.control import Control
    for r in Control.stats(_conn(cfg), 24):
        print(f"  {r['action']:<12} calls={r['calls']} err%={r['error_pct'] or 0} "
              f"p50={r['p50_ms'] or '-'} p95={r['p95_ms'] or '-'}")


def cmd_health(args, cfg):
    from core.control import Control
    rows = Control.health(_conn(cfg), 24)
    print("OK — no findings" if not rows else "")
    for level, msg in rows:
        print(f"  [{level}] {msg}")


def cmd_whoami(args, cfg):
    from core.control import Control
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
    body = {"namespace": ns, "text": args.text}
    if getattr(args, "type", None):
        body["type"] = args.type
    out = _post(cfg, "/remember", body,
                args.token or os.environ.get("MEMNOS_TOKEN") or cfg.get("admin_token"))
    print(json.dumps(out))


def cmd_recall(args, cfg):
    import nsresolve
    ns = args.namespace if args.namespace and args.namespace != "auto" else nsresolve.resolve()
    body = {"namespace": ns, "query": args.query}
    if getattr(args, "scope", None) in ("all", "wide"):
        body["scope"] = "all"
    if getattr(args, "type", None):
        body["type"] = args.type
    out = _post(cfg, "/recall", body,
                args.token or os.environ.get("MEMNOS_TOKEN") or cfg.get("admin_token"))
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


def _ensure_claude_token(cfg, extra_ns=None):
    """A principal+token for the Claude integration: default namespace user:<user> plus a
    proj:* wildcard so per-project + widened recall work. (Grants, not namespace creation.)
    If `extra_ns` is a custom namespace the wiring will write to, GRANT it too — otherwise
    every write to that ns 403s and is silently swallowed. (Bug: agent-setup --namespace 403)"""
    from core.control import Control
    conn = _conn(cfg)
    Control.init(conn)
    name = (os.environ.get("USER") or "me").split()[0]
    try:
        pid = _principal_id(conn, name)
    except SystemExit:
        pid = Control.create_principal(conn, name, "user")
    Control.grant(conn, pid, f"user:{name}")
    Control.grant(conn, pid, "proj:*")
    if extra_ns and not _ns_covered(extra_ns, (f"user:{name}", "proj:*")):
        Control.grant(conn, pid, extra_ns)
    return Control.mint_token(conn, pid, "claude-code"), f"user:{name}"


def _ns_covered(target, grants):
    """True if `target` is already authorized by one of `grants` (exact match or a
    `prefix:*` wildcard). Used to avoid a redundant grant for the common case."""
    for g in grants:
        if g == target:
            return True
        if g.endswith(":*") and target.startswith(g[:-1]):
            return True
    return False


def _ensure_agent_token(cfg, agent, extra_ns=None):
    """Identity for an AUTONOMOUS agent (hermes, openclaw, ...). Unlike a user editor, the
    agent gets its OWN principal named after itself, GRANTED on its own namespace
    `agent:<agent>`, and a token for THAT principal. This is Bug 3: previously every
    agent-setup wired the human user's token (no grant on agent:<agent>), so the agent's
    writes failed `forbidden`. If `extra_ns` is the custom namespace the wiring will use,
    GRANT it too (else --namespace writes 403). Returns (token, default_namespace)."""
    from core.control import Control
    conn = _conn(cfg)
    Control.init(conn)
    principal = re.sub(r"[^a-z0-9_-]", "-", agent.lower())   # safe principal name
    ns = f"agent:{principal}"
    try:
        pid = _principal_id(conn, principal)
    except SystemExit:
        pid = Control.create_principal(conn, principal, "agent")
    Control.grant(conn, pid, ns)            # the agent's own namespace — read+write
    Control.grant(conn, pid, "proj:*")      # shared project memory (read+write), same as users
    if extra_ns and not _ns_covered(extra_ns, (ns, "proj:*")):
        Control.grant(conn, pid, extra_ns)  # the custom --namespace the agent will write to
    return Control.mint_token(conn, pid, f"agent-setup:{principal}"), ns


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
    token, default_ns = _ensure_claude_token(cfg, extra_ns=args.namespace)
    ns = args.namespace or default_ns

    # 1. MCP server -> ~/.claude.json (the file Claude Code reads for MCP)
    cj = os.path.join(home, ".claude.json")
    try:
        d = json.load(open(cj)) if os.path.exists(cj) else {}
    except Exception:
        d = {}
    cmd, cargs = _mcp_launcher()               # absolute path — GUI/min-PATH launches
    d.setdefault("mcpServers", {})["memnos"] = {
        "command": cmd, "args": cargs,
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
    wire("SessionStart", f"{env} memnos hook status")   # visible "memory ON/OFF" each session
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

_DESKTOP_SKILL = """---
name: memnos-memory
description: >-
  Long-term memory via memnos. Use when the user references past work, prior decisions,
  preferences, people, projects, or anything from earlier conversations — and after giving
  answers that contain decisions, identifiers, or outcomes worth keeping.
---

# memnos — long-term memory

You have persistent, governed long-term memory through the **memnos** MCP tools
(`recall`, `recall_wide`, `remember`, `reconcile_claim`, `get_entity`, `get_provenance`).
Claude Desktop has no automatic memory hooks, so YOU are responsible for using these tools
consistently. Follow these rules:

## Recall — before answering
- When the user references past conversations, prior decisions, preferences, ongoing
  projects, or people/things not introduced in this session, call `recall` with a focused
  query BEFORE answering. If nothing relevant returns, call `recall_wide`.
- Prefer recalled facts over guessing. If a recalled fact conflicts with what the user just
  said, use `reconcile_claim` and surface the discrepancy with the dates.

## Remember — after answering
- After any answer that contains a DECISION, conclusion, identifier, or outcome (ticket
  keys like ABC-123, PR/MR numbers, versions, URLs, chosen options, agreed plans), call
  `remember` with a ONE-LINE summary — keep identifiers VERBATIM, never paraphrase them away.
- Also `remember` durable facts the user states about themselves, their projects,
  preferences, and commitments. One fact per call, self-contained, dates absolute.
- Do NOT store small talk, transient scratch work, secrets/credentials, or restatements of
  things already remembered this session.

## Notes
- Memories are namespace-scoped and access-controlled server-side; if a read/write is
  denied, say so rather than retrying.
- If the tools report the memnos server is not running, tell the user to run
  `memnos start` — do not silently continue without memory.
"""

# MCP-capable agents: where each keeps its MCP server config, and the format.
_AGENTS = {
    # claude-code routes to the full Claude Code setup (MCP + lifecycle hooks + /memnos +
    # CLAUDE.md) — same as the legacy `memnos claude-setup` alias.
    "claude-code":    {"special": "claude"},
    "codex":          {"path": "~/.codex/config.toml",                         "fmt": "toml", "agents_md": "~/.codex/AGENTS.md"},
    "cursor":         {"path": "~/.cursor/mcp.json",                           "fmt": "json"},
    "windsurf":       {"path": "~/.codeium/windsurf/mcp_config.json",          "fmt": "json"},
    # Claude Desktop's config lives in a platform-specific app-data dir (resolved below)
    "claude-desktop": {"path": "~/Library/Application Support/Claude/claude_desktop_config.json", "fmt": "json",
                       "note": "fully quit Claude Desktop (Cmd-Q / system tray) and reopen — the tools "
                               "appear under the search-and-tools icon"},
    # OpenClaw keeps MCP servers under mcp.servers in its main config. It is an AUTONOMOUS
    # agent (not a user editor): it gets its OWN principal + token scoped to agent:openclaw,
    # so its writes are attributable and isolated from the human user's memory.
    "openclaw":       {"path": "~/.openclaw/openclaw.json",                    "fmt": "json", "key": ("mcp", "servers"),
                       "agent": True,
                       "note": "restart the OpenClaw gateway, then verify with: openclaw mcp list"},
    # Hermes Agent (Nous Research) — YAML config, stdio MCP client since v0.2.0. Autonomous
    # agent: own principal + token scoped to agent:hermes (see "agent" flag above).
    "hermes":         {"path": "~/.hermes/config.yaml",                        "fmt": "yaml", "key": ("mcp_servers",),
                       "agent": True,
                       "note": "run /reload-mcp inside Hermes (or restart it), then check the tool list"},
}


def _mcp_launcher():
    """(command, args) for spawning the memnos MCP adapter — ABSOLUTE command path, because
    GUI apps launch MCP servers with a minimal PATH. Falls back to `python memnos_cli.py mcp`
    for source checkouts where no `memnos` executable is installed."""
    import shutil
    exe = shutil.which("memnos")
    if exe:
        return exe, ["mcp"]
    return sys.executable, [os.path.abspath(__file__), "mcp"]


def cmd_agent_setup(args, cfg):
    """Wire memnos into another MCP-capable agent (codex/cursor/windsurf/claude-desktop/
    openclaw/hermes). Writes its MCP server config (+ an AGENTS.md instruction for codex).
    Idempotent; backs up."""
    spec = _AGENTS.get(args.agent)
    if not spec:
        sys.exit(f"unknown agent '{args.agent}' — choose: {', '.join(_AGENTS)}")
    if spec.get("special") == "claude":       # full Claude Code setup (MCP + hooks + /memnos)
        return cmd_claude_setup(argparse.Namespace(namespace=args.namespace,
                                                   force=getattr(args, "force", False)), cfg)
    spec = dict(spec)
    if args.agent == "claude-desktop":        # app-data dir is platform-specific
        if sys.platform == "win32":
            spec["path"] = os.path.join(os.environ.get("APPDATA", "~"), "Claude", "claude_desktop_config.json")
        elif sys.platform.startswith("linux"):
            spec["path"] = "~/.config/Claude/claude_desktop_config.json"
    url = os.environ.get("MEMNOS_URL") or f"http://127.0.0.1:{cfg.get('port', 8900)}"
    # Autonomous agents (hermes, openclaw) get their OWN principal+token scoped to
    # agent:<name>; user editors (cursor, codex, ...) use the human user's identity. (Bug 3)
    if spec.get("agent"):
        token, default_ns = _ensure_agent_token(cfg, args.agent, extra_ns=args.namespace)
    else:
        token, default_ns = _ensure_claude_token(cfg, extra_ns=args.namespace)
    ns = args.namespace or default_ns
    path = os.path.expanduser(spec["path"])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # absolute command path: GUI apps (Claude Desktop especially) spawn MCP servers with a
    # minimal PATH that doesn't include ~/.local/bin — a bare "memnos" fails to resolve there
    cmd, cargs = _mcp_launcher()
    entry = {"command": cmd, "args": cargs,
             "env": {"MEMNOS_URL": url, "MEMNOS_TOKEN": token, "MEMNOS_NS": ns}}

    if spec["fmt"] == "json":
        try:
            d = json.load(open(path)) if os.path.exists(path) else {}
        except Exception:
            d = {}
        node = d
        for k in spec.get("key", ("mcpServers",)):
            node = node.setdefault(k, {})
        node["memnos"] = entry
        _backup(path); json.dump(d, open(path, "w"), indent=2)
    elif spec["fmt"] == "yaml":
        import yaml
        try:
            d = yaml.safe_load(open(path)) if os.path.exists(path) else {}
            d = d if isinstance(d, dict) else {}
        except Exception:
            d = {}
        node = d
        for k in spec.get("key", ("mcp_servers",)):
            node = node.setdefault(k, {})
        node["memnos"] = entry
        _backup(path)
        with open(path, "w") as f:
            yaml.safe_dump(d, f, sort_keys=False, default_flow_style=False)
    else:  # toml (codex)
        existing = open(path).read() if os.path.exists(path) else ""
        targs = ", ".join(f'"{a}"' for a in cargs)
        block = (f'\n[mcp_servers.memnos]\ncommand = "{cmd}"\nargs = [{targs}]\n\n'
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
    if args.agent == "claude-desktop":
        # Desktop has no hooks — a personal SKILL makes tool use consistent instead of
        # occasional. Write it where the user can add it via Customize → Skills → "+".
        sk_dir = os.path.join(CONFIG_DIR, "claude-desktop-skill")
        os.makedirs(sk_dir, exist_ok=True)
        with open(os.path.join(sk_dir, "SKILL.md"), "w") as f:
            f.write(_DESKTOP_SKILL)
        print(f"  • memory skill   -> {sk_dir}/SKILL.md")
        print("    Recommended: open Claude Desktop → Customize → Skills → '+' and add that")
        print("    folder — it teaches Claude to recall before answering and to save")
        print("    decisions/identifiers after. (Desktop has no hooks; the skill makes")
        print("    memory use consistent. Also consider turning OFF Desktop's own")
        print("    'Generate memory from chat history' to avoid two competing memories.)")
    print("  Note: this agent uses the memnos MCP *tools* (recall/remember/reconcile_claim) — "
          "no auto inject/save hooks (those are Claude Code only). Restart the agent to load it.")
    if spec.get("note"):
        print(f"  Next: {spec['note']}")


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

    if args.which == "status":
        # SessionStart: ONE visible line so the user always knows whether memory is on —
        # no silent loss of capture after a reboot.
        parts = []                                  # 1s health timeouts — session start must
        if _server_up(url, timeout=1):              # never feel slowed by this hook
            parts.append(f"memory ACTIVE → {ns}")
        else:
            parts.append(f"⚠ memory OFF — server unreachable at {url}. Run `memnos start` "
                         "(`memnos autostart` makes it survive reboots)")
        if cfg.get("proxy_token"):                  # proxy configured on this machine
            pport = (cfg.get("proxy") or {}).get("port", 8910)
            parts.append("capture proxy ACTIVE" if _server_up(f"http://127.0.0.1:{pport}", timeout=1)
                         else f"⚠ capture proxy DOWN (:{pport}) — run `memnos proxy`")
        print(json.dumps({"systemMessage": "memnos: " + "  ·  ".join(parts)}))
        return

    if args.which == "recall":
        prompt = (data.get("prompt") or "").strip()
        if not prompt:
            return
        try:
            req = urllib.request.Request(f"{url}/recall", method="POST",
                data=json.dumps({"namespace": ns, "query": prompt}).encode(), headers=hdr)
            ctx = json.load(urllib.request.urlopen(req, timeout=8)).get("context", "")
        except Exception:
            # server down must NEVER block or break the session — but the user should
            # know memory is off. Tell them once per ~10 min (marker-file throttle).
            marker = os.path.join(CONFIG_DIR, ".hook_down_notified")
            import time
            try:
                stale = (not os.path.exists(marker)) or (time.time() - os.path.getmtime(marker) > 600)
                if stale:
                    open(marker, "w").close()
                    print(json.dumps({"systemMessage":
                        f"memnos: memory server unreachable/unhealthy at {url} — recall/auto-save "
                        "are OFF for now. Check `memnos status`; start with `memnos start`, or "
                        "`memnos autostart` to keep it running across reboots."}))
            except Exception:
                pass
            return
        if ctx.strip():
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                  "additionalContext": "## Relevant memories (memnos)\n" + ctx}}))
        return

    # remember (Stop): save the last user message AND the assistant's reply to it.
    # The reply is where conclusions live (decisions, ticket IDs, outcomes) — capturing
    # only the question made everything the agent said invisible across sessions.
    text = data.get("prompt", "")
    a_text = ""
    tp = data.get("transcript_path", "")
    if tp and os.path.exists(tp):
        try:
            last = ""
            with open(tp) as f:
                for line in f:
                    ev = json.loads(line); c = ev.get("message", {}).get("content")
                    if ev.get("type") == "user" and isinstance(c, str):
                        last = c; a_text = ""          # keep only the reply AFTER the last user msg
                    elif ev.get("type") == "assistant" and isinstance(c, list):
                        t = "".join(b.get("text", "") for b in c
                                    if isinstance(b, dict) and b.get("type") == "text")
                        if t.strip():
                            a_text = (a_text + "\n" + t.strip()) if a_text else t.strip()
            text = last or text
        except Exception:
            pass

    def _save(t, speaker):
        try:
            # async: the hook never reads the fact count — the server stores the raw
            # turn and queues extraction, so the Stop hook returns in ~200ms
            req = urllib.request.Request(f"{url}/remember", method="POST",
                data=json.dumps({"namespace": ns, "text": t, "speaker": speaker,
                                 "async": True}).encode(), headers=hdr)
            urllib.request.urlopen(req, timeout=12).read()
        except Exception:
            pass

    text = (text or "").strip()
    low = text.lower()
    user_noise = (not text or low.startswith("<") or text.startswith("# ") or "<<autonomous-loop" in low
                  or low.startswith("# autonomous loop") or "</task-notification" in low
                  or "this is an automated background-task event" in low
                  or "reference answer:" in low or "reply with only" in low or low.startswith("question:")
                  or len(text) < 15 or len(text.split()) < 3)
    if not user_noise:
        _save(text, "user")
        a_text = a_text.strip()
        if len(a_text) >= 30:                          # the answer to a noise prompt is noise too
            if len(a_text) > 8000:                     # cap extraction cost on huge agent replies
                a_text = a_text[:8000] + " …[truncated]"
            _save(a_text, "assistant")


# ---- CLI grammar -------------------------------------------------------------
# Noun-verb grammar (0.1.6): heroes (remember/recall) + lifecycle verbs stay top-level;
# identity/access management is `noun verb`: principal create|ls, token mint|ls|revoke,
# grant add|ls|rm, secret set|ls|rm|rotate|keygen, namespace add|ls|rm|set|link|unlink|
# links|copy|move. The PRE-0.1.6 forms keep working forever as HIDDEN aliases
# (`memnos principal <name>`, `memnos token <principal>`, `memnos grant <p> <ns>`) —
# scripts/hooks/agents in the field must never break — but help teaches only the new
# grammar.
_NOUN_DEFAULT_VERB = {"principal": ("create", {"create", "ls"}),
                      "token": ("mint", {"mint", "ls", "revoke"}),
                      "grant": ("add", {"add", "ls", "rm"})}

# one runnable example per command — rendered into docs/cli.md + the console reference
EXAMPLES = {
    "remember": 'memnos remember "We chose Postgres 16 for staging" --namespace proj:myapp',
    "recall": 'memnos recall "what did we decide about staging?" --scope all',
    "setup": "memnos setup --docker",
    "start": "memnos start",
    "stop": "memnos stop",
    "restart": "memnos restart",
    "status": "memnos status",
    "serve": "memnos serve --port 8900",
    "autostart": "memnos autostart",
    "upgrade": "memnos upgrade --check",
    "proxy": "memnos proxy --namespace user:me",
    "mcp": "memnos mcp --namespace proj:myapp",
    "hook": "echo '{}' | memnos hook status",
    "claude-setup": "memnos claude-setup",
    "agent-setup": "memnos agent-setup claude-code",
    "principal create": "memnos principal create ci-bot --kind service",
    "principal ls": "memnos principal ls",
    "token mint": "memnos token mint ci-bot --label deploy --ttl-days 90",
    "token ls": "memnos token ls ci-bot",
    "token revoke": "memnos token revoke 42",
    "grant add": "memnos grant add ci-bot proj:myapp --read-only",
    "grant ls": "memnos grant ls ci-bot",
    "grant rm": "memnos grant rm ci-bot proj:myapp",
    "namespace": "memnos namespace add proj:myapp --desc 'my app'",
    "secret": "memnos secret set openai",
    "stats": "memnos stats",
    "health": "memnos health",
    "whoami": "memnos whoami mnk_...",
    "ns": "memnos ns proj:myapp",
    "admin": "memnos admin",
    "migrate-embeddings": "memnos migrate-embeddings --to 1536",
    "help": "memnos help",
}


def build_parser():
    """The complete argparse tree — shared by main() and `memnos docs-gen` (which renders
    docs/cli.md + the console CLI reference from it, so docs can never drift)."""
    ap = argparse.ArgumentParser(
        prog="memnos", description="memnos memory platform CLI",
        epilog="Remote use: set MEMNOS_URL and MEMNOS_TOKEN to point the data commands "
               "(remember/recall) at a remote memnos server.")
    ap.add_argument("-V", "--version", action="version", version=f"memnos {_version()}")
    sub = ap.add_subparsers(dest="cmd", metavar="<command>")   # not required — bare `memnos` prints help

    # ---- heroes ----
    p = sub.add_parser("remember", help="save a memory (data client — talks to the server)")
    p.add_argument("text", help="the text to remember")
    p.add_argument("--namespace", default="auto", help="target namespace (default: auto-resolve for this folder)")
    p.add_argument("--type", choices=["decision", "incident", "constraint", "skill", "fact"],
                   help="classify the memory (constraints are pinned into every recall)")
    p.add_argument("--token", help="bearer token (default: MEMNOS_TOKEN or the config admin token)")
    p.set_defaults(fn=cmd_remember)
    p = sub.add_parser("recall", help="recall relevant memories (data client)")
    p.add_argument("query", help="what to recall")
    p.add_argument("--namespace", default="auto", help="namespace to search (default: auto-resolve)")
    p.add_argument("--scope", choices=["all", "wide"], help="widen across every namespace your token may read")
    p.add_argument("--type", choices=["decision", "incident", "constraint", "skill", "fact"],
                   help="only memories of this type (pinned constraints always included)")
    p.add_argument("--token", help="bearer token (default: MEMNOS_TOKEN or the config admin token)")
    p.set_defaults(fn=cmd_recall)

    # ---- lifecycle ----
    p = sub.add_parser("setup", help="connect to Postgres, create schema + admin token")
    p.add_argument("--dsn", help="Postgres DSN (skips the interactive wizard)")
    p.add_argument("--docker", action="store_true",
                   help="provision a pgvector Postgres in Docker (no Postgres setup needed)")
    p.set_defaults(fn=cmd_setup)
    p = sub.add_parser("start", help="start the memory server in the background")
    p.add_argument("--port", type=int, help="HTTP port (default: config / 8900)")
    p.set_defaults(fn=cmd_start)
    sub.add_parser("stop", help="stop the background server").set_defaults(fn=cmd_stop)
    p = sub.add_parser("restart", help="restart the background server")
    p.add_argument("--port", type=int, help="HTTP port (default: config / 8900)")
    p.set_defaults(fn=cmd_restart)
    sub.add_parser("status", help="show server + config + embedding mode").set_defaults(fn=cmd_status)
    p = sub.add_parser("autostart", help="install a login service (launchd/systemd) so the server always runs")
    p.add_argument("--remove", action="store_true", help="uninstall the login service(s)")
    p.add_argument("--proxy", action="store_true", help="also keep the LLM capture proxy running at login")
    p.set_defaults(fn=cmd_autostart)
    p = sub.add_parser("serve", help="run the server in the FOREGROUND (process managers / Docker / debug)")
    p.add_argument("--port", type=int, help="HTTP port (default: config / 8900)")
    p.set_defaults(fn=cmd_serve)
    p = sub.add_parser("mcp", help="run the stdio MCP adapter (Claude Code / Cursor / Windsurf config)")
    p.add_argument("--namespace", help="default namespace for the MCP tools")
    p.set_defaults(fn=cmd_mcp)
    p = sub.add_parser("proxy", help="LLM-API capture proxy — point ANTHROPIC_BASE_URL/OPENAI_BASE_URL "
                                     "at it; both sides of every conversation are remembered")
    p.add_argument("--port", type=int, help="proxy port (default 8910)")
    p.add_argument("--namespace", help="namespace captured turns go to (default user:<you>)")
    p.add_argument("--no-capture", action="store_true", help="relay only, capture off")
    p.set_defaults(fn=cmd_proxy)
    p = sub.add_parser("upgrade", help="check PyPI for a newer version and install it")
    p.add_argument("--check", action="store_true", help="only check; don't install")
    p.set_defaults(fn=cmd_upgrade)

    # ---- identity & access (noun-verb) ----
    p = sub.add_parser("principal", help="manage principals (identities): create | ls")
    p.set_defaults(fn=lambda a, c, _p=p: _p.print_help())
    ps = p.add_subparsers(dest="verb", metavar="<verb>")
    v = ps.add_parser("create", help="create a principal (user / agent / service)")
    v.add_argument("name", help="principal name")
    v.add_argument("--kind", default="user", help="user | agent | service (default user)")
    v.set_defaults(fn=cmd_principal)
    ps.add_parser("ls", help="list principals").set_defaults(fn=cmd_principal_ls)

    p = sub.add_parser("token", help="manage bearer tokens: mint | ls | revoke")
    p.set_defaults(fn=lambda a, c, _p=p: _p.print_help())
    ps = p.add_subparsers(dest="verb", metavar="<verb>")
    v = ps.add_parser("mint", help="mint a token for a principal (plaintext shown ONCE)")
    v.add_argument("principal", help="principal name")
    v.add_argument("--label", help="label (e.g. 'ci', 'laptop')")
    v.add_argument("--ttl-days", type=int, help="expiry in days (default: never)")
    v.set_defaults(fn=cmd_token)
    v = ps.add_parser("ls", help="list a principal's tokens (metadata only, never the secret)")
    v.add_argument("principal", help="principal name")
    v.set_defaults(fn=cmd_token_ls)
    v = ps.add_parser("revoke", help="revoke a token by id (see: memnos token ls)")
    v.add_argument("id", type=int, help="token id")
    v.set_defaults(fn=cmd_token_revoke)

    p = sub.add_parser("grant", help="manage namespace grants: add | ls | rm")
    p.set_defaults(fn=lambda a, c, _p=p: _p.print_help())
    ps = p.add_subparsers(dest="verb", metavar="<verb>")
    v = ps.add_parser("add", help="grant a principal access to a namespace")
    v.add_argument("principal", help="principal name")
    v.add_argument("namespace", help="namespace (exact, prefix like team:*, or *)")
    v.add_argument("--read-only", action="store_true", help="read access only (default read+write)")
    v.set_defaults(fn=cmd_grant)
    v = ps.add_parser("ls", help="list a principal's grants")
    v.add_argument("principal", help="principal name")
    v.set_defaults(fn=cmd_grant_ls)
    v = ps.add_parser("rm", help="revoke a grant")
    v.add_argument("principal", help="principal name")
    v.add_argument("namespace", help="namespace of the grant to revoke")
    v.set_defaults(fn=cmd_grant_rm)

    # ---- namespaces & secrets (already noun-verb via the action positional) ----
    p = sub.add_parser("namespace", help="manage namespaces: add | ls | rm | set | link | unlink | links | copy | move | reconcile")
    p.add_argument("action", choices=["add", "ls", "rm", "copy", "move", "set", "link", "unlink", "links", "reconcile"],
                   help="what to do")
    p.add_argument("name", nargs="?", help="namespace (or copy/move SOURCE, or link SRC)")
    p.add_argument("dst", nargs="?", help="link/unlink destination namespace")
    p.add_argument("--to", help="copy/move destination namespace")
    p.add_argument("--like", help="copy/move: only memories containing this substring")
    p.add_argument("--desc", help="add: description")
    p.add_argument("--kind", choices=["memory", "knowledge"], help="set: namespace kind")
    p.add_argument("--purge", action="store_true", help="rm: also delete the stored memories")
    p.add_argument("--dry-run", action="store_true",
                   help="reconcile: report would-close/would-dedupe counts, write nothing")
    p.add_argument("--limit", type=int,
                   help="reconcile: cap the number of facts walked this run (newest first)")
    p.set_defaults(fn=cmd_namespace)
    p = sub.add_parser("secret", help="encrypted secret vault: set | ls | rm | rotate | keygen")
    p.add_argument("action", choices=["set", "ls", "rm", "keygen", "rotate"], help="what to do")
    p.add_argument("name", nargs="?", help="secret name (set/rm)")
    p.add_argument("--value", help="set: the value (omit to be prompted, hidden)")
    p.add_argument("--desc", help="set: description")
    p.set_defaults(fn=cmd_secret)
    p = sub.add_parser("ns", help="show or pin this folder's namespace (ns <value> | ns clear)")
    p.add_argument("value", nargs="?", help="namespace to pin for this folder ('clear' reverts)")
    p.set_defaults(fn=cmd_ns)

    # ---- observability ----
    sub.add_parser("stats", help="per-op reliability stats (last 24h)").set_defaults(fn=cmd_stats)
    sub.add_parser("health", help="actionable findings (the platform doctor)").set_defaults(fn=cmd_health)
    p = sub.add_parser("whoami", help="validate a token and show its grants")
    p.add_argument("token", help="bearer token to check")
    p.set_defaults(fn=cmd_whoami)

    # ---- integrations ----
    p = sub.add_parser("agent-setup", help="wire memnos into an agent (claude-code, codex, cursor, ...)")
    p.add_argument("agent", choices=list(_AGENTS), help="which agent to wire")
    p.add_argument("--namespace", help="default namespace for the agent")
    p.add_argument("--force", action="store_true", help="set up even if the agent isn't detected")
    p.set_defaults(fn=cmd_agent_setup)
    p = sub.add_parser("claude-setup", help="(alias of: memnos agent-setup claude-code)")
    p.add_argument("--namespace", help="default namespace for Claude Code")
    p.add_argument("--force", action="store_true", help="set up even if ~/.claude is missing")
    p.set_defaults(fn=cmd_claude_setup)
    p = sub.add_parser("hook", help="Claude Code hook entry (stdin JSON; wired by agent-setup)")
    p.add_argument("which", choices=["recall", "remember", "status"], help="which hook")
    p.set_defaults(fn=cmd_hook)

    # ---- maintenance ----
    p = sub.add_parser("admin", help="mint a fresh admin token (direct DB, no server needed)")
    p.set_defaults(fn=cmd_admin)
    p = sub.add_parser("migrate-embeddings",
                       help="re-embed all memories to a different dimension (384 local ↔ 1536 OpenAI)")
    p.add_argument("--to", choices=["384", "1536"],
                   help="target dimension (default: inferred from your OpenAI-key setup)")
    p.add_argument("--yes", "-y", action="store_true", help="skip the confirmation prompt")
    p.set_defaults(fn=cmd_migrate_embeddings)
    sub.add_parser("help", help="show this help").set_defaults(fn=lambda a, c: ap.print_help())

    # hidden (no help= → excluded from docs + help): docs generator / CI staleness check
    p = sub.add_parser("docs-gen")
    p.add_argument("--check", action="store_true")
    p.set_defaults(fn=cmd_docs_gen)
    return ap


def _normalize_argv(argv):
    """Legacy alias forms → new grammar (hidden, permanent):
         memnos principal <name>        → memnos principal create <name>
         memnos token <principal>       → memnos token mint <principal>
         memnos grant <p> <namespace>   → memnos grant add <p> <namespace>"""
    if len(argv) >= 2 and argv[0] in _NOUN_DEFAULT_VERB and not argv[1].startswith("-"):
        default_verb, verbs = _NOUN_DEFAULT_VERB[argv[0]]
        if argv[1] not in verbs:
            return [argv[0], default_verb] + argv[1:]
    return argv


# ---- docs generation (docs/cli.md + ui/cli-reference.json) -------------------
_DOC_GROUPS = [
    ("Memory (heroes)", ["remember", "recall"]),
    ("Server lifecycle", ["setup", "start", "stop", "restart", "status", "serve",
                          "autostart", "upgrade", "proxy", "mcp"]),
    ("Identity & access", ["principal create", "principal ls", "token mint", "token ls",
                           "token revoke", "grant add", "grant ls", "grant rm", "whoami"]),
    ("Namespaces", ["namespace", "ns"]),
    ("Secrets", ["secret"]),
    ("Observability", ["stats", "health"]),
    ("Agent integrations", ["agent-setup", "claude-setup", "hook"]),
    ("Maintenance", ["admin", "migrate-embeddings", "help"]),
]

_CLI_MD_PREAMBLE = """# memnos CLI reference

> AUTO-GENERATED by `memnos docs-gen` from the CLI's argparse tree — do not edit by
> hand. CI fails if this file is stale (`tests/test_cli_docs.py`).

One cross-platform `memnos` command covers the whole platform: server lifecycle,
identity/access administration, namespaces, secrets, and a data client.

**Grammar:** heroes (`remember`, `recall`) and lifecycle verbs are top-level; management
is noun-verb (`principal create`, `token mint`, `grant add`, `namespace link`,
`secret set`). Pre-0.1.6 forms (`memnos principal <name>`, `memnos token <principal>`,
`memnos grant <p> <ns>`) keep working as permanent hidden aliases.

## Remote use

The data commands (`remember`, `recall`) and the MCP adapter talk to a memnos server
over HTTP — which doesn't have to be local. Point the CLI at any reachable server:

```bash
export MEMNOS_URL=https://memnos.internal.example.com:8900
export MEMNOS_TOKEN=mnk_...        # a token minted for you by the server admin
memnos recall "what did the team decide about retries?"
```

`MEMNOS_URL`/`MEMNOS_TOKEN` always win over the local `~/.memnos/config.json`.
Admin commands (`principal`, `token`, `grant`, `namespace`, `secret`, `stats`,
`health`) connect to Postgres directly via `MEMNOS_DSN` and are for the operator
of the server, not remote clients — remote administration goes through the
`/admin` console or the [REST API](api.md).
"""


def _walk_commands(parser, prefix=""):
    """Flatten the argparse tree into documented command entries. A subcommand with no
    help= is hidden (excluded). Nouns with verbs (principal/token/grant) contribute their
    `noun verb` children, not themselves."""
    sa = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
    if sa is None:
        return []
    helps = {ca.dest: ca.help for ca in sa._choices_actions}
    out, seen = [], set()
    for name, sp in sa.choices.items():
        if id(sp) in seen:
            continue
        seen.add(id(sp))
        full = f"{prefix} {name}".strip()
        if helps.get(name) is None:                      # hidden (e.g. docs-gen)
            continue
        children = _walk_commands(sp, full)
        if children:
            out.extend(children)
            continue
        args = []
        for a in sp._actions:
            if isinstance(a, (argparse._HelpAction, argparse._VersionAction,
                              argparse._SubParsersAction)):
                continue
            if a.option_strings:
                spec = {"arg": ", ".join(a.option_strings), "positional": False,
                        "required": bool(a.required)}
            else:
                spec = {"arg": a.dest, "positional": True, "required": a.nargs not in ("?", "*")}
            spec["help"] = a.help or ""
            if a.choices:
                spec["choices"] = [str(c) for c in a.choices]
            if a.default not in (None, False, argparse.SUPPRESS):
                spec["default"] = str(a.default)
            args.append(spec)
        out.append({"command": full, "help": helps[name], "args": args,
                    "example": EXAMPLES.get(full)})
    return out


def _docs_payload():
    cmds = {c["command"]: c for c in _walk_commands(build_parser())}
    groups, used = [], set()
    for title, names in _DOC_GROUPS:
        items = [cmds[n] for n in names if n in cmds]
        used.update(c["command"] for c in items)
        if items:
            groups.append({"title": title, "commands": items})
    leftover = [c for n, c in sorted(cmds.items()) if n not in used]
    if leftover:
        groups.append({"title": "Other", "commands": leftover})
    return groups


def _render_cli_md(groups):
    lines = [_CLI_MD_PREAMBLE]
    for g in groups:
        lines.append(f"\n## {g['title']}\n")
        for c in g["commands"]:
            lines.append(f"### `memnos {c['command']}`\n")
            lines.append(c["help"] + "\n")
            if c["args"]:
                lines.append("| argument | description |")
                lines.append("|---|---|")
                for a in c["args"]:
                    name = f"`{a['arg']}`" if a["positional"] else f"`{a['arg']}`"
                    extra = []
                    if a["positional"] and not a["required"]:
                        extra.append("optional")
                    if a.get("choices"):
                        extra.append("one of: " + ", ".join(f"`{x}`" for x in a["choices"]))
                    if a.get("default") not in (None, "auto") and not a["positional"]:
                        extra.append(f"default `{a['default']}`")
                    desc = a["help"] or ""
                    if extra:
                        desc = (desc + " " if desc else "") + "(" + "; ".join(extra) + ")"
                    lines.append(f"| {name} | {desc} |")
                lines.append("")
            if c.get("example"):
                lines.append("```bash\n" + c["example"] + "\n```\n")
    return "\n".join(lines).rstrip() + "\n"


def cmd_docs_gen(args, cfg):
    """(hidden) Regenerate docs/cli.md + ui/cli-reference.json from the argparse tree.
    --check: exit 1 if the committed files are stale (CI staleness gate)."""
    root = os.path.dirname(os.path.abspath(__file__))
    groups = _docs_payload()
    md = _render_cli_md(groups)
    js = json.dumps({"_generated": "memnos docs-gen — do not edit", "groups": groups},
                    indent=1, sort_keys=True) + "\n"
    targets = [(os.path.join(root, "docs", "cli.md"), md),
               (os.path.join(root, "ui", "cli-reference.json"), js)]
    if getattr(args, "check", False):
        stale = []
        for path, want in targets:
            try:
                cur = open(path, encoding="utf-8").read()
            except FileNotFoundError:
                cur = ""
            if cur != want:
                stale.append(os.path.relpath(path, root))
        if stale:
            sys.exit("CLI docs STALE vs the argparse tree: " + ", ".join(stale) +
                     "\n  regenerate + commit:  memnos docs-gen")
        print("CLI docs up to date.")
        return
    for path, want in targets:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(want)
        print(f"wrote {os.path.relpath(path, root)}")


def main():
    if sys.platform == "win32":                 # Windows consoles default to cp1252 —
        for _s in (sys.stdout, sys.stderr):     # our help/output uses Unicode (— · ↔)
            try:
                _s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    cfg = load_config()
    ap = build_parser()
    args = ap.parse_args(_normalize_argv(sys.argv[1:]))
    if not getattr(args, "cmd", None):     # bare `memnos` → version + help (like Claude Code)
        cur, lk = _installed_version(), cfg.get("latest_known")
        hint = f"   ↑ v{lk} available — run: memnos upgrade" if (cur and lk and _vparts(lk) > _vparts(cur)) else ""
        print(f"memnos {_version()}{hint}\n")
        ap.print_help()
        return
    args.fn(args, cfg)


if __name__ == "__main__":
    main()
