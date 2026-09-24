"""Speaker-aware extraction + non-circular corroboration (issue #154).

Field finding: ~78% of all stored facts were mined from the ASSISTANT's own replies by a
prompt tuned for a personal-life benchmark ("be EXHAUSTIVE… one fact per martial art"),
turning each coding-agent status report into ~17 activity-log "facts"
(`user | did_activity | add brief card in vs-others section` ×275, `agent | did_activity`
×116), and the assistant echoing a fact counted as corroborating it.

Pins, $0 (no paid API — a FAKE OpenAI-compatible client, the tests/test_local_extraction.py
pattern, returns canned extractor output and CAPTURES the prompt it was sent):

  A. (no DB) prompt profiles
     A1  the `benchmark` profile prompt is BYTE-IDENTICAL to the pre-#154 prompt (sha256
         golden taken from master before the change) and the request kwargs are unchanged
     A2  `_extract` with no profile still defaults to that exact prompt
     A3  the live `developer` (user-turn) and `developer_assistant` prompts drop the
         EXHAUSTIVE / martial-arts / did_activity framing and resolve the human's name
     A4  an assistant status-report reply whose (old-style) extraction yields 17
         activity-log facts is narrowed to a few decisions/outcomes/identifiers
     A5  user-turn extraction output is passed through UNCHANGED (no filter, same facts
         as the pre-#154 path returned) — speaker='user' and speaker=None alike
     A6  a legacy two-arg extract_fn still works, and assistant narrowing applies to it
  B. benchmark-path isolation (static): the LoCoMo harness ingests via Encoder +
     Consolidator, which never reach MemnosMemory._extract / bump_restatement, and whose
     prompts are byte-identical to master
  C. (DB) raw-turn preservation: the assistant reply is stored VERBATIM with
     speaker='assistant' and is returned by the turn arm (recall_fetch raw + full recall),
     however few facts were extracted from it; facts carry source_speaker + provenance
  D. (DB) ingest_session sends the pinned benchmark prompt
  E. (DB) corroboration: assistant-only restatements leave restatements=0 / salience
     unchanged (provenance still unioned, no new row); a user restatement increments;
     an unattributed (speaker NULL) restatement keeps today's behavior

Run: python tests/test_speaker_aware_extraction.py   (needs MEMNOS_DSN with the schema
created by a server from this branch, like the rest of the DB tests)
"""
import hashlib
import json
import os
import re
import sys
import uuid
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.service import (MemnosMemory, _EXTRACT_PROMPT_BENCHMARK, _extract_prompt,  # noqa: E402
                          filter_assistant_facts)

DSN = os.environ.get("MEMNOS_DSN", "postgresql://memnos:memnos@localhost:5432/memnos")
PASS = FAIL = 0

# sha256 of the COMPLETE system prompt master's _extract sent for date "2026-01-01" —
# captured from the pre-#154 code (commit a1551e5) before any edit.
GOLDEN_FULL_PROMPT_2026_01_01 = "d38a9d7666622812d098ce8d0980734c9faffa1bf5991ef82cc9201d041cc77d"
GOLDEN_PROMPT_BODY = "76836fbacce8ac46e4d94cddb5ea871386fe9eea03f1fedb47fd81c92a5212f2"
# sha256(PROP_SYS + "\0" + DOSSIER_SYS) of core/consolidate.py at a1551e5 (LoCoMo ingest).
GOLDEN_CONSOLIDATE_PROMPTS = "95b75e3cd66063e6584cfef2f09aecb4413782ae6c34f5046ec3328e6cb9862a"


def check(name, cond):
    global PASS, FAIL
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    PASS += bool(cond); FAIL += (not cond)


def sha(s):
    return hashlib.sha256(s.encode()).hexdigest()


# --- a fake OpenAI-compatible client: canned facts, captured requests -------------------
class _Usage:
    prompt_tokens = 10
    completion_tokens = 5


