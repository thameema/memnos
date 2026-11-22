"""Scratch-isolated regression tests for release blockers (B1-B6 & #241).

Always creates its own temporary PostgreSQL scratch database, isolated HOME,
and ephemeral server port — never touches a live server or database.
"""
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
from psycopg.rows import dict_row

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMNOS_BIN = os.environ.get(
    "MEMNOS_BIN", os.path.join(REPO_ROOT, "target", "release", "memnos")
)
ADMIN_DSN = os.environ.get(
    "MEMNOS_ADMIN_DSN",
    os.environ.get("MEMNOS_DSN", "postgresql://localhost:5432/postgres"),
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    tag = "PASS" if cond else "FAIL"
    suffix = f"  — {detail[:280]}" if (detail and not cond) else ""
    print(f"  {tag}  {name}{suffix}")
    if cond:
        PASS += 1
    else:
        FAIL += 1


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_scratch_db(admin_dsn: str, db_name: str) -> str:
    base = admin_dsn.rsplit("/", 1)[0]
    for maint in (admin_dsn, f"{base}/postgres", f"{base}/template1"):
        try:
            with psycopg.connect(maint, autocommit=True) as conn:
                conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
                conn.execute(f'CREATE DATABASE "{db_name}"')
                break
        except Exception:
            continue
    scratch = f"{base}/{db_name}"
    for ext_dsn in (scratch, f"postgresql://localhost:5432/{db_name}"):
        try:
            with psycopg.connect(ext_dsn, autocommit=True) as conn:
                conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
                break
        except Exception:
            continue
    return scratch


def drop_scratch_db(admin_dsn: str, db_name: str) -> None:
    base = admin_dsn.rsplit("/", 1)[0]
    for maint in (admin_dsn, f"{base}/postgres", f"{base}/template1"):
        try:
            with psycopg.connect(maint, autocommit=True) as conn:
                conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
                return
        except Exception:
            continue


def http_json(url: str, method: str, path: str, token: str = "", body=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url + path,
        method=method,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        raw = e.read() or b"{}"
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw.decode("utf-8", errors="replace")}


def wait_ready(url: str, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            st, body = http_json(url, "GET", "/readyz")
            if st == 200 and body.get("ready"):
                return True
        except Exception:
            pass
        time.sleep(0.15)
    return False


def test_b1_b2_b6_data_safety_and_backups() -> None:
    print("\n=== B1 + B2 + B6: CLI `ns move --like`, Single-Tx Atomic Rollback, Retired Constraints & Pre-Op Verified Backups ===")
    db_name = f"memnos_blk1_{int(time.time())}"
    dsn = make_scratch_db(ADMIN_DSN, db_name)
    home = tempfile.mkdtemp(prefix="memnos_blk1_home_")
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    hf_cache = os.path.expanduser("~/.cache/huggingface/hub")

    base_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("OPENAI_API_KEY", "MEMNOS_SECRET_KEY", "MEMNOS_DSN", "MEMNOS_TOKEN")
    }
    base_env.update(
        {
            "HOME": home,
            "MEMNOS_NO_DOTENV": "1",
            "MEMNOS_EMBED_CHOICE": "local",
            "MEMNOS_RERANK": "0",
            "HF_HUB_CACHE": hf_cache,
        }
    )
    os.makedirs(os.path.join(home, ".memnos"), exist_ok=True)
    with open(os.path.join(home, ".memnos", "config.json"), "w", encoding="utf-8") as fh:
        json.dump({"dsn": dsn, "port": port, "embed_choice": "local"}, fh)

    srv = subprocess.Popen(
        [MEMNOS_BIN, "serve", "--port", str(port), "--require-empty-db"],
        env=base_env,
        cwd=home,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        check("Scratch server boots cleanly on empty DB", wait_ready(url))
        adm_run = subprocess.run(
            [MEMNOS_BIN, "admin", "--raw"],
            env=base_env,
            cwd=home,
            capture_output=True,
            text=True,
            timeout=15,
        )
        admin_tok = adm_run.stdout.strip()
        check("CLI `memnos admin --raw` mints admin token on scratch DB", admin_tok.startswith("mnk_"), adm_run.stderr)

        # ---- B1: CLI `ns move --like` must NOT delete non-matching rows or source namespace ----
        http_json(url, "POST", "/remember", admin_tok, {"namespace": "proj:b1-src", "text": "Payments webhook retry backoff policy", "extract": False})
        http_json(url, "POST", "/remember", admin_tok, {"namespace": "proj:b1-src", "text": "Billing ledger reconciliation job", "extract": False})

        mv_like = subprocess.run(
            [MEMNOS_BIN, "namespace", "move", "proj:b1-src", "--to", "proj:b1-dst", "--like", "Payments"],
            env=base_env,
            cwd=home,
            capture_output=True,
            text=True,
            timeout=25,
        )
        mv_json = json.loads(mv_like.stdout or "{}") if mv_like.returncode == 0 else {}
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            src_turns = [r["text"] for r in conn.execute("SELECT text FROM tenant_memnos.raw_turns WHERE namespace='proj:b1-src'")]
            dst_turns = [r["text"] for r in conn.execute("SELECT text FROM tenant_memnos.raw_turns WHERE namespace='proj:b1-dst'")]
            src_ns_exists = conn.execute("SELECT 1 FROM memnos_control.namespaces WHERE name='proj:b1-src'").fetchone() is not None

        check(
            "B1: CLI `ns move --like` moves matching rows ONLY and preserves source namespace & non-matching rows",
            mv_like.returncode == 0
            and mv_json.get("removed_source") is False
            and src_ns_exists
            and src_turns == ["Billing ledger reconciliation job"]
            and dst_turns == ["Payments webhook retry backoff policy"],
            f"rc={mv_like.returncode} src={src_turns} dst={dst_turns} stderr={mv_like.stderr}",
        )
        check(
            "B6 (CLI move): pre-transfer verified backup file created before move",
            bool(mv_json.get("pre_backup")) and os.path.isfile(mv_json["pre_backup"]) and os.path.getsize(mv_json["pre_backup"]) > 0,
            str(mv_json),
        )

        # ---- B2a: Retired constraint rows are preserved (copied with constraint_retired_at) in move mode ----
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            conn.execute(
                """
                INSERT INTO tenant_memnos.raw_turns(namespace, speaker, text, memory_type, constraint_subject, constraint_retired_at)
                VALUES ('proj:b2-ret-src', 'user', 'Retired legacy rule v1', 'constraint', 'legacy-rule', now())
                """
            )
            conn.execute(
                """
                INSERT INTO tenant_memnos.semantic(namespace, kind, statement, memory_type, constraint_subject, constraint_retired_at)
                VALUES ('proj:b2-ret-src', 'constraint', 'Retired legacy rule v1', 'constraint', 'legacy-rule', now())
                """
            )
        st_ret, res_ret = http_json(
            url,
            "POST",
            "/workspace/api/namespace/transfer",
            admin_tok,
            {"src": "proj:b2-ret-src", "dst": "proj:b2-ret-dst", "mode": "merge", "delete_source": True},
        )
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            ret_rt = conn.execute(
                "SELECT constraint_retired_at FROM tenant_memnos.raw_turns WHERE namespace='proj:b2-ret-dst' AND constraint_subject='legacy-rule'"
            ).fetchone()
            ret_sem = conn.execute(
                "SELECT constraint_retired_at FROM tenant_memnos.semantic WHERE namespace='proj:b2-ret-dst' AND constraint_subject='legacy-rule'"
            ).fetchone()
        check(
            "B2a: Namespace move/merge preserves retired constraint rows in destination (never destroyed uncopied)",
            st_ret == 200
            and ret_rt is not None
            and ret_rt["constraint_retired_at"] is not None
            and ret_sem is not None
            and ret_sem["constraint_retired_at"] is not None,
            f"st={st_ret} res={res_ret} rt={ret_rt} sem={ret_sem}",
        )

        # ---- B2b: Single-transaction failure injection rolls back BOTH memory move AND control-plane ----
        http_json(url, "POST", "/remember", admin_tok, {"namespace": "proj:b2-tx-src", "text": "Critical architectural fact that must survive rollback", "extract": False})
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            conn.execute(
                "INSERT INTO tenant_memnos.semantic(namespace, kind, statement) VALUES ('proj:b2-tx-src', 'fact', 'Service uses TLS 1.3 everywhere')"
            )
        http_json(url, "POST", "/workspace/api/channel/create", admin_tok, {"namespace": "proj:b2-tx-src", "channel": "#ops"})
        http_json(
            url,
            "POST",
            "/workspace/api/channel/publish",
            admin_tok,
            {"namespace": "proj:b2-tx-src", "channel": "#ops", "sender_session": "lead", "content": "Deploy checklist"},
        )

        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                """
                CREATE OR REPLACE FUNCTION fail_cp_transfer_test() RETURNS trigger AS $$
                BEGIN
                    IF NEW.namespace = 'proj:b2-tx-dst' THEN
                        RAISE EXCEPTION 'injected_control_plane_transfer_failure';
                    END IF;
                    RETURN NEW;
                END;
                $$ LANGUAGE plpgsql;
                CREATE TRIGGER trg_fail_cp_transfer
                BEFORE UPDATE ON memnos_control.channel_messages
                FOR EACH ROW EXECUTE FUNCTION fail_cp_transfer_test();
                """
            )

        st_fail, res_fail = http_json(
            url,
            "POST",
            "/workspace/api/namespace/transfer",
            admin_tok,
            {"src": "proj:b2-tx-src", "dst": "proj:b2-tx-dst", "mode": "merge", "delete_source": True},
        )
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            src_rt_after = conn.execute("SELECT count(*) AS n FROM tenant_memnos.raw_turns WHERE namespace='proj:b2-tx-src'").fetchone()["n"]
            src_sem_live = conn.execute("SELECT count(*) AS n FROM tenant_memnos.semantic WHERE namespace='proj:b2-tx-src' AND expired_at IS NULL").fetchone()["n"]
            dst_rt_after = conn.execute("SELECT count(*) AS n FROM tenant_memnos.raw_turns WHERE namespace='proj:b2-tx-dst'").fetchone()["n"]
            dst_sem_after = conn.execute("SELECT count(*) AS n FROM tenant_memnos.semantic WHERE namespace='proj:b2-tx-dst'").fetchone()["n"]
            conn.execute("DROP TRIGGER trg_fail_cp_transfer ON memnos_control.channel_messages")
            conn.execute("DROP FUNCTION fail_cp_transfer_test()")

        check(
            "B2b: Control-plane failure mid-transfer rolls back memory move & source deletes atomically (0 rows lost)",
            st_fail >= 400
            and src_rt_after == 1
            and src_sem_live == 1
            and dst_rt_after == 0
            and dst_sem_after == 0,
            f"st={st_fail} res={res_fail} src_rt={src_rt_after} src_sem={src_sem_live} dst_rt={dst_rt_after}",
        )

        # ---- B6: Doctor pre-backup + constraint protection + namespace-scoped orphan mentions + failed-backup abort ----
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            # Duplicate live constraints must NEVER be expired by Doctor dedupe
            for _ in range(2):
                conn.execute(
                    """
                    INSERT INTO tenant_memnos.semantic(namespace, kind, statement, memory_type, constraint_subject)
                    VALUES ('proj:doc-a', 'constraint', 'NEVER disable TLS verification', 'constraint', 'tls-verify')
                    """
                )
            # Duplicate normal facts SHOULD be deduped down to 1
            for _ in range(2):
                conn.execute(
                    """
                    INSERT INTO tenant_memnos.semantic(namespace, kind, statement, memory_type)
                    VALUES ('proj:doc-a', 'fact', 'Queue worker concurrency is 8', 'fact')
                    """
                )
            # Orphan mention in ANOTHER namespace (`proj:doc-other`) must NOT be deleted when Doctor runs on `proj:doc-a`
            ent_other = conn.execute(
                "INSERT INTO tenant_memnos.entities(namespace, name) VALUES ('proj:doc-other', 'PaymentGateway') RETURNING id"
            ).fetchone()["id"]
            conn.execute(
                "INSERT INTO tenant_memnos.mentions(entity_id, memory_id, memory_kind) VALUES (%s, 99999999, 'semantic')",
                (ent_other,),
            )

        st_doc, res_doc = http_json(
            url,
            "POST",
            "/workspace/api/doctor/run",
            admin_tok,
            {"namespace": "proj:doc-a", "actions": ["repair_graph", "optimize_memories"], "dry_run": False},
        )
        doc_bkp = (res_doc.get("results") or {}).get("pre_backup", "")
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            live_constraints = conn.execute(
                "SELECT count(*) AS n FROM tenant_memnos.semantic WHERE namespace='proj:doc-a' AND memory_type='constraint' AND expired_at IS NULL"
            ).fetchone()["n"]
            live_facts = conn.execute(
                "SELECT count(*) AS n FROM tenant_memnos.semantic WHERE namespace='proj:doc-a' AND memory_type='fact' AND expired_at IS NULL"
            ).fetchone()["n"]
            other_mentions = conn.execute(
                "SELECT count(*) AS n FROM tenant_memnos.mentions WHERE entity_id=%s",
                (ent_other,),
            ).fetchone()["n"]

        check(
            "B6a: `doctor/run` creates verified pre-doctor backup BEFORE mutating",
            st_doc == 200 and bool(doc_bkp) and os.path.isfile(doc_bkp) and os.path.getsize(doc_bkp) > 0,
            str(res_doc),
        )
        check(
            "B6b: Doctor dedupe never expires `constraint` rows and scopes orphan-mention cleanup strictly to target namespace",
            live_constraints == 2 and live_facts == 1 and other_mentions == 1,
            f"constraints={live_constraints} facts={live_facts} other_mentions={other_mentions}",
        )

        # DELETE /workspace/api/namespace?purge=1 creates pre-purge backup
        http_json(url, "POST", "/remember", admin_tok, {"namespace": "proj:purge-ok", "text": "Ephemeral scratch note", "extract": False})
        st_del, res_del = http_json(
            url, "DELETE", "/workspace/api/namespace?namespace=proj%3Apurge-ok&purge=1", admin_tok
        )
        check(
            "B6c: `DELETE /workspace/api/namespace?purge=1` creates verified pre-purge backup",
            st_del == 200 and bool(res_del.get("pre_backup")) and os.path.isfile(res_del["pre_backup"]),
            str(res_del),
        )

        # CLI `ns rm --purge` MUST abort and leave data untouched if `pg_dump` is unavailable
        http_json(url, "POST", "/remember", admin_tok, {"namespace": "proj:purge-guard", "text": "Must survive failed backup", "extract": False})
        bad_bkp_env = {**base_env, "MEMNOS_PG_DUMP": "/nonexistent/pg_dump"}
        rm_fail = subprocess.run(
            [MEMNOS_BIN, "namespace", "rm", "proj:purge-guard", "--purge"],
            env=bad_bkp_env,
            cwd=home,
            capture_output=True,
            text=True,
            timeout=15,
        )
        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            survived = conn.execute(
                "SELECT count(*) AS n FROM tenant_memnos.raw_turns WHERE namespace='proj:purge-guard'"
            ).fetchone()["n"]
        check(
            "B6d: `ns rm --purge` refuses & aborts BEFORE deleting any data when verified backup cannot be taken",
            rm_fail.returncode != 0 and survived == 1,
            f"rc={rm_fail.returncode} survived={survived} stderr={rm_fail.stderr}",
        )
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()
        shutil.rmtree(home, ignore_errors=True)
        drop_scratch_db(ADMIN_DSN, db_name)


def main() -> None:
    if not os.path.isfile(MEMNOS_BIN):
        print(f"ERROR: binary not found at {MEMNOS_BIN} — run `bash scripts/build_release.sh --bins` first")
        sys.exit(2)
    test_b1_b2_b6_data_safety_and_backups()
    print(f"\nPassed: {PASS}, Failed: {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
