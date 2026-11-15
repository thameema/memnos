"""Tests for git-anchored provenance (`code_anchors`) & recall-time code drift (issue #239)
plus `speaker='git'` rank weight (issue #236)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.code_anchors import (
    annotate_rows_with_code_drift,
    check_code_drift,
    compute_git_blob_sha,
    extract_code_anchors,
    format_drift_notice,
    normalize_code_anchors,
)
from core.service import MemnosMemory


def test_compute_git_blob_sha_matches_git_hash_object(tmp_path: Path):
    f = tmp_path / "sample.py"
    f.write_text("def verify_token(tok: str) -> bool:\n    return bool(tok)\n", encoding="utf-8")
    expected = subprocess.run(
        ["git", "hash-object", str(f)],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    assert compute_git_blob_sha(f) == expected


def test_extract_code_anchors_captures_file_blob_and_identifiers(tmp_path: Path):
    src = tmp_path / "core"
    src.mkdir()
    svc = src / "auth.py"
    svc.write_text(
        "def verify_jwt_signature(token: str) -> bool:\n"
        "    return token.startswith('ok')\n\n"
        "def unrelated_helper() -> int:\n"
        "    return 42\n",
        encoding="utf-8",
    )

    text = "Fixed `verify_jwt_signature` in core/auth.py so expired tokens are rejected."
    anchors = extract_code_anchors(text, repo_root=tmp_path)
    assert len(anchors) == 1
    assert anchors[0]["file"] == "core/auth.py"
    assert anchors[0]["blob_sha"] == compute_git_blob_sha(svc)
    assert "verify_jwt_signature" in anchors[0]["identifiers"]


def test_check_code_drift_detects_symbol_modification_and_ignores_unrelated_hunks(tmp_path: Path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "dev@example.com"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "Test Dev"], check=True)

    svc = tmp_path / "auth.py"
    svc.write_text(
        "def verify_jwt_signature(token: str) -> bool:\n"
        "    return token.startswith('ok')\n\n"
        "\n\n\n\n\n"
        "def unrelated_helper() -> int:\n"
        "    return 42\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "auth.py"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "initial"], check=True, capture_output=True)

    anchors = extract_code_anchors(
        "auth.py uses `verify_jwt_signature` to validate tokens.",
        repo_root=tmp_path,
    )
    assert check_code_drift(anchors, repo_root=tmp_path) == []

    # Edit only `unrelated_helper` far away from `verify_jwt_signature`
    svc.write_text(
        "def verify_jwt_signature(token: str) -> bool:\n"
        "    return token.startswith('ok')\n\n"
        "\n\n\n\n\n"
        "def unrelated_helper() -> int:\n"
        "    return 99\n",
        encoding="utf-8",
    )
    assert check_code_drift(anchors, repo_root=tmp_path) == []

    # Now edit `verify_jwt_signature` itself -> should report drift!
    svc.write_text(
        "def verify_jwt_signature(token: str) -> bool:\n"
        "    return token.startswith('v2:')\n\n"
        "\n\n\n\n\n"
        "def unrelated_helper() -> int:\n"
        "    return 99\n",
        encoding="utf-8",
    )
    drifted = check_code_drift(anchors, repo_root=tmp_path)
    assert len(drifted) == 1
    assert drifted[0]["file"] == "auth.py"
    assert drifted[0]["reason"] == "modified"
    assert "verify_jwt_signature" in drifted[0]["identifiers"]
    assert "code changed: auth.py modified since recorded" in format_drift_notice(drifted)


def test_check_code_drift_detects_deleted_file_and_render_context_annotates(tmp_path: Path):
    f = tmp_path / "legacy.py"
    f.write_text("def old_ entrypoint():\n    pass\n", encoding="utf-8")
    anchors = extract_code_anchors("legacy.py holds the old entrypoint.", repo_root=tmp_path)
    f.unlink()

    rows = [
        {
            "content": "legacy.py holds the old entrypoint.",
            "kind": "fact",
            "score": 0.9,
            "date": "2026-11-14",
            "code_anchors": anchors,
        }
    ]
    ctx = MemnosMemory.render_context(rows, repo_root=tmp_path)
    assert "code changed: legacy.py deleted since recorded" in ctx


def test_normalize_code_anchors_rejects_malformed_entries_and_preserves_dot_dirs():
    assert normalize_code_anchors(None) is None
    assert normalize_code_anchors([{"file": "", "blob_sha": "abc"}]) is None
    assert normalize_code_anchors([{"file": "../secret.py", "blob_sha": "a1b2c3d4"}]) is None
    assert normalize_code_anchors([{"file": "/etc/passwd", "blob_sha": "a1b2c3d4"}]) is None
    valid = normalize_code_anchors([
        {"file": "./core/service.py", "blob_sha": "a1b2c3d4", "identifiers": ["recall_rank"]},
        {"file": "./.github/workflows/ci.yml", "blob_sha": "12345678", "identifiers": []},
    ])
    assert valid == [
        {"file": "core/service.py", "blob_sha": "a1b2c3d4", "identifiers": ["recall_rank"]},
        {"file": ".github/workflows/ci.yml", "blob_sha": "12345678", "identifiers": []},
    ]


def test_render_context_without_repo_root_does_not_probe_cwd_filesystem(tmp_path: Path):
    rows = [
        {
            "content": "core/service.py implements recall_rank.",
            "kind": "fact",
            "score": 0.9,
            "date": "2026-11-14",
            "code_anchors": [{"file": "nonexistent_in_cwd.py", "blob_sha": "0000000", "identifiers": []}],
        }
    ]
    ctx = MemnosMemory.render_context(rows)  # repo_root=None (server path)
    assert "code changed:" not in ctx


def test_git_speaker_weight_demotes_hydrated_git_commits_below_organic_memories():
    mem = MemnosMemory(None, lambda _: [0.0] * 4)
    raw = [
        {"content": "organic session memory", "speaker": "user", "score": 0.5, "_ns": "ns"},
        {"content": "hydrated git commit memory", "speaker": "git", "score": 0.5, "_ns": "ns"},
    ]
    ranked = mem.recall_wide_rank("session memory", raw, [], use_rerank=False)
    # At equal base rank position, had they started equal, git is weighted by 0.85x
    git_row = next(r for r in ranked if "git commit" in r["content"])
    # Position 1 in use_rerank=False has base score 1/(1+1) = 0.5, weighted by 0.85 -> 0.425
    assert abs(git_row["score"] - 0.5 * 0.85) < 1e-6
