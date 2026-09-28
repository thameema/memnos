# memnos on Windows — full installation guide

memnos runs natively on Windows 10/11 — the `memnos` CLI, the server, and the agent
integrations all work in PowerShell. The historically fiddly part on Windows was
**pgvector**: PostgreSQL's Windows installer doesn't ship it, and the official install
path is a source build. `--embedded` sidesteps that entirely now — no Postgres install,
no compiler, no Docker. This guide ranks the options honestly.

**Requirements recap:** PostgreSQL **13+** with **pgvector ≥ 0.6**, and Python **3.10+**
(not needed at all for the `--embedded` path below).

---

## Fastest path (recommended): `--embedded` (zero dependencies)

No Postgres install, no Docker, no compiler — `memnos` downloads a self-contained
PostgreSQL 16 + pgvector binary and runs it as your user, listening on localhost only.

```powershell
# 1. install uv (Python package runner — installs to %USERPROFILE%\.local\bin)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"

# 2. open a NEW PowerShell window so PATH refreshes, then:
uv tool install memnos
memnos --help                  # verify the command resolves

# 3. download + start embedded PostgreSQL 16 + pgvector, create the schema + admin token
memnos setup --embedded

# 4. start the server (background) — first start downloads local models (~1 GB)
memnos start
memnos status
```

`memnos setup --embedded` downloads a ~20-30 MB archive into `%USERPROFILE%\.memnos\embedded_pg\`
on first run and never touches any Postgres you already have installed. `memnos start`
auto-starts the embedded database on every boot.

Then open the console at **http://127.0.0.1:8900/admin** and paste the admin token that
setup printed. Continue with the normal flow in [`QUICKSTART.md`](../../QUICKSTART.md)
(namespaces, tokens, `remember`/`recall`, agent wiring).

> **PATH gotchas:** both the uv installer and `uv tool install` place executables in
> `%USERPROFILE%\.local\bin` and update your user PATH — but only **new** terminals see
> it. If `memnos` (or `uv`) isn't found, open a fresh PowerShell window first. Prefer
> `pipx`? `pipx install memnos` works too, as does `.\install.ps1` from a source checkout.

> **First start can be slow** — Windows Defender (or your AV) scans the freshly downloaded,
> unsigned `postgres.exe`/`pg_ctl.exe`/`initdb.exe` and their DLLs the first time each one
> runs, which on a cold cache can add real minutes (our own Windows CI saw this too). It's
> a one-time cost per binary — subsequent `memnos start`s are fast. If it seems stuck for
> more than ~5 minutes, check `%USERPROFILE%\.memnos\embedded_pg\pg.log`.

---

## Alternative: Docker Desktop (any platform)

Let memnos run a pre-configured pgvector Postgres in a container instead — useful if you
already run Docker or prefer isolating Postgres from your host entirely.

```powershell
memnos setup --docker   # needs Docker Desktop running; provisions pgvector/pgvector:pg16
memnos start
memnos status
```

`memnos setup --docker` starts (or reuses) a container named `memnos-pg` from the
`pgvector/pgvector:pg16` image — Postgres with pgvector pre-baked, version-matched —
and writes the connection to `%USERPROFILE%\.memnos\config.json`. Re-running it is safe;
it reuses the existing container and never wipes data.

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
role), and verifies the version is ≥ 0.6 (it uses the `halfvec` storage optimization on
≥ 0.7, full-precision `vector` columns on 0.6). If pgvector is missing it
stops with the exact message
`pgvector (the 'vector' extension, >= 0.6) is NOT available to THIS Postgres server.` —
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
with `memnos setup --embedded` (or `--docker`).

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
installs the real package, verifies the CLI at the *parse* level (`memnos --help`,
version output, the docs-staleness gate, `--help` for every public subcommand under
`PYTHONUTF8=1`) — **and now also runs `memnos setup --embedded` → `start` → a real
`/healthz` check → `stop` end to end**, downloading the actual released windows-amd64
PG16 + pgvector archive and starting a real Postgres server, not a mock. The pgvector
build itself is compiled on `windows-latest` via MSVC/nmake as part of publishing that
archive, with its own initdb/start/`CREATE EXTENSION`/stop smoke test before anything is
uploaded.

The **full application-level server test suite (governance, extraction, recall behavior)
still runs on Linux CI only** — the server code is cross-platform Python with no
POSIX-only calls in the serve path, but we haven't run that full suite on Windows in CI.
And a clean `windows-latest` Server runner isn't the same as your actual Windows 11 Home
laptop: CI proves the code path works, it doesn't prove your specific AV/SmartScreen
configuration won't add delay or friction on first run (see the callout above). Issues
welcome.