class _Resp:
    def __init__(self, content):
        self.usage = _Usage()
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class FakeLLM:
    def __init__(self):
        self.calls = []
        self.facts = []
        outer = self

        class _Comp:
            def create(self, **kw):
                outer.calls.append(kw)
                return _Resp(json.dumps({"facts": outer.facts}))

        self.chat = type("Chat", (), {"completions": _Comp()})()

    def system_prompt(self, i=-1):
        return self.calls[i]["messages"][0]["content"]


def F(subject, predicate, obj, statement):
    return {"subject": subject, "predicate": predicate, "object": obj, "statement": statement}


# A realistic coding-agent status-report reply (modeled on the field examples).
ASSISTANT_REPLY = (
    "Done. I added the brief card in the vs-others section of compare.html and updated the "
    "hero copy to match. I also ran the full test suite locally: 212 passed, 0 failed. The "
    "root cause of the flaky recall test was a stale HNSW index after the schema reset; I "
    "fixed it by recreating the index in the fixture. Opened PR #146 (fixes #144), CI is "
    "green, and it is merged as a1b2c3d. Deployed v0.4.12 to host3. Next I can look at the "
    "dossier page if you want.")

# What the ORIGINAL exhaustive prompt produced for such a reply: ~17 facts, mostly an
# activity log (shape taken from the production audit).
OLD_STYLE_ASSISTANT_FACTS = [
    F("user", "did_activity", "add brief card in vs-others section",
      "The user added a brief card in the vs-others section."),
    F("agent", "did_activity", "updated the hero copy", "The agent updated the hero copy."),
    F("assistant", "did_activity", "ran the full test suite", "The assistant ran the full test suite."),
    F("test suite", "result", "212 passed, 0 failed", "The full test suite result was 212 passed, 0 failed."),
    F("flaky recall test", "root_cause", "stale HNSW index after the schema reset",
      "The root cause of the flaky recall test was a stale HNSW index after the schema reset."),
    F("assistant", "fixed", "flaky recall test",
      "The assistant fixed the flaky recall test by recreating the index in the fixture."),
    F("PR #146", "status", "opened", "PR #146 was opened to fix issue #144."),
    F("PR #146", "status", "CI green", "CI is green for PR #146."),
    F("PR #146", "merged_as", "a1b2c3d", "PR #146 was merged as a1b2c3d."),
    F("memnos", "deployed_version", "v0.4.12", "v0.4.12 was deployed to host3."),
    F("compare.html", "includes", "brief card", "compare.html includes a brief card in the vs-others section."),
    F("hero section", "has", "updated copy", "The hero section has updated copy."),
    F("user", "likes", "brief cards", "The user likes brief cards."),
    F("assistant", "plans_to", "look at the dossier page", "The assistant plans to look at the dossier page."),
    F("dossier page", "is", "next", "The dossier page is the next item to look at."),
    F("agent", "did_activity", "opened PR #146", "The agent opened PR #146."),
    F("test fixture", "contains", "index recreation", "The test fixture contains the index recreation."),
]
EXPECTED_ASSISTANT_KEPT = [
    "The full test suite result was 212 passed, 0 failed.",
    "The root cause of the flaky recall test was a stale HNSW index after the schema reset.",
    "PR #146 was opened to fix issue #144.",
    "CI is green for PR #146.",
    "PR #146 was merged as a1b2c3d.",
    "v0.4.12 was deployed to host3.",
]

USER_TURN = ("I want the compare page to stay honest: never claim benchmark numbers we haven't "
             "reproduced. Also I'm moving the demo to Thursday 10am because I'm at the Frisco "
             "office on Wednesday.")
