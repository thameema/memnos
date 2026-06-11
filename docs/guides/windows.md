# memnos on Windows — full installation guide

memnos runs natively on Windows 10/11 — the `memnos` CLI, the server, and the agent
integrations all work in PowerShell. The only genuinely fiddly part on Windows is
**pgvector**: PostgreSQL's Windows installer doesn't ship it, and the official install
path is a source build. This guide ranks the options honestly and gives you a
copy-paste path that avoids the pain entirely.

**Requirements recap:** PostgreSQL **13+** with **pgvector ≥ 0.7**, and Python **3.10+**.

---

## Fastest path (recommended): Docker Desktop

Let memnos run a pre-configured pgvector Postgres for you — no Postgres install, no
pgvector compile, no version-matching. This is the path we recommend for every Windows
user who doesn't already operate their own Postgres.

1. Install **[Docker Desktop](https://www.docker.com/products/docker-desktop/)** and start
   it (whale icon in the system tray).

2. Open **PowerShell** and run:

```powershell
# 1. install uv (Python package runner — installs to %USERPROFILE%\.local\bin)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. open a NEW PowerShell window so PATH refreshes, then:
uv tool install memnos
memnos --help                  # verify the command resolves

# 3. provision a pgvector Postgres in Docker + create the schema + admin token
memnos setup --docker

# 4. start the server (background) — first start downloads local models (~1 GB)
memnos start
memnos status
```

`memnos setup --docker` starts (or reuses) a container named `memnos-pg` from the
`pgvector/pgvector:pg16` image — Postgres with pgvector pre-baked, version-matched —
and writes the connection to `%USERPROFILE%\.memnos\config.json`. Re-running it is safe;
it reuses the existing container and never wipes data.

Then open the console at **http://127.0.0.1:8900/admin** and paste the admin token that
setup printed. Continue with the normal flow in [`QUICKSTART.md`](../../QUICKSTART.md)
(namespaces, tokens, `remember`/`recall`, agent wiring).

> **PATH gotchas:** both the uv installer and `uv tool install` place executables in
> `%USERPROFILE%\.local\bin` and update your user PATH — but only **new** terminals see
> it. If `memnos` (or `uv`) isn't found, open a fresh PowerShell window first. Prefer
> `pipx`? `pipx install memnos` works too, as does `.\install.ps1` from a source checkout.

---

## Native PostgreSQL path (advanced)

If you want memnos on a Postgres you install yourself (no Docker), be aware up front:
**getting pgvector onto a Windows Postgres is the hard part.** Ranked by friction:

### 1. Install PostgreSQL (easy)

Use the [EDB installer](https://www.postgresql.org/download/windows/) for PostgreSQL 16
or 17. **StackBuilder — the add-on catalog the EDB installer offers — does not include
pgvector** (as of this writing), so finishing the installer does *not* get you pgvector.

### 2. Get pgvector onto it (the hard part)

**Option A — pre-built community binaries (least friction, unofficial).** The pgvector
project publishes **no** Windows binaries in its GitHub releases. A community repo,
[`andreiramani/pgvector_pgsql_windows`](https://github.com/andreiramani/pgvector_pgsql_windows),
ships pre-compiled zips (pgvector 0.7.x–0.8.x for PG 13–18): download the zip matching
your **exact** PostgreSQL major version, extract it into your PostgreSQL install
directory per its readme (DLL into `lib\`, control/SQL files into `share\extension\`),
then restart the PostgreSQL service. These builds are **not** maintained by the pgvector
project or EDB — you're trusting a third-party binary; inspect/verify before using it on
anything that matters.

**Option B — build from source (the official way, painful).** pgvector's documented
Windows path requires **Visual Studio with C++ support** (Build Tools are enough). From
an *x64 Native Tools Command Prompt for VS*, run **as administrator**:

```bat
set "PGROOT=C:\Program Files\PostgreSQL\17"
cd %TEMP%
git clone --branch v0.8.2 https://github.com/pgvector/pgvector.git
cd pgvector
nmake /F Makefile.win
nmake /F Makefile.win install
```

(Adjust `PGROOT` to your version. Full details:
[pgvector installation notes](https://github.com/pgvector/pgvector#windows).) This is a
real compiler toolchain install for one extension — if that sounds like more than you
signed up for, use the Docker path above.

### 3. Verify and connect

```powershell
# in psql, connected to the database memnos will use:
#   CREATE EXTENSION vector;
#   SELECT extversion FROM pg_extension WHERE extname = 'vector';   -- needs >= 0.7

memnos setup --dsn postgresql://postgres:yourpassword@localhost:5432/memnos
memnos start
```

`memnos setup` runs this preflight for you: it checks the PG version (13+), checks
pgvector is available, runs `CREATE EXTENSION IF NOT EXISTS vector` (needs a superuser
role), and verifies the version is ≥ 0.7 (`halfvec` support). If pgvector is missing it
stops with the exact message
`pgvector (the 'vector' extension, >= 0.7) is NOT available to THIS Postgres server.` —
that means the extension files aren't in this server's `share\extension\` directory yet
(step 2 above).

---

## Windows specifics

### Start at login (autostart)

`memnos autostart` installs a login service on macOS (launchd) and Linux (systemd). On
Windows it doesn't install anything — it prints the Task Scheduler command for you to run
(with the real path to your `memnos.exe` substituted):

```
[memnos] Windows: create a logon task that runs `memnos serve`:
  schtasks /create /tn memnos /tr "C:\Users\you\.local\bin\memnos.exe serve" /sc onlogon
  (remove with: schtasks /delete /tn memnos)
```

Note this is a plain logon task: unlike the launchd/systemd services, it does **not**
auto-restart the server if it dies. Day to day, `memnos start` / `stop` / `restart` /
`status` manage the background server the same as on macOS/Linux.

### Files and logs

Everything lives under `%USERPROFILE%\.memnos\`:

| file | purpose |
|---|---|
| `config.json` | DSN, port, vault key — created by `memnos setup` |
| `server.log` | server logs (auto-rotated at 10 MB) — `Get-Content -Tail 50 -Wait "$env:USERPROFILE\.memnos\server.log"` |
| `server.pid` | background-server pid (managed by `start`/`stop`) |

### Console output (UTF-8)

Windows consoles default to cp1252; memnos output uses Unicode (`—`, `·`, `↔`). The CLI
reconfigures its own stdout/stderr to UTF-8 automatically, so output renders correctly in
PowerShell and Windows Terminal with no action needed. If you pipe memnos output through
other tools and see mojibake, set `$env:PYTHONUTF8 = "1"` (that's what our Windows CI
uses).

### Agent wiring paths

`memnos agent-setup` writes to the Windows-native config locations:

```powershell
memnos agent-setup claude-code      # %USERPROFILE%\.claude.json (MCP) + %USERPROFILE%\.claude\settings.json (hooks)
memnos agent-setup claude-desktop   # %APPDATA%\Claude\claude_desktop_config.json
```

Each is idempotent and backs up the file it edits. Restart the agent afterward.

---

## Troubleshooting

**`memnos` / `uv` not found after install** — the installer updated your user PATH, but
only new terminals pick it up. Open a fresh PowerShell window. The executables are in
`%USERPROFILE%\.local\bin`.

**Port 8900 already in use** — find and stop the occupant, or run on another port:

```powershell
Get-NetTCPConnection -LocalPort 8900 | Select-Object OwningProcess
memnos start --port 8901
```

**`pgvector ... is NOT available to THIS Postgres server`** — the extension isn't
installed for the server you connected to (or was built for a different PG major
version). See the [native path](#native-postgresql-path-advanced) above — or skip it all
with `memnos setup --docker`.

**Windows Firewall prompt on first start** — the server binds `127.0.0.1` only, so
localhost traffic works regardless; you can safely allow or dismiss the prompt. For
remote access put a TLS reverse proxy in front (see
[`docs/cli.md` → Remote use](../cli.md#remote-use)).

**Docker path: `Docker is installed but not running`** — start Docker Desktop and wait
for the whale icon to settle, then re-run `memnos setup --docker`.

**Container Postgres after a reboot** — Docker Desktop doesn't auto-start the
`memnos-pg` container unless Docker itself starts at login. `memnos setup --docker` (or
`docker start memnos-pg`) brings it back; the data volume persists.

---

## What's tested on Windows (honesty section)

Our CI runs a **3-OS matrix** (Linux, macOS, **Windows**) on every push. The Windows job
installs the real package and verifies the CLI end to end at the *parse* level: `memnos
--help`, version output, the docs-staleness gate, and `--help` for every public
subcommand — under `PYTHONUTF8=1`. The **full PostgreSQL-backed server test suite runs on
Linux CI only**. The server code is cross-platform Python with no POSIX-only calls in the
serve path, and the Docker path uses the same `pgvector/pgvector` image on every OS — but
we have not run the complete server suite on Windows in CI, so treat long-running
server-on-Windows operation as community-tested rather than CI-proven. Issues welcome.
