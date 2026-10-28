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
    check("tommy core pulls in memnos only; every skill bundle is opt-in",
          json.load(open(os.path.join(ROOT, "plugins/tommy/.claude-plugin/plugin.json")))["dependencies"] == ["memnos"])
    check("opt-in bundles exist for engineering, review, architecture, design review, QA and security",
          {"tommy-engineering", "tommy-review", "tommy-architect", "tommy-design-review", "tommy-qa", "tommy-security"} <= set(local))
    tv = re.search(r'^version = "([^"]+)"', open(os.path.join(ROOT, "agents/tommy/pyproject.toml")).read(), re.M).group(1)
    pinned = {n: json.load(open(os.path.join(ROOT, p["source"], ".claude-plugin", "plugin.json"))).get("version")
              for n, p in local.items() if n.startswith("tommy")}
    check(f"every tommy plugin pins version == tommy-orchestrator {tv}", all(v == tv for v in pinned.values()), pinned)

    print("=== upstream plugins are pinned, attributed, never copied ===")
    for n, p in upstream.items():
        s = p["source"]
        check(f"'{n}': github source (git-subdir or whole repo), full 40-hex sha",
              ((s["source"] == "git-subdir" and s["url"].startswith("https://github.com/") and s.get("path"))
               or (s["source"] == "github" and "/" in s.get("repo", "")))
              and re.fullmatch(r"[0-9a-f]{40}", s.get("sha", "")), s)
        check(f"'{n}': licence declared and upstream linked", bool(p.get("license")) and "github.com" in p.get("homepage", ""))
        check(f"'{n}': entry declares no hooks (upstream hooks were read by hand at this sha: only fp-check has one, and it is excluded)", "hooks" not in p)
    sa = [n for n, p in upstream.items() if "SA" in p.get("license", "").upper().split("-")]
    check("share-alike (CC-BY-SA) plugins exist only as upstream sources, so nothing is vendored here",
          all(n in upstream for n in sa) and not any(
              "SA" in local[n].get("license", "").upper().split("-") for n in local))
    own = {"tommy-design-review", "tommy-qa"}  # the only plugins whose skills Tommy authors itself
    vendored = [d for d, _, fs in os.walk(os.path.join(ROOT, "plugins")) for f in fs
                if f == "SKILL.md" and os.path.relpath(d, os.path.join(ROOT, "plugins")).split(os.sep)[0] not in own]
    check("no upstream SKILL.md is vendored under plugins/ (only Tommy-authored skills live here)", not vendored, vendored)
    for n in own:
        for d in sorted(os.listdir(os.path.join(ROOT, "plugins", n, "skills"))):
            t = open(os.path.join(ROOT, "plugins", n, "skills", d, "SKILL.md")).read()
            check(f"{n}/{d}: frontmatter name matches dir and description is set",
                  f"\nname: {d}\n" in t and "\ndescription: " in t.split("---")[1])
    check("no pentest plugin is bundled (needs a gated design first)", not [n for n in names if "pentest" in n])

    if shutil.which("claude"):
        r = subprocess.run(["claude", "plugin", "validate", ROOT], capture_output=True, text=True, timeout=90)
        check("`claude plugin validate` passes", r.returncode == 0, r.stdout[-300:] + r.stderr[-300:])
    else:
        print("  SKIP  claude CLI not installed — `claude plugin validate` not run")
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