USER_TURN_FACTS = [
    F("user", "rule", "never claim unreproduced benchmark numbers",
      "The user requires the compare page to never claim benchmark numbers that have not been reproduced."),
    F("demo", "scheduled_for", "Thursday 10am", "The demo was moved to Thursday at 10am."),
    F("user", "location", "Frisco office on Wednesday", "The user is at the Frisco office on Wednesday."),
    F("compare page", "", "", "The compare page must stay honest."),
]

BAD_FRAMING = ("EXHAUSTIVE", "martial", "dessert", "did_activity", "met_person")


def part_a():
    print("=== A. extraction profiles (no DB, fake LLM) ===")
    llm = FakeLLM()
    mem = MemnosMemory(None, lambda t: [0.0] * 8, dim=8, llm=llm)

    # A1 — benchmark profile byte-identical to master
    mem._extract("hello", "2026-01-01", profile="benchmark")
    kw = llm.calls[-1]
    check("A1 benchmark prompt sha256 == pre-#154 golden",
          sha(llm.system_prompt()) == GOLDEN_FULL_PROMPT_2026_01_01)
    check("A1 pinned prompt body constant sha256 == golden", sha(_EXTRACT_PROMPT_BENCHMARK) == GOLDEN_PROMPT_BODY)
    check("A1 request kwargs unchanged (model/temperature/max_tokens/response_format/user msg)",
          kw["model"] == "gpt-4o-mini" and kw["temperature"] == 0 and kw["max_tokens"] == 2000
          and kw["response_format"] == {"type": "json_object"}
          and kw["messages"][1] == {"role": "user", "content": "hello"} and len(kw["messages"]) == 2)
    # A2 — default profile is still the pinned one
    mem._extract("hello", "2026-01-01")
    check("A2 _extract() default profile sends the identical pinned prompt",
          sha(llm.system_prompt()) == GOLDEN_FULL_PROMPT_2026_01_01)
    try:
        mem._extract("x", "2026-01-01", profile="nope")
        check("A2 unknown profile rejected", False)
    except ValueError:
        check("A2 unknown profile rejected", True)

    # A3 — live prompts
    named = MemnosMemory(None, lambda t: [0.0] * 8, dim=8, llm=llm, user_name="Thameem Ansari")
    named.extract_facts(USER_TURN, "2026-09-24", speaker="user")
    p_user = llm.system_prompt()
    named.extract_facts(ASSISTANT_REPLY, "2026-09-24", speaker="assistant")
    p_asst = llm.system_prompt()
    check("A3 user-turn prompt = developer profile",
          p_user == _extract_prompt("developer", "2026-09-24", "Thameem Ansari"))
    check("A3 assistant-turn prompt = developer_assistant profile",
          p_asst == _extract_prompt("developer_assistant", "2026-09-24", "Thameem Ansari"))
    check("A3 neither live prompt carries the benchmark framing",
          not any(b in p for p in (p_user, p_asst) for b in BAD_FRAMING))
    check("A3 display name resolves 'I'/'the user' in both live prompts",
          all('"Thameem Ansari"' in p and 'resolve "I"' in p for p in (p_user, p_asst)))
    mem.extract_facts(USER_TURN, "2026-09-24", speaker="user")
    check("A3 no display name -> consistent 'user' subject instruction",
          'consistently with the subject "user"' in llm.system_prompt())
    check("A3 assistant prompt restricts to decisions/outcomes/identifiers/state changes",
          all(k in p_asst for k in ("DECISIONS", "OUTCOMES", "IDENTIFIERS", "STATE CHANGES",
                                    "do NOT log or summarize")))

    # A4 — assistant narrowing: 17 old-style facts -> a few high-value ones
    llm.facts = OLD_STYLE_ASSISTANT_FACTS
    old = mem._extract(ASSISTANT_REPLY, "2026-09-24", profile="benchmark")   # pre-#154 behavior
    new = mem.extract_facts(ASSISTANT_REPLY, "2026-09-24", speaker="assistant")
    print(f"      assistant reply: pre-#154 path -> {len(old)} facts, new path -> {len(new)} facts")
    for f in new:
        print(f"        kept: {f['subject']} | {f['predicate']} | {f['statement']}")
    check("A4 pre-#154 path would store all 17 facts", len(old) == 17)
    check("A4 new path keeps only the decisions/outcomes/identifiers (17 -> 6)",
          [f["statement"] for f in new] == EXPECTED_ASSISTANT_KEPT)
    check("A4 no activity-log / personal-life predicate survives",
          not any(f["predicate"] in ("did_activity", "likes", "plans_to", "is", "has", "includes",
                                     "contains") for f in new))
    check("A4 identifiers preserved verbatim (#146, a1b2c3d, v0.4.12, host3)",
          all(any(i in f["statement"] for f in new) for i in ("#146", "a1b2c3d", "v0.4.12", "host3")))
    # A4b — the FILTER (not the cap) removes the noise: with the cap out of the way, only
    # the 6 high-value facts + the one state-change ('fixed') outcome survive.
    os.environ["MEMNOS_ASSISTANT_FACT_CAP"] = "100"
    uncapped = mem.extract_facts(ASSISTANT_REPLY, "2026-09-24", speaker="assistant")
    check("A4b uncapped: 17 -> 7 (6 high-value + 1 explicit 'fixed' outcome), all noise dropped",
          [f["statement"] for f in uncapped] == EXPECTED_ASSISTANT_KEPT + [
              "The assistant fixed the flaky recall test by recreating the index in the fixture."])
    # rows shaped like REAL llama3.1:8b output for such replies: file-path narration under
    # invented predicates must not be rescued by the file name
    real_shaped = [
        F("compare.html", "updated", "brief card",
          "The brief card in the vs-others section of compare.html was updated."),
        F("tests/test_hooks.py", "updated", "", "tests/test_hooks.py was updated."),
        F("memnos_cli.py", "added", "_flatten_content", "I added a helper _flatten_content in memnos_cli.py."),
        F("README section on hooks", "identified", "outdated", "The README section on hooks is a bit outdated."),
        F("", "can_look_at", "dossier page", "Next I can look at the dossier page if you want."),
        F("tests/test_hooks.py", "covers", "list-shaped content",
          "tests/test_hooks.py covers list-shaped transcript content."),
        F("hook tests", "pass", "38", "All 38 hook tests pass."),
        F("", "ran_test_suite", "", "I also ran the full test suite locally: 212 passed, 0 failed."),
        F("PR #140", "opened", "", "PR #140 was opened to fix #139."),
        F("PR #140", "merged", "100b2a2", "PR #140 was merged as 100b2a2."),
    ]
    got = [f["statement"] for f in filter_assistant_facts(real_shaped)]
    check("A4b file-path narration / invented predicates dropped even uncapped",
          got == ["All 38 hook tests pass.",
                  "I also ran the full test suite locally: 212 passed, 0 failed.",
                  "PR #140 was opened to fix #139.", "PR #140 was merged as 100b2a2."])
    os.environ["MEMNOS_ASSISTANT_FACT_CAP"] = "3"
    check("A4 cap is configurable (MEMNOS_ASSISTANT_FACT_CAP=3)",
          len(mem.extract_facts(ASSISTANT_REPLY, "2026-09-24", speaker="assistant")) == 3)
    del os.environ["MEMNOS_ASSISTANT_FACT_CAP"]
    check("A4 speaker match is case-insensitive ('Assistant')",
          mem.extract_facts(ASSISTANT_REPLY, "2026-09-24", speaker="Assistant")
          == filter_assistant_facts(OLD_STYLE_ASSISTANT_FACTS) == new)
    llm.facts = []
    check("A4 a reply with nothing durable yields 0 facts",
          mem.extract_facts("Sure — want me to keep going?", "2026-09-24", speaker="assistant") == [])

    # A5 — user turns: facts pass through untouched (same as the pre-#154 path returned)
    llm.facts = USER_TURN_FACTS
    before = mem._extract(USER_TURN, "2026-09-24", profile="benchmark")
    after_user = mem.extract_facts(USER_TURN, "2026-09-24", speaker="user")
    after_none = mem.extract_facts(USER_TURN, "2026-09-24")
    print(f"      user turn: pre-#154 path -> {len(before)} facts, new path -> {len(after_user)} facts")
    check("A5 user-turn facts identical to the pre-#154 path (count + content)",
          after_user == before and len(after_user) == 4)
    check("A5 speaker=None (files / legacy) also unfiltered", after_none == before)

    # A6 — pluggable extract_fn: legacy two-arg signature still works + narrowing applies
    seen = []

    def legacy_fn(text, date):
        seen.append((text, date))
        return list(OLD_STYLE_ASSISTANT_FACTS)
    m2 = MemnosMemory(None, lambda t: [0.0] * 8, dim=8, extract_fn=legacy_fn)
    got = m2.extract_facts(ASSISTANT_REPLY, "2026-09-24", speaker="assistant")
    check("A6 legacy two-arg extract_fn called unchanged", seen == [(ASSISTANT_REPLY, "2026-09-24")])
    check("A6 assistant narrowing applies to extract_fn output too",
          [f["statement"] for f in got] == EXPECTED_ASSISTANT_KEPT)
    prof = []

    def modern_fn(text, date, *, profile, user_name=None):
        prof.append((profile, user_name))
        return []
    MemnosMemory(None, lambda t: [0.0] * 8, dim=8, extract_fn=modern_fn,
                 user_name="Thameem").extract_facts("x", "d", speaker="assistant")
    check("A6 profile-aware extract_fn receives profile + user_name",
          prof == [("developer_assistant", "Thameem")])
    check("A6 constraint bypass unchanged (no extraction)",
          mem.extract_facts("must never deploy on Fridays", "d", memory_type="constraint",
                            speaker="user") == [])


