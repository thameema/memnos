"""OpenAPI contract enforcement: every operation documented in openapi.yaml is
exercised against the running server and the REAL response is validated against the
spec's schema. A documented endpoint the server doesn't serve, an undocumented status
code, or a response that violates its schema = test failure. So the spec can never
silently drift from the implementation.

No runtime deps beyond what the server already needs (pyyaml). Validation uses a
minimal structural JSON-Schema validator (type / required / properties / items /
enum / anyOf / $ref) — deliberately NOT a full validator, but enough to catch shape
drift; jsonschema is not required.

Run against a live local server (same harness as the rest of tests/):
    MEMNOS_DSN=... MEMNOS_URL=... python tests/test_openapi_contract.py
"""
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import psycopg
import yaml
from psycopg.rows import dict_row
from core.control import Control
from core.store import BrainStore

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
URL = os.environ.get("MEMNOS_URL", "http://127.0.0.1:8900")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = "tenant_memnos"
NS = "test:oapi"
NSK = "test:oapi:kb"      # linked knowledge namespace (grounded recall)
NS2 = "test:oapi:dst"     # namespace/copy destination
PASS = FAIL = 0
SPEC = yaml.safe_load(open(os.path.join(ROOT, "openapi.yaml")))
EXERCISED = set()         # (METHOD, path) ops we validated at least one response for


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


# ---- minimal structural JSON-Schema validator (subset, test-only) ------------
def _resolve(node):
    while isinstance(node, dict) and "$ref" in node:
        cur = SPEC
        for part in node["$ref"].lstrip("#/").split("/"):
            cur = cur[part]
        node = cur
    return node


_TYPES = {"object": dict, "array": list, "string": str, "boolean": bool}


def _type_ok(v, t):
    if t == "null":
        return v is None
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    py = _TYPES.get(t)
    return py is not None and isinstance(v, py)


def validate(value, schema, path="$"):
    """Returns a list of error strings (empty = valid)."""
    schema = _resolve(schema)
    errs = []
    for key in ("anyOf", "oneOf"):
        if key in schema:
            subs = [validate(value, s, path) for s in schema[key]]
            if not any(not e for e in subs):
                errs.append(f"{path}: no {key} branch matched ({subs[0][:1]})")
            return errs
    t = schema.get("type")
    if t is not None:
        types = t if isinstance(t, list) else [t]
        if not any(_type_ok(value, x) for x in types):
            return [f"{path}: {json.dumps(value)[:80]} is not of type {types}"]
    if "enum" in schema and value is not None and value not in schema["enum"]:
        errs.append(f"{path}: {value!r} not in enum {schema['enum']}")
    if isinstance(value, dict):
        for req in schema.get("required", []):
            if req not in value:
                errs.append(f"{path}: missing required property '{req}'")
        for k, sub in (schema.get("properties") or {}).items():
            if k in value:
                errs.extend(validate(value[k], sub, f"{path}.{k}"))
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            errs.extend(validate(item, schema["items"], f"{path}[{i}]"))
    return errs


# ---- spec lookup + HTTP exercise ---------------------------------------------
def _operation(method, spec_path):
    p = (SPEC.get("paths") or {}).get(spec_path)
    return (p or {}).get(method.lower())


