"""Namespace resolution for memnos Claude-Code integration — PACKAGED so it works after a
`pipx install` (no repo paths). Used by `memnos hook recall|remember`, `memnos mcp`, and the
/memnos slash command. Resolution order (highest first):

  1. Override file ~/.memnos/ns_overrides.json — keyed by git-root (set via `/memnos ns=...`).
  2. MEMNOS_NS env, if set to anything other than 'auto'.
  3. 'proj:<git-repo-name>'.
  4. 'proj:<cwd-basename>'.
"""
import json
import os
import subprocess

_OVR = os.path.join(os.path.expanduser("~"), ".memnos", "ns_overrides.json")


def _git_root(cwd=None):
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd or os.getcwd(),
                           capture_output=True, text=True, timeout=3).stdout.strip()
        return r or None
    except Exception:
        return None


def resolve(data=None):
    cwd = (data or {}).get("cwd") or os.getcwd()
    root = _git_root(cwd)
    try:
        m = json.load(open(_OVR))
        for k in (cwd, os.path.realpath(cwd), root):
            if k and m.get(k):
                return m[k]
    except Exception:
        pass
    env = os.environ.get("MEMNOS_NS", "").strip()
    if env and env.lower() != "auto":
        return env
    return "proj:" + (os.path.basename(root) if root else (os.path.basename(cwd.rstrip("/")) or "default"))


def set_override(namespace, cwd=None):
    cwd = cwd or os.getcwd()
    key = _git_root(cwd) or cwd
    os.makedirs(os.path.dirname(_OVR), exist_ok=True)
    try:
        m = json.load(open(_OVR))
    except Exception:
        m = {}
    if not namespace or namespace.lower() in ("clear", "none", "auto"):
        m.pop(key, None)
        action = f"namespace override cleared for {key}"
    else:
        m[key] = namespace
        action = f"namespace for {key} set to {namespace}"
    json.dump(m, open(_OVR, "w"), indent=2)
    return action
