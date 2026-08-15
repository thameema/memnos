"""
Tommy MCP stdio server.

Invoked by editors (Cursor, Claude Desktop, VS Code+Continue, Zed) as:

    tommy --mcp

Tommy reads MCP JSON-RPC from stdin and writes to stdout.  The editor owns
the process — Tommy never opens a port and never runs as a daemon.

Eight tools:
  tommy_recall          — query memnos memory
  tommy_remember        — persist a fact to memnos
  tommy_dispatch        — launch a harness task (async by default)
  tommy_status          — check a running task's output / exit code
  tommy_control         — send wrap_up / abort / pivot / answer to a running task
  tommy_switch_project  — set the active project context
  tommy_route           — dry-run: which harness would Tommy pick?
  tommy_list_harnesses  — available harnesses + health + active routing
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP

from .config import TommyConfig, ProjectEntry
from .control import ControlServer
from .discovery.harnesses import all_harnesses, apply_skip_permissions, apply_session_name
from .prompt import build_prompt


# ---------------------------------------------------------------------------
# In-process task registry (lives for the lifetime of this stdio process)
# ---------------------------------------------------------------------------

@dataclass
class Task:
    task_id: str
    harness: str
    proc: subprocess.Popen
    output_lines: list = field(default_factory=list)
    _lock: object = field(default_factory=threading.Lock, repr=False)
    _drain_thread: Optional[threading.Thread] = field(default=None, repr=False)
    _ctrl: Optional[ControlServer] = field(default=None, repr=False)

    def status(self) -> str:
        rc = self.proc.poll()
        if rc is None:
            return "running"
        return "done" if rc == 0 else f"failed (exit {rc})"

    def tail(self, n: int = 50) -> str:
        with self._lock:
            lines = self.output_lines[-n:]
        return "\n".join(lines)


_tasks: dict = {}       # task_id -> Task; capped at _TASK_CAP entries
_TASK_CAP = 100         # oldest completed tasks are evicted when limit is hit
_active_project: Optional[str] = None
_cfg: Optional[TommyConfig] = None


def _get_cfg() -> TommyConfig:
    global _cfg
    if _cfg is None:
        _cfg = TommyConfig.load()
    return _cfg

def _evict_tasks() -> None:
    """
    Keep _tasks at or below _TASK_CAP entries.
    Eviction order: oldest completed first, then oldest running if still over cap.
    dict insertion order (Python 3.7+) is used as a proxy for age.
    """
    if len(_tasks) <= _TASK_CAP:
        return
    # Separate completed from running, preserving insertion order
    completed = [tid for tid, t in _tasks.items() if t.status() != "running"]
    running   = [tid for tid, t in _tasks.items() if t.status() == "running"]
    evict_order = completed + running          # evict completed before killing running
    to_remove = len(_tasks) - _TASK_CAP
    for tid in evict_order[:to_remove]:
        del _tasks[tid]



def _effective_namespace(cfg: TommyConfig) -> str:
    """Return the memnos namespace for the active project, or the default."""
    if _active_project:
        proj = cfg.project_by_key(_active_project)
        if proj:
            return getattr(proj, "namespace", cfg.default_ns)
    return cfg.default_ns


def _memnos_client(cfg: TommyConfig):
    """Return a MemnosClient or None if memnos is unreachable."""
    try:
        from memnos_sdk import MemnosClient  # type: ignore
        client = MemnosClient(
            base_url=cfg.memnos_url,
            token=cfg.memnos_token,
            namespace=_effective_namespace(cfg),
        )
        if client.healthy():
            return client
    except Exception:
        pass
    return None


def _drain_stdout(proc: subprocess.Popen, task: Task, prompt_file: str = "") -> None:
    """Background thread: drain proc stdout into task.output_lines.
    Cleans up the temp prompt file once the process exits.
    """
    try:
        for line in proc.stdout:
            decoded = line.decode(errors="replace").rstrip()
            with task._lock:
                task.output_lines.append(decoded)
    except Exception:
        pass
    finally:
        # Clean up the temp prompt file that was created with delete=False
        if prompt_file:
            try:
                import os as _os
                _os.unlink(prompt_file)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# FastMCP server
# ---------------------------------------------------------------------------

mcp = FastMCP(
    name="tommy",
    instructions=(
        "Tommy is your memnos-native coding orchestrator. "
        "Use tommy_dispatch to start coding tasks, tommy_recall to retrieve "
        "persistent memory, and tommy_remember to save decisions across sessions."
    ),
)


@mcp.tool()
def tommy_recall(
    query: str,
    namespace: str = "",
    limit: int = 10,
) -> str:
    """
    Query Tommy's persistent memory (memnos) for context relevant to the
    current task.  Returns ranked fragments with provenance.

    Args:
        query:     Natural language query.
        namespace: memnos namespace, e.g. 'org:engineering'. Omit for
                   the current project namespace.
        limit:     Max fragments to return (default 10).
    """
    cfg = _get_cfg()
    client = _memnos_client(cfg)
    if client is None:
        return "memnos unreachable — no memory available."
    try:
        ns = namespace or _effective_namespace(cfg)
        result = client.recall(query, namespace=ns, fact_quota=limit)
        context = result.get("context", "")
        if not context:
            return f"No memories found for: {query!r}"
        return context
    except Exception as exc:
        return f"memnos recall error: {exc}"


@mcp.tool()
def tommy_remember(
    content: str,
    namespace: str = "",
    kind: str = "fact",
) -> str:
    """
    Persist a fact, decision, or constraint to Tommy's memory so it survives
    across sessions and editors.

    Args:
        content:   The fact to store (plain language).
        namespace: Target namespace. Omit for the current project namespace.
        kind:      One of: fact, constraint, decision, learning.
    """
    cfg = _get_cfg()
    client = _memnos_client(cfg)
    if client is None:
        return "memnos unreachable — memory not saved."
    try:
        ns = namespace or _effective_namespace(cfg)
        # SDK doesn't support memory_type — prefix kind into text for searchability
        text = f"[{kind}] {content}" if kind and kind != "fact" else content
        client.remember(text, namespace=ns)
        return f"Saved to {ns} ({kind})"
    except Exception as exc:
        return f"memnos remember error: {exc}"


@mcp.tool()
def tommy_dispatch(
    task: str,
    harness: str = "auto",
    workspace: str = "",
    async_run: bool = True,
    inject_memory: bool = True,
) -> dict:
    """
    Dispatch a coding task to the best available harness (Claude Code, Codex,
    etc.).  Returns a task handle immediately when async_run is True.

    The launched harness receives the same core.md coordinator system prompt
    (thin-coordinator identity, leases, corpus_check, wave-based fan-out —
    see tommy/prompts/core.md) that the interactive `tommy` CLI injects, via
    the same tommy.prompt.build_prompt() loader, plus a non-interactive
    framing note and this task appended as the final layer.

    Args:
        task:         Task description / full prompt.
        harness:      Which harness: 'auto', 'claude', 'codex', etc.
        workspace:    Absolute path for the harness to run in.
        async_run:    Return task_id immediately (default True).
        inject_memory: Enrich prompt with memnos recall before dispatch.
    """
    cfg = _get_cfg()
    harnesses = all_harnesses()

    chosen = harness if harness != "auto" else cfg.harness
    spec = harnesses.get(chosen)
    if spec is None or not spec.available:
        available = [n for n, s in harnesses.items() if s.available]
        return {"error": f"Harness '{chosen}' not available. Available: {available or ['none']}"}

    ws_path = Path(workspace) if workspace else Path.cwd()
    if _active_project:
        proj = cfg.project_by_key(_active_project)
        if proj and not workspace:
            ws_path = Path(getattr(proj, "git_root", str(Path.cwd())))

    # Optionally inject memnos context
    task_with_memory = task
    if inject_memory:
        client = _memnos_client(cfg)
        if client:
            try:
                recall_result = client.recall(task, fact_quota=5)
                ctx_text = recall_result.get("context", "")
                if ctx_text:
                    task_with_memory = f"## Context from memory\n{ctx_text}\n\n---\n\n{task}"
            except Exception:
                pass

    # Same coordinator prompt the interactive CLI builds (core.md -> org ->
    # project -> workspace-local -> runtime config -> MCP manifest), via the
    # same build_prompt() helper cli.py's _launch_harness() calls — not a
    # reimplementation — plus the dispatched task as build_prompt()'s final
    # layer, since there's no live human turn to type it on this path.
    # `chosen` (not cfg.harness) drives the runtime-config block's "Active
    # harness" line so it reflects what's actually being launched even when
    # the caller overrides the default harness via the `harness` argument.
    prompt_cfg = replace(cfg, harness=chosen)
    full_prompt = build_prompt(prompt_cfg, project_key=_active_project, task=task_with_memory)

    tf = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", prefix="tommy-mcp-", delete=False
    )
    tf.write(full_prompt)
    tf.flush()
    tf.close()
    _tf_path = tf.name  # capture for cleanup after proc exits

    cmd = [part.replace("{prompt_file}", _tf_path) for part in spec.launch_template]
    cmd = apply_skip_permissions(cmd, chosen, cfg.skip_permissions)
    _mcp_session_name = f"Tommy | {_active_project.upper()}" if _active_project else "Tommy"
    cmd = apply_session_name(cmd, chosen, _mcp_session_name)
    env = os.environ.copy()
    env["MEMNOS_URL"] = cfg.memnos_url
    env["TOMMY_NS"] = cfg.tommy_ns

    task_id = uuid.uuid4().hex[:8]

    # Control channel: lets Tommy send wrap_up / abort / pivot mid-run.
    # Use a ref-cell so _ctrl_msg doesn't capture `t` before Task() is constructed
    # (harness can connect and send messages between ControlServer() and Task()).
    _task_ref: list = [None]

    def _ctrl_msg(msg: dict) -> None:
        task_obj = _task_ref[0]
        if task_obj is None:
            return  # message arrived before Task was constructed — safe to drop
        with task_obj._lock:
            task_obj.output_lines.append(f"[ctrl:{msg.get('type','')}] {msg}")

    ctrl = ControlServer(on_message=_ctrl_msg, connect_timeout=30.0)
    env["TOMMY_CTRL_PORT"] = str(ctrl.port)

    proc = subprocess.Popen(
        cmd,
        cwd=str(ws_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,   # decouple from Tommy's process group / TTY
    )

    t = Task(task_id=task_id, harness=chosen, proc=proc)
    _task_ref[0] = t  # publish Task before any ctrl messages can be delivered
    t._ctrl = ctrl
    drain = threading.Thread(target=_drain_stdout, args=(proc, t, _tf_path), daemon=True)
    drain.start()
    t._drain_thread = drain
    _evict_tasks()
    _tasks[task_id] = t

    if async_run:
        return {"task_id": task_id, "status": "running", "harness": chosen}

    proc.wait()
    # Join the drain thread so all buffered stdout is captured before tail().
    # Without this, tail() may return truncated output on fast-exiting processes.
    drain.join(timeout=10.0)
    ctrl.close()  # release the control channel socket (no harness will reconnect now)
    return {"task_id": task_id, "status": t.status(), "output": t.tail(200)}


@mcp.tool()
def tommy_status(task_id: str, tail: int = 50) -> dict:
    """
    Check the status and partial output of a task dispatched via tommy_dispatch.

    Args:
        task_id: The task_id returned by tommy_dispatch.
        tail:    Return the last N lines of stdout (default 50).
    """
    t = _tasks.get(task_id)
    if t is None:
        return {"error": f"Unknown task_id: {task_id!r}"}
    return {"task_id": task_id, "harness": t.harness, "status": t.status(), "output": t.tail(tail)}


@mcp.tool()
def tommy_switch_project(project: str) -> str:
    """
    Set the active project context for this Tommy session.  Affects which
    memnos namespace is used by default and which workspace path is the
    default for tommy_dispatch.

    Args:
        project: Project key — must match a key in tommy.conf [projects].
    """
    global _active_project
    cfg = _get_cfg()
    entry = cfg.project_by_key(project)
    if entry is None:
        keys = [p.key for p in cfg.projects]
        return f"Unknown project '{project}'. Configured: {keys or ['(none)']}"
    _active_project = project
    return (
        f"Active project: {entry.name} ({entry.key})\n"
        f"  JIRA: {entry.jira_project}\n"
        f"  Workspace: {entry.git_root}"
    )


@mcp.tool()
def tommy_route(task: str, explain: bool = False) -> dict:
    """
    Ask Tommy's routing engine which harness it would choose for a task,
    without dispatching.

    Args:
        task:    Task description.
        explain: Include routing rationale.
    """
    cfg = _get_cfg()
    harnesses = all_harnesses()
    chosen = cfg.harness
    spec = harnesses.get(chosen)
    result: dict = {
        "chosen_harness": chosen,
        "available": spec.available if spec else False,
    }
    if explain:
        result["rationale"] = (
            f"smart_routing={'enabled' if cfg.smart_routing else 'disabled'}. "
            f"Default harness is '{cfg.harness}' (from tommy.conf). "
            "Task-type routing will be added in a future release."
        )
    return result


@mcp.tool()
def tommy_list_harnesses() -> dict:
    """Return available harnesses, health, and current routing config."""
    cfg = _get_cfg()
    harnesses = all_harnesses()
    return {
        "harnesses": [
            {
                "name": name,
                "available": spec.available,
                "description": spec.description,
                "active": name == cfg.harness,
            }
            for name, spec in harnesses.items()
        ],
        "smart_routing": cfg.smart_routing,
    }


@mcp.tool()
def tommy_control(
    task_id: str,
    action: str,
    message: str = "",
    budget_seconds: int = 60,
) -> dict:
    """
    Send a mid-run control message to a dispatched task.

    Args:
        task_id:        The task_id returned by tommy_dispatch.
        action:         One of: wrap_up, abort, pivot, answer.
                        wrap_up — ask harness to finish gracefully.
                        abort   — tell harness to stop immediately.
                        pivot   — redirect harness to a new goal (set `message`).
                        answer  — reply to a question the harness asked.
        message:        For pivot: the new goal.  For answer: the reply text.
        budget_seconds: For wrap_up: how many seconds to give the harness.
    """
    t = _tasks.get(task_id)
    if t is None:
        return {"error": f"Unknown task_id: {task_id!r}"}
    if t._ctrl is None:
        return {"error": "Task has no control channel (was it dispatched with an older Tommy?)"}
    if t.status() != "running":
        return {"error": f"Task is not running: {t.status()}"}

    ok: bool
    if action == "wrap_up":
        ok = t._ctrl.wrap_up(budget_seconds=budget_seconds)
    elif action == "abort":
        ok = t._ctrl.abort()
    elif action == "pivot":
        if not message:
            return {"error": "pivot requires a `message` (the new goal)."}
        ok = t._ctrl.pivot(message)
    elif action == "answer":
        ok = t._ctrl.answer(message)
    else:
        return {"error": f"Unknown action '{action}'. Use: wrap_up, abort, pivot, answer."}

    return {
        "task_id": task_id,
        "action": action,
        "sent": ok,
        "harness_connected": t._ctrl.harness_connected,
    }



# ---------------------------------------------------------------------------
# Entry point called from cli.py
# ---------------------------------------------------------------------------

def run_stdio() -> None:
    """Run Tommy as an MCP stdio server (invoked via `tommy --mcp`)."""
    mcp.run(transport="stdio")
