"""Tests for `/recall/explain` ring buffer & `memnos explain` / `memnos fix` CLI (issue #240, #238)."""
from __future__ import annotations

import argparse
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import memnos_cli
import memnos_server


def test_recall_explain_ring_buffer_is_namespace_isolated_and_capped():
    ns_a = "test:explain:a"
    ns_b = "test:explain:b"
    memnos_server._RECALL_EXPLAIN_LOG.pop(ns_a, None)
    memnos_server._RECALL_EXPLAIN_LOG.pop(ns_b, None)

    for i in range(60):
        memnos_server._record_recall_explain(ns_a, {
            "timestamp": f"2026-11-14T19:00:{i % 60:02d}Z",
            "namespace": ns_a,
            "query": f"query-{i}",
            "scope": "narrow",
            "pinned_count": 1,
            "ranked_count": 2,
            "timings_ms": {"embed_ms": 1.2, "sql_ms": 3.4, "rerank_ms": 5.6, "total_ms": 10.2},
            "memories": [
                {"content": f"fact-{i}", "kind": "fact", "type": "decision", "score": 0.91, "pinned": False}
            ],
        })
    memnos_server._record_recall_explain(ns_b, {
        "timestamp": "2026-11-14T19:01:00Z",
        "namespace": ns_b,
        "query": "other-ns-query",
        "scope": "narrow",
        "pinned_count": 0,
        "ranked_count": 1,
        "timings_ms": {"total_ms": 4.0},
        "memories": [],
    })

    latest_a = memnos_server.get_recall_explain(ns_a, limit=1)
    assert len(latest_a) == 1
    assert latest_a[0]["query"] == "query-59"

    all_a = memnos_server.get_recall_explain(ns_a, limit=100)
    assert len(all_a) == 50
    assert all_a[0]["query"] == "query-59"
    assert all_a[-1]["query"] == "query-10"

    latest_b = memnos_server.get_recall_explain(ns_b, limit=5)
    assert len(latest_b) == 1
    assert latest_b[0]["query"] == "other-ns-query"

    memnos_server._record_recall_explain(ns_b, {
        "timestamp": "2026-11-14T19:02:00Z",
        "namespace": ns_b,
        "query": "alice-private-query",
        "viewer": "alice",
        "scope": "narrow",
        "pinned_count": 0,
        "ranked_count": 0,
        "timings_ms": {"total_ms": 2.0},
        "memories": [],
    })
    bob_view = memnos_server.get_recall_explain(ns_b, limit=5, viewer="bob")
    assert all(it.get("query") != "alice-private-query" for it in bob_view)
    alice_view = memnos_server.get_recall_explain(ns_b, limit=5, viewer="alice")
    assert any(it.get("query") == "alice-private-query" for it in alice_view)


def test_cli_cmd_explain_renders_pinned_scores_timings_and_drift(monkeypatch, tmp_path: Path):
    f = tmp_path / "service.py"
    f.write_text("def handle_auth():\n    return 1\n", encoding="utf-8")
    from core.code_anchors import extract_code_anchors
    anchors = extract_code_anchors("service.py defines `handle_auth`.", repo_root=tmp_path)
    # Modify `handle_auth` so drift is detected
    f.write_text("def handle_auth():\n    return 2\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    def fake_post(cfg, path, body, tok=None):
        assert path == "/recall/explain"
        return {
            "namespace": "proj:demo",
            "recalls": [
                {
                    "timestamp": "2026-11-14T19:30:00Z",
                    "namespace": "proj:demo",
                    "query": "how does auth work?",
                    "scope": "narrow",
                    "pinned_count": 1,
                    "ranked_count": 1,
                    "timings_ms": {"embed_ms": 1.1, "sql_ms": 2.2, "rerank_ms": 4.4, "total_ms": 7.7},
                    "memories": [
                        {
                            "content": "NEVER log bearer tokens",
                            "kind": "turn",
                            "type": "constraint",
                            "score": None,
                            "pinned": True,
                        },
                        {
                            "content": "service.py defines `handle_auth`.",
                            "kind": "fact",
                            "type": "decision",
                            "score": 0.942,
                            "pinned": False,
                            "code_anchors": anchors,
                        },
                    ],
                }
            ],
        }

    monkeypatch.setattr(memnos_cli, "_post", fake_post)
    args = argparse.Namespace(namespace="proj:demo", limit=1, query=None, token="mnk_test", json=False)
    buf = io.StringIO()
    with redirect_stdout(buf):
        memnos_cli.cmd_explain(args, {})
    out = buf.getvalue()

    assert "Recall #1 [proj:demo]" in out
    assert "1 pinned constraints, 1 ranked memories" in out
    assert "total_ms=7.7ms" in out
    assert "[PINNED] (constraint) NEVER log bearer tokens" in out
    assert "0.942  (decision) service.py defines `handle_auth`." in out
    assert "code changed: service.py modified since recorded" in out


def test_cli_cmd_fix_posts_structured_fix_memory(monkeypatch):
    posted = {}

    def fake_post(cfg, path, body, tok=None):
        posted["path"] = path
        posted["body"] = body
        return {"turn_id": 777, "facts": 1, "superseded": 0}

    monkeypatch.setattr(memnos_cli, "_post", fake_post)
    args = argparse.Namespace(
        symptom="PoolTimeout on concurrent /recall",
        cause="Connection held during cross-encoder rerank",
        fix="Split recall_fetch and recall_rank",
        verify="pytest tests/test_perf_phases.py",
        namespace="proj:demo",
        token="mnk_test",
        json=False,
    )
    buf = io.StringIO()
    with redirect_stdout(buf):
        memnos_cli.cmd_fix(args, {})

    assert posted["path"] == "/remember"
    assert posted["body"]["type"] == "fix"
    assert "Symptom: PoolTimeout on concurrent /recall" in posted["body"]["text"]
    assert "Cause: Connection held during cross-encoder rerank" in posted["body"]["text"]
    assert "Fix: Split recall_fetch and recall_rank" in posted["body"]["text"]
    assert "Verify: pytest tests/test_perf_phases.py" in posted["body"]["text"]
    assert "recorded fix in proj:demo (turn 777)" in buf.getvalue()
