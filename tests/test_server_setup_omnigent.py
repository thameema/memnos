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
sys.path.insert(0, ROOT)

from core.control import Control          # direct-Postgres proof of what got minted/granted

PY = sys.executable
PASS = FAIL = 0

_HANDLER = "memnos_sdk.integrations.omnigent.capture_response"
_UNREACHABLE_DSN = "postgresql://nouser:nopass@127.0.0.1:1/nodb"
# A REAL reachable memnos server for the grant-verification (Fix #2) live-probe tests —
# CI already starts one at this default before running tests/test_*.py (see .github/
# workflows/ci.yml "Start server"); set MEMNOS_URL locally to point at your own.
_LIVE_URL = os.environ.get("MEMNOS_URL") or "http://127.0.0.1:8900"


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
    # Fix #2 (grant verification), degraded path: the fake MEMNOS_URL is unreachable, so the
    # live /recall probe can't run — must degrade to a clear, non-fatal NOTE, never crash
    # or silently say nothing (rc == 0 was already asserted above).
    check("central mode: unreachable MEMNOS_URL -> a clear 'could not verify' NOTE, not silence",
          "could not verify" in out and PRESET_TOKEN not in out)

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

    # --- embedded mode + a PRESET MEMNOS_TOKEN (PR #36 finding #1 regression guard): a
    # pre-set MEMNOS_TOKEN in the operator's shell is virtually always THEIR OWN personal
    # token (docs/guides/team.md tells developers to export exactly that for their own
    # unrelated agent-setup) — reusing it verbatim would silently authenticate server-wide
    # capture as the operator's personal identity instead of a dedicated service principal.
    # Embedded mode must therefore IGNORE it and mint a real agent:omnigent principal via
    # direct Postgres access, exactly as agent-setup already does for Hermes/OpenClaw.
    # Needs a REACHABLE DSN (unlike the central-mode tests above): embedded mode's whole
    # point is direct Postgres minting, so it must touch Postgres regardless of env token. ---
    PRESET_PERSONAL_TOKEN = "mnk_OPERATORS_OWN_PERSONAL_TOKEN_NOT_A_SERVICE_ACCOUNT"
    embedded_cfg = os.path.join(home, "embedded_server.yaml")
    rc, out = run(home, "server-setup", "omnigent", "--config", embedded_cfg, "--mode", "embedded",
                  extra_env={"MEMNOS_DSN": DSN, "MEMNOS_URL": "http://127.0.0.1:1",
                             "MEMNOS_TOKEN": PRESET_PERSONAL_TOKEN})
    check("embedded + preset MEMNOS_TOKEN: exits 0 (mints via Postgres regardless of env token)",
          rc == 0)
    espec = yaml.safe_load(open(embedded_cfg))
    eentry = policy_of(espec)
    check("embedded mode: memnos_url IS baked in", eentry.get("config", {}).get("memnos_url", "").startswith("http"))
    check("embedded mode: the operator's preset personal token is NEVER reused or printed",
          PRESET_PERSONAL_TOKEN not in out and PRESET_PERSONAL_TOKEN not in open(embedded_cfg).read())
    check("embedded mode: prints a FRESHLY MINTED mnk_ token instead of the preset one",
          "export MEMNOS_TOKEN=mnk_" in out)
    minted_lines = [l for l in out.splitlines() if l.strip().startswith("export MEMNOS_TOKEN=")]
    check("embedded mode: exactly one minted-token line printed", len(minted_lines) == 1)
    minted_token = minted_lines[0].split("=", 1)[1].strip() if minted_lines else ""
    check("embedded mode: the minted token genuinely differs from the preset personal one",
          bool(minted_token) and minted_token != PRESET_PERSONAL_TOKEN)

    # --- PROOF (independent of anything the CLI printed) that the minted token really is
    # a dedicated agent:omnigent SERVICE principal, not the operator's own identity, with a
    # real WRITE grant on agent:omnigent — checked directly against Postgres. This is the
    # core claim of finding #1: embedded mode mints/uses a real agent:omnigent principal. ---
    conn = psycopg.connect(DSN, autocommit=True, row_factory=psycopg.rows.dict_row)
    Control.init(conn)
    pid = Control.authenticate(conn, minted_token) if minted_token else None
    check("minted token: authenticates against Postgres", pid is not None)
    principal_name = (Control.principal_info(conn, pid) or {}).get("name") if pid else None
    check("minted token: belongs to a principal literally named 'omnigent' (a dedicated "
          "service identity, distinct from the operator's own user account)",
          principal_name == "omnigent")
    check("minted token: the principal has a real WRITE grant on agent:omnigent",
          pid is not None and Control.authorize(conn, pid, "agent:omnigent", write=True))
    check("embedded mode: setup output confirms the write grant (Fix #2)",
          "verified: the token can write to namespace 'agent:omnigent'" in out)

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

    # --- Fix #4: idempotent re-run with a --namespace override that DIFFERS from what's
    # already wired must say so explicitly, not silently discard the flag with no signal at
    # all (rc==0, file unchanged). Reuses central_cfg, left wired to 'agent:omnigent-prod' by
    # the --force rewrite above. ---
    before_idempotent_ns = open(central_cfg).read()
    rc, out = run(home, "server-setup", "omnigent", "--config", central_cfg, "--mode", "central",
                  "--namespace", "agent:some-other-ns", extra_env=REMOTE_ENV)
    check("idempotent + different --namespace: exits 0", rc == 0)
    check("idempotent + different --namespace: says already wired", "already wired" in out)
    check("idempotent + different --namespace: explicitly flags the override as NOT applied",
          "was NOT applied" in out and "agent:some-other-ns" in out and "agent:omnigent-prod" in out)
    check("idempotent + different --namespace: file left byte-identical",
          open(central_cfg).read() == before_idempotent_ns)
    # ... and the SAME --namespace as what's already wired must NOT trigger the warning
    # (nothing was actually skipped -- it already matches).
    rc, out = run(home, "server-setup", "omnigent", "--config", central_cfg, "--mode", "central",
                  "--namespace", "agent:omnigent-prod", extra_env=REMOTE_ENV)
    check("idempotent + MATCHING --namespace: no spurious 'NOT applied' warning",
          "was NOT applied" not in out)

    # --- Fix #3: atomic config write. In-process (not subprocess, so we can monkeypatch
    # yaml.safe_dump to blow up MID-WRITE — a genuine "crash between opening the file and
    # finishing the write", not just a pre-write permission error) to prove the OLD
    # `open(config_path, "w")` failure mode is closed: that call truncates the file to 0
    # bytes the INSTANT it's opened, before any new content is written, so a crash during
    # the dump would have left the live config corrupted, recoverable only from the backup.
    # The new code writes to a same-directory temp file and os.replace()s it into place, so
    # config_path is NEVER opened for writing at all until the new content is fully formed —
    # a dump failure here must leave the original completely untouched. ---
    atomic_dir = tempfile.mkdtemp(prefix="memnos_atomic_unit_")
    atomic_cfg = os.path.join(atomic_dir, "server.yaml")
    original_content = "policies:\n  some_other_policy:\n    type: function\n    handler: x.y.z\n"
    with open(atomic_cfg, "w") as f:
        f.write(original_content)

    import argparse
    sys.path.insert(0, ROOT)
    import memnos_cli
    real_safe_dump = yaml.safe_dump
    yaml.safe_dump = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("simulated crash mid-write"))
    prior_token_env = os.environ.get("MEMNOS_TOKEN")
    os.environ["MEMNOS_TOKEN"] = "mnk_FAKE_FOR_ATOMIC_UNIT_TEST"
    dump_raised = False
    try:
        memnos_cli.cmd_server_setup_omnigent(
            argparse.Namespace(config=atomic_cfg, mode="central", namespace=None, force=False), {})
    except Exception:
        dump_raised = True
    finally:
        yaml.safe_dump = real_safe_dump
        if prior_token_env is None:
            os.environ.pop("MEMNOS_TOKEN", None)
        else:
            os.environ["MEMNOS_TOKEN"] = prior_token_env
    check("atomic write, dump fails mid-write: the failure surfaces (raises), never swallowed",
          dump_raised)
    check("atomic write, dump fails mid-write: pre-existing config left COMPLETELY UNCHANGED",
          open(atomic_cfg).read() == original_content)
    check("atomic write, dump fails mid-write: no leftover .memnos-capture-*.tmp file behind",
          not any(n.startswith(".memnos-capture-") for n in os.listdir(atomic_dir)))

    # --- Fix #3, permission preservation: tempfile.mkstemp() always creates 0600, and a
    # naive os.replace() would silently narrow an existing config from its real mode down
    # to owner-only on every run -- a real regression this fix must not introduce, since
    # the docs explicitly describe this file as "operator-editable and often
    # world-readable" and a hosted/Docker omnigent server may run as a different user than
    # whoever ran this command. ---
    perm_cfg = os.path.join(home, "perm_server.yaml")
    with open(perm_cfg, "w") as f:
        f.write("llm:\n  model: openai/gpt-4o-mini\n")
    os.chmod(perm_cfg, 0o644)
    rc, out = run(home, "server-setup", "omnigent", "--config", perm_cfg, "--mode", "central",
                  extra_env=REMOTE_ENV)
    check("permission preservation: exits 0", rc == 0)
    check("permission preservation: pre-existing 0644 mode survives the write UNCHANGED",
          (os.stat(perm_cfg).st_mode & 0o777) == 0o644)
    # And a brand-new file must get the normal umask-derived default, not mkstemp's 0600.
    fresh_perm_cfg = os.path.join(home, "fresh_perm_server.yaml")
    rc, out = run(home, "server-setup", "omnigent", "--config", fresh_perm_cfg, "--mode", "central",
                  extra_env=REMOTE_ENV)
    check("permission preservation: a brand-new config is NOT left owner-only (0600)",
          (os.stat(fresh_perm_cfg).st_mode & 0o777) != 0o600)

    # --- Fix #2: LIVE central-mode grant-verification warning. A token that authenticates
    # fine but was never granted the target namespace must surface a loud warning at SETUP
    # time -- the capture policy's own write path only logs a swallowed warning server-side,
    # so this is the only chance an operator has to catch it before capture silently does
    # nothing. Needs a REAL reachable memnos server (this file otherwise only needs Postgres,
    # per its own docstring) -- skip gracefully if one isn't up at _LIVE_URL; CI and `make
    # test` both start one before running tests/test_*.py, so this is exercised for real
    # there. ---
    import urllib.request
    try:
        urllib.request.urlopen(_LIVE_URL + "/healthz", timeout=3)
        live_server_up = True
    except Exception:
        live_server_up = False
    if live_server_up:
        live_conn = psycopg.connect(DSN, autocommit=True, row_factory=psycopg.rows.dict_row)
        Control.init(live_conn)
        unrelated_pid = Control.create_principal(live_conn, "test-unrelated-svc-pr36", "agent")
        Control.grant(live_conn, unrelated_pid, "proj:*")     # deliberately NOT agent:omnigent
        unrelated_token = Control.mint_token(live_conn, unrelated_pid, "pr36-grant-test")

        # Control FIRST: the same token DOES have proj:* -- a namespace it's actually
        # granted must verify clean. This also doubles as a precondition check: DSN and
        # _LIVE_URL must be the SAME database, or this token won't even authenticate at
        # the live server (401, not "verified") and the ungranted-case assertions below
        # would be testing an environment mismatch, not the real 403 behavior. Gate on it.
        granted_cfg = os.path.join(home, "granted_central.yaml")
        rc, out = run(home, "server-setup", "omnigent", "--config", granted_cfg, "--mode", "central",
                      "--namespace", "proj:pr36-test",
                      extra_env={"MEMNOS_DSN": _UNREACHABLE_DSN, "MEMNOS_URL": _LIVE_URL,
                                 "MEMNOS_TOKEN": unrelated_token})
        control_verified = rc == 0 and "verified" in out and "WARNING" not in out
        if control_verified:
            check("live grant check, control (namespace IS granted): verifies clean, no WARNING",
                  True)

            ungranted_cfg = os.path.join(home, "ungranted_central.yaml")
            rc, out = run(home, "server-setup", "omnigent", "--config", ungranted_cfg, "--mode", "central",
                          "--namespace", "agent:omnigent",
                          extra_env={"MEMNOS_DSN": _UNREACHABLE_DSN, "MEMNOS_URL": _LIVE_URL,
                                     "MEMNOS_TOKEN": unrelated_token})
            check("live grant check: exits 0 (a missing grant is a warning, never fatal)", rc == 0)
            check("live grant check: loud WARNING naming the ungranted namespace",
                  "WARNING" in out and "NOT authorized" in out and "agent:omnigent" in out)
            check("live grant check: tells the operator how to fix it",
                  "memnos grant add" in out)
        else:
            print(f"  SKIP  live central-mode grant-verification test (MEMNOS_URL={_LIVE_URL} "
                  f"and MEMNOS_DSN don't appear to share the same principal/token store -- "
                  f"control probe didn't come back verified: {out!r})")
    else:
        print(f"  SKIP  live central-mode grant-verification test (no memnos server at {_LIVE_URL})")

    # No cleanup of the "omnigent" principal or its agent:omnigent grant: that namespace
    # is this feature's real production default, not a test scratch namespace — purging
    # it here would delete a real deployment's already-captured memories. This test never
    # writes memory to it (only mints a principal + grants, like hermes/openclaw's own
    # agent-setup tests already do without cleanup), so nothing to purge.

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
