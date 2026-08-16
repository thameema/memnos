"""No-AI tests for LIVE NAMESPACE INHERITANCE (issue #85, epic #70's last sub-issue).

Two mechanisms, already distinct before this PR (Mechanism B / namespace_links, built by
0.1.6) and newly added by this PR (Mechanism A):

  Mechanism A (same-root, automatic): a namespace's ':'-prefix ancestors are AUTOMATICALLY
  consulted for PINNED CONSTRAINTS at recall/enforce time (customer:example:widgets ->
  customer:example -> customer), gated by the caller's own read grant on each ancestor
  (same gate Mechanism B already used), and opt-out-able per namespace
  (inherit_ancestors=false). Pure ':'-prefix string derivation, so multi-hop is free — no
  recursive query needed. Deliberately does NOT widen general recall search results (only
  feeds pin_nss / constraint enforcement, not extra_namespaces) — the gap this issue closes
  is "parent constraints don't propagate to children," not "child search results should
  include parent content."

  Mechanism B (cross-root, explicit): namespace_links, unchanged in behavior — this PR only
  adds an informational `kind` column.

Covers all 6 items from issue #85's corrected scope:
  1. Mechanism A: automatic same-root ancestor pin fan-out, multi-hop, opt-in-by-default
  2. `kind` column on namespace_links (informational taxonomy)
  3. Multi-hop: A is multi-level (string-derived); B stays single-hop (regression-checked
     against test_grounded_recall.py, not re-tested here)
  4. Enforce-rule hook-cache fan-out (ancestors AND links) via _refresh_enforce_cache
  5. Copy-provenance record on /namespace/copy
  6. Cross-root non-inheritance: a shared LEAF segment across different roots must never
     imply inheritance

    MEMNOS_DSN=postgresql://memnos:...@localhost:5432/memnos MEMNOS_URL=http://127.0.0.1:8900 \
        python tests/test_namespace_inheritance.py
"""
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
from psycopg.rows import dict_row
from core.control import Control
from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
SCHEMA = "tenant_memnos"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable

# deep, distinctive namespaces — 'test:ni:*' — deliberately NOT bare 'test' at any level
# used by an add_constraint_enforcement/pin call, so this suite can never cross-pollute
# other test files that also use 'test:*' namespaces (they never write to a bare 'test'
# root either — verified before writing this file).
LEAF = "test:ni:multihop:root:mid:leaf"
MID = "test:ni:multihop:root:mid"
GRANDPARENT = "test:ni:multihop:root"
UNGRANTED_ANCESTOR = "test:ni:multihop"          # one level further up — left ungranted

OPTOUT_LEAF = "test:ni:optout:leaf"
OPTOUT_PARENT = "test:ni:optout"

CUST_A = "test:ni:cross:custA:widgets"
CUST_A_PARENT = "test:ni:cross:custA"
CUST_B = "test:ni:cross:custB:widgets"

ENF_CHILD = "test:ni:enforce:root:child"
ENF_ROOT = "test:ni:enforce:root"
ENF_OPTOUT_CHILD = "test:ni:enforce:optout:child"
ENF_OPTOUT_ROOT = "test:ni:enforce:optout"
ENF_LINK_SRC = "test:ni:enforce:linksrc"
ENF_LINK_DST = "test:ni:enforce:linkdst"

COPY_SRC = "test:ni:copy:src"
COPY_DST = "test:ni:copy:dst"

ALL_NS = [LEAF, MID, GRANDPARENT, UNGRANTED_ANCESTOR, OPTOUT_LEAF, OPTOUT_PARENT,
          CUST_A, CUST_A_PARENT, CUST_B, ENF_CHILD, ENF_ROOT, ENF_OPTOUT_CHILD,
          ENF_OPTOUT_ROOT, ENF_LINK_SRC, ENF_LINK_DST, COPY_SRC, COPY_DST]

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def call(path, token=None, body=None, method="POST"):
    req = urllib.request.Request(URL + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=25)
        return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def cli(*args, env_extra=None):
    env = dict(os.environ, MEMNOS_DSN=DSN, **(env_extra or {}))
    r = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), *args],
                       capture_output=True, text=True, timeout=60, env=env)
    return r.returncode, (r.stdout + r.stderr)


