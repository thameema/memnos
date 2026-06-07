"""No-AI tests for the unified `memnos` CLI. Exercises the CLI as a subprocess against the
local Postgres + running server. Run: python test_cli.py
"""
import json
import os
import subprocess
import sys

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos_poc@localhost:5433/memnos")
HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, ".venv", "bin", "python")
if not os.path.exists(PY):
    PY = sys.executable
PASS = FAIL = 0


def run(*args):
    env = dict(os.environ, MEMNOS_DSN=DSN)
    # load .env so the CLI shares the server's MEMNOS_SECRET_KEY
    try:
        for line in open(os.path.join(HERE, ".env")):
            if line.strip().startswith("MEMNOS_SECRET_KEY="):
                env["MEMNOS_SECRET_KEY"] = line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass
    r = subprocess.run([PY, os.path.join(HERE, "memnos_cli.py"), *args],
                       capture_output=True, text=True, env=env, timeout=60)
    return r.returncode, (r.stdout + r.stderr)


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


def main():
    print("=== memnos CLI ===")
    rc, out = run("--help")
    check("--help lists subcommands", rc == 0 and "setup" in out and "secret" in out and "remember" in out)

    NS = "test:cli"
    run("namespace", "rm", NS)                       # clean slate
    rc, out = run("namespace", "add", NS, "--desc", "cli test")
    check("namespace add", rc == 0 and "created" in out)
    rc, out = run("namespace", "ls")
    check("namespace ls shows it", NS in out)

    rc, out = run("principal", "clitester", "--kind", "agent")
    check("principal create", rc == 0 and "id=" in out)
    rc, out = run("token", "clitester", "--label", "t")
    check("token mint (plaintext once)", "mnk_" in out)
    tok = [w for w in out.split() if w.startswith("mnk_")][0]
    rc, out = run("grant", "clitester", NS)
    check("grant", rc == 0 and "granted" in out)
    rc, out = run("whoami", tok)
    check("whoami shows grant", NS in out)

    rc, out = run("secret", "set", "clisec", "--value", "v-cli-123")
    check("secret set", rc == 0 and "stored" in out)
    rc, out = run("secret", "ls")
    check("secret ls (no plaintext)", "clisec" in out and "v-cli-123" not in out)
    rc, out = run("secret", "rm", "clisec")
    check("secret rm", rc == 0)

    rc, out = run("health")
    check("health runs", rc == 0)

    # cleanup
    run("namespace", "rm", NS, "--purge")
    import psycopg
    conn = psycopg.connect(DSN, autocommit=True)
    with conn.cursor() as c:
        c.execute("DELETE FROM memnos_control.api_tokens t USING memnos_control.principals p "
                  "WHERE t.principal_id=p.id AND p.name='clitester'")
        c.execute("DELETE FROM memnos_control.grants g USING memnos_control.principals p "
                  "WHERE g.principal_id=p.id AND p.name='clitester'")
        c.execute("DELETE FROM memnos_control.principals WHERE name='clitester'")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
