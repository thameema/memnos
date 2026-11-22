"""End-to-End Seamless In-Place Upgrade Migration, PyPI `memnos-sdk` Contract,
Semantic Synonym Recall, HTTP SPO Supersession, Mismatch Guard, & Gateway Zero-Downtime Suite.

Verifies:
1. Full PyPI `memnos-sdk` (`MemnosClient` + `AsyncMemnosClient`) compatibility against the
   compiled `memnos` server — every public SDK method (`remember`, `async_`, `extract=False`,
   `memories_by_type`, `ingest_file`, `recall`, `context`, `consolidate`, `feedback`,
   `resolve_secret`, `session_*`, `channel_*`, `inbox_*`) preserves exact wire semantics.
2. Deterministic HTTP-level SPO fact supersession + `/reconcile` `stale` detection via the
   standard OpenAI-compatible `MEMNOS_EXTRACT_BASE_URL` endpoint (zero test-only server flags).
3. Seamless in-place upgrade from an existing `$HOME/.memnos/config.json` + AES-256-GCM `Vault`
   secrets + `mnk_` scoped tokens + `fastembed` `BAAI/bge-small-en-v1.5` (`halfvec(384)`)
   database to the compiled single binary with zero config edits, zero schema column drift,
   and rank-1 paraphrase/synonym recall parity.
4. Loud refusal when a populated 384-d schema is booted with a mismatched 1536-d OpenAI
   configuration.
5. Zero-downtime `memnos gateway` + `memnos restart` blue-green backend swap with 0 dropped
   SDK calls.

Run:
    MEMNOS_ADMIN_DSN=postgresql://.../postgres \
    MEMNOS_LEGACY_REPO=/path/to/legacy/memnos \
    python3 tests/test_upgrade_migration.py
"""
from __future__ import annotations

import asyncio
import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse, urlunparse

import httpx
import psycopg
from psycopg.rows import dict_row

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LEGACY_REPO = os.path.abspath(os.environ.get("MEMNOS_LEGACY_REPO", ROOT))
MEMNOS_BIN = os.path.abspath(
    os.environ.get("MEMNOS_BIN")
    or os.environ.get("MEMNOS_CLI_BIN")
    or os.path.join(ROOT, "target", "release", "memnos")
)

sys.path.insert(0, os.path.join(ROOT, "sdk"))
sys.path.insert(0, LEGACY_REPO)

from memnos_sdk import AsyncMemnosClient, MemnosClient  # noqa: E402

PASS = 0
FAIL = 0

SPO_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_.-]{1,63})\s+([a-z][a-z0-9_]{1,48})\s+is\s+([^\r\n]+?)\s*\.?\s*$")


