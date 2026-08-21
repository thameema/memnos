"""
Tommy MCP stdio server.

Invoked by editors (Cursor, Claude Desktop, VS Code+Continue, Zed) as:

    tommy --mcp

Tommy reads MCP JSON-RPC from stdin and writes to stdout.  The editor owns
the process — Tommy never opens a port and never runs as a daemon.

Eleven tools:
  tommy_recall          — query memnos memory
  tommy_remember        — persist a fact to memnos
  tommy_dispatch        — launch a harness task (async by default)
  tommy_status          — check a running task's output / exit code
  tommy_control         — send wrap_up / abort / pivot / answer to a running task
  tommy_switch_project  — set the active project context
  tommy_route           — dry-run: which harness would Tommy pick?
  tommy_list_harnesses  — available harnesses + health + active routing
  tommy_sketch          — mermaid sequence diagram -> CFC constraints -> corpus ingest
  tommy_drift_sweep     — check recent commits against the architecture corpus
  tommy_verdict         — post-dispatch diff-against-corpus verdict (violated/satisfied/uncovered)
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
from .corpus import (
    corpus_check as _http_corpus_check,
    corpus_check_diff as _http_corpus_check_diff,
    corpus_ingest as _http_corpus_ingest,
)
from .discovery.harnesses import all_harnesses, apply_skip_permissions, apply_session_name
from .effective_config import resolve_effective_config
from .project_config import TommyYamlError
from .prompt import build_prompt
from .secrets import (
    SecretResolutionError,
    collect_secret_refs,
    resolve_secret_env,
    secret_resolve_client,
)
from .sketch import _mermaid_to_cfc


# ---------------------------------------------------------------------------
# In-process task registry (lives for the lifetime of this stdio process)
# ---------------------------------------------------------------------------

@dataclass
class Task:
    task_id: str
    harness: str
    proc: subprocess.Popen
    # The exact workspace tommy_dispatch launched this task in (ws_path,
    # captured at dispatch time — issue #112: tommy_verdict diffs FROM
    # HERE, not from whatever tommy_switch_project has made the "active
    # project" by the time tommy_verdict is called later). Defaulted so
    # existing Task(...) call sites/tests that don't pass it keep working.
    workspace: str = ""
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


def _corpus_check(cfg: TommyConfig, namespace: str, snippet: str) -> dict:
    """Corpus-gate pre-flight check (issue #109): calls memnos's real
    `POST /corpus/check` (see tommy/corpus.py — not a reimplementation) with
    the dispatched task's text as the snippet, and returns a dict that
    distinguishes the two outcomes the issue's fail-open-but-visible
    resolution requires:

      {"ok": True,  "constraints": [...]}                — a real check ran;
          `constraints` may legitimately be empty (corpus had nothing
          relevant to this task) — that is NOT the same thing as outcome 2.
      {"ok": False, "constraints": [], "error": "..."}    — the check itself
          could not run (corpus unreachable, auth/config error, etc).

    Never raises, and never blocks tommy_dispatch either way — corpus_gate
    is fail-open by design (see issue #109's amendment comment: unlike
    Secret Shield's fail-closed /secret/resolve, a corpus check that can't
    run must not prevent a developer's dispatch)."""
    return _http_corpus_check(cfg.memnos_url, cfg.memnos_token, namespace, snippet)


def _format_constraint_block(check: dict) -> str:
    """Render a `_corpus_check()` result into the prompt-injected constraint
    block. Always returns non-empty text when called (corpus_gate was on),
    so the fact that a gate check happened is itself visible in the prompt —
    distinct from corpus_gate being off, where build_prompt() never receives
    a constraint_block argument at all (see prompt.py)."""
    if not check.get("ok"):
        return (
            "## Corpus Gate — check failed\n\n"
            f"The pre-dispatch architecture corpus check could not run: {check.get('error', 'unknown error')}\n\n"
            "This is a CHECK FAILURE, not a clean pass — the corpus was not "
            "actually consulted for this task. Proceeding anyway (corpus_gate "
            "fails open), but architecture compliance for this task has not "
            "been verified against the corpus. If this task touches "
            "constrained areas, verify manually."
        )
    constraints = check.get("constraints") or []
    if not constraints:
        return (
            "## Corpus Gate — no relevant constraints\n\n"
            "The pre-dispatch architecture corpus check ran successfully and "
            "found no constraints relevant to this task. Proceeding — this "
            "is a legitimate empty result, not a check failure."
        )
    lines = [
        "## Constraints to Check\n",
        "The architecture corpus has normative constraints relevant to this "
        "task. Treat these as hard constraints:\n",
    ]
    for c in constraints:
        source = c.get("source", "?")
        content = c.get("content", "")
        lines.append(f"- [{source}] {content}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Drift sweep (issue #110): check recent commits against the architecture
# corpus outside tommy_dispatch's per-dispatch corpus gate — e.g. commits
# made directly, or dispatched with a harness that ran with CORPUS_GATE=off.
# ---------------------------------------------------------------------------

_DRIFT_CHUNK_CHARS = 4000   # see _chunk_diff()'s docstring for why chunking matters
_DRIFT_MAX_CHUNKS = 25      # bounds worst-case latency on a huge diff window; the
                            # excess is reported (chunks_available > chunks_checked),
                            # never silently dropped


def _drift_workspace(cfg: TommyConfig, workspace: str) -> Path:
    """Resolve the git repo to sweep: explicit `workspace` arg, else the active
    project's git_root, else CWD. This mirrors tommy_dispatch's own
    workspace-resolution (see its `ws_path` lines above) but is kept as its
    own small copy rather than factored into a shared helper — a
    cross-cutting refactor of tommy_dispatch isn't worth the merge-conflict
    risk while other issues are landing in this same file in parallel; see
    PR description "Design decisions"."""
    if workspace:
        return Path(workspace)
    if _active_project:
        proj = cfg.project_by_key(_active_project)
        if proj:
            return Path(getattr(proj, "git_root", str(Path.cwd())))
    return Path.cwd()


def _drift_git(repo_root: Path, *args: str, timeout: float = 15.0) -> tuple:
    """Run a git command in `repo_root`. Returns (stdout, None) on exit 0, or
    (None, error_message) on any failure (non-zero exit, git missing, or a
    timeout) — never raises. The same "never raise, degrade to a visible
    error" contract cli.py's own `_git()` helper uses for auto-ingest
    (issue #109), kept as a small local copy here rather than imported:
    cli.py already imports `run_stdio` from this module, so importing back
    from cli.py would be circular."""
    try:
        out = subprocess.run(
            ["git", *args], cwd=str(repo_root),
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return None, "git not found on PATH"
    except subprocess.TimeoutExpired:
        return None, f"git {' '.join(args)} timed out after {timeout}s"
    if out.returncode != 0:
        detail = (out.stderr or out.stdout or "").strip()
        return None, f"git {' '.join(args)} failed: {detail or f'exit {out.returncode}'}"
    return out.stdout, None


def _clamp_commits(repo_root: Path, requested: int) -> tuple:
    """Return (effective_n, available_n, error).

    `available_n` is the number of ancestor commits reachable from HEAD,
    excluding HEAD itself — the most `git diff HEAD~N HEAD` can meaningfully
    use. Computed from `git rev-list --count HEAD`, which (unlike a plain
    walk) already reports only the commits actually present locally on a
    shallow clone (the shallow boundary commit is grafted with no parents),
    so this clamp is correct for shallow clones with no special-casing.

    `effective_n` is `requested` clamped into [0, available_n] — never
    negative, never more history than the repo actually has, so
    `git diff HEAD~{effective_n} HEAD` below can never fail for "not enough
    history" reasons (shallow clone or a young repo included).

    `error` is set (with the other two 0) only when HEAD itself can't be
    resolved at all — not a git repo, no commits yet, or git missing from
    PATH — the one case an effective_n truly cannot be computed."""
    total, err = _drift_git(repo_root, "rev-list", "--count", "HEAD")
    if err is not None:
        return 0, 0, err
    try:
        total_n = int((total or "0").strip())
    except ValueError:
        return 0, 0, f"unexpected `git rev-list --count HEAD` output: {total!r}"
    available_n = max(0, total_n - 1)
    effective_n = max(0, min(requested, available_n))
    return effective_n, available_n, None


def _chunk_diff(diff_text: str, chunk_chars: int = _DRIFT_CHUNK_CHARS) -> list:
    """Split a diff into fixed-size windows before each is passed to
    corpus_check() as its own `snippet`.

    core/store.py's corpus_check() only ever looks at a snippet's first 40
    unique 4+-letter words (core/store.py's `words[:40]`, pure SQL FTS, no
    LLM) — a single multi-commit diff passed whole would only ever be
    keyword-matched on whatever appears first (diff headers, repeated file
    paths), starving the FTS query of everything after that. Chunking
    spreads the 40-word budget across the whole diff instead, at the cost
    of one corpus_check() network round trip per chunk — see
    tommy_drift_sweep()'s docstring for the latency tradeoff this creates
    as `commits` (and therefore diff size) grows."""
    if not diff_text:
        return []
    return [diff_text[i:i + chunk_chars] for i in range(0, len(diff_text), chunk_chars)]


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

    Corpus gate (issue #109): when the target workspace's tommy.yaml sets
    corpus.corpus_gate: true, this task's text is checked against the
    architecture corpus (POST /corpus/check) BEFORE the harness launches,
    and the result is injected as its own prompt layer ahead of the task
    itself. Fail-open-but-visible: a match, an empty "nothing relevant"
    result, and a check-that-could-not-run are three DISTINCT outcomes, all
    rendered differently in the prompt, and none of them blocks the
    dispatch. The returned dict also carries a "corpus_gate" key with the
    same {"ok": ..., "constraints"/"error": ...} result whenever the gate
    ran (absent entirely when corpus_gate is off).

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

    # ── Secret Shield (issue #115) ────────────────────────────────────────
    # Resolve secret://NAME references BEFORE any other launch-prep work —
    # before the corpus gate below, before the (optional) memnos
    # memory-injection call, before build_prompt(), before the prompt
    # tempfile is written, before ControlServer binds a port. Mirrors
    # cli.py._launch_harness's ordering and reasoning exactly — see
    # tommy/secrets.py's module docstring and cli.py's comment at the
    # equivalent point for the full "why before env.copy() is too late"
    # reasoning; not repeated here to avoid the two copies drifting.
    #
    # Ordering vs. issue #109's corpus gate, now that #109 has actually
    # landed in this same function: secret resolution runs FIRST, before
    # the corpus-gate block below. Secret resolution is a hard fail-closed
    # security boundary; the corpus gate is fail-open-but-visible by its
    # own design (see _corpus_check()'s docstring). A hard stop should not
    # wait on an advisory network call whose result would be discarded
    # anyway if secret resolution is about to abort the dispatch — see PR
    # description "Design decisions" for the full reasoning. This block
    # does NOT diverge from #109's fail-open choice for a broken tommy.yaml
    # — collect_secret_refs() below degrades gracefully on a parse error,
    # same contract #109 already established here; see its docstring.
    resolved_secrets: dict[str, str] = {}
    resolve_client = None
    try:
        # collect_secret_refs() degrades to "no env: entries from a broken
        # tommy.yaml" rather than raising on a parse error — see its
        # docstring for why (short version: a ref that never got read isn't
        # a leak, just a missing env var; the corpus-gate block below
        # already surfaces "tommy.yaml could not be read" for this same
        # condition). Fail-closed here is for a ref that DID resolve to a
        # name and then failed to RESOLVE.
        secret_refs = collect_secret_refs(cfg, workspace=ws_path)
        if secret_refs:
            resolve_client = secret_resolve_client(cfg)
            resolved_secrets = resolve_secret_env(secret_refs, resolve_client)
    except SecretResolutionError as exc:
        return {"error": f"secret resolution failed — refusing to launch harness: {exc}"}
    finally:
        # This server process is long-lived (stdio MCP server) — unlike the
        # interactive CLI path, where the client's httpx.Client dies with
        # the process anyway, a dedicated resolution client here must be
        # closed explicitly on every dispatch or its connection pool leaks
        # across the server's lifetime.
        if resolve_client is not None:
            try:
                resolve_client.close()
            except Exception:
                pass
    # ─────────────────────────────────────────────────────────────────────

    # --- Corpus gate (issue #109) ------------------------------------------
    # tommy.yaml's corpus.corpus_gate/design work moved config-reading here
    # from tommy.conf per #109's amendment comment (depends on #113's
    # project_config.py/effective_config.py). tommy.yaml is discovered
    # relative to ws_path (the workspace this dispatch is about to run in),
    # not CWD — a dispatch into a different project's workspace must gate on
    # THAT project's tommy.yaml, not whatever directory the MCP server
    # process happened to start in.
    constraint_block: Optional[str] = None
    corpus_gate_result: Optional[dict] = None
    try:
        effective = resolve_effective_config(project_root=ws_path)
    except TommyYamlError as exc:
        # A broken tommy.yaml must not block dispatch (fail-open), but it
        # also must not silently disable a gate the user explicitly asked
        # for — same "never silently swallow" principle the corpus check
        # itself is held to, extended one layer up to config resolution.
        # We can't know here whether corpus_gate was even requested (the
        # file that would say so is what failed to parse), so we surface
        # the parse failure itself rather than guessing either way. (The
        # Secret Shield block above, for the same broken tommy.yaml,
        # degrades quietly instead of erroring — see collect_secret_refs()'s
        # docstring for why the two features reasonably differ here: this
        # block's whole job is surfacing a corpus-gate outcome, so silence
        # would defeat its purpose; Secret Shield's job is deciding whether
        # to inject an env var, and "yaml didn't parse" -> "nothing to
        # inject from it" is a complete, non-silent-about-security answer
        # on its own.)
        corpus_gate_result = {"ok": False, "constraints": [],
                               "error": f"tommy.yaml could not be read: {exc}"}
        constraint_block = _format_constraint_block(corpus_gate_result)
    else:
        if effective.value("corpus_gate"):
            # effective.value("namespace") (tommy.conf -> tommy.yaml's
            # memnos.namespace -> env), not _effective_namespace(cfg) (the
            # active-project helper used below for inject_memory) — the two
            # coincide today (ProjectEntry carries no `namespace` field, so
            # _effective_namespace(cfg) always falls through to
            # cfg.default_ns), but the corpus gate is deliberately
            # tommy.yaml-namespace-aware since we're already resolving that
            # config object right here to read corpus_gate itself. See PR
            # description "Design decisions" for the full reasoning.
            ns = effective.value("namespace")
            corpus_gate_result = _corpus_check(cfg, ns, task)
            constraint_block = _format_constraint_block(corpus_gate_result)
    # ------------------------------------------------------------------------

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
    full_prompt = build_prompt(
        prompt_cfg, project_key=_active_project, task=task_with_memory,
        constraint_block=constraint_block,
    )

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
    if resolved_secrets:
        env.update(resolved_secrets)  # real values only — never the secret:// reference string

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

    t = Task(task_id=task_id, harness=chosen, proc=proc, workspace=str(ws_path))
    _task_ref[0] = t  # publish Task before any ctrl messages can be delivered
    t._ctrl = ctrl
    drain = threading.Thread(target=_drain_stdout, args=(proc, t, _tf_path), daemon=True)
    drain.start()
    t._drain_thread = drain
    _evict_tasks()
    _tasks[task_id] = t

    # Names only, never values — same convention as the CLI path's log line.
    _secrets_info = {"secrets_resolved": sorted(resolved_secrets)} if resolved_secrets else {}

    if async_run:
        result = {"task_id": task_id, "status": "running", "harness": chosen, **_secrets_info}
        if corpus_gate_result is not None:
            result["corpus_gate"] = corpus_gate_result
        return result

    proc.wait()
    # Join the drain thread so all buffered stdout is captured before tail().
    # Without this, tail() may return truncated output on fast-exiting processes.
    drain.join(timeout=10.0)
    ctrl.close()  # release the control channel socket (no harness will reconnect now)
    result = {"task_id": task_id, "status": t.status(), "output": t.tail(200), **_secrets_info}
    if corpus_gate_result is not None:
        result["corpus_gate"] = corpus_gate_result
    return result


@mcp.tool()
def tommy_drift_sweep(commits: int = 20, namespace: str = "", workspace: str = "") -> dict:
    """
    Sweep the last `commits` commits' combined diff against the architecture
    corpus. This is a standalone check, not gated on a dispatch — it catches
    drift that tommy_dispatch's per-dispatch corpus gate (issue #109) can't
    see: commits made directly (outside Tommy entirely), or dispatched
    through a harness that ran with corpus_gate off.

    Mode (today): RECALL-FALLBACK. `git diff HEAD~N HEAD` is split into
    fixed-size windows and each window is checked with the same plain
    `POST /corpus/check` FTS-over-constraints endpoint the corpus gate uses
    (tommy/corpus.py's corpus_check — not reimplemented here). This is
    keyword-matched recall, NOT a violated/satisfied/uncovered verdict —
    the result's "mode" field is always "recall_fallback" and the matches
    are returned under "possibly_relevant_constraints", never presented
    with the confidence of a real pass/fail check. When memnos#105 ships a
    corpus_check_diff-style verdict endpoint, that becomes a second,
    additive branch here (keyed on server capability / a config flag) — this
    fallback keeps working unchanged for servers that don't have it yet.

    History handling: `commits` is clamped to the repo's actual ancestor
    count so a fresh checkout, a shallow clone, or a repo with fewer than
    `commits` commits never crashes. The clamp is always reported back
    (`commits_used`, `commits_available`, `clamped`) — never silent.

    Cost: pure SQL FTS per chunk (core/store.py's corpus_check(), no LLM
    call either way). Latency scales with the number of chunks, which scales
    with the diff's size, which scales with `commits`: each chunk is its own
    network round trip to /corpus/check. A larger `commits` costs more
    wall-clock time (more chunks => more round trips), not more $ (no LLM
    tokens spent either way). Capped at 25 chunks to bound worst-case
    latency on a huge window; if the diff has more than that, the excess is
    skipped and reported (`chunks_available` > `chunks_checked`), never
    silently dropped.

    Args:
        commits:   How many recent commits to diff (default 20). Clamped to
                   available history — see `commits_used`/`clamped` in the
                   result.
        namespace: memnos namespace to check against. Omit for the current
                   project namespace.
        workspace: Absolute path to the git repo to sweep. Omit for the
                   active project's git_root, or CWD.
    """
    cfg = _get_cfg()
    repo_root = _drift_workspace(cfg, workspace)

    effective_n, available_n, err = _clamp_commits(repo_root, commits)
    if err is not None:
        # HEAD itself couldn't be resolved — effective_n/available_n/clamped
        # are meaningless (not "0 by clamping", genuinely unknown), but the
        # keys are still present (0/False) so every tommy_drift_sweep result,
        # ok or not, has the same shape for a caller to inspect uniformly.
        return {
            "ok": False, "mode": "recall_fallback",
            "error": f"drift sweep could not run: {err}",
            "commits_requested": commits, "commits_used": 0,
            "commits_available": 0, "clamped": False,
        }

    clamped = effective_n != commits
    diff_text = ""
    if effective_n > 0:
        diff_out, diff_err = _drift_git(repo_root, "diff", f"HEAD~{effective_n}", "HEAD")
        if diff_err is not None:
            return {
                "ok": False, "mode": "recall_fallback",
                "error": f"drift sweep could not run: {diff_err}",
                "commits_requested": commits, "commits_used": effective_n,
                "commits_available": available_n, "clamped": clamped,
            }
        diff_text = diff_out or ""

    ns = namespace or _effective_namespace(cfg)
    all_chunks = _chunk_diff(diff_text, chunk_chars=_DRIFT_CHUNK_CHARS)
    chunks_available = len(all_chunks)
    chunks = all_chunks[:_DRIFT_MAX_CHUNKS]

    seen: set = set()
    constraints: list = []
    check_failures: list = []
    for chunk in chunks:
        result = _corpus_check(cfg, ns, chunk)
        if not result.get("ok"):
            check_failures.append(result.get("error", "unknown error"))
            continue
        for c in (result.get("constraints") or []):
            key = (c.get("source"), c.get("content"))
            if key in seen:
                continue
            seen.add(key)
            constraints.append(c)

    if diff_text:
        note = (
            "recall_fallback mode: possibly_relevant_constraints are keyword-matched "
            "via corpus FTS recall over the diff, NOT a violated/satisfied/uncovered "
            "verdict. Treat them as leads to review, not confirmed violations."
        )
    else:
        # diff_chars == 0 means the window produced no diff to check at all —
        # distinct from "checked the diff and found nothing relevant" (which
        # requires possibly_relevant_constraints to be empty AFTER a real
        # check ran). Without this, an empty possibly_relevant_constraints
        # list reads identically for both cases, which is exactly the
        # confidence-blurring the recall-fallback/verdict distinction (issue
        # #110's acceptance criteria) exists to avoid.
        note = (
            "recall_fallback mode: the requested commit window produced no diff "
            "(commits_used="
            f"{effective_n}) — nothing was sent to the corpus check. This is NOT "
            "the same as a check that ran and found no relevant constraints."
        )

    return {
        "ok": True,
        "mode": "recall_fallback",
        "note": note,
        "commits_requested": commits,
        "commits_used": effective_n,
        "commits_available": available_n,
        "clamped": clamped,
        "diff_chars": len(diff_text),
        "chunks_checked": len(chunks),
        "chunks_available": chunks_available,
        "chunks_truncated": chunks_available > len(chunks),
        "possibly_relevant_constraints": constraints,
        "check_failures": check_failures,
    }


# ---------------------------------------------------------------------------
# tommy_verdict (issue #112): post-dispatch diff-against-corpus verdict.
#
# Unlike tommy_drift_sweep's recall_fallback mode (issue #110 — built before
# memnos#105 shipped a real diff-verdict endpoint, and still keyword-matched
# FTS today), tommy_verdict calls memnos#105's actual POST /corpus/check_diff
# and returns its real violated/satisfied/uncovered classification for ONE
# already-dispatched task's diff — not a sweep over N commits.
#
# merge_blocked reuses tommy.yaml's `merge_gate` field (issue #113;
# effective_config.py's "merge_gate" — see adapters.py's harness-adapter
# projection of the same field) rather than adding a second flag, per the
# issue's acceptance criteria. No code enforces `merge_gate` today beyond
# this: tommy_verdict returns `merge_blocked` as data for a caller (a human,
# or a future CI step) to act on — it does not itself refuse anything.
#
# Fail posture — deliberately NOT a copy of corpus_gate's fail-open choice
# (see PR description "Design decisions" for the full argument): corpus_gate
# fails open because failing closed there would cost a developer their
# entire dispatch over an advisory check that couldn't run. tommy_verdict
# takes no action of its own — it only returns a dict — so there is no
# equivalent cost to being conservative. When merge_gate is on (or, for a
# broken tommy.yaml, unknown — see below) and the check could not actually
# run (git failure, an unreadable tommy.yaml, or /corpus/check_diff itself
# unreachable), merge_blocked is True with merge_blocked_reason "unverified"
# — never silently False. merge_blocked is only ever False — a real "nothing
# is blocking this" signal — when merge_gate is off ("gate_off"), when a
# check ran and found no violations ("clean"), or when there was legitimately
# no diff to check at all ("no_diff", distinct from "clean" the same way
# tommy_drift_sweep distinguishes "no diff produced" from "checked and found
# nothing"). `merge_gate` in the returned dict is reported exactly as
# resolved, including `None` when a broken tommy.yaml means it genuinely
# could not be determined — never guessed True/False just to justify
# merge_blocked's value; see _verdict_unverified()'s `blocked` parameter.
# ---------------------------------------------------------------------------


def _verdict_unverified(task_id: str, task_status: str, error: str,
                         merge_gate, *, blocked: Optional[bool] = None) -> dict:
    """Shared shape for every tommy_verdict path where the check itself
    could not run (git diff failed, tommy.yaml unreadable, or
    /corpus/check_diff unreachable/erroring) — never conflated with "ran and
    found nothing," per the issue's acceptance criteria.

    `merge_gate` is reported exactly as resolved — pass `None` when it is
    genuinely unknown (a broken tommy.yaml means the field that would say so
    never parsed) rather than guessing a value just to drive `merge_blocked`.
    `blocked` decouples the two: default (`None`) derives `merge_blocked`
    from `bool(merge_gate)`; pass it explicitly when `merge_gate` is `None`
    but the fail-closed posture (see this module's tommy_verdict comment
    block) still applies."""
    is_blocked = bool(merge_gate) if blocked is None else blocked
    return {
        "task_id": task_id, "task_status": task_status, "ok": False,
        "error": error,
        "violated": [], "satisfied": [], "uncovered": [],
        "score": None, "evaluated": 0,
        "merge_gate": merge_gate,
        "merge_blocked": is_blocked,
        "merge_blocked_reason": "unverified" if is_blocked else "gate_off",
    }


@mcp.tool()
def tommy_verdict(task_id: str, namespace: str = "", name: str = "") -> dict:
    """
    Post-dispatch check (issue #112): diff a completed tommy_dispatch task's
    actual change against the architecture corpus via memnos#105's real
    corpus_check_diff verdict endpoint (violated / satisfied / uncovered),
    not tommy_drift_sweep's keyword-matched recall_fallback.

    Diffs `git diff HEAD~1 HEAD` in the exact workspace tommy_dispatch
    launched `task_id` in — captured on the task registry at dispatch time,
    NOT re-resolved from whatever project is "active" now (tommy_
    switch_project may have changed that in between).

    merge_blocked mirrors tommy.yaml's `merge_gate` field — the same field
    `tommy generate` already projects into harness adapter files (issue
    #113) — no separate flag. See this module's "tommy_verdict" comment
    block above for the full fail-open/fail-closed reasoning; in short:
    merge_blocked is True whenever merge_gate is on and either real
    violations were found, or the check could not actually run at all
    (`merge_blocked_reason` distinguishes the two: "violations" vs
    "unverified"). It is False when merge_gate is off ("gate_off"), when a
    check ran clean ("clean"), or when there was no diff to check
    ("no_diff").

    Args:
        task_id:   The task_id returned by tommy_dispatch.
        namespace: memnos namespace to check against. Omit to use the
                   dispatch workspace's effective tommy.yaml/tommy.conf
                   namespace (same resolution tommy_dispatch's corpus gate
                   uses).
        name:      Optional corpus source filter, passed through to
                   /corpus/check_diff's `name`.
    """
    t = _tasks.get(task_id)
    if t is None:
        return {"error": f"Unknown task_id: {task_id!r}"}

    cfg = _get_cfg()
    repo_root = Path(t.workspace) if t.workspace else Path.cwd()
    task_status = t.status()

    try:
        effective = resolve_effective_config(project_root=repo_root)
    except TommyYamlError as exc:
        # merge_gate itself is unknown here (the file that would say so
        # didn't parse) — reported as None (not guessed True/False), but
        # still treated as "could not verify" for merge_blocked, per this
        # module's fail-posture comment above.
        return _verdict_unverified(task_id, task_status,
                                    f"tommy.yaml could not be read: {exc}",
                                    merge_gate=None, blocked=True)

    merge_gate = effective.value("merge_gate")
    ns = namespace or effective.value("namespace")

    diff_text, diff_err = _drift_git(repo_root, "diff", "HEAD~1", "HEAD")
    if diff_err is not None:
        return _verdict_unverified(
            task_id, task_status,
            f"tommy_verdict could not compute the task's diff: {diff_err}", merge_gate)
    diff_text = diff_text or ""

    if not diff_text.strip():
        return {
            "task_id": task_id, "task_status": task_status, "ok": True,
            "note": (
                "HEAD~1..HEAD produced no diff in this task's workspace — "
                "nothing was sent to the corpus check. This is NOT the same "
                "as a check that ran and found no violations."
            ),
            "violated": [], "satisfied": [], "uncovered": [],
            "score": None, "evaluated": 0,
            "merge_gate": merge_gate,
            "merge_blocked": False,
            "merge_blocked_reason": "no_diff" if merge_gate else "gate_off",
        }

    check = _http_corpus_check_diff(cfg.memnos_url, cfg.memnos_token, ns, diff_text,
                                     name=(name or None))
    if not check.get("ok"):
        return _verdict_unverified(task_id, task_status,
                                    check.get("error", "unknown error"), merge_gate)

    violated = check.get("violated") or []
    if not merge_gate:
        reason = "gate_off"
    elif violated:
        reason = "violations"
    else:
        reason = "clean"

    return {
        "task_id": task_id, "task_status": task_status, "ok": True,
        "violated": violated,
        "satisfied": check.get("satisfied") or [],
        "uncovered": check.get("uncovered") or [],
        "score": check.get("score"),
        "evaluated": check.get("evaluated", 0),
        "merge_gate": merge_gate,
        "merge_blocked": bool(merge_gate and violated),
        "merge_blocked_reason": reason,
    }


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


@mcp.tool()
def tommy_sketch(
    flow_name: str,
    mermaid_text: str = "",
    mermaid_file: str = "",
    namespace: str = "",
) -> dict:
    """
    Convert a mermaid sequence diagram into Canonical Flow Corpus (CFC)
    constraints and ingest them into memnos's architecture corpus
    (issue #111 — `/sketch`).

    Takes mermaid TEXT, never an image: this is the tommy-side,
    buildable-today half of `/sketch`. An image->mermaid vision step is a
    separate, not-yet-built memnos-server capability and is out of scope
    here — no harness in Tommy's discovery layer (`discovery/harnesses.py`)
    carries an image-input path today, so this tool only ever reads mermaid
    source, either inline (`mermaid_text`) or from a file (`mermaid_file`) —
    independently usable and testable without the vision step ever landing.

    The naive line-based `_mermaid_to_cfc()` parser (tommy/sketch.py)
    supports flat, single-level sequence diagrams: participant/actor
    declarations become actor labels, arrows (`->`, `-->`, `->>`, `-->>`,
    `-x`, `--x`, `-)`, `--)`) become `"<From> SHALL send \\"<label>\\" to
    <To>."` statements, a single-level `alt`/`else` becomes a "When
    <condition>, ..." prefix on statements inside it, and `Note` lines
    containing "must not"/"shall not"/"should not"/"may not" become
    prohibition statements. Nested alt/opt/loop blocks, multi-line/wrapped
    labels, and any other unrecognized syntax are skipped and reported in
    the returned `warnings` list rather than silently mis-parsed — see
    tommy/sketch.py's module docstring for the full breakdown of what v1
    does and doesn't handle.

    The generated CFC text is ingested via `POST /corpus/ingest` with
    `kind="cfc"` (the same tommy/corpus.py HTTP wrapper issue #109's corpus
    gate and auto-ingest use — see that module, not reimplemented here).
    `/corpus/ingest` is a WRITE_OPS endpoint (memnos_server.py's WRITE_OPS
    set): a read-only memnos token gets a 403 from the server, which
    surfaces here as `{"ok": False, "error": "...(403)..."}`, same as every
    other tommy/corpus.py caller — never raised. Re-ingesting under the same
    `flow_name` DELETE-then-replaces that source's prior constraints
    (core/store.py's `ingest_constraints`) — the same already-acknowledged
    "silent constraint wipe" risk issue #109's `auto_ingest` carries for
    design docs; pick a stable `flow_name` per diagram.

    Args:
        flow_name:     Name for this flow's corpus source — used as `name`
                        in /corpus/ingest. Re-using a name replaces its
                        prior constraints (see above).
        mermaid_text:   Mermaid sequence-diagram source, inline.
        mermaid_file:   Path to a file containing mermaid source. Used only
                         when mermaid_text is empty.
        namespace:      memnos namespace. Omit for the current project
                        namespace.
    """
    if not flow_name.strip():
        return {"error": "flow_name required"}

    if not mermaid_text:
        if not mermaid_file:
            return {"error": "mermaid_text or mermaid_file required"}
        try:
            mermaid_text = Path(mermaid_file).read_text(errors="replace")
        except OSError as exc:
            return {"error": f"could not read mermaid_file {mermaid_file!r}: {exc}"}

    if not mermaid_text.strip():
        return {"error": "mermaid text is empty"}

    cfc_text, warnings = _mermaid_to_cfc(mermaid_text, flow_name)
    if not cfc_text:
        return {
            "error": "no CFC constraints could be derived from this diagram",
            "warnings": warnings,
        }

    cfg = _get_cfg()
    ns = namespace or _effective_namespace(cfg)
    result = _http_corpus_ingest(cfg.memnos_url, cfg.memnos_token, ns, flow_name, cfc_text, kind="cfc")
    result["warnings"] = warnings
    result["cfc_text"] = cfc_text
    return result


# ---------------------------------------------------------------------------
# Entry point called from cli.py
# ---------------------------------------------------------------------------

def run_stdio() -> None:
    """Run Tommy as an MCP stdio server (invoked via `tommy --mcp`)."""
    mcp.run(transport="stdio")
