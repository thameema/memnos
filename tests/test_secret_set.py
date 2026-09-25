"""Tests for `memnos secret set` (issue #168): masked-preview feedback, whitespace
stripping, empty-value rejection, and the guard against storing a `secret://` reference
string as a secret's own plaintext value.

Run: python tests/test_secret_set.py   (needs MEMNOS_DSN + MEMNOS_SECRET_KEY)
"""
import os
import sys
import subprocess

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_env(path=".env"):
    try:
        for line in open(path):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_env()

import psycopg
from psycopg.rows import dict_row
from core.control import Control
from core.vault import Vault

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    PASS += bool(cond); FAIL += (not cond)


def _run(*args, env=None):
    return subprocess.run([sys.executable, "memnos_cli.py", "secret", "set", *args],
                          capture_output=True, text=True, env=env or {**os.environ, "MEMNOS_DSN": DSN})


def main():
    if not Vault.available():
        print("SKIP — MEMNOS_SECRET_KEY not set"); sys.exit(0)

    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)

    def cleanup(name):
        Vault.delete(conn, name)

    print("=== normal set via --value: success message previews what was captured ===")
    name = "test_ss_normal_168"
    cleanup(name)
    r = _run(name, "--value", "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789")
    check("exit 0", r.returncode == 0, r.stderr)
    check("success message present", "stored" in r.stdout, r.stdout)
    check("preview shows length", "chars" in r.stdout, r.stdout)
    check("preview does NOT leak the full value", "abcdefghijklmnopqrstuvwxyz" not in r.stdout, r.stdout)
    check("preview shows first/last few chars", "sk-p" in r.stdout and "6789" in r.stdout, r.stdout)
    check("value actually stored correctly",
          Vault.get(conn, name) == "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789")
    cleanup(name)

    print("=== issue #168 repro: storing a secret://... reference is rejected, not silently accepted ===")
    name = "test_ss_badref_168"
    cleanup(name)
    r = _run(name, "--value", "secret://openai")
    check("exit non-zero", r.returncode != 0, f"rc={r.returncode} out={r.stdout!r} err={r.stderr!r}")
    check("error mentions the problem", "secret://" in (r.stderr + r.stdout), r.stderr + r.stdout)
    check("nothing was stored", Vault.get(conn, name) is None)

    print("=== a pasted value with a trailing newline/whitespace is stripped, not stored dirty ===")
    name = "test_ss_whitespace_168"
    cleanup(name)
    r = _run(name, "--value", "  sk-real-value-here  \n")
    check("exit 0", r.returncode == 0, r.stderr)
    check("stored value has no leading/trailing whitespace",
          Vault.get(conn, name) == "sk-real-value-here", repr(Vault.get(conn, name)))
    cleanup(name)

    print("=== a whitespace-only value is treated as empty and rejected ===")
    name = "test_ss_empty_168"
    cleanup(name)
    r = _run(name, "--value", "   ")
    check("exit non-zero", r.returncode != 0)
    check("nothing was stored", Vault.get(conn, name) is None)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
