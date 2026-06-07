"""Claude via the `claude -p` CLI — an LLM backend that rides the Claude Code
subscription auth (NO Anthropic API key, no per-token charge). Use it for the
COST-HEAVY tasks (bulk extraction) and keep paid GPT-5 for the cheap-per-call
answerer — the provider mix.

Exposes:
  extract(text, date) -> [{subject,predicate,object,statement}]   (engine extract_fn)
  judge(q, expected, predicted) -> 0|1                            (cross-provider judge)

Latency is ~5-15s/call (CLI spin-up + no batching) but $0 marginal — parallelize.
"""
import json
import re
import subprocess

_EXTRACT_SYS = (
    "Extract EVERY atomic, self-contained FACT about any person in this conversation — be "
    "EXHAUSTIVE, do not skip minor details. Cover hobbies/activities, experiences/events, "
    "preferences/opinions, possessions, relationships & who they met, places been/lived, "
    "jobs/education, plans, and feelings/values. List EACH distinct item separately (one fact "
    "per martial art, per dessert, per country). RESOLVE relative dates ('yesterday','last "
    "Saturday') to ABSOLUTE using the SESSION DATE, and pronouns to named people. For each fact: "
    "statement = a full self-contained sentence (with the date if known); subject = the named "
    "person; predicate = a short normalized relation ('lives_in','works_at','did_activity',"
    "'met_person','visited','likes') or '' if it doesn't fit; object = the value or ''. ALWAYS "
    'include a statement. Output ONLY JSON {"facts":[{"subject":"","predicate":"","object":"","statement":""}]}.')

_JUDGE_TMPL = ("Question: {q}\nReference answer: {exp}\nModel answer: {pred}\n"
               "Does the model answer convey the same key information as the reference "
               "(ignore wording/format; for list answers it must cover all items)? "
               "Reply with ONLY the word YES or NO.")


def _run(prompt, timeout=120):
    # --bare = skip hooks/LSP/plugins: programmatic claude -p calls must NOT trigger the
    # global Claude Code hooks (e.g. the memnos remember hook would store every judge/
    # extract prompt into prod memory).
    r = subprocess.run(["claude", "-p", "--bare", prompt],
                       capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "").strip()


def _parse_json(out):
    m = re.search(r"\{.*\}", out, re.S)      # tolerate ```json fences / prose around it
    if not m:
        return {}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {}


def extract(text, date):
    out = _run(f"{_EXTRACT_SYS}\n\nSESSION DATE: {date}\n\n{text}")
    facts = _parse_json(out).get("facts", [])
    res = []
    for f in facts:
        if isinstance(f, dict) and str(f.get("statement", "")).strip():
            res.append({"subject": str(f.get("subject", "")).strip(),
                        "predicate": str(f.get("predicate", "")).strip().lower().replace(" ", "_"),
                        "object": str(f.get("object", "")).strip(),
                        "statement": str(f["statement"]).strip()})
    return res


def judge(q, expected, predicted):
    out = _run(_JUDGE_TMPL.format(q=q, exp=expected, pred=predicted), timeout=60)
    return 1 if out.strip().upper().startswith("YES") else 0
