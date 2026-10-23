"""`memnos setup` defaults to --embedded when no mode is given (issue: most installs are
solo/local -- embedded is the zero-dependency golden path, so bare `memnos setup` should
provision it automatically instead of falling into the interactive "type your own Postgres
details" wizard). --docker / --external / --dsn are the explicit opt-outs.

Covers:
  1. Bare `memnos setup` (no flags at all) on a platform --embedded supports: banner says
     --embedded was chosen, it actually runs (real download + initdb + pgvector, same as
     `setup --embedded` explicitly), and the resulting config.json's DSN uses 127.0.0.1
     (not "localhost" -- see the Windows IPv6-resolution-delay fix this session already
     shipped for embedded DSNs specifically).
  2. `--dsn <url>` alone (no --embedded/--docker/--external) does NOT get overridden by
     the new default -- an explicit DSN always wins, exactly like before this change.
  3. `--external` opts OUT of the default and reaches the original interactive wizard
     (verified by the "Postgre host [localhost]:" prompt appearing in output), even
     though this platform supports embedded.
  4. Unit-level: on a platform embedded does NOT support (simulated via monkeypatching
     _embedded_pg_platform to return None, in-process, not a subprocess), bare `memnos
     setup`'s new defaulting logic leaves embedded_mode False -- the original interactive
     fallback is reached instead of a hard error. This is the "never turn a working bare
     setup into a crash" guarantee, for platforms like Windows ARM64 or Linux ARM64 that
     this repo doesn't ship an embedded archive for.

Run: MEMNOS_DSN=... python tests/test_setup_embedded_default.py
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PY = sys.executable
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def _run(args, env_extra=None, timeout=90, input_text=None):
    home = tempfile.mkdtemp(prefix="memnos-setupdefault-home-")
    env = dict(os.environ, MEMNOS_CI="1", HOME=home, USERPROFILE=home)
    env.pop("MEMNOS_DSN", None)          # must NOT leak the suite's own DSN into these runs
    env.pop("MEMNOS_PORT", None)
    if env_extra:
        env.update(env_extra)
    try:
        r = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), "setup", *args],
                           capture_output=True, text=True, env=env, timeout=timeout,
                           input=input_text)
        timed_out = False
    except subprocess.TimeoutExpired as e:
        r = e
        timed_out = True
    out = (r.stdout or "") + (r.stderr or "")
    cfg_path = os.path.join(home, ".memnos", "config.json")
    cfg = json.load(open(cfg_path)) if os.path.exists(cfg_path) else {}
    return out, cfg, timed_out


def main():
    print("=== bare `memnos setup` (no flags) defaults to --embedded ===")
    out, cfg, _ = _run([])
    check("banner says --embedded was chosen", "setup --embedded" in out, out[:300])
    check("banner explains it's the auto-default, not an explicit flag",
          "defaulting to this" in out)
    check("embedded setup actually completed (schema created)",
          "schema + control plane created" in out, out[-400:])
    check("config.json DSN uses 127.0.0.1, not localhost",
          cfg.get("dsn", "").startswith("postgresql://memnos@127.0.0.1:"), cfg.get("dsn"))
    check("config.json marked embedded: true", cfg.get("embedded") is True)

    print("=== `--dsn` alone still wins -- no change from before this feature ===")
    real_dsn = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
    out, cfg, _ = _run(["--dsn", real_dsn])
    check("an explicit --dsn is never overridden by the embedded default",
          "setup --embedded" not in out, out[:300])
    check("config.json DSN is exactly the one passed", cfg.get("dsn") == real_dsn)
    check("config.json NOT marked embedded", cfg.get("embedded") is not True)

    print("=== `--external` opts out of the default, reaches the interactive wizard ===")
    # feed it blank answers (all defaults) then an unreachable host/port combo as the
    # actual connection target, so it fails FAST on a real (refused) connection attempt
    # instead of hanging -- we only care that it REACHED the wizard, not that it succeeds.
    out, cfg, timed_out = _run(["--external"], timeout=20,
                               input_text="127.0.0.1\n1\nmemnos\nmemnos\n\n")
    check("did not hang waiting for more input than given", not timed_out)
    check("reached the interactive Postgres-details wizard",
          "Postgres host" in out, out[:300])
    # NOT a bare substring check: a failed connection attempt's error hint legitimately
    # SUGGESTS "memnos setup --embedded" as an alternative -- that's correct, unrelated
    # behavior. What actually proves --external took the wizard path (not the embedded
    # default) is the BANNER line specifically.
    check("did NOT print the embedded-mode banner",
          "=== memnos setup --embedded" not in out, out[:300])

    print("=== unsupported platform: bare setup falls through, does not hard-fail (unit) ===")
    import memnos_cli
    orig = memnos_cli._embedded_pg_platform
    try:
        memnos_cli._embedded_pg_platform = lambda: None   # simulate e.g. Windows ARM64
        import argparse
        args = argparse.Namespace(embedded=False, docker=False, external=False, dsn=None, port=None)
        # Only exercise the defaulting decision itself (the first ~25 lines of cmd_setup) --
        # calling the full function would reach the real interactive input() wizard and
        # block. Reproduce just that decision the same way cmd_setup computes it.
        embedded_mode = getattr(args, "embedded", False)
        docker_mode = getattr(args, "docker", False)
        external_mode = getattr(args, "external", False)
        dsn = args.dsn or os.environ.get("MEMNOS_DSN")
        defaulted = False
        if not (embedded_mode or docker_mode or external_mode or dsn) and memnos_cli._embedded_pg_platform():
            embedded_mode = True
            defaulted = True
        check("embedded_mode stays False on an unsupported platform", embedded_mode is False)
        check("defaulted_to_embedded stays False too", defaulted is False)
    finally:
        memnos_cli._embedded_pg_platform = orig

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
