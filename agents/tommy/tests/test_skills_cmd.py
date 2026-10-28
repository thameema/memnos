"""`tommy skills`: usage stats from transcripts, installed-skill overhead, curated list, pin updates.
No network and no real ~/.claude: everything runs against a temp CLAUDE_CONFIG_DIR and a mocked `git ls-remote`."""
from __future__ import annotations

import json
import subprocess
from unittest.mock import MagicMock

from click.testing import CliRunner

import tommy.skills_cmd as sk


def _tool_use(name, **inp):
    return json.dumps({"message": {"content": [{"type": "tool_use", "name": name, "input": inp}]}})


def _claude(tmp_path):
    cd = tmp_path / "claude"
    (cd / "projects" / "p1").mkdir(parents=True)
    (cd / "projects" / "p1" / "s1.jsonl").write_text("\n".join([
        _tool_use("Skill", skill="differential-review:differential-review"),
        _tool_use("Skill", skill="differential-review:differential-review"),
        _tool_use("Skill", skill="security-and-hardening"),
        _tool_use("Task", subagent_type="code-reviewer"),
        _tool_use("Bash", command="ls"),
        "not json at all",
    ]))
    plug = cd / "plugins" / "cache" / "memnos" / "differential-review" / "1.0"
    (plug / "skills" / "differential-review").mkdir(parents=True)
    (plug / "skills" / "differential-review" / "SKILL.md").write_text(
        "---\nname: differential-review\ndescription: Reviews diffs for security regressions\n---\nbody")
    (plug / "skills" / "unused-one").mkdir(parents=True)
    (plug / "skills" / "unused-one" / "SKILL.md").write_text("---\nname: unused-one\ndescription: never called\n---\n")
    (cd / "plugins" / "installed_plugins.json").write_text(json.dumps(
        {"plugins": {"differential-review@memnos": [{"installPath": str(plug)}]}}))
    (cd / "skills" / "security-and-hardening").mkdir(parents=True)
    (cd / "skills" / "security-and-hardening" / "SKILL.md").write_text(
        "---\nname: security-and-hardening\ndescription: Hardens code\n---\n")
    return cd


def test_usage_counts_skills_and_subagents(tmp_path):
    cd = _claude(tmp_path)
    skills, agents, n = sk.usage(cd / "projects")
    assert n == 1
    assert skills["differential-review:differential-review"] == 2 and skills["security-and-hardening"] == 1
    assert agents["code-reviewer"] == 1


def test_usage_since_days_skips_old_transcripts(tmp_path):
    import os, time
    cd = _claude(tmp_path)
    f = cd / "projects" / "p1" / "s1.jsonl"
    os.utime(f, (time.time() - 90 * 86400,) * 2)
    assert sk.usage(cd / "projects", since_days=30)[2] == 0
    assert sk.usage(cd / "projects", since_days=None)[2] == 1


def test_stats_marks_unused_and_matches_plugin_prefixed_names(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(_claude(tmp_path)))
    out = CliRunner().invoke(sk.skills_group, ["stats", "--days", "0"]).output
    assert "3 installed, 1 never used" in out
    line = next(l for l in out.splitlines() if "differential-review" in l and "memnos" not in l and "desc" not in l)
    assert line.split()[0] == "2"
    assert "code-reviewer x1" in out


def test_list_shows_upstream_pin_and_installed_marker(tmp_path, monkeypatch):
    cd = _claude(tmp_path)
    loc = tmp_path / "elsewhere"          # a local-path marketplace lives wherever it was added from
    mp = loc / ".claude-plugin"
    mp.mkdir(parents=True)
    (cd / "plugins" / "known_marketplaces.json").write_text(json.dumps({"memnos": {"installLocation": str(loc)}}))
    (mp / "marketplace.json").write_text(json.dumps({"plugins": [
        {"name": "differential-review", "license": "CC-BY-SA-4.0",
         "source": {"source": "git-subdir", "url": "https://github.com/trailofbits/skills.git",
                    "path": "plugins/differential-review", "sha": "a" * 40}},
        {"name": "memnos", "source": "./plugins/memnos"}]}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cd))
    out = CliRunner().invoke(sk.skills_group, ["list"]).output
    assert "* differential-review" in out and "trailofbits/skills" in out and "aaaaaaa" in out and "CC-BY-SA-4.0" in out
    assert "  memnos" in out and "(this repo)" in out


def test_list_without_marketplace_fails_with_install_hint(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    r = CliRunner().invoke(sk.skills_group, ["list"])
    assert r.exit_code != 0 and "marketplace add thameema/memnos" in r.output


def _market(tmp_path):
    p = tmp_path / "marketplace.json"
    p.write_text(json.dumps({"plugins": [
        {"name": "moved", "source": {"source": "git-subdir", "url": "https://github.com/o/r.git", "path": "x", "sha": "1" * 40}},
        {"name": "same", "source": {"source": "git-subdir", "url": "https://github.com/o/s.git", "path": "y", "sha": "2" * 40}},
        {"name": "local", "source": "./plugins/local"}]}))
    return p


def _fake_ls_remote(monkeypatch):
    def run(cmd, **k):
        r = MagicMock(returncode=0)
        r.stdout = ("3" * 40 if cmd[2].endswith("/r.git") else "2" * 40) + "\tHEAD\n"
        return r
    monkeypatch.setattr(subprocess, "run", run)


def test_update_reports_but_does_not_change_pins_by_default(tmp_path, monkeypatch):
    p = _market(tmp_path); before = p.read_text(); _fake_ls_remote(monkeypatch)
    out = CliRunner().invoke(sk.skills_group, ["update", "--marketplace", str(p)]).output
    assert "moved" in out and "compare/111111111111...333333333333" in out and "same" in out and "up to date" in out
    assert p.read_text() == before


def test_update_apply_rewrites_only_the_named_pin(tmp_path, monkeypatch):
    p = _market(tmp_path); _fake_ls_remote(monkeypatch)
    CliRunner().invoke(sk.skills_group, ["update", "--marketplace", str(p), "--apply", "moved"])
    d = {e["name"]: e["source"] for e in json.loads(p.read_text())["plugins"]}
    assert d["moved"]["sha"] == "3" * 40 and d["same"]["sha"] == "2" * 40 and d["local"] == "./plugins/local"


def test_update_unreachable_upstream_leaves_pin(tmp_path, monkeypatch):
    p = _market(tmp_path); before = p.read_text()
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: MagicMock(returncode=128, stdout=""))
    out = CliRunner().invoke(sk.skills_group, ["update", "--marketplace", str(p), "--apply", "moved"]).output
    assert "could not reach" in out and p.read_text() == before


def test_frontmatter_reads_folded_multiline_description(tmp_path):
    f = tmp_path / "SKILL.md"
    f.write_text("---\nname: ponytail\ndescription: >\n  Lazy senior dev mode.\n  Use on any coding task.\nlevel: full\n---\n")
    fm = sk._frontmatter(f)
    assert fm["description"] == "Lazy senior dev mode. Use on any coding task." and fm["level"] == "full"
