"""Resolve the memnos namespace for the CURRENT project.

Order:
  1. MEMNOS_NS env, if set to anything other than 'auto'  (explicit override —
     e.g. a project's .claude/settings.json can pin its own namespace)
  2. 'proj:<git-repo-name>'  (so each repo gets isolated memory automatically)
  3. 'proj:<cwd-basename>'
Pair with a one-time wildcard grant: `python memnos_admin.py grant <principal> 'proj:*'`.
"""
import os
import subprocess


def resolve(data=None):
    ns = os.environ.get("MEMNOS_NS", "").strip()
    if ns and ns.lower() != "auto":
        return ns
    cwd = (data or {}).get("cwd") or os.getcwd()
    try:
        root = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=3).stdout.strip()
        if root:
            return "proj:" + os.path.basename(root)
    except Exception:
        pass
    return "proj:" + (os.path.basename(cwd.rstrip("/")) or "default")


if __name__ == "__main__":
    print(resolve())
