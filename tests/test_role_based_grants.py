"""Role-based grants (issue #81, epic #70 item 4): roles/groups as grantable subjects,
layered OVER the existing per-principal `grants` table + `authorize()` enforcement.

Gap this closes: `grants(principal_id, namespace, can_read, can_write)` only accepts a
PRINCIPAL as the granted subject -- "only architects write to standards" required
enumerating every architect principal. This adds `roles` + `role_members` (who's in the
role) + `role_grants` (what the role can access, same can_read/can_write/exact-or-
prefix-or-'*' shape as `grants`), and a new resolver `Control.effective_namespaces()`
that UNIONs a principal's direct grants with every role it belongs to. `authorize()`,
`readable_namespaces()`, and `writable_namespaces()` all go through that one resolver
(core/control.py) so role support composes everywhere instead of being re-implemented
per call site -- see those methods' docstrings for the design rationale.

`authorized_namespaces()` (the management accessor behind `GET /admin/grants` +
`memnos grant ls`) deliberately stays DIRECT-GRANTS-ONLY: its paired mutator
`revoke_grant()` only deletes from `grants`, so blending in role-inherited rows would
make `grant rm <principal> <role-inherited-ns>` silently no-op on a row the caller still
sees as revocable. This file asserts that separation explicitly (test 7).

No HTTP server needed -- authorize()/effective_namespaces()/is_admin() are pure
Control static methods over Postgres; the CLI section (test 10) shells out to
`memnos_cli.py role ...` against the same DSN to prove the wiring end-to-end.

Run against a live isolated Postgres (see the repo's CI workflow for the throwaway
pgvector/pgvector:pg16 container pattern):
    MEMNOS_DSN=postgresql://memnos:memnos@localhost:5432/memnos python tests/test_role_based_grants.py
"""
import os
import subprocess
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import psycopg
from psycopg.rows import dict_row

from core.control import Control
from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
SCHEMA = "tenant_memnos"
PASS = FAIL = 0

PREFIX = "test:role-grants"
PRINCIPALS = ["rbg-alice", "rbg-alice2", "rbg-alice3", "rbg-bob", "rbg-carol", "rbg-dave",
              "rbg-eve", "rbg-frank", "rbg-grace", "rbg-heidi", "rbg-cli-p"]
ROLES = ["rbg-architects", "rbg-eng-role", "rbg-sales-role", "rbg-readers", "rbg-writers",
         "rbg-admins", "rbg-doomed", "rbg-cli-role"]


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if (detail and not cond) else ""))
    PASS += bool(cond)
    FAIL += (not cond)


def reset(conn):
    with conn.cursor() as c:
        for name in PRINCIPALS:
            c.execute("SELECT id FROM memnos_control.principals WHERE name=%s", (name,))
            r = c.fetchone()
            if r:
                pid = r["id"]
                c.execute("DELETE FROM memnos_control.role_members WHERE principal_id=%s", (pid,))
                c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
                c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
                c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))
        for name in ROLES:
            c.execute("SELECT id FROM memnos_control.roles WHERE name=%s", (name,))
            r = c.fetchone()
            if r:
                rid = r["id"]
                c.execute("DELETE FROM memnos_control.role_grants WHERE role_id=%s", (rid,))
                c.execute("DELETE FROM memnos_control.role_members WHERE role_id=%s", (rid,))
                c.execute("DELETE FROM memnos_control.roles WHERE id=%s", (rid,))
        c.execute("DELETE FROM %s.raw_turns WHERE namespace LIKE %%s" % SCHEMA, (PREFIX + "%",))
        c.execute("DELETE FROM memnos_control.namespaces WHERE name LIKE %s", (PREFIX + "%",))