def part_b():
    print("=== B. benchmark path isolation (static) ===")
    import core.consolidate as cons
    src = {n: open(os.path.join(ROOT, p)).read() for n, p in (
        ("consolidate", "core/consolidate.py"), ("encode", "core/encode.py"),
        ("locomo", "benchmarks/locomo_eval.py"), ("harness", "benchmarks/_harness.py"))}
    check("B LoCoMo ingest modules (encode/consolidate) never import core.service",
          not any(re.search(r"\bservice\b", src[n]) for n in ("consolidate", "encode")))
    check("B encode/consolidate never call _extract / _write_fact / bump_restatement / ingest_session",
          not any(tok in src[n] for n in ("consolidate", "encode")
                  for tok in ("_extract(", "_write_fact(", "bump_restatement", "ingest_session")))
    check("B locomo_eval ingests via Encoder+Consolidator, not the live extraction path",
          "Consolidator(" in src["locomo"] and "enc.ingest_turn(" in src["locomo"]
          and not any(tok in src["locomo"] + src["harness"] for tok in
                      ("ingest_session", "extract_facts", "write_facts", ".remember(", "_extract(")))
    check("B Consolidator prompts byte-identical to pre-#154",
          sha(cons.PROP_SYS + "\0" + cons.DOSSIER_SYS) == GOLDEN_CONSOLIDATE_PROMPTS)


