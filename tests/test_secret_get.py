"""Tests for `memnos secret get` — audited read-back (issue #13).

Covers: value roundtrip; audit row written on success; audit row written on
not-found failure (exit 1); non-admin principal is denied.

Run: python tests/test_secret_get.py   (needs MEMNOS_DSN + MEMNOS_SECRET_KEY)
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


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += cond; FAIL += (not cond)


def _audit_rows(conn, action, name):
    with conn.cursor() as c:
        c.execute("SELECT ok, detail FROM memnos_control.audit_log "
                  "WHERE action=%s AND detail->>'name'=%s ORDER BY id DESC LIMIT 5",
                  (action, name))
        return c.fetchall()


def main():
    if not Vault.available():
        print("SKIP — MEMNOS_SECRET_KEY not set"); sys.exit(0)

    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)

    admin_pid = Control.create_principal(conn, "test_secret_get_admin", "service")
    Control.grant(conn, admin_pid, "*")
    admin_tok = Control.mint_token(conn, admin_pid, "test-secret-get")

    nonadmin_pid = Control.create_principal(conn, "test_secret_get_user", "user")
    Control.grant(conn, nonadmin_pid, "some:ns")
    nonadmin_tok = Control.mint_token(conn, nonadmin_pid, "test-secret-get-nonadmin")

    secret_name = "test_sg_secret_issue13"
    secret_value = "super-secret-value-42"

    Vault.set(conn, secret_name, secret_value, "issue #13 test")

    print("=== Control.secret_get ===")
    row = Control.secret_get(conn, secret_name)
    check("secret_get returns row for existing secret", row is not None)
    check("secret_get returns None for missing secret", Control.secret_get(conn, "__no_such__") is None)

    print("=== cmd_secret get via subprocess ===")
    env = {**os.environ, "MEMNOS_DSN": DSN, "MEMNOS_TOKEN": admin_tok}
    result = subprocess.run(
        [sys.executable, "memnos_cli.py", "secret", "get", secret_name],
        capture_output=True, text=True, env=env
    )
    check("exit 0 on success", result.returncode == 0)
    check("stdout is plaintext value", result.stdout.strip() == secret_value)
    check("value NOT in stderr", secret_value not in result.stderr)

    rows = _audit_rows(conn, "secret.get", secret_name)
    check("audit row written on success", len(rows) >= 1)
    check("audit row ok=True", any(r["ok"] for r in rows))
    check("audit detail has name, not value",
          all(r["detail"].get("name") == secret_name for r in rows) and
          all(secret_value not in str(r["detail"]) for r in rows))

    print("=== not-found returns exit 1 ===")
    result_miss = subprocess.run(
        [sys.executable, "memnos_cli.py", "secret", "get", "__no_such_secret__"],
        capture_output=True, text=True, env=env
    )
    check("exit 1 when secret not found", result_miss.returncode != 0)
    check("error message mentions secret name", "__no_such_secret__" in result_miss.stderr or
          "__no_such_secret__" in result_miss.stdout)

    miss_rows = _audit_rows(conn, "secret.get", "__no_such_secret__")
    check("audit row written on not-found", len(miss_rows) >= 1)
    check("not-found audit row ok=False", any(not r["ok"] for r in miss_rows))

    print("=== non-admin is denied ===")
    env_nonadmin = {**os.environ, "MEMNOS_DSN": DSN, "MEMNOS_TOKEN": nonadmin_tok}
    result_deny = subprocess.run(
        [sys.executable, "memnos_cli.py", "secret", "get", secret_name],
        capture_output=True, text=True, env=env_nonadmin
    )
    check("non-admin exit non-zero", result_deny.returncode != 0)
    check("plaintext not leaked to non-admin", secret_value not in result_deny.stdout and
          secret_value not in result_deny.stderr)

    # cleanup
    Vault.delete(conn, secret_name)
    with conn.cursor() as c:
        for pid in (admin_pid, nonadmin_pid):
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