def main():
    store = BrainStore(DSN)
    store.create_schema("memnos")
    conn = store.conn
    Control.init(conn)
    reset(conn)

    ns1 = f"{PREFIX}:direct-vs-role:ns1"
    ns2 = f"{PREFIX}:union:ns2"
    ns3 = f"{PREFIX}:readonly:ns3"
    eng_wild = f"{PREFIX}:eng:*"
    eng_ns = f"{PREFIX}:eng:widgets"
    sales_wild = f"{PREFIX}:sales:*"
    sales_ns = f"{PREFIX}:sales:pipeline"
    other_ns = f"{PREFIX}:other:x"

    # === 1. basic role membership grants access ============================
    print("=== 1. role membership grants access; non-member denied ===")
    alice = Control.create_principal(conn, "rbg-alice", "user")
    bob = Control.create_principal(conn, "rbg-bob", "user")
    Control.create_role(conn, "rbg-architects", "standards writers")
    Control.grant_role(conn, "rbg-architects", ns1, can_read=True, can_write=True)
    Control.add_role_member(conn, "rbg-architects", alice)
    check("role member can write via inherited role grant",
          Control.authorize(conn, alice, ns1, write=True) is True)
    check("role member can read via inherited role grant",
          Control.authorize(conn, alice, ns1, write=False) is True)
    check("non-member (no direct grant, no role) is denied write",
          Control.authorize(conn, bob, ns1, write=True) is False)
    check("non-member is denied read too",
          Control.authorize(conn, bob, ns1, write=False) is False)

    # === 2. wildcard-prefix role grant composes with per-principal wildcard ===
    print("=== 2. wildcard-prefix role grant + per-principal wildcard compose ===")
    carol = Control.create_principal(conn, "rbg-carol", "user")
    Control.create_role(conn, "rbg-eng-role", None)
    Control.grant_role(conn, "rbg-eng-role", eng_wild, can_read=True, can_write=True)
    Control.add_role_member(conn, "rbg-eng-role", carol)
    Control.grant(conn, carol, sales_wild, can_read=True, can_write=True)  # carol's OWN direct wildcard
    check("role wildcard grant matches a namespace under its prefix",
          Control.authorize(conn, carol, eng_ns, write=True) is True)
    check("direct per-principal wildcard grant still works alongside the role grant",
          Control.authorize(conn, carol, sales_ns, write=True) is True)
    check("neither wildcard leaks outside its own prefix",
          Control.authorize(conn, carol, other_ns, write=True) is False)

    # === 3. revoking a role membership removes access without affecting others
    print("=== 3. revoking one member's role membership doesn't affect other members ===")
    dave = Control.create_principal(conn, "rbg-dave", "user")
    Control.add_role_member(conn, "rbg-architects", dave)
    check("dave (2nd member) also inherits the role's grant before revocation",
          Control.authorize(conn, dave, ns1, write=True) is True)
    removed = Control.remove_role_member(conn, "rbg-architects", alice)
    check("remove_role_member reports the removal", removed is True)
    check("alice loses write access after her membership is revoked",
          Control.authorize(conn, alice, ns1, write=True) is False)
    check("dave (still a member) retains write access untouched",
          Control.authorize(conn, dave, ns1, write=True) is True)
    check("removing a non-member reports False (no-op)",
          Control.remove_role_member(conn, "rbg-architects", bob) is False)

    # === 4. direct grant + role grant on the SAME namespace = union, not conflict
    print("=== 4. direct grant + role grant on the same namespace unions, never conflicts ===")
    alice2 = Control.create_principal(conn, "rbg-alice2", "user")
    Control.grant(conn, alice2, ns2, can_read=True, can_write=False)   # direct: READ ONLY
    Control.grant_role(conn, "rbg-architects", ns2, can_read=True, can_write=True)  # role: read+write
    Control.add_role_member(conn, "rbg-architects", alice2)
    direct_row = next(g for g in Control.authorized_namespaces(conn, alice2) if g["namespace"] == ns2)
    check("direct grant alone is read-only (can_write=False), confirming the union isn't vacuous",
          direct_row["can_write"] is False, str(direct_row))
    check("authorize() unions direct read-only + role read+write -> write ALLOWED",
          Control.authorize(conn, alice2, ns2, write=True) is True)

    alice3 = Control.create_principal(conn, "rbg-alice3", "user")
    Control.grant(conn, alice3, ns2, can_read=True, can_write=True)    # direct: read+write
    Control.create_role(conn, "rbg-doomed", None)
    Control.grant_role(conn, "rbg-doomed", ns2, can_read=True, can_write=False)  # role: read-only
    Control.add_role_member(conn, "rbg-doomed", alice3)
    check("direct read+write survives a co-existing read-only role grant on the same ns",
          Control.authorize(conn, alice3, ns2, write=True) is True)

    # === 5. read-only role grant does NOT permit write ======================
    print("=== 5. a read-only role grant alone does not permit write ===")
    eve = Control.create_principal(conn, "rbg-eve", "user")
    Control.create_role(conn, "rbg-readers", "read-only role")
    Control.grant_role(conn, "rbg-readers", ns3, can_read=True, can_write=False)
    Control.add_role_member(conn, "rbg-readers", eve)
    check("read-only role member CAN read",
          Control.authorize(conn, eve, ns3, write=False) is True)
    check("read-only role member CANNOT write",
          Control.authorize(conn, eve, ns3, write=True) is False)

    # === 6. authorized_namespaces() stays direct-only (management accessor) ==
    print("=== 6. authorized_namespaces() (management view) excludes role-inherited grants ===")
    direct_only = Control.authorized_namespaces(conn, alice2)
    direct_nss = {(g["namespace"], g["can_read"], g["can_write"]) for g in direct_only}
    check("authorized_namespaces(alice2) shows ONLY her direct read-only row for ns2",
          direct_nss == {(ns2, True, False)}, str(direct_nss))
    effective = {(g["namespace"], g["can_read"], g["can_write"]) for g in Control.effective_namespaces(conn, alice2)}
    check("effective_namespaces(alice2) shows the UNIONED read+write row for ns2 instead",
          (ns2, True, True) in effective, str(effective))
    # Prove the separation matters operationally: revoke_grant (what `grant rm` calls)
    # only touches the direct table -- it must not silently no-op on a role-sourced row.
    Control.revoke_grant(conn, alice2, ns2)
    check("revoke_grant removes alice2's DIRECT grant on ns2",
          Control.authorized_namespaces(conn, alice2) == [])
    check("alice2 STILL has write access after her direct grant is revoked -- via the role",
          Control.authorize(conn, alice2, ns2, write=True) is True,
          "role grant must survive revocation of the unrelated direct grant")

    # === 7. readable_namespaces()/writable_namespaces() include role grants ==
    print("=== 7. readable/writable_namespaces() include role-granted namespaces ===")
    frank = Control.create_principal(conn, "rbg-frank", "user")
    read_wild = f"{PREFIX}:readwild:*"
    ns_alpha = f"{PREFIX}:readwild:alpha"
    ns_beta = f"{PREFIX}:readwild:beta"
    ns_unrelated = f"{PREFIX}:unrelated:gamma"
    for n in (ns_alpha, ns_beta, ns_unrelated):
        Control.create_namespace(conn, n)  # registers in memnos_control.namespaces
    Control.create_role(conn, "rbg-sales-role", "read-wild role")
    Control.grant_role(conn, "rbg-sales-role", read_wild, can_read=True, can_write=False)
    Control.add_role_member(conn, "rbg-sales-role", frank)
    readable = Control.readable_namespaces(conn, frank)
    check("readable_namespaces includes a role-wildcard-covered namespace (alpha)",
          ns_alpha in readable, str(readable))
    check("readable_namespaces includes a role-wildcard-covered namespace (beta)",
          ns_beta in readable, str(readable))
    check("readable_namespaces excludes a namespace outside the role's wildcard prefix",
          ns_unrelated not in readable, str(readable))

    grace = Control.create_principal(conn, "rbg-grace", "user")
    write_wild = f"{PREFIX}:writewild:*"
    ns_one = f"{PREFIX}:writewild:one"
    ns_two = f"{PREFIX}:writewild:two"
    ns_out = f"{PREFIX}:writeout:z"
    now = datetime.now(timezone.utc)
    store.insert_raw_turn(SCHEMA, ns_one, None, "user", "role-write-test row one", now, None)
    store.insert_raw_turn(SCHEMA, ns_two, None, "user", "role-write-test row two", now, None)
    store.insert_raw_turn(SCHEMA, ns_out, None, "user", "role-write-test outside row", now, None)
    Control.create_role(conn, "rbg-writers", "write-wild role")
    Control.grant_role(conn, "rbg-writers", write_wild, can_read=True, can_write=True)
    Control.add_role_member(conn, "rbg-writers", grace)
    writable = Control.writable_namespaces(conn, grace, limit=100)
    check("writable_namespaces includes role-wildcard-covered namespace with real data (one)",
          ns_one in writable, str(writable))
    check("writable_namespaces includes role-wildcard-covered namespace with real data (two)",
          ns_two in writable, str(writable))
    check("writable_namespaces excludes a namespace outside the role's write wildcard",
          ns_out not in writable, str(writable))

    # === 8. is_admin() resolves a '*' grant via role membership too ==========
    print("=== 8. is_admin() via role membership (consistent with authorize()) ===")
    heidi = Control.create_principal(conn, "rbg-heidi", "user")
    Control.create_role(conn, "rbg-admins", "granted '*' via role")
    Control.grant_role(conn, "rbg-admins", "*", can_read=True, can_write=True)
    Control.add_role_member(conn, "rbg-admins", heidi)
    check("principal granted '*' ONLY via a role is_admin",
          Control.is_admin(conn, heidi) is True)
    check("a principal with no grant at all is not admin",
          Control.is_admin(conn, bob) is False)

    # === 9. baseline: zero role membership behaves exactly as before =========
    print("=== 9. principal with no role membership: effective == direct (regression) ===")
    plain_ns = f"{PREFIX}:plain:x"
    Control.grant(conn, bob, plain_ns, can_read=True, can_write=True)
    direct_bob = {(g["namespace"], g["can_read"], g["can_write"]) for g in Control.authorized_namespaces(conn, bob)}
    effective_bob = {(g["namespace"], g["can_read"], g["can_write"]) for g in Control.effective_namespaces(conn, bob)}
    check("no roles involved -> authorized_namespaces() == effective_namespaces()",
          direct_bob == effective_bob, f"{direct_bob} vs {effective_bob}")
    check("plain direct-grant authorize() still works (no regression)",
          Control.authorize(conn, bob, plain_ns, write=True) is True)

    # === 10. delete_role() cleans up grants + memberships =====================
    print("=== 10. delete_role() removes grants + memberships; members lose access ===")
    check("dave has access via rbg-architects before role deletion",
          Control.authorize(conn, dave, ns1, write=True) is True)
    deleted = Control.delete_role(conn, "rbg-architects")
    check("delete_role reports deletion", deleted is True)
    check("dave loses access once the role itself is deleted",
          Control.authorize(conn, dave, ns1, write=True) is False)
    check("deleting an unknown role reports False",
          Control.delete_role(conn, "rbg-does-not-exist") is False)
    check("role_grants for the deleted role are gone",
          Control.list_role_grants(conn, "rbg-architects") == [])

    # === 11. CLI end-to-end (real Postgres, real memnos_cli.py subprocess) ===
    print("=== 11. `memnos role ...` CLI wiring against the same live Postgres ===")
    env = dict(os.environ)
    env["MEMNOS_DSN"] = DSN
    env.pop("OPENAI_API_KEY", None)
    cli_home = os.path.join(HERE, "_rbg_cli_home")
    os.makedirs(cli_home, exist_ok=True)
    env["HOME"] = cli_home

    def cli(*args):
        r = subprocess.run([sys.executable, os.path.join(ROOT, "memnos_cli.py"), *args],
                           capture_output=True, text=True, env=env, timeout=30)
        return r.returncode, (r.stdout or "") + (r.stderr or "")

    rc, out = cli("role", "create", "rbg-cli-role", "--desc", "cli test role")
    check("`role create` succeeds", rc == 0 and "rbg-cli-role" in out, out)
    rc, out = cli("role", "grant", "rbg-cli-role", f"{PREFIX}:cli:*")
    check("`role grant` succeeds", rc == 0 and "granted" in out, out)
    rc, out = cli("principal", "create", "rbg-cli-p", "--kind", "user")
    check("`principal create` succeeds", rc == 0, out)
    rc, out = cli("role", "add-member", "rbg-cli-role", "rbg-cli-p")
    check("`role add-member` succeeds", rc == 0 and "added" in out, out)
    rc, out = cli("role", "members", "rbg-cli-role")
    check("`role members` lists the added principal", rc == 0 and "rbg-cli-p" in out, out)
    rc, out = cli("role", "grants", "rbg-cli-role")
    check("`role grants` lists the granted namespace", rc == 0 and f"{PREFIX}:cli:*" in out, out)
    # verify the CLI-created role/membership actually authorizes, straight from Postgres
    cli_conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    with cli_conn.cursor() as c:
        c.execute("SELECT id FROM memnos_control.principals WHERE name='rbg-cli-p'")
        cli_pid = c.fetchone()["id"]
    check("CLI-created role membership actually authorizes against the CLI-created grant",
          Control.authorize(cli_conn, cli_pid, f"{PREFIX}:cli:widgets", write=True) is True)
    rc, out = cli("role", "rm-member", "rbg-cli-role", "rbg-cli-p")
    check("`role rm-member` succeeds", rc == 0 and "removed" in out, out)
    check("access is gone immediately after CLI rm-member",
          Control.authorize(cli_conn, cli_pid, f"{PREFIX}:cli:widgets", write=True) is False)
    rc, out = cli("role", "revoke", "rbg-cli-role", f"{PREFIX}:cli:*")
    check("`role revoke` succeeds", rc == 0 and "revoked" in out, out)
    rc, out = cli("role", "ls")
    check("`role ls` lists the role", rc == 0 and "rbg-cli-role" in out, out)
    rc, out = cli("role", "rm", "rbg-cli-role")
    check("`role rm` succeeds", rc == 0 and "removed" in out, out)
    cli_conn.close()

    reset(conn)
    conn.close()

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