def refresh_cache(ns, home):
    """Same helper as test_constraint_enforce_hook.py — drives _refresh_enforce_cache
    directly, isolated from the rest of `hook status`'s behavior."""
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "import memnos_cli\n"
        "cfg = memnos_cli.load_config()\n"
        "print(memnos_cli._refresh_enforce_cache(cfg, %r))\n"
    ) % (ROOT, ns)
    env = {**os.environ, "MEMNOS_DSN": DSN, "HOME": home}
    r = subprocess.run([PY, "-c", code], capture_output=True, text=True, timeout=30, env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    return int(r.stdout.strip())


def run_hook(ns, stdin_obj, home):
    env = {**os.environ, "MEMNOS_DSN": DSN, "MEMNOS_NS": ns, "HOME": home}
    r = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), "hook", "enforce"],
                       input=json.dumps(stdin_obj), capture_output=True, text=True,
                       timeout=30, env=env)
    return r.returncode, r.stdout.strip(), r.stderr


def cleanup(conn):
    with conn.cursor() as c:
        for ns in ALL_NS:
            c.execute(f"DELETE FROM {SCHEMA}.raw_turns WHERE namespace=%s", (ns,))
            c.execute(f"DELETE FROM {SCHEMA}.semantic WHERE namespace=%s", (ns,))
            c.execute("DELETE FROM memnos_control.namespace_links WHERE src_ns=%s OR dst_ns=%s", (ns, ns))
            c.execute("DELETE FROM memnos_control.constraint_enforcement WHERE namespace=%s", (ns,))
            c.execute("DELETE FROM memnos_control.namespace_copy_provenance WHERE dst_ns=%s OR src_ns=%s", (ns, ns))
            c.execute("DELETE FROM memnos_control.namespaces WHERE name=%s", (ns,))


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    cleanup(conn)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    user_id = Control.create_principal(conn, "ni-user", "agent")
    user_tok = Control.mint_token(conn, user_id, "t")
    admin_id = Control.create_principal(conn, "ni-admin", "user")
    admin_tok = Control.mint_token(conn, admin_id, "t")
    Control.grant(conn, admin_id, "*", can_read=True, can_write=True)
    # user granted leaf/mid/grandparent, deliberately NOT the ungranted ancestor further up
    for ns in (LEAF, MID, GRANDPARENT, OPTOUT_LEAF, OPTOUT_PARENT, CUST_A, CUST_A_PARENT, CUST_B,
              COPY_SRC, COPY_DST):
        Control.grant(conn, user_id, ns, can_read=True, can_write=True)

    print("=== namespace_ancestors: pure string derivation, multi-level ===")
    check("3-level namespace has 2 ancestors, nearest-first",
          Control.namespace_ancestors("a:b:c") == ["a:b", "a"])
    check("6-segment namespace has 5 ancestors, nearest-first, all the way to the root",
          Control.namespace_ancestors(LEAF) ==
          [MID, GRANDPARENT, UNGRANTED_ANCESTOR, "test:ni", "test"])
    check("single-segment namespace has no ancestors", Control.namespace_ancestors("solo") == [])
    check("effective_ancestors short-circuits on [] without needing the opt-out flag",
          Control.effective_ancestors(conn, "solo") == [])
    check("cross-root shared leaf: custB is NOT derived as an ancestor of custA (or vice versa)",
          CUST_A not in Control.namespace_ancestors(CUST_B)
          and CUST_B not in Control.namespace_ancestors(CUST_A))

    print("=== Mechanism A: multi-hop pin fan-out via /recall (authorized ancestors) ===")
    store.insert_raw_turn(SCHEMA, GRANDPARENT, None, "user", "grandparent quibblewok constraint",
                          now, None, memory_type="constraint")
    store.insert_raw_turn(SCHEMA, MID, None, "user", "mid-level quibblewok constraint",
                          now, None, memory_type="constraint")
    store.insert_raw_turn(SCHEMA, LEAF, None, "user", "leaf quibblewok note", now, None)
    s, j = call("/recall", user_tok, {"namespace": LEAF, "query": "quibblewok"})
    check("recall 200", s == 200)
    check("inherited_in lists BOTH authorized ancestors, nearest-first",
          j.get("inherited_in") == [MID, GRANDPARENT], detail=str(j.get("inherited_in")))
    check("inheritance_skipped lists the ungranted further ancestor",
          UNGRANTED_ANCESTOR in (j.get("inheritance_skipped") or []),
          detail=str(j.get("inheritance_skipped")))
    contents = " ".join(m.get("content", "") for m in j.get("memories", []))
    check("grandparent's constraint reached the leaf's recall (2-hop)",
          "grandparent quibblewok constraint" in contents)
    check("mid's constraint reached the leaf's recall (1-hop)",
          "mid-level quibblewok constraint" in contents)
    pinned = [m for m in j.get("memories", []) if m.get("pinned")]
    gp_rows = [m for m in pinned if m["content"] == "grandparent quibblewok constraint"]
    check("inherited pin is tagged with its SOURCE namespace (transparency)",
          gp_rows and gp_rows[0].get("namespace") == GRANDPARENT)
    check("no grounded_in/links_skipped keys leaked in (no explicit links exist here)",
          "grounded_in" not in j and "links_skipped" not in j)

    print("=== Mechanism A: general content search is NOT widened (constraints-only) ===")
    store.insert_raw_turn(SCHEMA, GRANDPARENT, None, "user",
                          "grandparent unrelated non-constraint memory about zylofoxtrot",
                          now, None)
    s, j = call("/recall", user_tok, {"namespace": LEAF, "query": "zylofoxtrot"})
    check("ancestor's NON-constraint content does not leak into child recall",
          "zylofoxtrot" not in " ".join(m.get("content", "") for m in j.get("memories", [])))

    print("=== opt-out (inherit_ancestors=false) suppresses Mechanism A entirely ===")
    store.insert_raw_turn(SCHEMA, OPTOUT_PARENT, None, "user", "opted-out-parent flibbertigibbet rule",
                          now, None, memory_type="constraint")
    s, j = call("/recall", user_tok, {"namespace": OPTOUT_LEAF, "query": "flibbertigibbet"})
    check("before opt-out: parent constraint DOES reach the child",
          "opted-out-parent flibbertigibbet rule" in
          " ".join(m.get("content", "") for m in j.get("memories", [])))
    check("before opt-out: inherited_in reports the parent", j.get("inherited_in") == [OPTOUT_PARENT])
    rc, out = cli("namespace", "set", OPTOUT_LEAF, "--inherit-ancestors", "false")
    check("CLI: namespace set --inherit-ancestors false", rc == 0 and "False" in out)
    s, j = call("/recall", user_tok, {"namespace": OPTOUT_LEAF, "query": "flibbertigibbet"})
    check("after opt-out: parent constraint no longer reaches the child",
          "opted-out-parent flibbertigibbet rule" not in
          " ".join(m.get("content", "") for m in j.get("memories", [])))
    check("after opt-out: no inherited_in/inheritance_skipped keys at all",
          "inherited_in" not in j and "inheritance_skipped" not in j)
    check("Control.effective_ancestors respects opt-out directly", Control.effective_ancestors(conn, OPTOUT_LEAF) == [])
    check("Control.namespace_ancestors (pure derivation) is unaffected by opt-out",
          Control.namespace_ancestors(OPTOUT_LEAF) == [OPTOUT_PARENT, "test:ni", "test"])
    rc, out = cli("namespace", "set", OPTOUT_LEAF, "--inherit-ancestors", "true")
    check("CLI: namespace set --inherit-ancestors true (re-enable)", rc == 0)
    check("missing registry row defaults to inherit=true (COALESCE-safe)",
          Control.namespace_inherits_ancestors(conn, "test:ni:never_registered:child") is True)

    print("=== item 6: cross-root non-inheritance (shared LEAF segment, different roots) ===")
    store.insert_raw_turn(SCHEMA, CUST_A_PARENT, None, "user", "custA-only wibbleflorp constraint",
                          now, None, memory_type="constraint")
    store.insert_raw_turn(SCHEMA, CUST_A, None, "user", "custA widgets note", now, None)
    store.insert_raw_turn(SCHEMA, CUST_B, None, "user", "custB widgets note", now, None)
    s, ja = call("/recall", user_tok, {"namespace": CUST_A, "query": "wibbleflorp"})
    check("custA:widgets DOES inherit from its real ancestor custA (sanity)",
          "custA-only wibbleflorp constraint" in
          " ".join(m.get("content", "") for m in ja.get("memories", [])))
    s, jb = call("/recall", user_tok, {"namespace": CUST_B, "query": "wibbleflorp"})
    check("custB:widgets does NOT inherit custA's constraint despite sharing leaf 'widgets'",
          "custA-only wibbleflorp constraint" not in
          " ".join(m.get("content", "") for m in jb.get("memories", [])))
    check("custB's recall reports no inherited_in from custA",
          CUST_A_PARENT not in (jb.get("inherited_in") or []))

    print("=== item 2: kind column on namespace_links ===")
    Control.link_namespaces(conn, ENF_LINK_SRC, ENF_LINK_DST)  # default kind
    links = {l["dst_ns"]: l["kind"] for l in Control.list_links(conn, ENF_LINK_SRC)}
    check("existing/default link kind is 'link' (no retroactive relabeling)",
          links.get(ENF_LINK_DST) == "link")
    Control.unlink_namespaces(conn, ENF_LINK_SRC, ENF_LINK_DST)
    Control.link_namespaces(conn, ENF_LINK_SRC, ENF_LINK_DST, kind="governed_by")
    links = {l["dst_ns"]: l["kind"] for l in Control.list_links(conn, ENF_LINK_SRC)}
    check("explicit kind='governed_by' is stored", links.get(ENF_LINK_DST) == "governed_by")
    try:
        Control.link_namespaces(conn, ENF_LINK_SRC, "test:ni:enforce:linkdst2", kind="bogus")
        check("invalid link kind rejected", False)
    except ValueError:
        check("invalid link kind rejected", True)
    rc, out = cli("namespace", "unlink", ENF_LINK_SRC, ENF_LINK_DST)
    check("CLI: unlink for re-link below", rc == 0)
    rc, out = cli("namespace", "link", ENF_LINK_SRC, ENF_LINK_DST, "--link-kind", "governed_by")
    check("CLI: namespace link --link-kind governed_by", rc == 0 and "governed_by" in out)
    rc, out = cli("namespace", "links", ENF_LINK_SRC)
    check("CLI: namespace links shows the kind", rc == 0 and "[governed_by]" in out)

    print("=== item 4: enforce-hook fan-out — ancestor rule reaches the descendant's cache ===")
    home = tempfile.mkdtemp()
    Control.add_constraint_enforcement(conn, ENF_ROOT, "never rm -rf (ancestor rule)",
                                       "block", "Bash(rm*)")
    count = refresh_cache(ENF_CHILD, home)
    check("fan-out count includes the ANCESTOR's rule even though child has none of its own",
          count == 1, detail=str(count))
    rc, out, err = run_hook(ENF_CHILD, {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}, home)
    body = json.loads(out) if out else {}
    check("PreToolUse actually DENIES based on the fanned-out ancestor rule",
          body.get("hookSpecificOutput", {}).get("permissionDecision") == "deny")
    check("deny reason names the ancestor as the rule's source",
          f"[from {ENF_ROOT}]" in (body.get("hookSpecificOutput", {}).get("permissionDecisionReason") or ""))
    Control.add_constraint_enforcement(conn, ENF_CHILD, "child's own rule", "ask", "Read")
    count = refresh_cache(ENF_CHILD, home)
    check("fan-out count includes BOTH own (1) + ancestor (1) rules", count == 2, detail=str(count))

    print("=== item 4: enforce-hook fan-out — opt-out suppresses the ancestor rule too ===")
    Control.add_constraint_enforcement(conn, ENF_OPTOUT_ROOT, "ancestor rule (should be suppressed)",
                                       "block", "Bash(rm*)")
    Control.set_namespace_inherit_ancestors(conn, ENF_OPTOUT_CHILD, False)
    count = refresh_cache(ENF_OPTOUT_CHILD, home)
    check("opted-out child's fan-out count excludes the ancestor's rule", count == 0, detail=str(count))
    rc, out, err = run_hook(ENF_OPTOUT_CHILD,
                            {"tool_name": "Bash", "tool_input": {"command": "rm -rf /tmp/x"}}, home)
    check("opted-out child: no cache/no rules -> hook defers (no output)", rc == 0 and out == "")

    print("=== item 4: enforce-hook fan-out — explicit LINK also reaches the cache ===")
    Control.add_constraint_enforcement(conn, ENF_LINK_DST, "linked-namespace rule", "block", "Read")
    count = refresh_cache(ENF_LINK_SRC, home)
    check("fan-out count includes the explicitly LINKED namespace's rule",
          count == 1, detail=str(count))
    rc, out, err = run_hook(ENF_LINK_SRC, {"tool_name": "Read", "tool_input": {"file_path": "/x"}}, home)
    body = json.loads(out) if out else {}
    check("PreToolUse denies based on the fanned-out LINKED rule",
          body.get("hookSpecificOutput", {}).get("permissionDecision") == "deny")

    print("=== item 5: copy provenance ===")
    store.insert_raw_turn(SCHEMA, COPY_SRC, None, "user", "copy-provenance source turn", now, None)
    before = Control.list_namespace_copy_provenance(conn, COPY_DST)
    check("no provenance rows before any copy", before == [])
    s, j = call("/namespace/copy", user_tok, {"namespace": COPY_DST, "src": COPY_SRC, "mode": "copy"})
    check("copy 200", s == 200)
    rows = Control.list_namespace_copy_provenance(conn, COPY_DST)
    check("exactly one provenance row recorded", len(rows) == 1, detail=str(rows))
    if rows:
        r = rows[0]
        check("provenance dst_ns correct", r["dst_ns"] == COPY_DST)
        check("provenance src_ns correct", r["src_ns"] == COPY_SRC)
        check("provenance mode correct", r["mode"] == "copy")
        check("provenance copied_by resolves to the calling principal", r["copied_by"] == user_id)
        check("provenance copied_at is a real recent timestamp",
              r["copied_at"] is not None and
              (datetime.now(timezone.utc) - r["copied_at"]).total_seconds() < 300)
    check("existing copy behavior unaffected: facts actually copied",
          Control.list_namespace_copy_provenance(conn, COPY_SRC)  # queryable from src side too
          and True)

    cleanup(conn)
    with conn.cursor() as c:
        for pid in (user_id, admin_id):
            c.execute("DELETE FROM memnos_control.api_tokens WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.grants WHERE principal_id=%s", (pid,))
            c.execute("DELETE FROM memnos_control.principals WHERE id=%s", (pid,))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
