"""
Ingest-path test for Secret Shield (issue #115).

Secret Shield's scope, stated precisely (see tommy/secrets.py and the issue
itself): it keeps a resolved secret out of the prompt Tommy builds and out
of Tommy's own logs. It does NOT — and cannot — stop a harness process from
reflecting its own environment back into its own output/transcript (e.g. it
runs `printenv`, hits a debug command, or dumps an error that includes env).
cli.py's `_post_run_capture` ingests the most recently modified Claude Code
transcript into memnos after every run; if that transcript contains a
secret, memnos's `core/redact.py` is the only thing between it and durable
plaintext storage.

This test does NOT mock ingestion or redaction. It builds a fixture
transcript containing two env-style lines, points the REAL
`_find_latest_claude_transcript()` lookup at that fixture (via
monkeypatch — NEVER at this machine's real ~/.claude/projects/, which is
real user data), runs it through the REAL `_post_run_capture` ->
`client.ingest_file()` -> `/ingest/file` -> `core/redact.py` -> Postgres
path against a live memnos server, then reads back what was ACTUALLY
stored:

  1. `DB_PASSWORD=hunter2xyz` — a prefixed env-var name (the `_` breaks
     core/redact.py's credential-pattern word boundary) with a value that
     has no recognizable secret shape and is under the 28-char entropy
     floor. Per issue #115's verified analysis, this is EXPECTED to leak in
     plaintext today. The assertion below pins that real, current gap — it
     is not a bug this issue introduces or is required to fix (see the
     issue's explicit scope note); if core/redact.py is later improved to
     catch this shape, this assertion should be updated to match, not
     treated as a regression.

  2. `OPENAI_API_KEY=sk-proj-...` — a positive control in the SAME
     transcript, expected to BE redacted (core/redact.py's openai_key
     pattern matches the value shape directly, independent of the var name
     in front of it). This is what makes finding (1) a real finding rather
     than "ingestion is broken and nothing gets stored" — the pipeline
     provably redacts the patterns it recognizes and provably misses this
     one.

Requires a live memnos server + Postgres — see conftest.py. Skips (or fails
under TOMMY_REQUIRE_SECRET_SHIELD=1) if none is reachable.
"""
from __future__ import annotations

import uuid

import tommy.cli as cli_mod
from tommy.config import TommyConfig

SCHEMA = "tenant_memnos"

# The empirically-verified blind spot from issue #115: a prefixed env-var
# name (breaks core/redact.py's `credential` pattern's word boundary) whose
# value has no recognizable secret shape and is under the 28-char entropy
# floor.
LEAKY_LINE = "DB_PASSWORD=hunter2xyz"
LEAKY_VALUE = "hunter2xyz"

# Positive control: recognizable by shape alone (core/redact.py's
# `openai_key` pattern), independent of the "OPENAI_API_KEY=" prefix.
REDACTED_LINE = "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
REDACTED_VALUE = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"


def _fixture_transcript(run: str) -> str:
    """A stand-in Claude Code transcript, as if the launched harness ran
    something that echoed its own environment into its output (e.g.
    `printenv`, a debug dump). Real transcripts are JSONL; ingestion treats
    the whole file as opaque chunked text regardless (memnos_server.py's
    /ingest/file handler does no JSON parsing), so plain text is sufficient
    here and keeps the fixture legible."""
    return (
        f"[issue-115-ingest-path-test run={run}]\n"
        "assistant: I'll check the environment for debugging.\n"
        "tool_use: Bash(printenv)\n"
        "tool_result:\n"
        "PATH=/usr/bin:/bin\n"
        f"{LEAKY_LINE}\n"
        f"{REDACTED_LINE}\n"
        "HOME=/home/harness\n"
    )


def _stored_text(conn, namespace: str, session_id: str) -> str:
    with conn.cursor() as c:
        c.execute(
            f"SELECT text FROM {SCHEMA}.raw_turns WHERE namespace=%s AND session_id=%s ORDER BY id",
            (namespace, session_id),
        )
        rows = c.fetchall()
    return "\n".join(r["text"] for r in rows), len(rows)


def test_env_reflected_transcript_ingest_leaks_the_documented_gap(live_memnos, tmp_path, monkeypatch):
    from memnos_sdk import MemnosClient
    from core.control import Control

    run = uuid.uuid4().hex[:10]
    ns = f"test:secret-shield-ingest:{run}"
    conn = live_memnos["conn"]

    agent_id = live_memnos["make_principal"]("agent")
    Control.grant(conn, agent_id, ns, can_read=True, can_write=True)
    agent_tok = Control.mint_token(conn, agent_id, "test-secret-shield-ingest")

    transcript_path = tmp_path / "fake-transcript.jsonl"
    transcript_path.write_text(_fixture_transcript(run))
    # NEVER point this at the real ~/.claude/projects/ glob — that's real
    # user data on this machine. Monkeypatching the lookup function itself
    # (rather than manipulating that directory) is the only safe way to
    # exercise this path.
    monkeypatch.setattr(cli_mod, "_find_latest_claude_transcript", lambda: transcript_path)

    client = MemnosClient(base_url=live_memnos["url"], token=agent_tok, namespace=ns)
    cfg = TommyConfig(tommy_ns=ns)
    run_id = f"ingesttest{run}"

    try:
        cli_mod._post_run_capture(client, cfg, run_id, project_key=None)

        session_id = f"tommy-run-{run_id}.jsonl"
        stored_text, n_rows = _stored_text(conn, ns, session_id)

        # Guard against a vacuous pass: if ingestion silently failed (403,
        # network error, etc. — _post_run_capture swallows ALL exceptions),
        # zero rows would exist and "the secret isn't in storage" would
        # look identical to "it got redacted". Prove ingestion actually
        # happened before drawing any conclusion from its content.
        assert n_rows > 0, (
            "_post_run_capture stored nothing at all for this run — ingestion "
            "itself failed silently (check grants/reachability), which is NOT "
            "the same thing as 'the secret was redacted'. This assertion "
            "existing and passing is a precondition for the two below meaning "
            "anything."
        )

        # Positive control: the pipeline really does redact what it
        # recognizes. Failing this would mean ingestion/redaction is broken
        # in a way that makes the leak assertion below meaningless.
        assert REDACTED_VALUE not in stored_text, (
            "OPENAI_API_KEY's value should have been redacted by core/redact.py's "
            "openai_key pattern (shape-based, independent of the var name) — "
            "found it in plaintext, which means redaction isn't running at all "
            "on this path and the DB_PASSWORD finding below can't be trusted."
        )
        assert "[REDACTED:openai_key]" in stored_text

        # The documented, verified gap (issue #115): a prefixed var name
        # ("DB_" breaks the credential pattern's \b word boundary) with a
        # short, unrecognizable-shape value is caught by neither
        # core/redact.py mechanism. EXPECTED TO LEAK — see module docstring.
        assert LEAKY_VALUE in stored_text, (
            "expected DB_PASSWORD's value to leak in plaintext per issue #115's "
            "verified redact.py gap analysis — if this now fails, core/redact.py "
            "was likely improved to catch prefixed-var-name credentials; update "
            "this test's expectation (and celebrate), don't just skip it"
        )
    finally:
        live_memnos["cleanup_namespace"](ns)