def part_cde():
    from core import local_models
    from core.store import BrainStore
    store = BrainStore(DSN)
    with store.conn.cursor() as c:
        c.execute("SELECT atttypmod AS d FROM pg_attribute "
                  "WHERE attrelid='tenant_memnos.semantic'::regclass AND attname='embedding'")
        dim = c.fetchone()["d"]
    if dim and dim != 384:
        print(f"  SKIP  DB parts need the local-384 schema (found dim={dim})")
        return
    llm = FakeLLM()
    mem = MemnosMemory(store, local_models.embed, dim=384, llm=llm)
    ns = f"test-speaker-{uuid.uuid4().hex[:8]}"

    def q(sql, *a):
        with store.conn.cursor() as c:
            c.execute(sql, a)
            return c.fetchall()

    print("=== C. raw-turn preservation (DB) ===")
    llm.facts = OLD_STYLE_ASSISTANT_FACTS
    out = mem.remember(ns, ASSISTANT_REPLY, speaker="assistant", session_id="s1")
    tid = out["turn_id"]
    row = q("SELECT speaker, text, session_id FROM tenant_memnos.raw_turns WHERE id=%s", tid)[0]
    check("C raw turn stored VERBATIM (full assistant reply, byte-for-byte)", row["text"] == ASSISTANT_REPLY)
    check("C raw turn keeps speaker='assistant' + session", row["speaker"] == "assistant" and row["session_id"] == "s1")
    check(f"C only a few facts promoted from it ({out['facts']} of 17)", 0 < out["facts"] <= 6)
    b = mem.recall_fetch(ns, "what was the root cause of the flaky recall test")
    raw_hit = [r for r in b["raw"] if r["id"] == tid]
    check("C turn arm (search_raw_turns) returns the assistant turn with its full text",
          len(raw_hit) == 1 and raw_hit[0]["content"] == ASSISTANT_REPLY)
    b2 = mem.recall_fetch(ns, "brief card in the vs-others section hero copy")
    check("C narration dropped from facts is STILL recallable via the raw turn",
          any(r["id"] == tid and "vs-others section" in r["content"] for r in b2["raw"])
          and not any("vs-others" in (s.get("content") or "") for s in b2["sem"]))
    ctx = mem.recall(ns, "brief card vs-others section")
    check("C full recall() surfaces the verbatim assistant turn",
          any(ASSISTANT_REPLY == (it.get("content") or it.get("text")) for it in ctx))
    facts = q("SELECT statement, source_speaker, source_turn_ids FROM tenant_memnos.semantic "
              "WHERE namespace=%s AND kind='fact'", ns)
    check("C every stored fact carries source_speaker='assistant' + provenance to the turn",
          facts and all(f["source_speaker"] == "assistant" and tid in f["source_turn_ids"] for f in facts))
    llm.facts = USER_TURN_FACTS
    out_u = mem.remember(ns, USER_TURN, speaker="user", session_id="s1")
    urow = q("SELECT speaker, text FROM tenant_memnos.raw_turns WHERE id=%s", out_u["turn_id"])[0]
    check("C user turn stored verbatim, all 4 of its facts written, source_speaker='user'",
          urow["text"] == USER_TURN and out_u["facts"] == 4 and all(
              f["source_speaker"] == "user" for f in q(
                  "SELECT source_speaker FROM tenant_memnos.semantic WHERE %s = ANY(source_turn_ids)",
                  out_u["turn_id"])))

    print("=== D. ingest_session keeps the pinned benchmark prompt (DB) ===")
    llm.facts = []
    sd = datetime(2023, 5, 8, 13, 56, tzinfo=timezone.utc)
    mem.ingest_session(f"{ns}-bench", [("Caroline", "I went to a LGBTQ support group yesterday."),
                                        ("Melanie", "That's great!")], session_date=sd)
    check("D ingest_session system prompt == 'DATE: <date>. ' + pinned golden body",
          llm.system_prompt() == f"DATE: {sd}. " + _EXTRACT_PROMPT_BENCHMARK
          and sha(llm.system_prompt()[len(f'DATE: {sd}. '):]) == GOLDEN_PROMPT_BODY)

    print("=== E. corroboration is not circular (DB) ===")

    def turn(speaker, text):
        return mem.remember_turn(ns, text, speaker=speaker, session_id="s2")[0]

    def fact_row(stmt):
        return q("SELECT id, restatements, salience, source_turn_ids FROM tenant_memnos.semantic "
                 "WHERE namespace=%s AND statement=%s AND kind='fact'", ns, stmt)

    # 1. a USER-sourced fact, then echoed by the assistant 3x
    s1 = "The billing service runs on host27."
    f1 = F("billing service", "runs_on", "host27", s1)
    t_u = turn("user", "billing runs on host27")
    mem.write_facts(ns, [f1], datetime.now(timezone.utc), t_u, speaker="user")
    echo_ids = []
    for i in range(3):
        t_a = turn("assistant", f"Right, the billing service runs on host27 ({i}).")
        echo_ids.append(t_a)
        nf, _ = mem.write_facts(ns, [f1], datetime.now(timezone.utc), t_a, speaker="assistant")
    r = fact_row(s1)
    check("E assistant echoes insert no new row (still one live fact)", len(r) == 1)
    check("E 3 assistant-only restatements -> restatements stays 0, salience stays 0.5",
          r[0]["restatements"] == 0 and abs(r[0]["salience"] - 0.5) < 1e-6)
    check("E assistant echo turns still unioned into provenance (history kept)",
          set(echo_ids) | {t_u} <= set(r[0]["source_turn_ids"]))
    t_u2 = turn("user", "yes billing is on host27")
    mem.write_facts(ns, [f1], datetime.now(timezone.utc), t_u2, speaker="user")
    r = fact_row(s1)[0]
    check("E a USER restatement increments corroboration (0 -> 1, salience bumped)",
          r["restatements"] == 1 and r["salience"] > 0.5)

    # 2. an ASSISTANT-originated fact restated only by the assistant
    s2 = "The staging database is now Postgres 16."
    f2 = F("staging database", "version", "Postgres 16", s2)
    for i in range(3):
        mem.write_facts(ns, [f2], datetime.now(timezone.utc),
                        turn("assistant", f"staging db is Postgres 16 now ({i})"), speaker="assistant")
    r = fact_row(s2)
    check("E assistant-originated fact restated only by the assistant stays at 0",
          len(r) == 1 and r[0]["restatements"] == 0)
    mem.write_facts(ns, [f2], datetime.now(timezone.utc), turn("user", "confirmed, staging is pg16"),
                    speaker="user")
    check("E ...and the first user confirmation counts (-> 1)", fact_row(s2)[0]["restatements"] == 1)

    # 3. unattributed (speaker NULL: files / legacy / scripts) keeps today's behavior
    s3 = "The nightly backup job runs at 02:00 UTC."
    f3 = F("nightly backup job", "runs_at", "02:00 UTC", s3)
    mem.write_facts(ns, [f3], datetime.now(timezone.utc), turn(None, "backup at 2am utc"))
    mem.write_facts(ns, [f3], datetime.now(timezone.utc), turn(None, "backups: 02:00 UTC"))
    check("E speaker=NULL restatement still counts (unchanged legacy behavior)",
          fact_row(s3)[0]["restatements"] == 1)
    # a mixed-provenance restatement (session batch with a user turn in it) counts
    fid = fact_row(s2)[0]["id"]
    counted = store.bump_restatement("tenant_memnos", fid, [turn("assistant", "a"), turn("user", "b u")])
    check("E mixed assistant+user source turns count as corroboration", counted
          and fact_row(s2)[0]["restatements"] == 2)
    check("E no source turn ids (legacy caller) still counts",
          store.bump_restatement("tenant_memnos", fid, []) and fact_row(s2)[0]["restatements"] == 3)

    # cleanup
    with store.conn.cursor() as c:
        for n in (ns, f"{ns}-bench"):
            c.execute("DELETE FROM tenant_memnos.semantic WHERE namespace=%s", (n,))
            c.execute("DELETE FROM tenant_memnos.raw_turns WHERE namespace=%s", (n,))


def main():
    part_a()
    part_b()
    part_cde()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
