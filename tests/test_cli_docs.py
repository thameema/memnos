"""CLI docs staleness gate: regenerates the CLI reference from the live argparse tree
(`memnos docs-gen --check`) and fails if the committed docs/cli.md or
ui/cli-reference.json no longer match — so a CLI change can't merge without its docs.

Also asserts the legacy alias forms still PARSE (the field-compat guarantee):
`memnos principal <name>` / `memnos token <principal>` / `memnos grant <p> <ns>`.

No DB, no server: pure parse + file comparison. Run: python tests/test_cli_docs.py
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = sys.executable
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond); FAIL += (not cond)


def main():
    sys.path.insert(0, ROOT)
    import memnos_cli

    print("=== docs staleness (docs-gen --check) ===")
    r = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), "docs-gen", "--check"],
                       capture_output=True, text=True, timeout=120)
    check("docs/cli.md + ui/cli-reference.json match the argparse tree",
          r.returncode == 0, (r.stdout + r.stderr).strip()[:300])

    print("=== generated artifacts are sane ===")
    md = open(os.path.join(ROOT, "docs", "cli.md"), encoding="utf-8").read()
    check("cli.md documents the new grammar", all(
        f"### `memnos {c}`" in md for c in
        ("remember", "recall", "principal create", "principal ls", "token mint",
         "token ls", "token revoke", "grant add", "grant ls", "grant rm",
         "namespace", "secret")))
    check("cli.md has the Remote use section", "## Remote use" in md and "MEMNOS_URL" in md)
    ref = json.load(open(os.path.join(ROOT, "ui", "cli-reference.json"), encoding="utf-8"))
    cmds = [c["command"] for g in ref["groups"] for c in g["commands"]]
    check("cli-reference.json mirrors the tree", "token mint" in cmds and "remember" in cmds)
    check("hidden docs-gen excluded from docs",
          "docs-gen" not in cmds and "### `memnos docs-gen`" not in md)

    print("=== legacy alias forms still parse (field compat) ===")
    ap = memnos_cli.build_parser()
    cases = [
        (["principal", "olduser", "--kind", "agent"], "principal", "create"),
        (["token", "olduser", "--label", "x"], "token", "mint"),
        (["grant", "olduser", "ns:x", "--read-only"], "grant", "add"),
        (["principal", "create", "newuser"], "principal", "create"),
        (["token", "ls", "newuser"], "token", "ls"),
        (["grant", "rm", "newuser", "ns:x"], "grant", "rm"),
    ]
    for argv, cmd, verb in cases:
        try:
            args = ap.parse_args(memnos_cli._normalize_argv(argv))
            ok = args.cmd == cmd and getattr(args, "verb", None) == verb
        except SystemExit:
            ok = False
        check(f"memnos {' '.join(argv)}  → {cmd} {verb}", ok)

    print("=== every public subcommand renders --help (parse-only) ===")
    bad = []
    for g in ref["groups"]:
        for c in g["commands"]:
            argv = c["command"].split() + ["--help"]
            r = subprocess.run([PY, os.path.join(ROOT, "memnos_cli.py"), *argv],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0 or "usage:" not in r.stdout:
                bad.append(c["command"])
    check(f"--help works for all {sum(len(g['commands']) for g in ref['groups'])} documented commands",
          not bad, "failed: " + ", ".join(bad))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