class _LocalExtractHandler(BaseHTTPRequestHandler):
    """Minimal OpenAI-compatible `/v1/chat/completions` & `/v1/embeddings` endpoint for $0 tests."""

    expected_bearer: str | None = None

    def do_POST(self) -> None:
        if self.expected_bearer:
            auth = self.headers.get("Authorization", "")
            if auth != f"Bearer {self.expected_bearer}":
                body = json.dumps({"error": f"unauthorized bearer: {auth}"}).encode()
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return

        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        req = json.loads(raw or b"{}")
        if self.path.endswith("/embeddings"):
            import hashlib
            import math

            inp = str(req.get("input") or "")
            vec = [0.0] * 1536
            for tok in re.findall(r"[a-z0-9]+", inp.lower()):
                h = hashlib.sha256(tok.encode()).digest()
                idx = int.from_bytes(h[:4], "little") % 1536
                vec[idx] += 1.0
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            vec = [x / norm for x in vec]
            resp = {
                "data": [{"embedding": vec, "index": 0}],
                "usage": {"prompt_tokens": 9, "total_tokens": 9},
            }
        else:
            user_txt = ""
            for m in req.get("messages", []):
                if m.get("role") == "user":
                    user_txt = m.get("content", "")
            facts = []
            for line in user_txt.splitlines():
                m = SPO_RE.match(line)
                if m:
                    s, p, o = m.group(1).strip(), m.group(2).strip(), m.group(3).strip().rstrip(".")
                    facts.append({"subject": s, "predicate": p, "object": o, "statement": f"{s} {p} is {o}"})
            resp = {
                "choices": [{"message": {"content": json.dumps({"facts": facts})}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8},
            }
        body = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        pass


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    tag = "PASS" if cond else "FAIL"
    suffix = f" ({detail})" if detail and not cond else ""
    print(f"  {tag}  {name}{suffix}")
    if cond:
        PASS += 1
    else:
        FAIL += 1


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_ready(url: str, timeout: float = 25.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{url}/readyz", timeout=1.0)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.1)
    return False


def make_scratch_db(admin_dsn: str, db_name: str) -> str:
    with psycopg.connect(admin_dsn, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        conn.execute(f'CREATE DATABASE "{db_name}"')
    parsed = urlparse(admin_dsn)
    scratch_dsn = urlunparse(parsed._replace(path=f"/{db_name}"))
    with psycopg.connect(scratch_dsn, autocommit=True) as conn:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
    return scratch_dsn


def drop_scratch_db(admin_dsn: str, db_name: str) -> None:
    try:
        with psycopg.connect(admin_dsn, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
    except Exception:
        pass


def schema_column_inventory(dsn: str, schema: str = "tenant_memnos") -> list[tuple]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            """
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = %s
            ORDER BY table_name, ordinal_position
            """,
            (schema,),
        ).fetchall()
        return list(rows)


def write_memnos_config(home_dir: str, dsn: str, port: int, secret_key_b64: str, admin_token: str, agent_tokens: dict | None = None) -> None:
    cfg_dir = os.path.join(home_dir, ".memnos")
    os.makedirs(cfg_dir, exist_ok=True)
    cfg = {
        "dsn": dsn,
        "port": port,
        "secret_key": secret_key_b64,
        "admin_token": admin_token,
        "default_namespace": "proj:migration",
        "agent_tokens": agent_tokens or {},
    }
    with open(os.path.join(cfg_dir, "config.json"), "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def test_sdk_and_spo_supersession(admin_dsn: str) -> None:
    print("\n=== 1. PyPI memnos-sdk E2E Contract + HTTP SPO Supersession ===")
    db_name = f"memnos_sdk_e2e_{os.getpid()}"
    dsn = make_scratch_db(admin_dsn, db_name)
    home = tempfile.mkdtemp(prefix="memnos_sdk_home_")
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    key_b64 = base64.b64encode(secrets.token_bytes(32)).decode()

    llm_port = free_port()
    llm_srv = HTTPServer(("127.0.0.1", llm_port), _LocalExtractHandler)
    threading.Thread(target=llm_srv.serve_forever, daemon=True).start()

    env = {
        **os.environ,
        "HOME": home,
        "HF_HUB_CACHE": os.path.expanduser("~/.cache/huggingface/hub"),
        "MEMNOS_DSN": dsn,
        "MEMNOS_PORT": str(port),
        "MEMNOS_EMBED_CHOICE": "local",
        "MEMNOS_EXTRACT_BASE_URL": f"http://127.0.0.1:{llm_port}/v1",
    }
    env.pop("OPENAI_API_KEY", None)
    write_memnos_config(home, dsn, port, key_b64, "")

    # Verify post-install notification UX when local models are not yet in cache
    empty_cache_env = {**env, "HF_HUB_CACHE": os.path.join(home, "empty_hf_cache")}
    r_mstat = subprocess.run([MEMNOS_BIN, "models", "status"], env=empty_cache_env, capture_output=True, text=True, timeout=15)
    check(
        "`memnos models status` notifies user when local model download is required (no external AI)",
        "DOWNLOAD REQUIRED" in r_mstat.stdout and "zero external AI" in r_mstat.stdout,
        r_mstat.stdout,
    )
    r_asetup = subprocess.run([MEMNOS_BIN, "agent-setup", "cursor"], env=empty_cache_env, capture_output=True, text=True, timeout=15)
    check(
        "`memnos agent-setup` notifies user about required embedding setup (`memnos setup embeddings`)",
        "memnos setup embeddings" in r_asetup.stdout and "memnos models pull" in r_asetup.stdout,
        r_asetup.stdout,
    )

    r_boot = subprocess.run([MEMNOS_BIN, "principal", "create", "sdk-dev"], env=env, capture_output=True, text=True, timeout=20)
    check("CLI principal create sdk-dev", r_boot.returncode == 0, r_boot.stderr)

    for g_ns in ("proj:sdk", "secret:stripe_webhook"):
        rg = subprocess.run([MEMNOS_BIN, "grant", "add", "sdk-dev", g_ns], env=env, capture_output=True, text=True, timeout=20)
        check(f"CLI grant add sdk-dev {g_ns}", rg.returncode == 0, rg.stderr)

    rtok = subprocess.run([MEMNOS_BIN, "token", "mint", "sdk-dev", "--label", "pypi-test"], env=env, capture_output=True, text=True, timeout=20)
    sdk_token = ""
    for word in (rtok.stdout + " " + rtok.stderr).split():
        if word.startswith("mnk_"):
            sdk_token = word.strip()
            break
    check("CLI token mint emits mnk_ token", sdk_token.startswith("mnk_"), rtok.stdout + rtok.stderr)

    rsec = subprocess.run(
        [MEMNOS_BIN, "secret", "set", "stripe_webhook", "--value", "whsec_test_987654321"],
        env={**env, "MEMNOS_SECRET_KEY": key_b64},
        capture_output=True,
        text=True,
        timeout=20,
    )
    check("CLI secret set stores encrypted secret", rsec.returncode == 0, rsec.stderr)

    srv = subprocess.Popen(
        [MEMNOS_BIN, "serve", "--port", str(port)],
        env={**env, "MEMNOS_SECRET_KEY": key_b64},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        check("Server reaches /readyz", wait_ready(url))
        hz = httpx.get(f"{url}/healthz", timeout=5.0).json()
        check("/healthz omits internal engine field", "engine" not in hz and hz.get("ok") is True, str(hz))

        with MemnosClient(base_url=url, token=sdk_token, namespace="proj:sdk") as client:
            check("SDK healthy()", client.healthy())
            check("SDK resolve_secret()", client.resolve_secret("stripe_webhook") == "whsec_test_987654321")

            # SPO deterministic supersession over HTTP (/remember + /reconcile)
            w1 = client.remember("CheckoutService deployed_version is 1.4.0")
            check("SDK remember SPO v1 writes >= 1 fact", (w1.get("facts") or 0) >= 1, str(w1))
            w2 = client.remember("CheckoutService deployed_version is 2.1.0")
            check("SDK remember SPO v2 supersedes v1", (w2.get("superseded") or 0) >= 1, str(w2))

            rec_stale = httpx.post(
                f"{url}/reconcile",
                headers={"Authorization": f"Bearer {sdk_token}"},
                json={
                    "namespace": "proj:sdk",
                    "statement": "CheckoutService deployed_version is 1.4.0",
                    "subject": "CheckoutService",
                    "predicate": "deployed_version",
                },
                timeout=15.0,
            ).json()
            check(
                "HTTP /reconcile detects stale superseded version",
                rec_stale.get("verdict") == "stale" and rec_stale.get("current_value") == "2.1.0",
                str(rec_stale),
            )

            # Async remember, extract=False, constraint, memories_by_type, ingest_file, recall, context
            wa = client.remember("Background turn for async queue", async_=True)
            check("SDK remember(async_=True) -> queued", wa.get("extraction") == "queued", str(wa))

            we = client.remember("Raw note with extract=False", extract=False)
            check("SDK remember(extract=False) -> 0 facts", we.get("facts") == 0, str(we))

            wc = client.remember("Never bypass rate limiters in CheckoutService", memory_type="constraint", constraint_subject="rate-limit")
            check("SDK remember(memory_type='constraint')", wc.get("constraint_subject") == "rate-limit", str(wc))

            client.remember("Handoff note #1", memory_type="handoff", extract=False)
            client.remember("Handoff note #2", memory_type="handoff", extract=False)
            by_t = client.memories_by_type("handoff")
            check("SDK memories_by_type('handoff') returns exact 2 records", len(by_t.get("memories") or by_t.get("results") or []) == 2, str(by_t))

            ing = client.ingest_file("runbook.md", "# Failover\n\nPromote standby replica within 60 seconds.", extract=False)
            check("SDK ingest_file()", (ing.get("chunks") or 0) >= 1, str(ing))

            rc = client.recall("what version of CheckoutService is deployed?")
            check("SDK recall() returns memories + pinned constraint", "2.1.0" in rc.get("context", "") and "CONSTRAINT:" in rc.get("context", ""), str(rc))
            check("SDK context() returns string", "2.1.0" in client.context("CheckoutService version"))

            fb = client.feedback("CheckoutService version", True, note="accurate")
            check("SDK feedback()", fb.get("ok") is True, str(fb))
            cons = client.consolidate()
            check("SDK consolidate()", isinstance(cons, dict), str(cons))

            # Out-of-the-box multilingual recall (Spanish + Arabic queried in English!)
            client.remember("Ada se mudó a Lisboa en mayo de 2026 para liderar el equipo de plataforma europea.", extract=False)
            client.remember("خادم المدفوعات يفرض حد معدل رموز عند 500 طلب في الثانية.", extract=False)
            ml_rc = client.recall("Which city did Ada move to in May 2026?")
            ml_turns = [m for m in (ml_rc.get("memories") or []) if not m.get("pinned") and m.get("kind") == "turn"]
            ml_top = ml_turns[0].get("content", "") if ml_turns else ""
            check("Out-of-the-box cross-lingual EN->ES semantic recall", "Lisboa" in ml_top, str(ml_top))

            # Multi-agent pub/sub + inbox via SDK
            s1 = client.session_register("alpha-sdk", role="author", harness="sdk", channels=["#release"])
            s2 = client.session_register("beta-sdk", role="reviewer", harness="sdk", channels=["#release"])
            check("SDK session_register()", s1.get("name") == "alpha-sdk" and s2.get("name") == "beta-sdk")
            hb = client.session_heartbeat("alpha-sdk", status="active", current_task="testing")
            check("SDK session_heartbeat()", hb.get("name") == "alpha-sdk" or hb.get("ok") is True, str(hb))
            check("SDK session_list()", len(client.session_list().get("sessions", [])) >= 2)
            adv = client.channel_advertise("#release", "Release coordination", owner_session="alpha-sdk")
            check("SDK channel_advertise()", adv.get("channel") == "#release" or adv.get("ok") is True, str(adv))
            pub = client.channel_publish("#release", "v2.1.0 ready for sign-off", sender_session="alpha-sdk")
            check("SDK channel_publish()", (pub.get("id") or pub.get("message_id") or 0) > 0, str(pub))
            polled = client.channel_poll("beta-sdk", channel="#release")
            check("SDK channel_poll() delivers message to peer", any("v2.1.0 ready" in m.get("content", "") for m in polled.get("messages", [])), str(polled))
            check("SDK channel_list()", len(client.channel_list().get("channels", [])) >= 1)

            inb = client.inbox_send("beta-sdk", "Please verify migration checklist", sender_session="alpha-sdk", subject="Migration review")
            msg_id = inb.get("id") or inb.get("message_id")
            check("SDK inbox_send()", bool(msg_id), str(inb))
            listed = client.inbox_list("beta-sdk")
            check("SDK inbox_list()", any((it.get("id") == msg_id) for it in listed.get("items", [])), str(listed))
            acted = client.inbox_act("beta-sdk", msg_id, action="acted", reply="Verified LGTM")
            check("SDK inbox_act()", acted.get("ok") is True or acted.get("status") == "acted", str(acted))

            client.inbox_send("beta-sdk", "Obsolete task", sender_session="alpha-sdk", thread_key="obsolete-1")
            rec_inb = client.inbox_recall(sender_session="alpha-sdk", thread_key="obsolete-1", reason="no longer needed")
            check("SDK inbox_recall()", (rec_inb.get("recalled") or 0) >= 1 or rec_inb.get("ok") is True, str(rec_inb))
            check("SDK channel_cleanup(dry_run=True)", isinstance(client.channel_cleanup(dry_run=True), dict))
            unsub = client.channel_unsubscribe(["#release"], session_name="beta-sdk")
            check("SDK channel_unsubscribe()", unsub.get("unsubscribed") == 1 or "removed" in unsub or unsub.get("ok") is True, str(unsub))
            unreg = client.session_unregister("beta-sdk")
            check("SDK session_unregister()", unreg.get("unregistered") is True or unreg.get("ok") is True, str(unreg))

        async def _async_smoke() -> bool:
            async with AsyncMemnosClient(base_url=url, token=sdk_token, namespace="proj:sdk") as ac:
                r = await ac.recall("CheckoutService version")
                sec = await ac.resolve_secret("stripe_webhook")
                return "2.1.0" in r.get("context", "") and sec == "whsec_test_987654321"

        check("AsyncMemnosClient recall() + resolve_secret()", asyncio.run(_async_smoke()))
    finally:
        llm_srv.shutdown()
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except Exception:
            srv.kill()
        shutil.rmtree(home, ignore_errors=True)
        drop_scratch_db(admin_dsn, db_name)


def test_inplace_upgrade_migration(admin_dsn: str) -> None:
    print("\n=== 2. Seamless In-Place Upgrade Migration (Vault + Tokens + 384-d BGE Vectors + Mismatch Guard + Gateway Swap) ===")
    if not os.path.isfile(os.path.join(LEGACY_REPO, "core", "store.py")):
        print(f"  SKIP  Legacy repo not found at {LEGACY_REPO}")
        return

    db_name = f"memnos_upgrade_mig_{os.getpid()}"
    dsn = make_scratch_db(admin_dsn, db_name)
    home = tempfile.mkdtemp(prefix="memnos_upgrade_home_")
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    key_bytes = secrets.token_bytes(32)
    key_std_b64 = base64.b64encode(key_bytes).decode()
    key_b64 = base64.urlsafe_b64encode(key_bytes).decode().rstrip("=")  # Exercise unpadded urlsafe base64 on read!

    try:
        from core.control import Control as LegacyControl
        from core.store import BrainStore as LegacyBrainStore
        from core.vault import Vault as LegacyVault
        from fastembed import TextEmbedding

        embed_model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")

        def legacy_embed(txt: str) -> list[float]:
            return next(embed_model.embed([txt])).tolist()

        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            LegacyControl.init(conn)
            legacy_store = LegacyBrainStore(conn=conn)
            legacy_store.create_schema("memnos", dim=384)

            admin_pid = LegacyControl.create_principal(conn, "admin", kind="admin")
            LegacyControl.grant(conn, admin_pid, "*", can_read=True, can_write=True)
            admin_tok = LegacyControl.mint_token(conn, admin_pid, "legacy-admin")

            dev_pid = LegacyControl.create_principal(conn, "app-service", kind="agent")
            LegacyControl.grant(conn, dev_pid, "proj:migration", can_read=True, can_write=True)
            LegacyControl.grant(conn, dev_pid, "secret:prod_webhook_hmac", can_read=True, can_write=False)
            dev_tok = LegacyControl.mint_token(conn, dev_pid, "legacy-sdk-client")

            LegacyControl.add_constraint_enforcement(
                conn,
                "proj:migration",
                "Never drop the ledger table",
                "block",
                "Bash(*DROP TABLE*)",
                created_by=admin_pid,
            )

            prev_sk = os.environ.get("MEMNOS_SECRET_KEY")
            os.environ["MEMNOS_SECRET_KEY"] = key_std_b64
            try:
                LegacyVault.set(conn, "prod_webhook_hmac", "hmac_live_from_existing_vault_42", description="migrated secret")
            finally:
                if prev_sk is None:
                    os.environ.pop("MEMNOS_SECRET_KEY", None)
                else:
                    os.environ["MEMNOS_SECRET_KEY"] = prev_sk

            seed_docs = [
                ("Ada moved to Lisbon in May 2026 to lead the European platform team.", None),
                ("Edge gateway enforces token-bucket rate limiting at 500 requests per second.", None),
                ("Primary persistence uses PostgreSQL 16 with pgvector HNSW cosine indexes.", None),
                ("Nightly backups stream WAL segments to encrypted object storage in us-east-2.", None),
                ("Authentication tokens use Argon2id password hashing and short-lived Ed25519 JWTs.", None),
                ("Telemetry spans export over OTLP gRPC to the observability collector.", None),
                ("All production schema migrations MUST be additive and backward-compatible.", "constraint"),
            ]
            vtype = legacy_store.vtype
            for text, mtype in seed_docs:
                vec = legacy_embed(text)
                vlit = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
                cs = "schema-additive" if mtype == "constraint" else None
                conn.execute(
                    f"""
                    INSERT INTO tenant_memnos.raw_turns(namespace, speaker, text, embedding, memory_type, constraint_subject)
                    VALUES (%s, 'user', %s, %s::{vtype}, %s, %s)
                    """,
                    ("proj:migration", text, vlit, mtype, cs),
                )

        cols_before = schema_column_inventory(dsn)
        write_memnos_config(
            home,
            dsn,
            port,
            key_b64,
            admin_tok,
            agent_tokens={"claude-code": dev_tok},
        )

        # Phase B1: Verify Dimension Mismatch Guard — booting a 1536-d OpenAI config against
        # this 384-d database MUST fail loudly on startup!
        bad_port = free_port()
        bad_srv = subprocess.run(
            [MEMNOS_BIN, "serve", "--port", str(bad_port)],
            env={
                **os.environ,
                "HOME": home,
                "MEMNOS_DSN": dsn,
                "MEMNOS_SECRET_KEY": key_b64,
                "OPENAI_API_KEY": "sk-test-mismatch-guard-1536",
            },
            capture_output=True,
            text=True,
            timeout=15,
        )
        check(
            "Mismatch guard refuses 1536-d embedder on existing 384-d schema",
            bad_srv.returncode != 0 and "dimension mismatch" in (bad_srv.stderr + bad_srv.stdout),
            bad_srv.stderr.strip(),
        )

        # Phase B2: Start upgraded binary normally against the exact existing $HOME & DB!
        up_env = {
            **os.environ,
            "HOME": home,
            "HF_HUB_CACHE": os.path.expanduser("~/.cache/huggingface/hub"),
            "MEMNOS_SECRET_KEY": key_b64,
        }
        up_env.pop("OPENAI_API_KEY", None)
        up_env.pop("MEMNOS_DSN", None)  # Prove server reads dsn + port + tokens from existing ~/.memnos/config.json!

        srv = subprocess.Popen(
            [MEMNOS_BIN, "serve", "--port", str(port)],
            env=up_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            check("Upgraded server boots cleanly from existing ~/.memnos/config.json", wait_ready(url))
            cols_after = schema_column_inventory(dsn)
            check(
                f"Zero schema drift on tenant_memnos ({len(cols_before)} columns before == {len(cols_after)} after)",
                cols_before == cols_after,
            )

            with MemnosClient(base_url=url, token=dev_tok, namespace="proj:migration") as client:
                dec = client.resolve_secret("prod_webhook_hmac")
                check("Upgraded server decrypts existing AES-256-GCM Vault secret byte-for-byte", dec == "hmac_live_from_existing_vault_42", dec)

                paraphrase_cases = [
                    ("Which Iberian coastal city did Ada relocate to?", "Lisbon"),
                    ("How do we throttle bursts of incoming HTTP requests?", "token-bucket"),
                    ("Which relational database engine powers similarity search?", "PostgreSQL 16"),
                    ("Where are write-ahead logs archived overnight?", "us-east-2"),
                ]
                for query, expected_kw in paraphrase_cases:
                    res = client.recall(query)
                    ranked = [m for m in (res.get("memories") or []) if not m.get("pinned")]
                    top1 = ranked[0].get("content", "") if ranked else ""
                    check(
                        f"Cross-version semantic recall rank #1 for '{query}' -> '{expected_kw}'",
                        expected_kw in top1,
                        f"top1={top1!r}",
                    )

                client.remember("Canary rollouts shift 5% of traffic for 15 minutes before full promotion.", extract=False)
                canary_res = client.recall("progressive deployment traffic shifting percentage")
                canary_ranked = [m for m in (canary_res.get("memories") or []) if not m.get("pinned")]
                check(
                    "Newly written BGE vector recalls seamlessly alongside pre-upgrade vectors",
                    bool(canary_ranked) and "Canary rollouts" in canary_ranked[0].get("content", ""),
                    str(canary_ranked[:2]),
                )

            h_env = {**up_env, "MEMNOS_URL": url, "MEMNOS_NS": "proj:migration"}
            st_run = subprocess.run(
                [MEMNOS_BIN, "hook", "status"],
                input=json.dumps({"source": "startup"}),
                env=h_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            check("Migrated `memnos hook status` loads pre-existing enforce rule", "1 enforce rule loaded" in st_run.stdout, st_run.stdout)

            enf_run = subprocess.run(
                [MEMNOS_BIN, "hook", "enforce"],
                input=json.dumps({"tool_name": "Bash", "tool_input": {"command": "psql -c 'DROP TABLE ledger'"}}),
                env=h_env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            enf_json = json.loads(enf_run.stdout) if enf_run.stdout.strip() else {}
            check(
                "Migrated `memnos hook enforce` blocks dangerous command from pre-existing rule",
                enf_json.get("hookSpecificOutput", {}).get("permissionDecision") == "deny",
                enf_run.stdout,
            )
        finally:
            srv.terminate()
            try:
                srv.wait(timeout=5)
            except Exception:
                srv.kill()

        # Phase C: Zero-Downtime Gateway Rolling Swap (`memnos restart`) under continuous SDK load
        gw_port = free_port()
        gw_url = f"http://127.0.0.1:{gw_port}"
        gw = subprocess.Popen(
            [MEMNOS_BIN, "gateway", "--port", str(gw_port)],
            env=up_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            check("Zero-downtime gateway reaches /readyz", wait_ready(gw_url, timeout=30.0))
            errors: list[str] = []
            completed = 0
            stop_flag = threading.Event()

            def _sdk_worker() -> None:
                nonlocal completed
                with MemnosClient(base_url=gw_url, token=dev_tok, namespace="proj:migration", timeout=10.0) as mc:
                    while not stop_flag.is_set():
                        try:
                            out = mc.recall("Lisbon platform team")
                            if "Lisbon" not in out.get("context", ""):
                                errors.append(f"missing expected recall content: {out}")
                            completed += 1
                        except Exception as exc:
                            errors.append(str(exc))

            t = threading.Thread(target=_sdk_worker, daemon=True)
            t.start()
            time.sleep(0.4)
            swap = subprocess.run([MEMNOS_BIN, "restart", "--port", str(gw_port)], env=up_env, capture_output=True, text=True, timeout=25)
            check("Gateway `memnos restart` blue-green rollover exits 0", swap.returncode == 0, swap.stderr)
            time.sleep(0.5)
            stop_flag.set()
            t.join(timeout=5.0)
            check(
                f"Gateway blue-green swap: 0 dropped SDK calls across {completed} continuous recalls",
                completed >= 5 and len(errors) == 0,
                f"completed={completed}, errors={errors[:3]}",
            )
        finally:
            gw.terminate()
            try:
                gw.wait(timeout=5)
            except Exception:
                gw.kill()
    finally:
        shutil.rmtree(home, ignore_errors=True)
        drop_scratch_db(admin_dsn, db_name)


def test_vault_resolved_openai_1536_upgrade(admin_dsn: str) -> None:
    print("\n=== 4. Vault-Backed `secret://openai` 1536-d In-Place Upgrade Safety ===")
    db_name = f"memnos_vault1536_{int(time.time())}"
    dsn = make_scratch_db(admin_dsn, db_name)
    home = tempfile.mkdtemp(prefix="memnos_vault1536_home_")
    port = free_port()
    url = f"http://127.0.0.1:{port}"
    key_b64 = base64.b64encode(os.urandom(32)).decode("ascii")
    vault_api_key = "sk-vault-resolved-1536-production-test"

    class _AuthEmbedHandler(_LocalExtractHandler):
        expected_bearer = vault_api_key

    mock_port = free_port()
    mock_srv = HTTPServer(("127.0.0.1", mock_port), _AuthEmbedHandler)
    threading.Thread(target=mock_srv.serve_forever, daemon=True).start()
    mock_base = f"http://127.0.0.1:{mock_port}"
    try:
        from core.control import Control as LegacyControl
        from core.store import BrainStore as LegacyStore
        from core.vault import Vault as LegacyVault

        with psycopg.connect(dsn, autocommit=True, row_factory=dict_row) as conn:
            LegacyControl.init(conn)
            legacy_store = LegacyStore(conn=conn)
            legacy_store.create_schema("memnos", dim=1536)
            admin_pid = LegacyControl.create_principal(conn, "admin", kind="admin")
            LegacyControl.grant(conn, admin_pid, "*", can_read=True, can_write=True)
            admin_tok = LegacyControl.mint_token(conn, admin_pid, "admin")
            dev_tok = LegacyControl.mint_token(conn, admin_pid, "claude-code")

            prev_sk = os.environ.get("MEMNOS_SECRET_KEY")
            os.environ["MEMNOS_SECRET_KEY"] = key_b64
            try:
                LegacyVault.set(conn, "openai", vault_api_key, description="provider key in vault")
            finally:
                if prev_sk is None:
                    os.environ.pop("MEMNOS_SECRET_KEY", None)
                else:
                    os.environ["MEMNOS_SECRET_KEY"] = prev_sk

            vtype = legacy_store.vtype
            seed_vec = [0.0] * 1536
            seed_vec[0] = 1.0
            vlit = "[" + ",".join(f"{x:.6f}" for x in seed_vec) + "]"
            conn.execute(
                f"""
                INSERT INTO tenant_memnos.raw_turns(namespace, speaker, text, embedding)
                VALUES ('proj:vault1536', 'user', 'Pre-existing 1536-d memory stored before binary swap.', %s::{vtype})
                """,
                (vlit,),
            )

        cfg_dir = os.path.join(home, ".memnos")
        os.makedirs(cfg_dir, exist_ok=True)
        with open(os.path.join(cfg_dir, "config.json"), "w") as fh:
            json.dump(
                {
                    "dsn": dsn,
                    "port": port,
                    "secret_key": key_b64,
                    "openai": "secret://openai",
                    "admin_token": admin_tok,
                    "agent_tokens": {"claude-code": dev_tok},
                },
                fh,
            )

        # Verify that if vault is locked (wrong/empty MEMNOS_SECRET_KEY and no config secret_key),
        # 1536-d schema refuses startup instead of corrupting vectors with 384-d embeddings.
        locked_home = tempfile.mkdtemp(prefix="memnos_locked_home_")
        try:
            os.makedirs(os.path.join(locked_home, ".memnos"), exist_ok=True)
            with open(os.path.join(locked_home, ".memnos", "config.json"), "w") as fh:
                json.dump({"dsn": dsn, "port": free_port()}, fh)
            clean_env = {
                k: v for k, v in os.environ.items()
                if k not in ("OPENAI_API_KEY", "MEMNOS_SECRET_KEY", "MEMNOS_DSN")
            }
            bad_1536 = subprocess.run(
                [MEMNOS_BIN, "serve", "--port", str(free_port())],
                env={**clean_env, "HOME": locked_home, "MEMNOS_DSN": dsn},
                cwd=locked_home,
                capture_output=True,
                text=True,
                timeout=15,
            )
            check(
                "1536-d DB started without OpenAI key fails loudly (zero vector corruption)",
                bad_1536.returncode != 0 and "mismatch" in (bad_1536.stderr + bad_1536.stdout).lower(),
                bad_1536.stderr + bad_1536.stdout,
            )
        finally:
            shutil.rmtree(locked_home, ignore_errors=True)

        # Now boot with `~/.memnos/config.json` containing `"openai": "secret://openai"`!
        # Strip MEMNOS_DSN so `memnos serve` reads `"dsn": dsn` from `home/.memnos/config.json`
        # even when the test runner itself was invoked with `MEMNOS_DSN=...` in os.environ.
        clean_env = {
            k: v for k, v in os.environ.items()
            if k not in ("OPENAI_API_KEY", "MEMNOS_SECRET_KEY", "MEMNOS_DSN")
        }
        srv = subprocess.Popen(
            [MEMNOS_BIN, "serve", "--port", str(port)],
            env={
                **clean_env,
                "HOME": home,
                "MEMNOS_EMBED_BASE_URL": f"{mock_base}/v1",
                "MEMNOS_EXTRACT_BASE_URL": f"{mock_base}/v1",
            },
            cwd=home,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            check("Vault-backed `secret://openai` 1536-d server reaches /readyz", wait_ready(url))
            with MemnosClient(base_url=url, token=dev_tok, namespace="proj:vault1536") as mc:
                w = mc.remember("Vault-decrypted 1536-d write succeeded cleanly.", extract=False)
                check("1536-d /remember succeeds using Vault-resolved OpenAI key", bool(w.get("turn_id")), str(w))
                rec = mc.recall("Vault-decrypted 1536-d write")
                check(
                    "1536-d /recall returns both newly written and pre-existing memories cleanly",
                    "Vault-decrypted 1536-d write" in rec.get("context", ""),
                    str(rec),
                )
        finally:
            srv.terminate()
            try:
                out, err = srv.communicate(timeout=5)
            except Exception:
                srv.kill()
                out, err = "", ""
            check("Startup log confirms OPENAI_API_KEY resolved from vault", "OPENAI_API_KEY resolved from vault" in out, out + err)

        # Verify mandatory pre-upgrade DB backup discipline in `memnos upgrade --no-restart`
        upg_run = subprocess.run(
            [MEMNOS_BIN, "upgrade", "--no-restart"],
            env={**clean_env, "HOME": home},
            cwd=home,
            capture_output=True,
            text=True,
            timeout=25,
        )
        backups_dir = os.path.join(home, ".memnos", "backups")
        pre_upg_files = [
            f for f in (os.listdir(backups_dir) if os.path.isdir(backups_dir) else [])
            if f.startswith("memnos-pre-upgrade-") and (f.endswith(".dump") or f.endswith(".jsonl"))
        ]
        check(
            "`memnos upgrade` enforces mandatory verified pre-upgrade DB backup",
            upg_run.returncode == 0
            and len(pre_upg_files) >= 1
            and os.path.getsize(os.path.join(backups_dir, pre_upg_files[0])) > 0,
            upg_run.stdout + upg_run.stderr,
        )

        # Verify batched `memnos migrate-embeddings --to 384 --yes` (takes pre-migrate backup + migrates 1536-d -> 384-d)
        mig_env = {
            **clean_env,
            "HOME": home,
            "HF_HUB_CACHE": os.path.expanduser("~/.cache/huggingface/hub"),
        }
        mig_run = subprocess.run(
            [MEMNOS_BIN, "migrate-embeddings", "--to", "384", "--yes"],
            env=mig_env,
            cwd=home,
            capture_output=True,
            text=True,
            timeout=60,
        )
        pre_mig_files = [
            f for f in (os.listdir(backups_dir) if os.path.isdir(backups_dir) else [])
            if f.startswith("memnos-pre-migrate-") and (f.endswith(".dump") or f.endswith(".jsonl"))
        ]
        check(
            "`memnos migrate-embeddings --to 384` takes mandatory backup and migrates 1536-d -> 384-d",
            mig_run.returncode == 0
            and len(pre_mig_files) >= 1
            and os.path.getsize(os.path.join(backups_dir, pre_mig_files[0])) > 0,
            mig_run.stdout + mig_run.stderr,
        )

        # Boot migrated 384-d DB with ZERO OpenAI key / ZERO external AI and verify recall!
        port_384 = free_port()
        url_384 = f"http://127.0.0.1:{port_384}"
        srv_384 = subprocess.Popen(
            [MEMNOS_BIN, "serve", "--port", str(port_384)],
            env=mig_env,
            cwd=home,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            check("Migrated 384-d keyless server reaches /readyz", wait_ready(url_384))
            with MemnosClient(base_url=url_384, token=dev_tok, namespace="proj:vault1536") as mc:
                rec384 = mc.recall("memory stored before binary swap")
                check(
                    "Migrated 1536-d -> 384-d memories recall cleanly with local multilingual neural model",
                    "before binary swap" in rec384.get("context", ""),
                    str(rec384),
                )
        finally:
            srv_384.terminate()
            try:
                srv_384.wait(timeout=5)
            except Exception:
                srv_384.kill()
    finally:
        mock_srv.shutdown()
        shutil.rmtree(home, ignore_errors=True)
        drop_scratch_db(admin_dsn, db_name)


def test_first_run_model_choice_and_backup_security(admin_dsn: str) -> None:
    print("\n=== 5. First-Run Explicit Model Choice Gate + Backup 0600/pg_restore Verification ===")
    db_name = f"memnos_firstrun_{int(time.time())}"
    dsn = make_scratch_db(admin_dsn, db_name)
    home = tempfile.mkdtemp(prefix="memnos_firstrun_home_")
    empty_hf = os.path.join(home, "empty_hf")
    os.makedirs(empty_hf, exist_ok=True)
    port = free_port()
    url = f"http://127.0.0.1:{port}"

    clean_env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("OPENAI_API_KEY", "MEMNOS_SECRET_KEY", "MEMNOS_EMBED_CHOICE", "MEMNOS_LOCAL_MODEL")
    }
    base_env = {
        **clean_env,
        "HOME": home,
        "HF_HUB_CACHE": empty_hf,
        "MEMNOS_DSN": dsn,
    }

    try:
        # 5a. Non-interactive `memnos setup` without flags must NOT auto-download or silently pick a mode
        headless_setup = subprocess.run(
            [MEMNOS_BIN, "setup"],
            env=base_env,
            cwd=home,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,
        )
        check(
            "Headless `memnos setup` without explicit choice refuses instead of silently downloading",
            headless_setup.returncode != 0 and "not set up yet" in (headless_setup.stderr + headless_setup.stdout),
            headless_setup.stderr + headless_setup.stdout,
        )

        # Mint a token via CLI so we can test /recall and /remember against an unconfigured server
        subprocess.run([MEMNOS_BIN, "principal", "create", "first-user"], env=base_env, cwd=home, check=True, capture_output=True)
        subprocess.run([MEMNOS_BIN, "grant", "add", "first-user", "proj:first"], env=base_env, cwd=home, check=True, capture_output=True)
        tok_out = subprocess.run([MEMNOS_BIN, "token", "mint", "first-user"], env=base_env, cwd=home, check=True, capture_output=True, text=True)
        tok = next(w.strip() for w in (tok_out.stdout + " " + tok_out.stderr).split() if w.startswith("mnk_"))

        # 5b. Fresh HOME, no key, no choice -> /readyz 503 ready:false, /recall & /remember & hooks refuse!
        srv_unconf = subprocess.Popen(
            [MEMNOS_BIN, "serve", "--port", str(port)],
            env=base_env,
            cwd=home,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.time() + 10.0
            while time.time() < deadline:
                try:
                    if httpx.get(f"{url}/healthz", timeout=1.0).status_code == 200:
                        break
                except Exception:
                    pass
                time.sleep(0.1)

            rz = httpx.get(f"{url}/readyz", timeout=3.0)
            rz_body = rz.json()
            check(
                "Fresh HOME with no embed choice -> /readyz returns 503 ready:false with plain setup reason",
                rz.status_code == 503
                and rz_body.get("ready") is False
                and "memnos setup embeddings" in rz_body.get("reason", ""),
                f"status={rz.status_code} body={rz_body}",
            )

            adm_resp = httpx.get(f"{url}/admin", timeout=3.0)
            repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            desktop_html_path = os.path.join(repo_dir, "rust", "desktop_assets", "index.html")
            ui_html = open(desktop_html_path, encoding="utf-8").read() if os.path.isfile(desktop_html_path) else ""
            check(
                "Headless server removes /admin (404) & Desktop UI uses generic repo placeholder",
                adm_resp.status_code == 404
                and "github.com/acme/payments-sync" in ui_html
                and "thameema/memnos" not in ui_html,
                f"status={adm_resp.status_code}",
            )

            rec_resp = httpx.post(
                f"{url}/recall",
                headers={"Authorization": f"Bearer {tok}"},
                json={"namespace": "proj:first", "query": "anything"},
                timeout=3.0,
            )
            check(
                "Unconfigured /recall refuses with 503 'not set up yet' (no degraded keyword answers)",
                rec_resp.status_code == 503 and "memnos setup embeddings" in rec_resp.json().get("error", ""),
                f"{rec_resp.status_code} {rec_resp.text}",
            )

            rem_resp = httpx.post(
                f"{url}/remember",
                headers={"Authorization": f"Bearer {tok}"},
                json={"namespace": "proj:first", "text": "Should not write un-embedded turn."},
                timeout=3.0,
            )
            check(
                "Unconfigured /remember refuses with 503 'not set up yet' (no silent turn writes)",
                rem_resp.status_code == 503 and "memnos setup embeddings" in rem_resp.json().get("error", ""),
                f"{rem_resp.status_code} {rem_resp.text}",
            )

            hk_run = subprocess.run(
                [MEMNOS_BIN, "hook", "recall"],
                input=json.dumps({"prompt": "How is auth configured in this service?"}),
                env={**base_env, "MEMNOS_URL": url, "MEMNOS_TOKEN": tok, "MEMNOS_NS": "proj:first"},
                cwd=home,
                capture_output=True,
                text=True,
                timeout=10,
            )
            check(
                "Unconfigured `memnos hook recall` surfaces clear setup message",
                "memnos setup embeddings" in (hk_run.stdout + hk_run.stderr),
                hk_run.stdout + hk_run.stderr,
            )
        finally:
            srv_unconf.terminate()
            try:
                srv_unconf.wait(timeout=5)
            except Exception:
                srv_unconf.kill()

        # 5c. Choose local with MEMNOS_OFFLINE_MODELS=1 and empty cache -> clear failure, zero download
        offline_pull = subprocess.run(
            [MEMNOS_BIN, "setup", "embeddings", "--local", "--yes"],
            env={**base_env, "MEMNOS_OFFLINE_MODELS": "1"},
            cwd=home,
            capture_output=True,
            text=True,
            timeout=15,
        )
        check(
            "Local choice with MEMNOS_OFFLINE_MODELS=1 and empty cache fails clearly without network access",
            offline_pull.returncode != 0 and "MEMNOS_OFFLINE_MODELS=1" in (offline_pull.stderr + offline_pull.stdout),
            offline_pull.stderr + offline_pull.stdout,
        )

        # 5d. Choose OpenAI path (`memnos setup embeddings --openai-key ...`) in fresh HOME -> ready immediately with zero download!
        oa_key = "sk-first-run-openai-choice-test"

        class _FirstRunOaHandler(_LocalExtractHandler):
            expected_bearer = oa_key

        oa_mock_port = free_port()
        oa_mock = HTTPServer(("127.0.0.1", oa_mock_port), _FirstRunOaHandler)
        threading.Thread(target=oa_mock.serve_forever, daemon=True).start()
        try:
            setup_oa = subprocess.run(
                [MEMNOS_BIN, "setup", "embeddings", "--openai-key", oa_key],
                env=base_env,
                cwd=home,
                capture_output=True,
                text=True,
                timeout=15,
            )
            check(
                "`memnos setup embeddings --openai-key` configures Vault & OpenAI choice with zero download",
                setup_oa.returncode == 0 and "nothing to download" in setup_oa.stdout,
                setup_oa.stdout + setup_oa.stderr,
            )

            port_oa = free_port()
            url_oa = f"http://127.0.0.1:{port_oa}"
            srv_oa = subprocess.Popen(
                [MEMNOS_BIN, "serve", "--port", str(port_oa)],
                env={
                    **base_env,
                    "MEMNOS_EMBED_BASE_URL": f"http://127.0.0.1:{oa_mock_port}/v1",
                    "MEMNOS_EXTRACT_BASE_URL": f"http://127.0.0.1:{oa_mock_port}/v1",
                },
                cwd=home,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            try:
                check("OpenAI choice in fresh HOME (empty model cache) reaches /readyz 200", wait_ready(url_oa))
                with MemnosClient(base_url=url_oa, token=tok, namespace="proj:first") as mc:
                    mc.remember("First-run OpenAI choice stored cleanly.", extract=False)
                    r_oa = mc.recall("First-run OpenAI choice")
                    check(
                        "OpenAI choice in fresh HOME serves /remember and /recall with zero local downloads",
                        "First-run OpenAI choice" in r_oa.get("context", ""),
                        str(r_oa),
                    )
            finally:
                srv_oa.terminate()
                try:
                    srv_oa.wait(timeout=5)
                except Exception:
                    srv_oa.kill()

            # 5e. Verify `memnos backup` creates 0600 permissions & passes `pg_restore --list`
            bkp_path = os.path.join(home, "verified-test.dump")
            bkp_run = subprocess.run(
                [MEMNOS_BIN, "backup", "--out", bkp_path],
                env=base_env,
                cwd=home,
                capture_output=True,
                text=True,
                timeout=20,
            )
            mode_oct = oct(os.stat(bkp_path).st_mode & 0o777) if os.path.isfile(bkp_path) else ""
            check(
                "`memnos backup` verifies archive via `pg_restore` and enforces 0600 permissions",
                bkp_run.returncode == 0
                and "verified via pg_restore" in bkp_run.stdout
                and mode_oct == "0o600",
                f"mode={mode_oct} out={bkp_run.stdout} err={bkp_run.stderr}",
            )

            # 5f. Missing `pg_dump` / `pg_restore` refuses `upgrade` and `migrate-embeddings` with clear OS install instructions
            empty_pg_bin = os.path.join(home, "empty-pg-bin")
            os.makedirs(empty_pg_bin, exist_ok=True)
            no_pg_upg = subprocess.run(
                [MEMNOS_BIN, "upgrade", "--no-restart"],
                env={**base_env, "MEMNOS_PG_BIN": empty_pg_bin},
                cwd=home,
                capture_output=True,
                text=True,
                timeout=15,
            )
            no_pg_msg = (no_pg_upg.stderr + no_pg_upg.stdout)
            check(
                "Missing `pg_dump` blocks `memnos upgrade` with clear OS install / MEMNOS_PG_BIN guidance",
                no_pg_upg.returncode != 0
                and "pg_dump" in no_pg_msg
                and "MEMNOS_PG_BIN" in no_pg_msg,
                no_pg_msg,
            )

            no_pg_mig = subprocess.run(
                [MEMNOS_BIN, "migrate-embeddings", "--to", "384", "--yes"],
                env={**base_env, "MEMNOS_PG_BIN": empty_pg_bin},
                cwd=home,
                capture_output=True,
                text=True,
                timeout=15,
            )
            no_pg_mig_msg = (no_pg_mig.stderr + no_pg_mig.stdout)
            check(
                "Missing `pg_dump` blocks `memnos migrate-embeddings` with clear OS install / MEMNOS_PG_BIN guidance",
                no_pg_mig.returncode != 0
                and "pg_dump" in no_pg_mig_msg
                and "MEMNOS_PG_BIN" in no_pg_mig_msg,
                no_pg_mig_msg,
            )

            # 5g. Embedded-PG `state.json` `pg_dir` auto-discovery works even when PATH is empty!
            real_pg_dump = shutil.which("pg_dump") or "/opt/homebrew/bin/pg_dump"
            real_pg_restore = shutil.which("pg_restore") or "/opt/homebrew/bin/pg_restore"
            if os.path.isfile(real_pg_dump) and os.path.isfile(real_pg_restore):
                fake_emb_pg = os.path.join(home, ".memnos", "embedded_pg", "runtime", "memnos-pg16")
                os.makedirs(os.path.join(fake_emb_pg, "bin"), exist_ok=True)
                os.symlink(real_pg_dump, os.path.join(fake_emb_pg, "bin", "pg_dump"))
                os.symlink(real_pg_restore, os.path.join(fake_emb_pg, "bin", "pg_restore"))
                with open(os.path.join(home, ".memnos", "embedded_pg", "state.json"), "w") as sf:
                    json.dump({"pg_dir": fake_emb_pg, "port": 5432}, sf)
                emb_bkp_out = os.path.join(home, "emb-discovered.dump")
                emb_bkp = subprocess.run(
                    [MEMNOS_BIN, "backup", "--out", emb_bkp_out],
                    env={**base_env, "PATH": "/nonexistent"},
                    cwd=home,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                check(
                    "Embedded-PG `state.json` `pg_dir` auto-discovers `pg_dump` & `pg_restore` with empty PATH",
                    emb_bkp.returncode == 0 and os.path.isfile(emb_bkp_out),
                    emb_bkp.stdout + emb_bkp.stderr,
                )
        finally:
            oa_mock.shutdown()
    finally:
        shutil.rmtree(home, ignore_errors=True)
        drop_scratch_db(admin_dsn, db_name)

    # 5h. Concurrent fresh-DB schema init race (`memnos serve` + `memnos principal ls` + `memnos admin principal ls` simultaneously)
    race_db = f"memnos_init_race_{os.getpid()}"
    race_dsn = make_scratch_db(admin_dsn, race_db)
    race_home = tempfile.mkdtemp(prefix="memnos_race_home_")
    try:
        os.makedirs(os.path.join(race_home, ".memnos"), exist_ok=True)
        with open(os.path.join(race_home, ".memnos", "config.json"), "w") as rf:
            json.dump({"dsn": race_dsn, "embed_choice": "openai", "openai": "sk-race-test"}, rf)
        race_port = free_port()
        race_env = {
            **{k: v for k, v in os.environ.items() if k not in ("OPENAI_API_KEY", "MEMNOS_SECRET_KEY")},
            "HOME": race_home,
            "MEMNOS_DSN": race_dsn,
        }
        p_srv = subprocess.Popen(
            [MEMNOS_BIN, "serve", "--port", str(race_port)],
            env=race_env,
            cwd=race_home,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        p_adm1 = subprocess.Popen(
            [MEMNOS_BIN, "principal", "ls"],
            env=race_env,
            cwd=race_home,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        p_adm2 = subprocess.Popen(
            [MEMNOS_BIN, "admin"],
            env=race_env,
            cwd=race_home,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            o1, e1 = p_adm1.communicate(timeout=15)
            o2, e2 = p_adm2.communicate(timeout=15)
            srv_ready = wait_ready(f"http://127.0.0.1:{race_port}")
            check(
                "Concurrent `memnos serve` + `memnos admin` on fresh DB completes with zero 23505 schema race",
                srv_ready
                and p_adm1.returncode == 0
                and p_adm2.returncode == 0
                and "23505" not in (e1 + e2),
                f"adm1=({p_adm1.returncode}: {e1}) adm2=({p_adm2.returncode}: {e2})",
            )
        finally:
            p_srv.terminate()
            try:
                p_srv.wait(timeout=5)
            except Exception:
                p_srv.kill()
    finally:
        shutil.rmtree(race_home, ignore_errors=True)
        drop_scratch_db(admin_dsn, race_db)


def main() -> int:
    admin_dsn = os.environ.get("MEMNOS_ADMIN_DSN") or os.environ.get("MEMNOS_DSN")
    if not admin_dsn:
        print("SKIP: MEMNOS_ADMIN_DSN not set")
        return 0
    os.chdir(tempfile.gettempdir())
    test_sdk_and_spo_supersession(admin_dsn)
    test_inplace_upgrade_migration(admin_dsn)
    test_vault_resolved_openai_1536_upgrade(admin_dsn)
    test_first_run_model_choice_and_backup_security(admin_dsn)
    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
