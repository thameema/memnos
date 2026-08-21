# verdict

Diff one already-dispatched `tommy_dispatch` task against the architecture
corpus, via memnos#105's real `/corpus/check_diff` verdict endpoint —
violated / satisfied / uncovered, not `tommy_drift_sweep`'s keyword-matched
`recall_fallback`.

Unlike the `memnos-*` slash commands in this directory, which call memnos's
own MCP tools directly, this command calls `tommy_verdict` — a
Tommy-specific tool, only available when Tommy's own MCP server (`tommy
--mcp`) is registered in this editor, not just memnos's.

## Usage
```
/verdict <task_id>
```
`task_id` is required — the id `tommy_dispatch` returned when the task was
launched.

## What to do
1. Call `tommy_verdict(task_id="<task_id>")` using the first token of
   `$ARGUMENTS` as `task_id`. If `$ARGUMENTS` is empty, ask the user for the
   task_id instead of guessing.
2. If the result has a top-level `error` (unknown task_id), tell the user
   plainly — do not invent a verdict for a task that was never dispatched.
3. If `ok` is `false`, tell the user the verdict check itself could not run
   (show `error`) — this is a check FAILURE, not a clean pass. Never present
   an `ok: false` result as "no violations found."
4. If `ok` is `true` and the result carries a `note` (no diff was produced
   for this task), say so plainly — this is NOT the same as "checked and
   found nothing," and must not be presented as a clean pass.
5. Otherwise present `violated`, `satisfied`, and `uncovered` under headings
   that say exactly that — these are a real verdict (not leads to review,
   unlike `/drift`'s `recall_fallback` mode). Lead with `violated` if it is
   non-empty.
6. Always report `merge_gate` and `merge_blocked` together with
   `merge_blocked_reason`:
   - `"gate_off"` — merge_gate is off; this check is informational only.
   - `"clean"` — the check ran and found no violations. Always state
     `evaluated` alongside it — `evaluated: 0` means no constraint was
     actually evaluated (the diff didn't match anything in the corpus),
     which is NOT the same as "verified compliant." Don't let `"clean"`
     alone read as a stronger guarantee than it is.
   - `"no_diff"` — there was nothing to check (see step 4).
   - `"violations"` — real violations were found; merge_blocked is true.
   - `"unverified"` — merge_gate is on (or, for a broken tommy.yaml,
     `merge_gate` is `null`/unknown) but the check could not actually run
     (git failure, unreadable tommy.yaml, or the corpus endpoint was
     unreachable); merge_blocked is true here too, deliberately fail-closed
     — do NOT tell the user this is safe to merge.
7. Check `task_status`. If it is `"running"`, `tommy_verdict` refuses to
   compute a verdict at all — no diff is taken, `ok` is `false`, and
   `error` explains the task hasn't finished yet (step 3 applies: this is a
   check FAILURE, not a partial pass, and never `merge_gate`-off-safe by
   itself — see step 6's "unverified" bullet). Tell the user to call
   `/verdict` again once `tommy_status` shows the task is no longer
   running; never present a still-running task's result as a real verdict,
   partial or otherwise.
8. Never say a task "passed" or "is safe to merge" unless `task_status` is
   `"done"` AND `merge_blocked` is `false` AND `merge_blocked_reason` is
   `"clean"` or `"gate_off"`.
