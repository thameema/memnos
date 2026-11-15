"""Tests for cold-start repository hydration (`memnos init` — issue #236)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.repo_init import (
    _deterministic_rule_subject,
    init_repo,
    parse_git_history,
    parse_repo_rules,
)


def test_parse_repo_rules_extracts_verbatim_constraints_and_skips_code_fences(tmp_path: Path):
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(
        "# Project Overview\n"
        "A fast developer memory engine.\n\n"
        "## Architecture Rules & Constraints\n"
        "- NEVER hold a database pool connection across an LLM call.\n"
        "- Always run `python3 memnos_cli.py docs-gen` after modifying CLI flags.\n"
        "```bash\n"
        "# - MUST NOT be extracted from inside a code fence\n"
        "pytest -q\n"
        "```\n"
        "- Short item\n",
        encoding="utf-8",
    )
    cursorrules = tmp_path / ".cursorrules"
    cursorrules.write_text(
        "- Prefer explicit SQL migrations in core/schema.sql over ORM magic.\n",
        encoding="utf-8",
    )

    rules = parse_repo_rules(tmp_path)
    texts = [r["text"] for r in rules]

    assert any("NEVER hold a database pool connection across an LLM call." in t for t in texts)
    assert any("Always run `python3 memnos_cli.py docs-gen`" in t for t in texts)
    assert any("Prefer explicit SQL migrations in core/schema.sql" in t for t in texts)
    assert not any("MUST NOT be extracted from inside a code fence" in t for t in texts)

    for r in rules:
        assert r["memory_type"] == "constraint"
        assert r["constraint_subject"].startswith("init:")


def test_deterministic_rule_subject_is_idempotent_and_collision_safe():
    s1 = _deterministic_rule_subject("CLAUDE.md", "NEVER hold a pool connection across LLM I/O.")
    s2 = _deterministic_rule_subject("CLAUDE.md", "  never   hold a pool connection across LLM I/O. ")
    s3 = _deterministic_rule_subject("CLAUDE.md", "NEVER hold a pool connection across HTTP calls.")
    assert s1 == s2
    assert s1 != s3


def test_parse_git_history_extracts_conventional_and_trailer_commits(tmp_path: Path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "dev@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test Dev"], check=True)

    f = tmp_path / "app.py"
    f.write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "app.py"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "feat(auth): switch session store to Redis (#42)"],
        check=True, capture_output=True,
    )

    f.write_text("x = 2\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "app.py"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "wip"],
        check=True, capture_output=True,
    )

    f.write_text("x = 3\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "app.py"], check=True)
    subprocess.run(
        [
            "git", "-C", str(tmp_path), "commit",
            "-m", "fix(pool): release connection before cross-encoder rerank\n\nFixes: #128",
        ],
        check=True, capture_output=True,
    )

    commits = parse_git_history(tmp_path, since_days=30, max_commits=20)
    assert len(commits) == 2
    assert commits[0]["speaker"] == "git"
    assert commits[0]["memory_type"] == "fix"
    assert "fix(pool): release connection before cross-encoder rerank" in commits[0]["text"]
    assert commits[1]["memory_type"] == "fact"
    assert "feat(auth): switch session store to Redis (#42)" in commits[1]["text"]


def test_init_repo_posts_constraints_and_git_commits_with_zero_llm(tmp_path: Path):
    (tmp_path / "CLAUDE.md").write_text(
        "## Guardrails\n"
        "- Always preserve backward compatibility in openapi.json.\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "dev@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test Dev"], check=True)
    (tmp_path / "CLAUDE.md").touch()
    subprocess.run(["git", "-C", str(tmp_path), "add", "CLAUDE.md"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "arch(db): adopt bi-temporal supersession on semantic facts"],
        check=True, capture_output=True,
    )

    calls: list[tuple[str, dict]] = []

    def fake_post(path: str, payload: dict) -> dict:
        calls.append((path, payload))
        return {"turn_id": len(calls), "facts": 0, "superseded": 0}

    summary = init_repo(tmp_path, fake_post, namespace="proj:demo", since_days=30)
    assert summary["rules_found"] == 1
    assert summary["rules_written"] == 1
    assert summary["commits_found"] == 1
    assert summary["commits_written"] == 1

    import memnos_server

    rule_call = calls[0][1]
    assert rule_call["type"] == "constraint"
    assert memnos_server._memory_type(rule_call) == (True, "constraint")
    assert rule_call["extract"] is False
    assert rule_call["constraint_subject"].startswith("init:")

    commit_call = calls[1][1]
    assert commit_call["speaker"] == "git"
    assert commit_call["type"] == "decision"
    assert memnos_server._memory_type(commit_call) == (True, "decision")
    assert commit_call["extract"] is False
