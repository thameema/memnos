"""
Regression test for issue #100: every Task Tommy dispatches for review must
carry a fixed "MANDATORY REVIEW PASSES" block — Pass 4 (System Invariant
Check), Pass 5 (Call Graph Mandate), Pass 6 (Safety Claim Verification) —
appended verbatim to the subagent's Task prompt.

Tommy has no code that assembles Task prompts — the harness LLM does that by
following core.md's instructions when it calls its own Task tool. So the fix
and this test both live at the prompt-content layer: core.md is the
implementation.

This does NOT re-derive parity with the tommy_dispatch MCP path.
test_dispatch_core_prompt_parity.py already asserts core.md's *entire* text
lands, byte-for-byte, as a literal substring in the prompt built by both the
interactive CLI path (build_prompt()) and the MCP path (tommy_dispatch() ->
build_prompt()) — so any content added to core.md, including this block, is
parity-covered automatically without a second harness-capture test. What
this file checks instead is that the *content* of the injected block is
complete and unambiguous: the exact template text from issue #100, present
as one contiguous run (not scattered keywords that could pass by accident),
plus the dispatch-time instruction that makes it mandatory for every review
Task, not just a "code reviewer" agent.
"""
from __future__ import annotations

from pathlib import Path

CORE_MD = Path(__file__).parent.parent / "tommy" / "prompts" / "core.md"

# The exact prompt-injection template from issue #100, reproduced verbatim.
# Asserting this as ONE contiguous substring (not individual tokens) is the
# point: scattered-keyword assertions would already pass against core.md
# before this change (e.g. "PURPOSE: review" and "LLD" both predate it), so
# they'd prove nothing. A single multi-line substring match can't pass by
# accident.
MANDATORY_REVIEW_PASSES_BLOCK = """## MANDATORY REVIEW PASSES (in addition to standard checks)

### Pass 4 — System Invariant Check
Produce a CLEAR or BLOCKER verdict for each:
- I-1: No live-tenant mutation reachable from reaper/scheduler paths
- I-2: Credential rotation paired with pool invalidation or service restart
- I-3: Missing tenant context fails closed (no shared/default fallback)
- I-4: Paired writes to two stores have gap error handling

### Pass 5 — Call Graph Mandate
For every new/renamed function: enumerate ALL callers (schedulers, reapers,
lifecycle hooks, Helm hooks, internal REST). State reachability explicitly.
Unknown callers = assume live-tenant reachable.

### Pass 6 — Safety Claim Verification
Trace every safety claim ("idempotent", "does not rotate", "fails closed",
"no side effects", "safe to retry") to source. Unverifiable = MAJOR finding,
block approval until author adds a test."""


def _core_text() -> str:
    return CORE_MD.read_text()


def test_mandatory_review_passes_block_present_verbatim():
    """The full Pass 4/5/6 template appears in core.md as one contiguous
    block — not paraphrased, not split up, not missing an invariant."""
    text = _core_text()
    assert MANDATORY_REVIEW_PASSES_BLOCK in text, (
        "core.md is missing the exact MANDATORY REVIEW PASSES block from "
        "issue #100 (Pass 4 System Invariant Check / Pass 5 Call Graph "
        "Mandate / Pass 6 Safety Claim Verification)"
    )


def test_all_four_system_invariants_present():
    text = _core_text()
    for invariant_id, keyword in [
        ("I-1", "reaper/scheduler"),
        ("I-2", "pool invalidation"),
        ("I-3", "fails closed"),
        ("I-4", "gap error handling"),
    ]:
        assert invariant_id in text, f"missing invariant {invariant_id}"
        assert keyword in text, f"invariant {invariant_id} missing its keyword ({keyword!r})"


def test_dispatch_instruction_applies_to_every_review_task_uniformly():
    """The issue is explicit: 'Applies to All review agents Tommy dispatches
    — not just code reviewers. LLD reviewers, architecture reviewers, and
    any future review agent type should receive the same three passes.'
    core.md must say so, not just define the passes in isolation."""
    text = _core_text()
    assert "Reviewer Dispatch — Mandatory Passes" in text
    assert "any future review agent type" in text
    assert "not just code reviewers" in text
    # The instruction must be tied to the actual dispatch moment, not just
    # floating as reference material the harness might never read.
    assert "appended verbatim to the Task prompt" in text


def test_unverifiable_safety_claims_block_approval():
    text = _core_text()
    assert "MAJOR finding" in text
    assert "block approval until author adds a test" in text


def test_dispatch_first_rule_cross_references_mandatory_passes():
    """The existing "Dispatch-first for review tasks" absolute rule (the
    place a coordinator session actually decides to fire a review Task)
    must point at the new section, so the instruction is reachable from
    where the dispatch decision is made, not just from a section that
    might get skimmed past."""
    text = _core_text()
    idx_rule = text.index("Dispatch-first for review tasks")
    idx_section = text.index("## Reviewer Dispatch — Mandatory Passes")
    dispatch_rule_body = text[idx_rule:idx_section]
    assert "Reviewer Dispatch" in dispatch_rule_body, (
        "the 'Dispatch-first for review tasks' rule does not reference the "
        "mandatory review-passes section"
    )
    # And the section must physically follow the rule that triggers it.
    assert idx_section > idx_rule
