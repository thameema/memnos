"""MCP stdio schema parity gate between a reference MCP adapter and the compiled `memnos mcp` binary.

Verifies:
1. Identical 55 tool names (zero missing or renamed tools in either direction).
2. Identical `required` parameter sets on all 55 tools.
3. Every parameter defined in the reference `inputSchema` exists with a compatible JSON-Schema type
   in the compiled `memnos mcp` `inputSchema`.

Run:
    MEMNOS_BIN=./target/release/memnos python3 tests/test_mcp_schema_parity.py
"""
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANDIDATE_BIN = os.environ.get("MEMNOS_BIN", os.path.join(ROOT, "target", "release", "memnos"))
REF_MCP_CMD = os.environ.get("MEMNOS_REF_MCP_CMD", "")


def _list_tools(cmd: list[str]) -> dict[str, dict]:
    env = dict(os.environ)
    env["MEMNOS_MCP_AUTO_REGISTER"] = "0"
    env["MEMNOS_ADAPTER_REEXEC"] = "0"
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    payload = "\n".join(
        json.dumps(m)
        for m in [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-parity-test", "version": "1.0"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ]
    ) + "\n"
    out, _ = proc.communicate(payload, timeout=20)
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("id") == 2:
            return {t["name"]: t["inputSchema"] for t in msg["result"]["tools"]}
    raise RuntimeError(f"no tools/list response from {cmd}: {out!r}")


def _types_of(prop_schema: dict) -> set[str]:
    out = set()
    t = prop_schema.get("type")
    if isinstance(t, str):
        out.add(t)
    elif isinstance(t, list):
        out.update(t)
    for branch in prop_schema.get("anyOf") or []:
        if isinstance(branch, dict) and isinstance(branch.get("type"), str):
            out.add(branch["type"])
    return out


def main() -> int:
    ref_cmd = REF_MCP_CMD.split() if REF_MCP_CMD else [sys.executable, os.path.join(ROOT, "memnos_mcp.py")]
    if not os.path.isfile(ref_cmd[-1]):
        which_memnos = shutil.which("memnos")
        if which_memnos and os.path.abspath(which_memnos) != os.path.abspath(CANDIDATE_BIN):
            ref_cmd = [which_memnos, "mcp"]
    ref_tools = _list_tools(ref_cmd)
    cand_tools = _list_tools([CANDIDATE_BIN, "mcp"])

    failures = []
    if set(ref_tools) != set(cand_tools):
        only_ref = sorted(set(ref_tools) - set(cand_tools))
        only_cand = sorted(set(cand_tools) - set(ref_tools))
        failures.append(f"tool name set mismatch: only_ref={only_ref}, only_candidate={only_cand}")

    for name in sorted(set(ref_tools) & set(cand_tools)):
        ps, cs = ref_tools[name], cand_tools[name]
        p_req = sorted(ps.get("required") or [])
        c_req = sorted(cs.get("required") or [])
        if p_req != c_req:
            failures.append(f"{name}: required mismatch ref={p_req} candidate={c_req}")
        p_props = ps.get("properties") or {}
        c_props = cs.get("properties") or {}
        for pname, pdef in p_props.items():
            if pname not in c_props:
                failures.append(f"{name}: missing reference property '{pname}' in inputSchema")
                continue
            pt = _types_of(pdef)
            ct = _types_of(c_props[pname])
            if pt and not pt.issubset(ct):
                failures.append(f"{name}.{pname}: type mismatch ref={sorted(pt)} candidate={sorted(ct)}")

    total = len(ref_tools)
    if failures:
        for f in failures:
            print("  FAIL ", f)
        print(f"\n{total - len(failures)}/{total} clean, {len(failures)} schema diff(s)")
        return 1
    print(f"PASS: {total}/{total} MCP tools match reference names, required args, and property types (0 diffs)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
