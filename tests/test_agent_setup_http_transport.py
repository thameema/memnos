"""Config-generation CONTRACT test for `memnos agent-setup omnigent --transport http`
(issue #37 Layer 1) — the companion to test_agent_setup.py's stdio-shape checks. It's not
enough that the YAML we emit LOOKS right; it must parse through Omnigent's OWN
AgentSpec parser + validator as a genuinely valid `transport: http` MCP server with a
bearer header, with ZERO validation errors.

Omnigent (github.com/omnigent-ai/omnigent) requires Python >=3.12 and its own uv-managed
virtualenv, incompatible with this repo's own interpreter — so the parser/validator run as
a SUBPROCESS inside Omnigent's checkout via `uv run`, not an in-process import. If neither
~/git/omnigent (or $OMNIGENT_REPO) nor `uv` is available, this test SKIPS — printed as a
loud, visually distinct "SKIP:" line (never silently indistinguishable from a real pass)
— UNLESS `MEMNOS_REQUIRE_OMNIGENT=1` is set, in which case the same conditions FAIL the
suite instead. CI (.github/workflows/ci.yml) clones + syncs omnigent and sets that env var,
so the real parser genuinely runs on every CI push/PR; locally, without omnigent cloned,
it stays a loud skip rather than blocking unrelated work.

Note: `uv run` on a checkout that has never been synced will run a (multi-minute) `uv sync`
first, which can exceed this test's own subprocess timeout — that's caught explicitly and
reported as a skip/fail (per MEMNOS_REQUIRE_OMNIGENT above) with a clear reason, not an
uncaught crash. Run `cd ~/git/omnigent && uv sync` once to avoid paying for it here.

Run: python tests/test_agent_setup_http_transport.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
OMNIGENT_REPO = os.environ.get("OMNIGENT_REPO", os.path.expanduser("~/git/omnigent"))
# CI sets this (after cloning + `uv sync`-ing omnigent) so a missing dependency or a
# timeout is a real, enforced FAIL there instead of a locally-convenient skip — see
# .github/workflows/ci.yml. Unset by default so this test never blocks unrelated local work.
REQUIRE_OMNIGENT = os.environ.get("MEMNOS_REQUIRE_OMNIGENT") == "1"
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += cond; FAIL += (not cond)


def _skip_or_fail(reason):
    """The contract genuinely wasn't verified this run — say so loudly. Under
    MEMNOS_REQUIRE_OMNIGENT=1 (CI, once omnigent is provisioned) that's a hard failure;
    otherwise a visibly-labeled SKIP, never silently indistinguishable from a real pass."""
    if REQUIRE_OMNIGENT:
        print(f"FAIL: {reason} (MEMNOS_REQUIRE_OMNIGENT=1 — this environment must have "
              "a synced omnigent checkout + uv)")
        sys.exit(1)
    print(f"SKIP: {reason}")
    sys.exit(0)


def run_cli(home, *args):
    env = dict(os.environ, MEMNOS_DSN=DSN, HOME=home)
    try:
        for line in open(os.path.join(ROOT, ".env")):
            if line.strip().startswith("MEMNOS_SECRET_KEY=") and "MEMNOS_SECRET_KEY" not in os.environ:
                env["MEMNOS_SECRET_KEY"] = line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass
    r = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), *args],
                       capture_output=True, text=True, env=env, timeout=60)
    return r.returncode, (r.stdout + r.stderr)


def main():
    if not os.path.isdir(OMNIGENT_REPO):
        _skip_or_fail(f"omnigent repo not found at {OMNIGENT_REPO} (set $OMNIGENT_REPO to "
                      "override) — config-generation contract not exercised against the "
                      "real parser this run.")
    if not shutil.which("uv"):
        _skip_or_fail("`uv` not on PATH — can't run omnigent's Python>=3.12 environment.")

    home = tempfile.mkdtemp(prefix="memnos_omni_http_")
    agent_dir = os.path.join(home, "agent")
    os.makedirs(agent_dir, exist_ok=True)
    # Minimal but fully valid omnigent agent spec (spec_version + executor.config.harness
    # are both required by the validator independent of anything MCP-related — a bare
    # `tools:` block alone won't parse) so a clean validation result is ATTRIBUTABLE to
    # the memnos MCP entry, not to an incomplete unrelated fixture.
    with open(os.path.join(agent_dir, "config.yaml"), "w") as f:
        f.write("spec_version: 1\nname: memnos-contract-test\nexecutor:\n"
                "  type: omnigent\n  model: claude-sonnet-5\n  config:\n    harness: claude\n")

    print("=== memnos agent-setup omnigent --transport http ===")
    rc, out = run_cli(home, "agent-setup", "omnigent", "--transport", "http",
                      "--agent-dir", agent_dir, "--force")
    check("exits 0", rc == 0, out[:300])

    script = (
        "import sys, json\n"
        "from pathlib import Path\n"
        "from omnigent.spec.parser import parse\n"
        "from omnigent.spec.validator import validate\n"
        f"spec = parse(Path({agent_dir!r}))\n"
        "servers = {m.name: m for m in spec.mcp_servers}\n"
        "result = validate(spec)\n"
        "out = {\n"
        "    'has_memnos': 'memnos' in servers,\n"
        "    'transport': getattr(servers.get('memnos'), 'transport', None),\n"
        "    'url': getattr(servers.get('memnos'), 'url', None),\n"
        "    'command': getattr(servers.get('memnos'), 'command', None),\n"
        "    'headers': dict(getattr(servers.get('memnos'), 'headers', {}) or {}),\n"
        "    'errors': [str(e) for e in result.errors],\n"
        "}\n"
        "print(json.dumps(out))\n"
    )
    try:
        r = subprocess.run(["uv", "run", "python3", "-c", script], cwd=OMNIGENT_REPO,
                           capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        # An unsynced checkout pays for `uv sync` here (multi-minute, first use only) —
        # that's an environment-provisioning gap, not a contract failure: none of the
        # assertions below ran, so this must not read as either PASS or a raw crash.
        shutil.rmtree(home, ignore_errors=True)
        _skip_or_fail(f"`uv run` in {OMNIGENT_REPO} exceeded 120s — likely an unsynced "
                      f"checkout paying for `uv sync` on first use; run `cd {OMNIGENT_REPO} "
                      "&& uv sync` once, then re-run this test.")
    check("omnigent parser+validator subprocess exits 0", r.returncode == 0,
          (r.stdout + r.stderr)[-500:])
    try:
        result = json.loads(r.stdout.strip().splitlines()[-1])
    except Exception as e:
        check("omnigent output is parseable JSON", False, f"{e}: {r.stdout[-300:]}")
        result = {}

    check("MCPServerConfig 'memnos' present in the parsed AgentSpec", result.get("has_memnos"))
    check("transport == 'http'", result.get("transport") == "http", str(result.get("transport")))
    check("url points at this server's /mcp endpoint",
          isinstance(result.get("url"), str) and result["url"].endswith("/mcp"), str(result.get("url")))
    check("command is None (http transport must not carry a stdio command)",
          result.get("command") is None, str(result.get("command")))
    auth = (result.get("headers") or {}).get("Authorization", "")
    check("headers.Authorization is a real Bearer token (not a literal placeholder)",
          auth.startswith("Bearer mnk_"), auth)
    check("omnigent's OWN validator reports ZERO errors for the generated spec",
          result.get("errors") == [], str(result.get("errors")))

    shutil.rmtree(home, ignore_errors=True)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
