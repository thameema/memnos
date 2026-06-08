"""Resolve the memnos namespace for the CURRENT project/session.

Order (highest first):
  1. Override file ~/.memnos/ns_overrides.json — keyed by git-root (or cwd). Set this with
     the `/memnos ns=<namespace>` slash command. Assign a folder to a namespace at runtime
     without restarting.
  2. MEMNOS_NS env, if set to anything other than 'auto' (e.g. `MEMNOS_NS=team:eng claude`).
  3. 'proj:<git-repo-name>'  (each repo gets isolated memory automatically).
  4. 'proj:<cwd-basename>'.

The hook token must be GRANTED the resolved namespace, else reads/writes are ACL-denied and
the hooks fail open. A `proj:*` / wildcard grant covers the per-project pattern.
"""
import json
import os
import subprocess

_OVR = os.path.join(os.path.expanduser("~"), ".memnos", "ns_overrides.json")


def _git_root(cwd):
    try:
        r = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=3).stdout.strip()
        return r or None
    except Exception:
        return None


def _override(cwd):
    try:
        m = json.load(open(_OVR))
    except Exception:
        return None
    for key in (cwd, os.path.realpath(cwd), _git_root(cwd)):
        if key and m.get(key):
            return m[key]
    return None


def resolve(data=None):
    cwd = (data or {}).get("cwd") or os.getcwd()
    ov = _override(cwd)
    if ov and ov.lower() != "auto":
        return ov
    ns = os.environ.get("MEMNOS_NS", "").strip()
    if ns and ns.lower() != "auto":
        return ns
    root = _git_root(cwd)
    if root:
        return "proj:" + os.path.basename(root)
    return "proj:" + (os.path.basename(cwd.rstrip("/")) or "default")


def set_override(namespace, cwd=None):
    """Pin (or clear, if namespace is falsy/'clear'/'auto') the namespace for a folder
    (keyed by its git-root). Used by the /memnos slash command."""
    cwd = cwd or os.getcwd()
    key = _git_root(cwd) or cwd
    os.makedirs(os.path.dirname(_OVR), exist_ok=True)
    try:
        m = json.load(open(_OVR))
    except Exception:
        m = {}
    if not namespace or namespace.lower() in ("clear", "none", "auto"):
        m.pop(key, None)
        action = f"cleared (back to default) for {key}"
    else:
        m[key] = namespace
        action = f"set to {namespace} for {key}"
    json.dump(m, open(_OVR, "w"), indent=2)
    return action


if __name__ == "__main__":
    import sys
    print(set_override(sys.argv[1]) if len(sys.argv) > 1 else resolve())
