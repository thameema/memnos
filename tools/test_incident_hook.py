"""
tools/test_incident_hook.py — Unit tests for post-merge-incident git hook.

Tests cover:
- _is_incident_branch()  — branch pattern detection
- _parse_rca_content()   — Markdown and inline-label RCA extraction
- cmd_post_merge_incident() — end-to-end with a mocked git repo and HTTP call
- install --incident installs the post-merge hook file
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, "/Users/thameema/git/engram/packages/core")

from engram.cli.git_hooks import (
    _is_incident_branch,
    _parse_rca_content,
    _INCIDENT_BRANCH_PATTERNS,
    cmd_install,
    cmd_post_merge_incident,
    _INCIDENT_HOOK_TEMPLATE,
)


# ---------------------------------------------------------------------------
# _is_incident_branch
# ---------------------------------------------------------------------------

class TestIsIncidentBranch(unittest.TestCase):
    def test_incident_prefix(self):
        self.assertTrue(_is_incident_branch("incident/db-outage-2026-05"))

    def test_rca_prefix(self):
        self.assertTrue(_is_incident_branch("rca/payment-timeout"))

    def test_hotfix_prefix(self):
        self.assertTrue(_is_incident_branch("hotfix/auth-bypass"))

    def test_fix_incident(self):
        self.assertTrue(_is_incident_branch("fix/incident-null-pointer"))

    def test_fix_inc_prefix(self):
        self.assertTrue(_is_incident_branch("fix/inc-503-loop"))

    def test_postmortem_prefix(self):
        self.assertTrue(_is_incident_branch("postmortem/q2-outage"))

    def test_normal_feature_branch(self):
        self.assertFalse(_is_incident_branch("feature/member-match-v2"))

    def test_main_branch(self):
        self.assertFalse(_is_incident_branch("main"))

    def test_release_branch(self):
        self.assertFalse(_is_incident_branch("release/v1.2.0"))

    def test_case_insensitive(self):
        self.assertTrue(_is_incident_branch("Incident/DB-Outage"))


# ---------------------------------------------------------------------------
# _parse_rca_content
# ---------------------------------------------------------------------------

class TestParseRcaContent(unittest.TestCase):
    def test_markdown_headings(self):
        body = """## Root Cause
The database connection pool was exhausted due to a missing index.

## Resolution
Added index on user_id column and increased pool size to 50.

## Impact
~200 users saw 503 errors for 12 minutes.

