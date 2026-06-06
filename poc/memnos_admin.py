"""memnos admin CLI — bootstrap identity, tokens, and namespace grants.

  python memnos_admin.py init
  python memnos_admin.py principal <name> [--kind user|agent|service]
  python memnos_admin.py token <principal> [--label L] [--ttl-days N]      # prints token ONCE
  python memnos_admin.py grant <principal> <namespace> [--read-only]        # supports 'team:eng:*' / '*'
  python memnos_admin.py whoami <namespace> <token>                         # test auth+ACL
  python memnos_admin.py usage [--limit 20]                                 # cost ledger
  python memnos_admin.py audit [--limit 20]
"""
import argparse
import os
import sys

sys.path.insert(0, ".")
import psycopg
from psycopg.rows import dict_row
from memnos_brain.control import Control

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos_poc@localhost:5433/memnos")


def conn():
    return psycopg.connect(DSN, autocommit=True, row_factory=dict_row)


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("principal"); p.add_argument("name"); p.add_argument("--kind", default="user")
    p = sub.add_parser("token"); p.add_argument("principal"); p.add_argument("--label"); p.add_argument("--ttl-days", type=int)
    p = sub.add_parser("grant"); p.add_argument("principal"); p.add_argument("namespace"); p.add_argument("--read-only", action="store_true")
    p = sub.add_parser("whoami"); p.add_argument("namespace"); p.add_argument("token")
    p = sub.add_parser("usage"); p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("audit"); p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("stats"); p.add_argument("--hours", type=int, default=24)
    p = sub.add_parser("errors"); p.add_argument("--hours", type=int, default=24); p.add_argument("--limit", type=int, default=20)
    p = sub.add_parser("quality"); p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("health"); p.add_argument("--hours", type=int, default=24)
    args = ap.parse_args()

    c = conn()
    if args.cmd == "init":
        Control.init(c); print("control plane initialized")
    elif args.cmd == "principal":
        pid = Control.create_principal(c, args.name, args.kind); print(f"principal '{args.name}' id={pid}")
    elif args.cmd == "token":
        with c.cursor() as cur:
            cur.execute("SELECT id FROM memnos_control.principals WHERE name=%s", (args.principal,))
            r = cur.fetchone()
        if not r:
            sys.exit(f"no principal '{args.principal}'")
        tok = Control.mint_token(c, r["id"], args.label, args.ttl_days)
        print(f"TOKEN (store now, shown once):\n  {tok}")
    elif args.cmd == "grant":
        with c.cursor() as cur:
            cur.execute("SELECT id FROM memnos_control.principals WHERE name=%s", (args.principal,))
            r = cur.fetchone()
        if not r:
            sys.exit(f"no principal '{args.principal}'")
        Control.grant(c, r["id"], args.namespace, can_read=True, can_write=not args.read_only)
        print(f"granted {args.principal} -> {args.namespace} ({'read' if args.read_only else 'read+write'})")
    elif args.cmd == "whoami":
        pid = Control.authenticate(c, args.token)
        if pid is None:
            print("auth: FAIL (invalid/revoked/expired token)"); return
        r = Control.authorize(c, pid, args.namespace, write=False)
        w = Control.authorize(c, pid, args.namespace, write=True)
        print(f"auth OK principal_id={pid}; namespace '{args.namespace}': read={r} write={w}")
        print("grants:", [(g["namespace"], g["can_read"], g["can_write"]) for g in Control.authorized_namespaces(c, pid)])
    elif args.cmd == "usage":
        with c.cursor() as cur:
            cur.execute("SELECT op, count(*) n, round(sum(cost_usd),4) cost FROM memnos_control.usage_ledger "
                        "GROUP BY op ORDER BY cost DESC NULLS LAST")
            print("usage by op:"); [print(f"  {r['op']:<12} {r['n']:>5} calls  ${r['cost'] or 0}") for r in cur.fetchall()]
    elif args.cmd == "audit":
        with c.cursor() as cur:
            cur.execute("SELECT ts, principal_id, action, namespace, ok FROM memnos_control.audit_log "
                        "ORDER BY ts DESC LIMIT %s", (args.limit,))
            print("recent audit:"); [print(f"  {r['ts']:%H:%M:%S} p{r['principal_id']} {r['action']} {r['namespace']} ok={r['ok']}") for r in cur.fetchall()]
    elif args.cmd == "stats":
        rows = Control.stats(c, args.hours)
        print(f"=== memnos reliability (last {args.hours}h) ===")
        print(f"  {'op':<12}{'calls':>7}{'err%':>7}{'p50ms':>8}{'p95ms':>8}{'recall_empty%':>15}")
        for r in rows:
            print(f"  {r['action']:<12}{r['calls']:>7}{(r['error_pct'] or 0):>7}{(r['p50_ms'] or 0):>8}"
                  f"{(r['p95_ms'] or 0):>8}{('' if r['recall_empty_pct'] is None else r['recall_empty_pct']):>15}")
    elif args.cmd == "errors":
        rows = Control.recent_errors(c, args.hours, args.limit)
        print(f"=== failures (last {args.hours}h) — actionable detail ===")
        if not rows:
            print("  (none)")
        for r in rows:
            d = r["detail"] or {}
            what = d.get("error") or d.get("reason") or "?"
            print(f"  {r['ts']:%m-%d %H:%M} p{r['principal_id']} {r['action']} ns={r['namespace']} "
                  f"status={r['status']} :: {what} {d.get('msg','')}")
    elif args.cmd == "quality":
        tr = Control.eval_trend(c, "stale_suppression", "rate", args.limit)
        print("=== quality canary: stale-suppression trend (target ~0.85) ===")
        if not tr:
            print("  (no eval runs yet — run memnos_eval.py)")
        for r in tr:
            print(f"  {r['ts']:%m-%d %H:%M}  suppress={float(r['value']):.0%}  (n={r['n']})")
    elif args.cmd == "health":
        print(f"=== memnos health (last {args.hours}h) ===")
        for level, msg in Control.health(c, args.hours):
            print(f"  [{level}] {msg}")


if __name__ == "__main__":
    main()
