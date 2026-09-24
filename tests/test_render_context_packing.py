"""No-DB unit tests for MemnosMemory.render_context budget packing (issue #153, item 3).

Before the fix, the render loop did `if used + len(line) > max_chars: break`, so the
FIRST row that didn't fit stopped rendering entirely, even when shorter rows after it
would have fit in the remaining budget. The fix skips (`continue`s past) a row that
doesn't fit and keeps trying later rows, while preserving precedence order: rows are
still considered in the given order (pins, then facts, then turns), so an earlier row
always claims space before a later one.

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

    print("=== oversized PIN does not starve the ranked facts behind it ===")
    ctx = render([pin("P" * 20000), pin("keep the small pin")] + shorts, max_chars=2000)
    check("small pin after the oversized pin renders", "CONSTRAINT: keep the small pin" in ctx, ctx)
    check("facts after the oversized pin render", "short fact number 4" in ctx, ctx)

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
