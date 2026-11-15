"""P2 (#237): deterministic developer signals + extract_facts local-mode fallback. DB-free."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.encode import extract_developer_signals  # noqa: E402
from core.service import MemnosMemory  # noqa: E402

COMMIT = """fix(auth): stop token reuse

Fixes: #412
Co-authored-by: Dana <dana@example.com>
Signed-off-by: Lee <lee@example.com>
See PR 415 and commit 154a480 shipped in v0.1.56.
== 41 passed, 2 failed, 1 skipped in 3.2s =="""


def _stmts(t):
    return [f["statement"] for f in extract_developer_signals(t)]


def test_all_signal_kinds():
    s = _stmts(COMMIT)
    assert "Git trailer Fixes: #412" in s
    assert any(x.startswith("Git trailer Co-Authored-By: Dana") for x in s)
    assert "References PR #415" in s
    assert "References commit 154a480" in s
    assert "Mentions version 0.1.56" in s
    assert "Test run: 41 passed, 2 failed, 1 skipped" in s


def test_no_false_positives_on_prose():
    for t in ("", "The user lives in Frisco, Texas.", "decided to add feedback face",
              "deadbeef is a word; cafe 1234567", "we passed the bar", "5 people failed to show"):
        assert extract_developer_signals(t) == [], t


def test_facts_never_supersede_and_are_capped_deduped():
    facts = extract_developer_signals("PR #1 " * 3 + " ".join(f"issue #{i}" for i in range(50)))
    assert len(facts) <= 12 and len({f["statement"] for f in facts}) == len(facts)
    assert all(f["subject"] == "" and f["predicate"] == "" for f in facts)


def test_extract_facts_fallback_only_without_llm_or_extract_fn():
    local = MemnosMemory(None, lambda t: None, llm=None)
    assert any("PR #415" in f["statement"] for f in local.extract_facts("merged PR #415", "2026-11-14"))
    assert local.extract_facts("anything", "2026-11-14") == []        # test_perf_phases contract
    assert local.extract_facts("PR #9", "2026-11-14", memory_type="constraint") == []
    seen = []
    fn = MemnosMemory(None, lambda t: None, llm=None,
                      extract_fn=lambda t, d: seen.append(t) or [])
    assert fn.extract_facts("merged PR #415", "2026-11-14") == [] and seen
