"""Integration-config CONTRACT tests for `memnos server-setup omnigent` — same testing
philosophy as tests/test_agent_setup.py (read its docstring first): these prove the
CLI writes the documented `policies:` YAML shape correctly — merge-not-clobber, idempotent,
backed up, secrets never embedded — against a temp target file and a real Postgres for the
token-minting path. What this CANNOT prove is that Omnigent's own function-policy engine
actually resolves and calls the generated `handler:` path correctly at runtime — that is
covered separately by sdk/tests/test_omnigent_integration.py (handler contract, mocked
transport) and tests/test_omnigent_capture_live.py (a REAL event -> a REAL memnos server,
proving a captured fact becomes recallable). Run: python tests/test_server_setup_omnigent.py
"""
import os
import subprocess
import sys
import tempfile

import psycopg
import yaml

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
PASS = FAIL = 0

_HANDLER = "memnos_sdk.integrations.omnigent.capture_response"
_UNREACHABLE_DSN = "postgresql://nouser:nopass@127.0.0.1:1/nodb"


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


def run(home, *args, extra_env=None):
    """extra_env: dict merged into the subprocess env after the defaults below — a value
    of None DELETES that key, so a test can force e.g. MEMNOS_TOKEN absent regardless of
    what the host shell happens to export."""
    env = dict(os.environ, MEMNOS_DSN=DSN, HOME=home)
    try:
        for line in open(os.path.join(ROOT, ".env")):
            if line.strip().startswith("MEMNOS_SECRET_KEY=") and "MEMNOS_SECRET_KEY" not in os.environ:
                env["MEMNOS_SECRET_KEY"] = line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass
    if extra_env:
        for k, v in extra_env.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    r = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), *args],
                       capture_output=True, text=True, env=env, timeout=60)
    return r.returncode, (r.stdout + r.stderr)


def policy_of(spec):
    return (spec.get("policies") or {}).get("memnos_capture", {})


