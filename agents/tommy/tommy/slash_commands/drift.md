# drift

Sweep recent commits against the architecture corpus — catches drift that
`tommy_dispatch`'s per-dispatch corpus gate (issue #109) can't see: commits
made directly outside Tommy, or dispatched through a harness that ran with
the corpus gate off.

Unlike the `memnos-*` slash commands in this directory, which call memnos's
own MCP tools directly, this command calls `tommy_drift_sweep` — a
Tommy-specific tool, only available when Tommy's own MCP server (`tommy
--mcp`) is registered in this editor, not just memnos's.

## Usage
```
/drift [commits]
```
`commits` is optional — how many recent commits to sweep. Defaults to 20 if
omitted or not a number.

## What to do
1. Call `tommy_drift_sweep(commits=<N>)`, where `<N>` is `$ARGUMENTS` parsed
   as an integer if it's a number, otherwise call `tommy_drift_sweep()` for
   the default window.
2. If the result's `ok` is `false`, tell the user the sweep itself could not
   run (bad workspace, git missing, etc.) and show the `error`. Do not
   present this as "no drift found."
3. If `clamped` is `true`, tell the user the effective commit count actually
   used (`commits_used` vs `commits_requested`) and why — the repo has fewer
   commits than asked for.
4. Present `possibly_relevant_constraints` under a heading that says exactly
   that — "possibly relevant constraints," not "violations" or "verdict."
   The result's `mode` is always `"recall_fallback"` today: these are
   keyword-matched via corpus FTS recall over the diff, not a
   violated/satisfied/uncovered verdict. Do not present them with the
   confidence of a pass/fail check, and do not say a commit "violates" or
   "satisfies" a constraint — say a constraint is "possibly relevant to"
   the changes.
5. If `check_failures` is non-empty, tell the user some part of the diff
   could not be checked (corpus unreachable for that chunk) — this is a
   partial result, not a clean pass.
6. If `diff_chars` is `0`, the commit window produced no diff at all — say
   so plainly ("no diff in the last `commits_used` commits — nothing was
   checked"). Do NOT phrase this as "no relevant constraints found," which
   implies a check ran and came back clean; here no check ran at all.
7. Only when `diff_chars` is greater than `0` AND there are no possibly
   relevant constraints AND no check failures, say so plainly: "No relevant
   constraints found in the last `commits_used` commits."
