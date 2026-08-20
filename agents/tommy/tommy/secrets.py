"""
Secret Shield (issue #115) — resolve `secret://NAME` references from static
tommy.yaml/tommy.conf config into the launched harness subprocess's
environment, before any other launch-prep work.

Precondition, enforced by construction, not just documented: every mapping
this module resolves comes from `collect_secret_refs()`, which reads ONLY
`TommyConfig.secret_env` (parsed from tommy.conf's `SECRET_ENV` key) and a
discovered tommy.yaml's `env:` block (validated at parse time in
project_config.py — see SECRET_REF_RE there). Neither source is ever
derived from a built prompt, a dispatched task string, or anything else
that varies per-run. If a future use case needs a reference computed from
task text, that is a deliberately different, unbuilt code path — see
tommy/prompt.py and issue #115's "fallback" note; do not thread task/prompt
strings into this module.

Injection point: callers (tommy.cli._launch_harness, tommy.mcp_server.
tommy_dispatch) MUST call resolve_secret_env() before build_prompt(), before
the prompt tempfile is written, and before ControlServer binds a port — not
merely before `env = os.environ.copy()`. See both call sites' comments for
why "before env.copy()" is provably too late (a written tempfile + a bound
control-server socket are both live state created after that line and
before Popen, with cleanup only wired into the try/finally around Popen/
proc.wait()).

Fails closed: resolve_secret_env() raises SecretResolutionError (never
returns a partial result) if ANY reference fails to resolve — server
unreachable, secret not found, or the caller's token lacks a grant for that
secret's pseudo-namespace. Callers must treat that exception as "refuse to
launch the subprocess entirely" and must not have created the tempfile or
ControlServer yet when it's raised.

Scope, stated precisely (see issue #115): this protects secret values from
ever reaching the prompt tommy builds or any file tommy itself writes. It
does NOT prevent a harness process from reflecting its own environment back
into its own output — and that carve-out reaches the LLM through more than
one channel: (1) cli.py's `_post_run_capture` ingests the harness's Claude
Code transcript into memnos after every interactive run, where
`core/redact.py` only partially covers it (that gap is real and is
exercised, not fixed, by this issue's ingest-path test); (2) on the
mcp_server.py `tommy_dispatch` path, the harness's raw stdout is also what
`tommy_dispatch(async_run=False)` returns as `output` and what
`tommy_status` returns as its tail — both go straight back to the calling
LLM as MCP tool output, uninspected by anything in this module.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .config import TommyConfig
from .project_config import SECRET_REF_RE, TommyYamlError, find_tommy_yaml, load_tommy_yaml


class SecretResolutionError(RuntimeError):
    """One or more secret:// references could not be resolved. Fail closed:
    callers must abort the launch entirely on this exception, before any
    tempfile/socket/subprocess side effects occur."""

    def __init__(self, failures: dict[str, str]):
        self.failures = dict(failures)
        detail = "; ".join(f"{k} ({v})" for k, v in sorted(self.failures.items()))
        super().__init__(f"secret resolution failed for {len(self.failures)} reference(s): {detail}")


def collect_secret_refs(cfg: TommyConfig, workspace: Optional[Path] = None) -> dict[str, str]:
    """Merge tommy.conf's SECRET_ENV with a discovered tommy.yaml's `env:`
    block. tommy.yaml wins on a shared ENV_VAR_NAME — same precedence
    direction (tommy.conf -> tommy.yaml) that effective_config.py already
    establishes for every other field. `workspace` is where tommy.yaml
    discovery starts (the harness's working directory, not necessarily
    Tommy's own CWD) — falls back to CWD when None, same as
    find_tommy_yaml()'s own default.

    This call is UNCONDITIONAL on every launch (cli.py._launch_harness,
    mcp_server.py.tommy_dispatch), regardless of whether cfg.secret_env is
    already non-empty — a project using only tommy.yaml's `env:` block (no
    tommy.conf SECRET_ENV at all) still needs discovery to run.

    A tommy.yaml that fails to parse degrades to "no env: entries from
    yaml" — the merged result falls back to cfg.secret_env alone — rather
    than raising. An earlier version of this function raised
    SecretResolutionError on any unparseable tommy.yaml ("we can't know
    whether it would have declared a secret, so fail closed"). That was
    reverted after rebasing onto issue #109, which landed in this exact
    code path (mcp_server.py.tommy_dispatch) with an already-tested,
    opposite contract for this exact failure mode: a broken tommy.yaml must
    not block dispatch (see #109's effective_config handling and
    test_corpus_gate.py::test_gate_on_but_broken_tommy_yaml_proceeds_and_
    surfaces_the_parse_error). Re-examined against the actual threat model,
    #109's choice is right here too: failing to pick up a yaml-declared
    secret ref because the file didn't parse just means that env var never
    gets set in the subprocess — a functional gap (whatever needed it will
    error loudly on its own), not a leak. There is no path from "this
    function silently returned fewer refs than a fully-parsed file would
    have" to "a secret value reached the prompt or a log" — unresolved
    means absent, not exposed. Fail-closed in THIS module is reserved for a
    reference that WAS successfully declared and then failed to RESOLVE
    (wrong name, no grant, unreachable server) — see resolve_secret_env()
    below — not for "the file that would have declared it didn't parse."
    Visibility into a broken tommy.yaml is still provided, just by #109's
    existing code rather than duplicated here: mcp_server.py's corpus-gate
    block surfaces "tommy.yaml could not be read" via its own
    `corpus_gate.error` whenever corpus_gate would apply, and cli.py's
    auto-ingest block prints the same to stderr on the interactive path.
    """
    merged: dict[str, str] = dict(cfg.secret_env)

    yaml_path = find_tommy_yaml(workspace)
    if yaml_path is not None:
        try:
            yaml_cfg = load_tommy_yaml(yaml_path)
        except TommyYamlError:
            return merged
        merged.update(yaml_cfg.env)

    return merged


def resolve_secret_env(mapping: dict[str, str], client) -> dict[str, str]:
    """Resolve every `secret://NAME` value in `mapping` via
    client.resolve_secret(name). Returns {ENV_VAR_NAME: plaintext}.

    All-or-nothing: if `mapping` is empty, returns {} immediately without
    touching `client` at all (a project with no secret:// refs configured
    pays zero cost and has zero new failure surface — unchanged behavior).
    Otherwise, ANY failure — client is None (memnos unreachable / SDK
    import failed), a malformed reference, or an exception from
    resolve_secret (403 forbidden, 404 not found, network/transport error)
    — raises SecretResolutionError covering every failure encountered, not
    just the first one, so a caller printing the error shows the whole
    picture in one shot.
    """
    if not mapping:
        return {}

    if client is None:
        raise SecretResolutionError({k: "memnos client unavailable" for k in mapping})

    resolved: dict[str, str] = {}
    failures: dict[str, str] = {}
    for env_name, ref in mapping.items():
        m = SECRET_REF_RE.match(ref)
        if not m:
            failures[env_name] = f"not a valid secret:// reference: {ref!r}"
            continue
        secret_name = m.group(1)
        try:
            resolved[env_name] = client.resolve_secret(secret_name)
        except Exception as exc:  # MemnosError (403/404/etc.) or any transport failure
            failures[env_name] = str(exc)

    if failures:
        raise SecretResolutionError(failures)
    return resolved


def secret_resolve_client(cfg: TommyConfig):
    """Build a MemnosClient dedicated to secret resolution.

    Deliberately independent of any memnos client the caller already built
    for its own purposes (e.g. the interactive CLI's health-check/journal
    client, which is None under --no-memnos-check): --no-memnos-check exists
    to skip the startup banner/health-check, not to silently disable this
    fail-closed security control. Returns None (never raises) on import or
    construction failure — resolve_secret_env() turns that into the same
    fail-closed SecretResolutionError every other failure mode produces.
    """
    try:
        from memnos_sdk import MemnosClient  # type: ignore
        return MemnosClient(base_url=cfg.memnos_url, token=cfg.memnos_token, namespace=cfg.tommy_ns)
    except Exception:
        return None
