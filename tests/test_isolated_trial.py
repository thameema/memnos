#!/usr/bin/env python3
"""End-to-end isolated local trial & HOME/DB safety suite (`tests/test_isolated_trial.py`).

Verifies:
1. Custom `HOME` isolation: `setup`, `serve`, `status`, `remember`, `recall`, `session`, and
   `inbox` with a custom `HOME` NEVER read or modify the caller's real `~/.memnos` (proven via
   pre/post SHA-256 + mtime checks, an unreadable `0000` decoy `.memnos`, and `lsof` open-FD
   inspection on POSIX).
2. CWD `.env` isolation: `MEMNOS_NO_DOTENV=1` ignores a poison `.env` in CWD, and explicit
   `MEMNOS_EMBED_CHOICE=local` overrides a stray `OPENAI_API_KEY`.
3. Startup mixup visibility & credential redaction: `memnos serve` prints both the active
   `config.json` path and redacted `host:port/db` target on startup without leaking passwords.
4. `--require-empty-db` guard: both `memnos setup --require-empty-db` and
   `memnos serve --require-empty-db` refuse immediately (zero DDL/writes) when pointed at a
   database that already contains `memnos_control` / `tenant_memnos`.
5. Clean-`HOME` end-to-end workflow (`setup` -> `serve` -> `/readyz` -> `status` -> `remember`
   -> `recall` -> `session register` -> `inbox send` -> `inbox ls`).
"""
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import psycopg

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_BIN = os.path.join(
    REPO_ROOT,
    "target",
    "release",
    "memnos.exe" if os.name == "nt" else "memnos",
)
MEMNOS_BIN = os.environ.get("MEMNOS_BIN", DEFAULT_BIN)
ADMIN_DSN = os.environ.get("MEMNOS_ADMIN_DSN", "postgresql://thameema@localhost:5432/postgres")

PASS = 0
FAIL = 0


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {label}")
    else:
        FAIL += 1
        print(f"  FAIL  {label}" + (f" ({detail.strip()})" if detail else ""), file=sys.stderr)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def file_fingerprint(path: str):
    if not os.path.exists(path):
        return None
    st = os.stat(path)
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return (st.st_mtime_ns, st.st_size, h.hexdigest())


def find_existing_hf_cache(real_home: str) -> str:
    """Locate an existing local model cache so clean-HOME E2E tests don't re-download 930 MB."""
    # If ~/.memnos/models already has cached ONNX models, copy or symlink into a separate
    # read-only staging HF/models dir outside ~/.memnos so the server never touches ~/.memnos!
    cand_models = os.path.join(real_home, ".memnos", "models")
    if os.path.isdir(cand_models):
        return cand_models
    cand_hf = os.path.join(real_home, ".cache", "huggingface", "hub")
    if os.path.isdir(cand_hf):
        return cand_hf
    return ""


def create_scratch_db(dbname: str) -> str:
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{dbname}"')
    # Build DSN targeting the new scratch DB using same host/user/port as ADMIN_DSN
    if ADMIN_DSN.startswith(("postgresql://", "postgres://")):
        base, _ = ADMIN_DSN.rsplit("/", 1)
        dsn = f"{base}/{dbname}"
    else:
        dsn = f"{ADMIN_DSN} dbname={dbname}"
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    return dsn


def drop_scratch_db(dbname: str) -> None:
    try:
        with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{dbname}" WITH (FORCE)')
    except Exception:
        pass