def main():
    print("=== server-setup omnigent (contract) ===")
    home = tempfile.mkdtemp(prefix="memnos_server_setup_")

    # --- no --config and no $OMNIGENT_CONFIG: fail loud, never guess a path ---
    rc, out = run(home, "server-setup", "omnigent")
    check("no --config/$OMNIGENT_CONFIG: exits non-zero", rc != 0)
    check("no --config/$OMNIGENT_CONFIG: error names the agent-registry confusion to avoid",
          "default_agent" in out and "~/.omnigent/config.yaml" in out)

    # --- central mode with no MEMNOS_TOKEN: fail loud (never mints against a DB it may lack) ---
    central_cfg = os.path.join(home, "central_server.yaml")
    rc, out = run(home, "server-setup", "omnigent", "--config", central_cfg, "--mode", "central",
                  extra_env={"MEMNOS_TOKEN": None})
    check("central mode, no MEMNOS_TOKEN: exits non-zero", rc != 0)
    check("central mode, no MEMNOS_TOKEN: error mentions MEMNOS_TOKEN", "MEMNOS_TOKEN" in out)
    check("central mode, no MEMNOS_TOKEN: no file written", not os.path.exists(central_cfg))

    # --- central mode WITH a preset token, against an UNREACHABLE DSN: must never touch
    # Postgres (same precedent as test_agent_setup.py's MEMNOS_TOKEN-env fix for agent-setup) ---
    PRESET_TOKEN = "mnk_PRESET_CENTRAL_TOKEN_FOR_TEST"
    REMOTE_ENV = {"MEMNOS_DSN": _UNREACHABLE_DSN, "MEMNOS_URL": "https://memnos.example.internal",
                  "MEMNOS_TOKEN": PRESET_TOKEN}
    rc, out = run(home, "server-setup", "omnigent", "--config", central_cfg, "--mode", "central",
                  extra_env=REMOTE_ENV)
    check("central mode + MEMNOS_TOKEN env: exits 0 against an unreachable DSN (no DB touched)",
          rc == 0)
    spec = yaml.safe_load(open(central_cfg))
    entry = policy_of(spec)
    check("central mode: type: function", entry.get("type") == "function")
    check("central mode: handler is the exact dotted path", entry.get("handler") == _HANDLER)
    check("central mode: default namespace agent:omnigent", entry.get("config", {}).get("memnos_namespace") == "agent:omnigent")
    check("central mode: memnos_url NOT baked in (server's own $MEMNOS_URL decides at runtime)",
          "memnos_url" not in entry.get("config", {}))
    check("central mode: the bearer token is NEVER written into the YAML",
          PRESET_TOKEN not in open(central_cfg).read())
    check("central mode: instructs pip install memnos-sdk", "pip install memnos-sdk" in out)

    # --- idempotent re-run: no --force -> unchanged, says already wired ---
    before = open(central_cfg).read()
    rc, out = run(home, "server-setup", "omnigent", "--config", central_cfg, "--mode", "central",
                  extra_env=REMOTE_ENV)
    check("idempotent re-run: exits 0", rc == 0)
    check("idempotent re-run: says already wired", "already wired" in out)
    check("idempotent re-run: file byte-identical", open(central_cfg).read() == before)

    # --- backup written on the NEXT actual write (--force) ---
    bak = central_cfg + ".memnos-bak"
    rc, out = run(home, "server-setup", "omnigent", "--config", central_cfg, "--mode", "central",
                  "--force", "--namespace", "agent:omnigent-prod", extra_env=REMOTE_ENV)
    check("--force re-wire: exits 0", rc == 0)
    check("--force re-wire: backup file created", os.path.exists(bak))
    check("--force re-wire: backup matches the PRE-force content", open(bak).read() == before)
    spec2 = yaml.safe_load(open(central_cfg))
    check("--force re-wire: --namespace override applied",
          policy_of(spec2).get("config", {}).get("memnos_namespace") == "agent:omnigent-prod")
    check("--force re-wire: still exactly one memnos_capture entry",
          sum(1 for k in spec2.get("policies", {}) if k == "memnos_capture") == 1)

    # --- merge: a pre-existing policies: block (and an unrelated top-level key) survive ---
    merge_cfg = os.path.join(home, "merge_server.yaml")
    with open(merge_cfg, "w") as f:
        yaml.safe_dump({
            "llm": {"model": "openai/gpt-4o-mini"},
            "policies": {"block_bash_rm": {"type": "function",
                                           "handler": "myorg.policies.block_bash_rm"}},
        }, f)
    rc, out = run(home, "server-setup", "omnigent", "--config", merge_cfg, "--mode", "central",
                  extra_env=REMOTE_ENV)
    check("merge: exits 0", rc == 0)
    mspec = yaml.safe_load(open(merge_cfg))
    check("merge: pre-existing llm: block preserved", mspec.get("llm", {}).get("model") == "openai/gpt-4o-mini")
    check("merge: pre-existing unrelated policy preserved",
          mspec.get("policies", {}).get("block_bash_rm", {}).get("handler") == "myorg.policies.block_bash_rm")
    check("merge: memnos_capture added alongside it", "memnos_capture" in mspec.get("policies", {}))

    # --- malformed existing policies: block (not a mapping) -> refuse, never clobber ---
    bad_cfg = os.path.join(home, "bad_server.yaml")
    with open(bad_cfg, "w") as f:
        f.write("policies: \"not-a-mapping\"\n")
    before_bad = open(bad_cfg).read()
    rc, out = run(home, "server-setup", "omnigent", "--config", bad_cfg, "--mode", "central",
                  extra_env=REMOTE_ENV)
    check("malformed policies: block: exits non-zero", rc != 0)
    check("malformed policies: block: file left untouched", open(bad_cfg).read() == before_bad)

    # --- $OMNIGENT_CONFIG env as an alternative to --config ---
    env_cfg = os.path.join(home, "env_server.yaml")
    rc, out = run(home, "server-setup", "omnigent", "--mode", "central",
                  extra_env=dict(REMOTE_ENV, OMNIGENT_CONFIG=env_cfg))
    check("$OMNIGENT_CONFIG env: exits 0 without --config", rc == 0)
    check("$OMNIGENT_CONFIG env: wrote the file it pointed at", os.path.exists(env_cfg))

    # --- embedded mode + preset MEMNOS_TOKEN: bakes the concrete local URL in, still no token ---
    embedded_cfg = os.path.join(home, "embedded_server.yaml")
    rc, out = run(home, "server-setup", "omnigent", "--config", embedded_cfg, "--mode", "embedded",
                  extra_env=REMOTE_ENV)
    check("embedded + MEMNOS_TOKEN env: exits 0 against an unreachable DSN (no DB touched)", rc == 0)
    espec = yaml.safe_load(open(embedded_cfg))
    eentry = policy_of(espec)
    check("embedded mode: memnos_url IS baked in", eentry.get("config", {}).get("memnos_url", "").startswith("http"))
    check("embedded mode: the bearer token is NEVER written into the YAML",
          PRESET_TOKEN not in open(embedded_cfg).read())
    check("embedded mode: prints an export MEMNOS_TOKEN instruction instead",
          f"export MEMNOS_TOKEN={PRESET_TOKEN}" in out)

    # --- embedded mode, NO MEMNOS_TOKEN: mints a real token via Postgres (regression guard —
    # local-mint path must still work when there's genuinely no env token, exactly like
    # agent-setup's own windsurf regression guard in test_agent_setup.py) ---
    minted_cfg = os.path.join(home, "minted_server.yaml")
    rc, out = run(home, "server-setup", "omnigent", "--config", minted_cfg, "--mode", "embedded",
                  extra_env={"MEMNOS_TOKEN": None, "MEMNOS_URL": None})
    check("embedded, no MEMNOS_TOKEN: exits 0 (local-mint-via-Postgres path)", rc == 0)
    check("embedded, no MEMNOS_TOKEN: prints a freshly minted mnk_ token", "export MEMNOS_TOKEN=mnk_" in out)
    check("embedded, no MEMNOS_TOKEN: minted token never written into the YAML",
          "mnk_" not in open(minted_cfg).read())
    minted_spec = yaml.safe_load(open(minted_cfg))
    check("embedded, no MEMNOS_TOKEN: default namespace still agent:omnigent",
          policy_of(minted_spec).get("config", {}).get("memnos_namespace") == "agent:omnigent")

    # No cleanup of the "omnigent" principal or its agent:omnigent grant: that namespace
    # is this feature's real production default, not a test scratch namespace — purging
    # it here would delete a real deployment's already-captured memories. This test never
    # writes memory to it (only mints a principal + grants, like hermes/openclaw's own
    # agent-setup tests already do without cleanup), so nothing to purge.

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