## Severity
P1
"""
        rca = _parse_rca_content(body)
        self.assertIn("connection pool", rca["root_cause"])
        self.assertIn("index on user_id", rca["resolution"])
        self.assertIn("200 users", rca["impact"])
        self.assertEqual(rca["severity"], "P1")
        self.assertIn(body.strip(), rca["full_text"])

    def test_inline_labels(self):
        body = "root_cause: misconfigured timeout\nresolution: reverted deploy\nseverity: P2"
        rca = _parse_rca_content(body)
        self.assertIn("misconfigured", rca["root_cause"])
        self.assertIn("reverted", rca["resolution"])
        self.assertEqual(rca["severity"], "P2")

    def test_empty_body(self):
        rca = _parse_rca_content("")
        self.assertEqual(rca["full_text"], "")
        self.assertNotIn("root_cause", rca)

    def test_no_structured_sections(self):
        body = "We had a bad time. Everything exploded."
        rca = _parse_rca_content(body)
        self.assertEqual(rca["full_text"], body.strip())
        self.assertNotIn("root_cause", rca)

    def test_affected_services_heading(self):
        body = "## Affected Services\npayments, auth, notifications"
        rca = _parse_rca_content(body)
        self.assertIn("payments", rca["affected_services"])

    def test_multiple_paragraphs_in_section(self):
        body = "## Root Cause\nFirst paragraph.\nSecond paragraph.\n\n## Resolution\nFixed it."
        rca = _parse_rca_content(body)
        self.assertIn("First paragraph", rca["root_cause"])
        self.assertIn("Second paragraph", rca["root_cause"])
        self.assertIn("Fixed", rca["resolution"])


# ---------------------------------------------------------------------------
# cmd_post_merge_incident — integration (mocked subprocess + HTTP)
# ---------------------------------------------------------------------------

def _git_outputs(merged_branch: str, sha: str = "abc1234", author: str = "Dev <dev@example.com>",
                  body: str = "") -> dict:
    """Map git subcommands to return values for subprocess.check_output mock."""
    return {
        ("log", "-1", "--format=%s"): f"Merge branch '{merged_branch}' into main",
        ("rev-parse", "--abbrev-ref", "HEAD"): "main",
        ("rev-parse", "--short", "HEAD"): sha,
        ("log", "-1", "--format=%an <%ae>"): author,
        ("log", "-1", "--format=%B"): body,
    }


def _make_check_output(outputs: dict):
    def fake_check_output(cmd, text=True, stderr=None):
        # cmd = ["git", "-C", repo_path, *subcmd]
        subcmd = tuple(cmd[3:])
        for key, val in outputs.items():
            if subcmd == key:
                return val
        return ""
    return fake_check_output


class TestCmdPostMergeIncident(unittest.TestCase):
    def _run(self, merged_branch: str, body: str, expect_write: bool = True) -> MagicMock:
        """Helper: run cmd_post_merge_incident with mocked git + HTTP, return the mock."""
        outputs = _git_outputs(merged_branch, body=body)
        args = argparse.Namespace(
            repo=".",
            server="http://localhost:8766",
            namespace="test:ns",
        )
        posted: list[dict] = []

        async def fake_post(server, headers, payload):
            posted.append(payload)
            return 0

        with patch("subprocess.check_output", side_effect=_make_check_output(outputs)), \
             patch("engram.cli.git_hooks._post_memory", side_effect=fake_post):
            import asyncio
            # cmd_post_merge_incident calls asyncio.run internally
            rc = cmd_post_merge_incident(args)

        self.assertEqual(rc, 0)
        if expect_write:
            self.assertEqual(len(posted), 1, "Expected exactly one memory write")
        else:
            self.assertEqual(len(posted), 0, "Expected no memory write for non-incident branch")
        return posted[0] if posted else {}

    def test_incident_branch_writes_memory(self):
        body = "## Root Cause\nDB pool exhausted.\n## Resolution\nAdded index."
        payload = self._run("incident/db-outage", body)
        self.assertEqual(payload["memory_type"], "incident")
        self.assertIn("incident", payload["tags"])
        self.assertIn("rca", payload["tags"])
        self.assertIn("DB pool", payload["content"])
        self.assertIn("Added index", payload["content"])

    def test_hotfix_branch_writes_memory(self):
        body = "root_cause: null pointer in auth\nresolution: guarded check\nseverity: P0"
        payload = self._run("hotfix/auth-null-ptr", body)
        self.assertEqual(payload["memory_type"], "incident")
        self.assertIn("P0", payload["content"])
        self.assertIn("p0", payload["tags"])

    def test_non_incident_branch_skipped(self):
        self._run("feature/my-feature", body="some content", expect_write=False)

    def test_unstructured_body_stored_as_details(self):
        body = "This was a bad incident. We fixed it."
        payload = self._run("incident/vague-outage", body)
        self.assertIn("Details", payload["content"])
        self.assertIn("bad incident", payload["content"])

    def test_namespace_passed_through(self):
        body = "## Root Cause\nMemory leak."
        payload = self._run("rca/memory-leak", body)
        self.assertEqual(payload["namespace"], "test:ns")

    def test_metadata_contains_merged_branch(self):
        body = "## Root Cause\nDisk full."
        payload = self._run("incident/disk-full", body)
        self.assertEqual(payload["metadata"]["merged_branch"], "incident/disk-full")


# ---------------------------------------------------------------------------
# cmd_install --incident installs post-merge hook
# ---------------------------------------------------------------------------

class TestInstallIncidentHook(unittest.TestCase):
    def test_incident_flag_creates_post_merge_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git_dir = repo / ".git" / "hooks"
            git_dir.mkdir(parents=True)
            # Create a fake .git dir
            (repo / ".git").mkdir(exist_ok=True)

            args = argparse.Namespace(
                repo=str(repo),
                server="http://localhost:8766",
                namespace="org:default",
                incident=True,
            )
            rc = cmd_install(args)
            self.assertEqual(rc, 0)

            post_merge = repo / ".git" / "hooks" / "post-merge"
            self.assertTrue(post_merge.exists(), "post-merge hook should be created")
            content = post_merge.read_text()
            self.assertIn("post-merge-incident", content)
            self.assertIn("http://localhost:8766", content)
            self.assertTrue(os.access(post_merge, os.X_OK), "hook must be executable")

    def test_no_incident_flag_skips_post_merge_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            (repo / ".git" / "hooks").mkdir(parents=True)

            args = argparse.Namespace(
                repo=str(repo),
                server="http://localhost:8766",
                namespace="org:default",
                incident=False,
            )
            cmd_install(args)
            post_merge = repo / ".git" / "hooks" / "post-merge"
            self.assertFalse(post_merge.exists(), "post-merge hook should NOT be created without --incident")


if __name__ == "__main__":
    unittest.main(verbosity=2)