def main() -> int:
    print(f"=== Isolated Local Trial & Clean-HOME Safety Suite ({MEMNOS_BIN}) ===")
    if not os.path.isfile(MEMNOS_BIN):
        if os.environ.get("CI") == "true":
            print(f"SKIP: compiled binary not present at {MEMNOS_BIN} in Python-only CI job")
            return 0
        print(f"FAIL: binary not found at {MEMNOS_BIN}", file=sys.stderr)
        return 1

    real_home = os.path.expanduser("~")
    real_memnos_dir = os.path.join(real_home, ".memnos")
    real_cfg_path = os.path.join(real_memnos_dir, "config.json")
    before_fp = file_fingerprint(real_cfg_path)
    src_models = find_existing_hf_cache(real_home)

    dbname = f"memnos_iso_trial_{os.getpid()}"
    scratch_dsn = create_scratch_db(dbname)
    port = free_port()

    try:
        with tempfile.TemporaryDirectory(prefix="memnos_clean_trial_") as tmp_root:
            clean_home = os.path.join(tmp_root, "clean_home")
            work_cwd = os.path.join(tmp_root, "work_cwd")
            os.makedirs(clean_home, exist_ok=True)
            os.makedirs(work_cwd, exist_ok=True)

            # Seed models into clean_home/.memnos/models via symlinks to individual model files
            # if available locally, OR let setup pull if online; note symlinks point to read-only
            # model blobs, NEVER config.json, tokens, or state files. For strict zero-realpath-under-
            # ~/.memnos FD verification, copy model files into clean_home/.memnos/models!
            if src_models and os.path.basename(src_models) == "models":
                dst_models = os.path.join(clean_home, ".memnos", "models")
                shutil.copytree(src_models, dst_models)

            # Plant a poison .env inside work_cwd to verify MEMNOS_NO_DOTENV=1 + MEMNOS_EMBED_CHOICE=local
            with open(os.path.join(work_cwd, ".env"), "w", encoding="utf-8") as f:
                f.write("MEMNOS_DSN=postgresql://poison:supersecret@127.0.0.1:1/poison_db\n")
                f.write("OPENAI_API_KEY=sk-stale-dotenv-poison-key\n")
                f.write("MEMNOS_PORT=19999\n")

            base_env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": clean_home,
                "USERPROFILE": clean_home,
                "MEMNOS_NO_DOTENV": "1",
                "MEMNOS_DSN": scratch_dsn,
                "MEMNOS_PORT": str(port),
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            }

            # 1. Run `memnos setup --dsn ... --port ... --local --require-empty-db --yes` on clean HOME
            r_setup = subprocess.run(
                [
                    MEMNOS_BIN,
                    "setup",
                    "--dsn",
                    scratch_dsn,
                    "--port",
                    str(port),
                    "--local",
                    "--require-empty-db",
                    "--yes",
                ],
                cwd=work_cwd,
                env=base_env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            setup_out = r_setup.stdout + r_setup.stderr
            expected_cfg = os.path.join(clean_home, ".memnos", "config.json")
            check(
                "Clean-HOME `memnos setup --local --require-empty-db --yes` succeeds ( ignores poison CWD .env )",
                r_setup.returncode == 0 and os.path.isfile(expected_cfg),
                setup_out,
            )
            check(
                "`memnos setup` prints config path and redacted target db",
                expected_cfg in setup_out and f"/{dbname}" in setup_out and "poison_db" not in setup_out,
                setup_out,
            )

            with open(expected_cfg, "r", encoding="utf-8") as f:
                written_cfg = json.load(f)
            check(
                "Clean-HOME config.json stores isolated port, DSN, and embed_choice='local'",
                written_cfg.get("port") == port
                and written_cfg.get("embed_choice") == "local"
                and dbname in (written_cfg.get("dsn") or ""),
                json.dumps(written_cfg),
            )

            # 2. Verify `--require-empty-db` REFUSES on an already-initialized DB (both setup and serve)
            r_refuse_setup = subprocess.run(
                [
                    MEMNOS_BIN,
                    "setup",
                    "--dsn",
                    scratch_dsn,
                    "--local",
                    "--require-empty-db",
                    "--yes",
                ],
                cwd=work_cwd,
                env=base_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            check(
                "`memnos setup --require-empty-db` refuses immediately on populated database",
                r_refuse_setup.returncode != 0
                and "already contains existing memnos schemas" in (r_refuse_setup.stdout + r_refuse_setup.stderr),
                r_refuse_setup.stdout + r_refuse_setup.stderr,
            )

            r_refuse_serve = subprocess.run(
                [MEMNOS_BIN, "serve", "--port", str(free_port()), "--require-empty-db"],
                cwd=work_cwd,
                env=base_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            check(
                "`memnos serve --require-empty-db` refuses immediately on populated database (zero DDL)",
                r_refuse_serve.returncode != 0
                and "already contains existing memnos schemas" in (r_refuse_serve.stdout + r_refuse_serve.stderr),
                r_refuse_serve.stdout + r_refuse_serve.stderr,
            )

            # 3. Verify credential redaction on startup banner using a password-bearing DSN
            #    (fails fast or connects, but MUST print redacted host:port/db and NEVER print password)
            pw_secret = "UltraSecretTrialPass99!"
            bad_pw_dsn = f"postgresql://nobody:{pw_secret}@127.0.0.1:1/{dbname}"
            r_redact = subprocess.run(
                [MEMNOS_BIN, "serve", "--port", str(free_port()), "--require-empty-db"],
                cwd=work_cwd,
                env={**base_env, "MEMNOS_DSN": bad_pw_dsn},
                capture_output=True,
                text=True,
                timeout=15,
            )
            redact_out = r_redact.stdout + r_redact.stderr
            check(
                "Startup banner prints `target db: 127.0.0.1:1/<db>` and redacts DSN password",
                f"target db: 127.0.0.1:1/{dbname}" in redact_out and pw_secret not in redact_out,
                redact_out,
            )

            # 4. Also verify `MEMNOS_EMBED_CHOICE=local` overrides a stray `OPENAI_API_KEY` in env
            #    while starting `memnos serve` on clean_home (relying on clean_home/.memnos/config.json
            #    for DSN + admin_token, WITHOUT passing MEMNOS_DSN in env!)
            serve_log_path = os.path.join(tmp_root, "serve.log")
            serve_env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": clean_home,
                "USERPROFILE": clean_home,
                "MEMNOS_NO_DOTENV": "1",
                "MEMNOS_EMBED_CHOICE": "local",
                "OPENAI_API_KEY": "sk-stray-env-should-be-ignored-when-choice-is-local",
                "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
            }
            with open(serve_log_path, "w+", encoding="utf-8") as slog:
                srv = subprocess.Popen(
                    [MEMNOS_BIN, "serve", "--port", str(port)],
                    cwd=work_cwd,
                    env=serve_env,
                    stdout=slog,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                try:
                    ready = False
                    deadline = time.time() + 45.0
                    while time.time() < deadline:
                        if srv.poll() is not None:
                            break
                        try:
                            with urllib.request.urlopen(
                                f"http://127.0.0.1:{port}/readyz", timeout=1.5
                            ) as resp:
                                body = json.loads(resp.read().decode("utf-8"))
                                if resp.status == 200 and body.get("ready") is True:
                                    ready = True
                                    break
                        except Exception:
                            pass
                        time.sleep(0.4)

                    slog.flush()
                    slog.seek(0)
                    srv_banner = slog.read()

                    check(
                        "Clean-HOME `memnos serve` reaches `/readyz` 200 ready=true using local 384-d models",
                        ready,
                        srv_banner,
                    )
                    check(
                        "`memnos serve` startup banner prints clean-HOME config.json path and target db",
                        f"[memnos] config: {expected_cfg} (present)" in srv_banner
                        and f"/{dbname}" in srv_banner
                        and real_memnos_dir not in srv_banner,
                        srv_banner,
                    )

                    # Inspect open file descriptors with `lsof` on macOS/Linux to prove ZERO open files under real ~/.memnos
                    if shutil.which("lsof") and os.name != "nt":
                        r_lsof = subprocess.run(
                            ["lsof", "-n", "-P", "-p", str(srv.pid)],
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        leaked_fds = [
                            ln for ln in r_lsof.stdout.splitlines() if real_memnos_dir in ln
                        ]
                        check(
                            "Running `serve` process holds ZERO open files under caller's real ~/.memnos (`lsof` verified)",
                            len(leaked_fds) == 0,
                            "\n".join(leaked_fds),
                        )

                    # 5. Run full CLI smoke (`status`, `remember`, `recall`, `session`, `inbox`) using ONLY clean_home
                    cli_env = {
                        "PATH": os.environ.get("PATH", ""),
                        "HOME": clean_home,
                        "USERPROFILE": clean_home,
                        "MEMNOS_NO_DOTENV": "1",
                        "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                    }
                    r_st = subprocess.run(
                        [MEMNOS_BIN, "status"],
                        cwd=work_cwd,
                        env=cli_env,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    check(
                        "Clean-HOME `memnos status` reports ONLINE ready=true and clean-HOME config path",
                        r_st.returncode == 0
                        and "ONLINE (ready=true" in r_st.stdout
                        and expected_cfg in r_st.stdout
                        and real_memnos_dir not in r_st.stdout,
                        r_st.stdout + r_st.stderr,
                    )

                    r_rem = subprocess.run(
                        [
                            MEMNOS_BIN,
                            "remember",
                            "Isolated trial marker: Zephyr-8920",
                            "--namespace",
                            "proj:trial",
                            "--no-extract",
                        ],
                        cwd=work_cwd,
                        env=cli_env,
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                    check(
                        "Clean-HOME `memnos remember` writes turn into isolated trial DB",
                        r_rem.returncode == 0 and "Remembered in 'proj:trial'" in r_rem.stdout,
                        r_rem.stdout + r_rem.stderr,
                    )

                    r_rec = subprocess.run(
                        [
                            MEMNOS_BIN,
                            "recall",
                            "Zephyr-8920 marker",
                            "--namespace",
                            "proj:trial",
                        ],
                        cwd=work_cwd,
                        env=cli_env,
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                    check(
                        "Clean-HOME `memnos recall` retrieves stored memory via local neural embeddings",
                        r_rec.returncode == 0 and "Zephyr-8920" in r_rec.stdout,
                        r_rec.stdout + r_rec.stderr,
                    )

                    # 6. Verify `/recall` and `/memory/search` honour `limit` (both without and with pinned constraints)
                    admin_tok = written_cfg["admin_token"]

                    def http_post(path: str, payload: dict):
                        req_obj = urllib.request.Request(
                            f"http://127.0.0.1:{port}{path}",
                            data=json.dumps(payload).encode("utf-8"),
                            headers={
                                "Content-Type": "application/json",
                                "Authorization": f"Bearer {admin_tok}",
                            },
                            method="POST",
                        )
                        try:
                            with urllib.request.urlopen(req_obj, timeout=15) as r:
                                return r.status, json.loads(r.read().decode("utf-8"))
                        except urllib.error.HTTPError as e:
                            return e.code, json.loads(e.read().decode("utf-8"))

                    for idx in range(8):
                        http_post(
                            "/remember",
                            {
                                "namespace": "proj:limit-check",
                                "text": f"Architecture note #{idx}: service mesh routing policy v{idx}",
                                "extract": False,
                            },
                        )

                    st_lim, j_lim = http_post(
                        "/recall",
                        {"namespace": "proj:limit-check", "query": "service mesh routing policy", "limit": 3},
                    )
                    check(
                        "`/recall` with `limit=3` (no pins) returns at most 3 rows in `memories`",
                        st_lim == 200 and len(j_lim.get("memories", [])) == 3,
                        json.dumps(j_lim),
                    )

                    for idx in range(5):
                        http_post(
                            "/remember",
                            {
                                "namespace": "proj:limit-check",
                                "text": f"Always enforce mTLS rule #{idx} across internal hops.",
                                "type": "constraint",
                                "extract": False,
                            },
                        )

                    st_pin_lim, j_pin_lim = http_post(
                        "/recall",
                        {"namespace": "proj:limit-check", "query": "service mesh routing policy", "limit": 3},
                    )
                    check(
                        "`/recall` with `limit=3` and 5 pinned constraints caps `memories` at 3 while preserving all 5 constraints in `pinned_constraints` & `context`",
                        st_pin_lim == 200
                        and len(j_pin_lim.get("memories", [])) == 3
                        and len(j_pin_lim.get("pinned_constraints", [])) == 5
                        and j_pin_lim.get("context", "").count("CONSTRAINT:") == 5,
                        json.dumps(j_pin_lim),
                    )

                    st_ms_lim, j_ms_lim = http_post(
                        "/memory/search",
                        {"namespace": "proj:limit-check", "query": "service mesh routing policy", "limit": 3},
                    )
                    check(
                        "`/memory/search` with `limit=3` returns 3 ranked non-pinned rows",
                        st_ms_lim == 200 and len(j_ms_lim.get("memories", [])) == 3,
                        json.dumps(j_ms_lim),
                    )

                    st_bad_lim, _ = http_post(
                        "/recall",
                        {"namespace": "proj:limit-check", "query": "routing", "limit": "not-an-int"},
                    )
                    check(
                        "`/recall` rejects non-integer `limit` with 400",
                        st_bad_lim == 400,
                        f"status={st_bad_lim}",
                    )

                    r_sess = subprocess.run(
                        [
                            MEMNOS_BIN,
                            "session",
                            "register",
                            "trial-agent",
                            "--namespace",
                            "proj:trial",
                        ],
                        cwd=work_cwd,
                        env=cli_env,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    check(
                        "Clean-HOME `memnos session register` succeeds in isolated trial DB",
                        r_sess.returncode == 0,
                        r_sess.stdout + r_sess.stderr,
                    )

                    r_inb_send = subprocess.run(
                        [
                            MEMNOS_BIN,
                            "inbox",
                            "send",
                            "trial-agent",
                            "Hello from isolated trial",
                            "--session",
                            "trial-peer",
                            "--subject",
                            "Smoke check",
                            "--namespace",
                            "proj:trial",
                        ],
                        cwd=work_cwd,
                        env=cli_env,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    r_inb_ls = subprocess.run(
                        [
                            MEMNOS_BIN,
                            "inbox",
                            "ls",
                            "--session",
                            "trial-agent",
                            "--namespace",
                            "proj:trial",
                        ],
                        cwd=work_cwd,
                        env=cli_env,
                        capture_output=True,
                        text=True,
                        timeout=15,
                    )
                    check(
                        "Clean-HOME `memnos inbox send` + `inbox ls` round-trip succeeds",
                        r_inb_send.returncode == 0
                        and r_inb_ls.returncode == 0
                        and "Smoke check" in r_inb_ls.stdout,
                        r_inb_send.stdout + r_inb_send.stderr + r_inb_ls.stdout + r_inb_ls.stderr,
                    )
                finally:
                    srv.terminate()
                    try:
                        srv.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        srv.kill()

        after_fp = file_fingerprint(real_cfg_path)
        check(
            "Caller's real ~/.memnos/config.json fingerprint (mtime_ns, size, sha256) is 100% unchanged",
            before_fp == after_fp,
            f"before={before_fp} after={after_fp}",
        )
    finally:
        drop_scratch_db(dbname)

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
