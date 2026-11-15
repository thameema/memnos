"""P5 (#238): format_fix_memory, bounded MEMNOS_FIX_BOOST, record_fix MCP tool. DB-free."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.service import (MemnosMemory, format_fix_memory, is_error_query,  # noqa: E402
                          _fix_boost)


def test_format_fix_memory():
    t = format_fix_memory(" 500 on login ", "stale token", "rotate key", "pytest -k login")
    assert t == "Symptom: 500 on login\nCause: stale token\nFix: rotate key\nVerify: pytest -k login"
    assert "Verify" not in format_fix_memory("a", "b", "c")


def test_error_query_detection():
    assert is_error_query("why does login crash with a traceback")
    assert not is_error_query("what is our deploy schedule")


def test_boost_bounded_and_gated(monkeypatch):
    assert _fix_boost("fix", True) == 1.10 and _fix_boost("skill", True) == 1.10
    assert _fix_boost("fix", False) == 1.0 and _fix_boost("fact", True) == 1.0
    monkeypatch.setenv("MEMNOS_FIX_BOOST", "5")
    assert _fix_boost("fix", True) == 1.25                       # capped
    monkeypatch.setenv("MEMNOS_FIX_BOOST", "0")
    assert _fix_boost("fix", True) == 1.0


def test_wide_rank_boosts_fix_on_error_query(monkeypatch):
    mem = MemnosMemory(None, lambda t: None, llm=None)
    raw = [{"content": "Symptom: x\nFix: y", "memory_type": "fix", "_ns": "n"}]

    def score(q):
        return mem.recall_wide_rank(q, raw, [], use_rerank=False)[0]["score"]
    assert abs(score("login crash error") / score("login schedule") - 1.10) < 1e-9


def test_record_fix_tool(monkeypatch):
    import memnos_mcp
    calls = {}
    monkeypatch.setattr(memnos_mcp, "remember",
                        lambda text, memory_type="", constraint_subject="": calls.update(t=text, m=memory_type) or "ok")
    assert memnos_mcp.record_fix("s", "c", "f", "make test") == "ok"
    assert calls["m"] == "fix" and calls["t"].startswith("Symptom: s\nCause: c\nFix: f\nVerify: make test")
