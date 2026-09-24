"""No-DB unit tests for MemnosMemory.render_context budget packing (issue #153, item 3).

Before the fix, the render loop did `if used + len(line) > max_chars: break`, so the
FIRST row that didn't fit stopped rendering entirely, even when shorter rows after it
would have fit in the remaining budget. The fix skips (`continue`s past) a NON-PINNED
row that doesn't fit and keeps trying later rows, while preserving precedence order:
rows are still considered in the given order (pins, then facts, then turns), so an
earlier row always claims space before a later one.

PINNED constraint rows are ADDITIVE: they always render in full, never skipped, never
truncated, regardless of size or count, and they NEVER count against max_chars. That
budget is spent by ranked content (facts/turns) only. The production incident was 6
pins alone (10,040 chars) exceeding the whole 9,000-char budget and starving every
fact to zero — pins consuming their own render space but still counting toward the
SAME budget facts draw from would reproduce that exact failure with a bigger pinned
set. Constraints are curated governance rules, not ranked content: they are never
traded off against facts, in either direction.

    python tests/test_render_context_packing.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.service import MemnosMemory

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))
    PASS += bool(cond); FAIL += (not cond)


def fact(content):
    return {"content": content, "kind": "fact", "type": None}


def pin(content):
    return {"content": content, "kind": "turn", "type": "constraint", "pinned": True}


def main():
    render = MemnosMemory.render_context

    print("=== one oversized row followed by short rows that fit ===")
    big = fact("X" * 5000)                       # alone exceeds the 1000-char budget
    shorts = [fact(f"short fact number {i}") for i in range(5)]
    ctx = render([big] + shorts, max_chars=1000)
    check("oversized row is not rendered", "XXXX" not in ctx, ctx[:80])
    for i in range(5):
        check(f"short row {i} after the oversized one still renders",
              f"short fact number {i}" in ctx, ctx[:200])

    print("=== pins are ADDITIVE: unbounded, and never eat into the facts' own budget ===")
    ctx = render([pin("P" * 20000), pin("keep the small pin")] + shorts, max_chars=2000)
    check("oversized pin still renders in full, over budget", "P" * 20000 in ctx, ctx[:80])
    check("small pin after the oversized pin also renders", "CONSTRAINT: keep the small pin" in ctx, ctx)
    # THE incident scenario: no matter how large or numerous the pins, ranked content
    # still gets its own FULL max_chars — pins never count against that budget.
    for i in range(5):
        check(f"short fact number {i} still renders despite the 20000-char pin ahead of it",
              f"short fact number {i}" in ctx, ctx[:200])
    non_pin_used = sum(len(l) for l in ctx.split("\n") if not l.startswith("CONSTRAINT:"))
    check("non-pin content alone stays within max_chars (pins truly don't count against it)",
          non_pin_used <= 2000, str(non_pin_used))

    print("=== a normal-sized pin still leaves room for facts behind it ===")
    ctx = render([pin("small rule")] + shorts, max_chars=2000)
    check("normal pin renders", "CONSTRAINT: small rule" in ctx, ctx)
    check("facts after a normal-sized pin render", "short fact number 4" in ctx, ctx)

    print("=== precedence preserved when there genuinely isn't room for everything ===")
    a = fact("A" * 600)
    b = fact("B" * 600)                           # does not fit after A (600+~610 > 1000)
    c = fact("c-small")
    ctx = render([a, b, c], max_chars=1000)
    lines = ctx.split("\n")
    check("earlier row A wins the space over later row B", "AAAA" in ctx and "BBBB" not in ctx, ctx[:120])
    check("small row C still fills remaining space", "c-small" in ctx)
    check("output order follows input order (A before C)",
          len(lines) == 2 and "AAAA" in lines[0] and "c-small" in lines[1], repr(lines))
    ctx2 = render([pin("rule one"), fact("fact one"), fact("fact two")], max_chars=9000)
    check("pins still lead the block ahead of facts",
          ctx2.split("\n")[0] == "CONSTRAINT: rule one", ctx2)

    print("=== budget is never exceeded ===")
    rows = [fact("w" * n) for n in (300, 900, 50, 700, 20, 400, 10)]
    ctx = render(rows, max_chars=1000)
    used = sum(len(l) for l in ctx.split("\n")) if ctx else 0
    check("sum of rendered line lengths <= max_chars", used <= 1000, str(used))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