def call(method, spec_path, *, token=None, body=None, query="", expect=None, name=None, url_path=None):
    """Hit the server, then validate status + body against the spec. Marks the
    operation exercised. `expect` (optional int) additionally asserts the status.
    `url_path` overrides the URL path for templated ops (e.g. spec '/bindings/{id}',
    url '/bindings/42') while the spec lookup still uses `spec_path`."""
    op = _operation(method, spec_path)
    label = name or f"{method} {spec_path}" + (f"?{query}" if query else "")
    if op is None:
        check(f"{label} documented in openapi.yaml", False, "operation missing from spec")
        return None, None
    url = URL + (url_path or spec_path) + (f"?{query}" if query else "")
    req = urllib.request.Request(url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    try:
        r = urllib.request.urlopen(req, timeout=60)
        status, payload = r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        status, payload = e.code, json.loads(e.read() or b"{}")
    resp_spec = op.get("responses", {}).get(str(status))
    if resp_spec is None:
        check(f"{label} -> {status} documented", False,
              f"status {status} not in spec (body: {json.dumps(payload)[:120]})")
        return status, payload
    resp_spec = _resolve(resp_spec)
    schema = (resp_spec.get("content", {}).get("application/json", {}) or {}).get("schema")
    errs = validate(payload, schema) if schema else []
    ok = not errs and (expect is None or status == expect)
    check(f"{label} -> {status} matches spec", ok,
          (f"expected {expect}, " if (expect is not None and status != expect) else "") + "; ".join(errs[:3]))
    EXERCISED.add((method.upper(), spec_path))
    return status, payload


def cleanup(conn):
    with conn.cursor() as c:
        for ns in (NS, NSK, NS2):
            c.execute(f"SELECT id FROM {SCHEMA}.entities WHERE namespace=%s", (ns,))
            eids = [r["id"] for r in c.fetchall()]
            if eids:
                c.execute(f"DELETE FROM {SCHEMA}.mentions WHERE entity_id = ANY(%s)", (eids,))
            for t in ("edges", "semantic", "episodic", "entities", "raw_turns"):
                c.execute(f"DELETE FROM {SCHEMA}.{t} WHERE namespace=%s", (ns,))
            c.execute("DELETE FROM memnos_control.namespaces WHERE name=%s", (ns,))
            c.execute("DELETE FROM memnos_control.namespace_links WHERE src_ns=%s OR dst_ns=%s", (ns, ns))
            c.execute("DELETE FROM memnos_control.subscriptions WHERE namespace=%s", (ns,))
            c.execute("DELETE FROM memnos_control.corpus_sources WHERE namespace=%s", (ns,))
        for p in ("oapi_admin", "oapi_limited", "oapi_tmp"):
            c.execute("DELETE FROM memnos_control.api_tokens t USING memnos_control.principals pr "
                      "WHERE t.principal_id=pr.id AND pr.name=%s", (p,))
            c.execute("DELETE FROM memnos_control.grants g USING memnos_control.principals pr "
                      "WHERE g.principal_id=pr.id AND pr.name=%s", (p,))
            c.execute("DELETE FROM memnos_control.subscriptions s USING memnos_control.principals pr "
                      "WHERE s.principal_id=pr.id AND pr.name=%s", (p,))
            c.execute("DELETE FROM memnos_control.bindings b USING memnos_control.principals pr "
                      "WHERE b.principal_id=pr.id AND pr.name=%s", (p,))
            c.execute("DELETE FROM memnos_control.hosts h USING memnos_control.principals pr "
                      "WHERE h.principal_id=pr.id AND pr.name=%s", (p,))
            c.execute("DELETE FROM memnos_control.principals WHERE name=%s", (p,))


def main():
    conn = psycopg.connect(DSN, autocommit=True, row_factory=dict_row)
    Control.init(conn)
    store = BrainStore(conn=conn)
    store.create_schema("memnos")
    cleanup(conn)

    admin_id = Control.create_principal(conn, "oapi_admin", "service")
    Control.grant(conn, admin_id, "*")
    TADM = Control.mint_token(conn, admin_id, "contract")
    lim_id = Control.create_principal(conn, "oapi_limited", "agent")
    Control.grant(conn, lim_id, NS)                 # read+write on NS only (NOT NSK)
    TLIM = Control.mint_token(conn, lim_id, "contract-limited")

    print("=== OpenAPI contract: health ===")
    call("GET", "/healthz", expect=200)
    call("GET", "/readyz", expect=200)
    call("GET", "/metrics", token=TADM, expect=200)

    print("=== admin plane ===")
    call("POST", "/admin/api/namespaces", token=TADM, body={"name": NS, "description": "contract"}, expect=200)
    call("POST", "/admin/api/namespaces", token=TADM,
         body={"name": NSK, "kind": "knowledge"}, expect=200, name="POST /admin/api/namespaces (kind)")
    call("POST", "/admin/api/namespaces", token=TADM, body={"name": ""}, expect=400,
         name="POST /admin/api/namespaces (empty name)")
    call("GET", "/admin/api/namespaces", token=TADM, expect=200)
    call("GET", "/admin/api/namespaces", token=TLIM, expect=403, name="GET /admin/api/namespaces (non-admin)")
    call("GET", "/admin/api/namespaces", expect=401, name="GET /admin/api/namespaces (no token)")
    call("POST", "/admin/api/namespaces/kind", token=TADM, body={"name": NSK, "kind": "knowledge"}, expect=200)
    call("POST", "/admin/api/namespaces/links", token=TADM, body={"src": NS, "dst": NSK}, expect=200)
    call("GET", "/admin/api/namespaces/links", token=TADM, query="ns=" + NS, expect=200)
    call("POST", "/admin/api/principals", token=TADM, body={"name": "oapi_tmp", "kind": "agent"}, expect=200)
    st, pr = call("GET", "/admin/api/principals", token=TADM, expect=200)
    tmp_id = next(p["id"] for p in pr["principals"] if p["name"] == "oapi_tmp")
    call("POST", "/admin/api/tokens", token=TADM, body={"principal_id": tmp_id, "label": "x"}, expect=200)
    st, toks = call("GET", "/admin/api/tokens", token=TADM, query=f"principal={tmp_id}", expect=200)
    call("POST", "/admin/api/tokens/revoke", token=TADM, body={"id": toks["tokens"][0]["id"]}, expect=200)
    call("POST", "/admin/api/grants", token=TADM,
         body={"principal_id": tmp_id, "namespace": NS, "can_read": True, "can_write": False}, expect=200)
    call("GET", "/admin/api/grants", token=TADM, query=f"principal={tmp_id}", expect=200)
    call("DELETE", "/admin/api/grants", token=TADM,
         query=f"principal={tmp_id}&namespace={NS}", expect=200)
    call("GET", "/admin/api/stats", token=TADM, query="hours=24", expect=200)
    call("GET", "/admin/api/usage", token=TADM, expect=200)
    call("GET", "/admin/api/audit", token=TADM, query="limit=10&offset=0", expect=200)
    call("GET", "/admin/api/memory/feed", token=TADM, query="limit=10&offset=0", expect=200)
    call("GET", "/admin/api/memory/feed", token=TADM,
         query="limit=10&offset=0&namespace=" + NS.replace(":", "%3A") + "&type=constraint",
         expect=200, name="GET /admin/api/memory/feed (filters)")
    call("GET", "/admin/api/memory/feed", token=TADM, query="type=bogus", expect=400,
         name="GET /admin/api/memory/feed (unknown type)")
    call("GET", "/admin/api/memory/feed", token=TLIM, expect=403,
         name="GET /admin/api/memory/feed (non-admin)")
    call("GET", "/admin/api/health", token=TADM, expect=200)
    call("GET", "/admin/api/quality", token=TADM, expect=200)
    call("GET", "/admin/api/subscriptions", token=TADM, expect=200)
    call("POST", "/admin/api/deliver", token=TADM, body={}, expect=200)
    call("GET", "/admin/api/provider", token=TADM, expect=200)
    st, sec = call("GET", "/admin/api/secrets", token=TADM)   # 200 unlocked / 409 locked — both documented
    if st == 200 and (sec or {}).get("unlocked"):
        call("POST", "/admin/api/secrets", token=TADM,
             body={"name": "oapi_secret", "value": "v1"}, expect=200)
        call("DELETE", "/admin/api/secrets", token=TADM, query="name=oapi_secret", expect=200)
    else:                                                     # vault locked: still exercise the ops
        call("POST", "/admin/api/secrets", token=TADM, body={"name": "oapi_secret", "value": "v1"}, expect=409)
        call("DELETE", "/admin/api/secrets", token=TADM, query="name=oapi_secret", expect=409)

    print("=== data plane: memory ===")
    call("POST", "/remember", body={"namespace": NS, "text": "Ada moved to Lisbon in May 2026."},
         token=TADM, expect=200)
    call("POST", "/remember", body={"namespace": NS, "text": "Ada works at Acme as a data engineer.",
         "speaker": "user", "session_id": "s1", "async": True}, token=TADM, expect=200,
         name="POST /remember (async)")
    call("POST", "/remember", body={"namespace": NS, "text": "x"}, expect=401,
         name="POST /remember (no token)")
    call("POST", "/remember", body={"namespace": NSK, "text": "forbidden write"}, token=TLIM,
         expect=403, name="POST /remember (forbidden ns)")
    call("POST", "/remember", body={"namespace": NS}, token=TADM, expect=400,
         name="POST /remember (missing text)")
    call("POST", "/remember", body={"namespace": NS, "type": "wrongtype", "text": "x"},
         token=TADM, expect=400, name="POST /remember (unknown type)")
    call("POST", "/remember", body={"namespace": NS, "type": "constraint",
         "text": "All deploys MUST be approved by two engineers."}, token=TADM, expect=200,
         name="POST /remember (typed constraint)")
    call("POST", "/memory/write", body={"namespace": NS, "content": "Bob prefers tea over coffee.",
         "type": "decision"}, token=TADM, expect=200)
    # knowledge namespace content for grounded recall
    st, _ = call("POST", "/corpus/ingest", token=TADM, expect=200,
                 body={"namespace": NSK, "name": "arch.md", "kind": "doc",
                       "text": "Services MUST validate input. Tokens SHALL NOT be logged."})

    st, rec = call("POST", "/recall", token=TADM, body={"namespace": NS, "query": "where does Ada live?"},
                   expect=200)
    check("grounded recall reports grounded_in", rec is not None and rec.get("grounded_in") == [NSK])
    st, rec2 = call("POST", "/recall", token=TLIM, body={"namespace": NS, "query": "Ada"},
                    expect=200, name="POST /recall (limited token, link skipped)")
    check("link without read grant lands in links_skipped",
          rec2 is not None and rec2.get("links_skipped") == [NSK])
    call("POST", "/recall", token=TADM, body={"namespace": NS, "query": "Ada", "scope": "all"},
         expect=200, name="POST /recall (scope=all)")
    call("POST", "/recall", token=TADM, body={"namespace": NS}, expect=400,
         name="POST /recall (missing query)")
    st, recp = call("POST", "/recall", token=TADM,
                    body={"namespace": NS, "query": "completely unrelated topic",
                          "type": "decision", "constraint_cap": 5},
                    expect=200, name="POST /recall (type filter + constraint pinning)")
    pins = [m for m in (recp or {}).get("memories", []) if m.get("pinned")]
    check("constraint pinned despite unrelated query + type filter",
          pins and all(m.get("type") == "constraint" for m in pins))
    check("pinned constraints lead the context block",
          (recp or {}).get("context", "").startswith("CONSTRAINT:"))
    call("POST", "/recall", token=TADM,
         body={"namespace": NS, "query": "Ada", "type": "nope"}, expect=400,
         name="POST /recall (unknown type)")
    call("POST", "/memory/search", token=TADM, body={"namespace": NS, "query": "Ada"}, expect=200)
    call("POST", "/recall_v2", token=TADM, body={"namespace": NS, "query": "Ada"}, expect=200)

    # DEADLINE-AWARE recall (issue #12): an already-expired deadline must return 200
    # with best-available results + degraded:true (schema-validated), never an error.
    st, recd = call("POST", "/recall", token=TADM,
                    body={"namespace": NS, "query": "Ada", "deadline_ms": 1},
                    expect=200, name="POST /recall (deadline_ms expired -> degraded)")
    check("expired deadline_ms yields degraded:true", (recd or {}).get("degraded") is True)
    call("POST", "/recall", token=TADM,
         body={"namespace": NS, "query": "Ada", "deadline_ms": "soon"}, expect=400,
         name="POST /recall (non-integer deadline_ms)")

    # STALE-TURN annotation (issue #10 residual B): seed a turn whose only derived fact
    # is superseded — /recall must return that turn row with superseded:true +
    # superseded_at (schema-validated above) and label it in the context block.
    t_old = store.insert_raw_turn(SCHEMA, NS, None, "user",
                                  "The orbit launcher is blocked by a fuel pump failure.",
                                  "2026-06-08T00:00:00+00:00", None)
    f_new = store.insert_semantic(SCHEMA, NS, "fact", "The orbit launcher is cleared for launch.",
                                  subject="orbit launcher", predicate="status", obj="cleared",
                                  valid_from="2026-06-11T00:00:00+00:00")
    f_old = store.insert_semantic(SCHEMA, NS, "fact", "The orbit launcher is blocked by a fuel pump.",
                                  subject="orbit launcher", predicate="status", obj="blocked",
                                  valid_from="2026-06-08T00:00:00+00:00", source_turn_ids=[t_old])
    store.close_out(SCHEMA, NS, f_old, valid_to="2026-06-11T00:00:00+00:00", superseded_by=f_new)
    st, recs = call("POST", "/recall", token=TADM,
                    body={"namespace": NS, "query": "is the orbit launcher blocked by the fuel pump failure"},
                    expect=200, name="POST /recall (stale-turn annotation)")
    stale = [m for m in (recs or {}).get("memories", [])
             if m.get("kind") == "turn" and m.get("superseded")]
    check("stale turn row carries superseded:true + superseded_at",
          stale and stale[0].get("superseded_at") == "2026-06-11"
          and "fuel pump" in stale[0]["content"])
    check("context labels the stale turn '(said, superseded as of <date>)'",
          "- (said, superseded as of 2026-06-11)" in (recs or {}).get("context", ""))
    call("POST", "/memory/context", token=TADM, body={"namespace": NS, "query": "Ada", "max_chars": 500},
         expect=200)
    call("POST", "/consolidate", token=TADM, body={"namespace": NS}, expect=200)
    call("POST", "/feedback", token=TADM,
         body={"namespace": NS, "query": "Ada", "helpful": True, "note": "contract"}, expect=200)

    # seed a graph + facts directly (no LLM in CI) for graph/provenance/delete/reconcile
    ada = store.upsert_entity(SCHEMA, NS, "Ada")
    acme = store.upsert_entity(SCHEMA, NS, "Acme")
    store.bump_edge(SCHEMA, NS, ada, acme, 2.0)
    s1 = store.insert_semantic(SCHEMA, NS, "proposition", "Ada works at Acme",
                               subject="Ada", predicate="works_at", obj="Acme",
                               valid_from="2026-01-01")
    store.add_mention(SCHEMA, ada, s1, "semantic"); store.add_mention(SCHEMA, acme, s1, "semantic")

    print("=== data plane: graph ===")
    call("POST", "/entity", token=TADM, body={"namespace": NS, "name": "Ada", "depth": 2}, expect=200)
    call("POST", "/entity", token=TADM, body={"namespace": NS, "name": "Nobody"}, expect=404,
         name="POST /entity (unknown)")
    call("POST", "/provenance", token=TADM, body={"namespace": NS, "id": s1}, expect=200)
    call("POST", "/related", token=TADM, body={"namespace": NS, "name": "Ada"}, expect=200)
    call("POST", "/graph", token=TADM, body={"namespace": NS, "entities": ["Ada"], "hops": 2}, expect=200)
    call("POST", "/community", token=TADM, body={"namespace": NS, "name": "Ada"}, expect=200)
    call("POST", "/contradictions", token=TADM, body={"namespace": NS}, expect=200)
    call("POST", "/knowledge/health", token=TADM, body={"namespace": NS}, expect=200)
    call("POST", "/reconcile", token=TADM,
         body={"namespace": NS, "statement": "Ada works at Initech", "subject": "Ada",
               "predicate": "works_at"}, expect=200)

    print("=== data plane: episodes ===")
    call("POST", "/episode/segment", token=TADM, body={"namespace": NS, "gap_minutes": 30}, expect=200)
    with conn.cursor() as c:
        c.execute(f"SELECT id FROM {SCHEMA}.episodic WHERE namespace=%s ORDER BY id LIMIT 1", (NS,))
        row = c.fetchone()
    check("segmentation produced an episode", row is not None)
    if row:
        call("POST", "/episode", token=TADM, body={"namespace": NS, "id": row["id"]}, expect=200)
    call("POST", "/episode", token=TADM, body={"namespace": NS, "id": 999999999}, expect=404,
         name="POST /episode (unknown id)")
    call("POST", "/episode/recall", token=TADM, body={"namespace": NS, "query": "Ada", "k": 4}, expect=200)
    call("POST", "/episode/decay", token=TADM, body={"namespace": NS, "half_life_days": 30}, expect=200)

    print("=== data plane: corpus / files / copy / delete ===")
    call("POST", "/corpus/check", token=TADM,
         body={"namespace": NSK, "snippet": "def log_token(token): print(token)"}, expect=200)
    call("POST", "/corpus/list", token=TADM, body={"namespace": NSK}, expect=200)
    call("POST", "/ingest/file", token=TADM,
         body={"namespace": NS, "filename": "notes.md",
               "text": "Project kickoff notes.\n\nDecisions: use Postgres."}, expect=200)
    call("POST", "/namespace/copy", token=TADM, body={"namespace": NS2, "src": NS, "mode": "copy"},
         expect=200)
    call("POST", "/namespace/copy", token=TADM, body={"namespace": NS, "src": NS}, expect=400,
         name="POST /namespace/copy (src == dst)")
    call("POST", "/memory/delete", token=TADM, body={"namespace": NS, "id": s1}, expect=200)
    call("POST", "/memory/delete", token=TADM, body={"namespace": NS, "id": 999999999}, expect=404,
         name="POST /memory/delete (unknown id)")

    print("=== data plane: pubsub ===")
    st, sub = call("POST", "/subscribe", token=TADM, body={"namespace": NS}, expect=200)
    call("POST", "/remember", token=TADM,
         body={"namespace": NS, "text": "An event after subscribing, for the feed."},
         expect=200, name="POST /remember (feed seed)")
    call("POST", "/feed", token=TADM,
         body={"namespace": NS, "subscription_id": sub["subscription_id"]}, expect=200)
    call("POST", "/feed", token=TADM, body={"namespace": NS, "subscription_id": 999999999},
         expect=404, name="POST /feed (unknown sub)")
    call("POST", "/unsubscribe", token=TADM,
         body={"namespace": NS, "subscription_id": sub["subscription_id"]}, expect=200)

    print("=== bindings + hosts (user-scoped, issue #20) ===")
    call("GET", "/bindings", expect=401, name="GET /bindings (no token)")
    call("GET", "/bindings", token=TLIM, expect=200)
    call("GET", "/bindings/recap", token=TLIM, query="days=7", expect=200,
         name="GET /bindings/recap (write-health recap, issue #20)")
    st, hb = call("POST", "/hosts", token=TLIM, body={"machine_id": "oapi-host", "friendly_name": "OAPI"}, expect=200)
    call("GET", "/hosts", token=TLIM, expect=200)
    st, bb = call("POST", "/bindings", token=TLIM,
                  body={"key_type": "repo", "key": "github.com/oapi/x", "namespace": NS}, expect=200)
    call("POST", "/bindings", token=TLIM, body={"key_type": "bogus", "key": "k", "namespace": NS},
         expect=400, name="POST /bindings (bad key_type)")
    bid = (bb or {}).get("binding", {}).get("id")
    call("DELETE", "/bindings/{id}", url_path=f"/bindings/{bid}", token=TLIM, expect=200,
         name="DELETE /bindings/{id}")
    call("DELETE", "/bindings/{id}", url_path="/bindings/999999999", token=TLIM, expect=404,
         name="DELETE /bindings/{id} (not theirs)")

    print("=== admin cleanup ops (delete link + namespaces via API) ===")
    call("DELETE", "/admin/api/namespaces/links", token=TADM, query=f"src={NS}&dst={NSK}", expect=200)
    call("DELETE", "/admin/api/namespaces", token=TADM,
         query="purge=1&name=" + NS2.replace(":", "%3A"), expect=200)

    print("=== coverage: every spec operation exercised ===")
    spec_ops = {(m.upper(), p) for p, item in (SPEC.get("paths") or {}).items()
                for m in item if m in ("get", "post", "put", "delete", "patch")}
    missing = sorted(spec_ops - EXERCISED)
    check(f"all {len(spec_ops)} documented operations exercised", not missing,
          "not exercised: " + ", ".join(f"{m} {p}" for m, p in missing))

    cleanup(conn)
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
