"""The memnos marketplace (memnos + Tommy + curated upstream plugins): structure and pinning rules.

Pure JSON checks, no network. The live proof (one `claude plugin install tommy@memnos` pulling every
dependency at its pinned commit) needs a real `claude` and is run by hand; see the PR.

Run: python tests/test_marketplace.py
"""
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  — {detail}" if (detail and not cond) else ""))
    PASS += bool(cond)
    FAIL += (not cond)


def main():
    m = json.load(open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")))
    plugins = m["plugins"]
    names = [p["name"] for p in plugins]
    local = {p["name"]: p for p in plugins if isinstance(p["source"], str)}
    upstream = {p["name"]: p for p in plugins if isinstance(p["source"], dict)}

    print("=== marketplace entries ===")
    check("names are unique", len(names) == len(set(names)))
    check("memnos is listed first (README install line)", names[0] == "memnos")
    for n, p in local.items():
        check(f"local plugin '{n}' has a manifest with the same name",
              json.load(open(os.path.join(ROOT, p["source"], ".claude-plugin", "plugin.json")))["name"] == n)

    print("=== every dependency resolves inside this marketplace ===")
    for n, p in local.items():
        deps = json.load(open(os.path.join(ROOT, p["source"], ".claude-plugin", "plugin.json"))).get("dependencies", [])
        missing = [d for d in deps if d not in names]
        check(f"'{n}' dependencies all listed ({len(deps)})", not missing, missing)
    check("tommy pulls in memnos and the three role stacks",
          set(json.load(open(os.path.join(ROOT, "plugins/tommy/.claude-plugin/plugin.json")))["dependencies"])
          == {"memnos", "tommy-architect", "tommy-review", "tommy-security"})

    print("=== upstream plugins are pinned, attributed, never copied ===")
    for n, p in upstream.items():
        s = p["source"]
        check(f"'{n}': git-subdir from github, full 40-hex sha, path set",
              s["source"] == "git-subdir" and s["url"].startswith("https://github.com/")
              and re.fullmatch(r"[0-9a-f]{40}", s.get("sha", "")) and s.get("path"), s)
        check(f"'{n}': licence declared and upstream linked", bool(p.get("license")) and "github.com" in p.get("homepage", ""))
        check(f"'{n}': entry declares no hooks (upstream hooks were read by hand at this sha: only fp-check has one, and it is excluded)", "hooks" not in p)
    sa = [n for n, p in upstream.items() if "SA" in p.get("license", "").upper().split("-")]
    check("share-alike (CC-BY-SA) plugins exist only as upstream sources, so nothing is vendored here",
          all(n in upstream for n in sa) and not any(
              "SA" in local[n].get("license", "").upper().split("-") for n in local))
    vendored = [d for d, _, fs in os.walk(os.path.join(ROOT, "plugins")) for f in fs if f == "SKILL.md"]
    check("no SKILL.md is vendored under plugins/ (all curated skills come from upstream)", not vendored, vendored)

    if shutil.which("claude"):
        r = subprocess.run(["claude", "plugin", "validate", ROOT], capture_output=True, text=True, timeout=90)
        check("`claude plugin validate` passes", r.returncode == 0, r.stdout[-300:] + r.stderr[-300:])
    else:
        print("  SKIP  claude CLI not installed — `claude plugin validate` not run")
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
