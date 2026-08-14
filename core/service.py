"""Phase C — memnos memory SERVICE: a clean remember/recall API over the engine,
plus the Anthropic memory-tool op surface. This is what an MCP server / Claude Code
hooks call. No query-time LLM on recall (quota retrieval + rerank).

Multi-tenant: ONE schema (tenant_memnos) with a `namespace` column per the design
(namespace = user/team/agent scope). recall() = Phase A quota retrieval.
"""
from __future__ import annotations

import functools
import logging
import math
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta

from .store import BrainStore, query_clamp, RECALL_ARM_FAILURES, classify_arm_failure
from . import rerank as brain_rerank

logger = logging.getLogger(__name__)

_TENANT = "memnos"
_PROPER = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")


def _record_arm_failure(reasons, namespace, arm, exc, hint=None):
    """issue #41 fix C: a recall arm (raw/semantic search, the timeline/entity-guarantee
    arm, a wide-recall per-namespace fetch) hit a RECALL_ARM_FAILURES-class error — log
    the FULL detail server-side (same place #41 was originally diagnosed from: the
    server log) and, if the caller wants a client-visible record, append a SANITIZED
    entry: namespace, which arm, the exception's class name, and its SQLSTATE if it has
    one. Deliberately never the raw exception message — that can echo query text — into
    anything that reaches the client; the message stays server-side in the log line.

    issue #59: `hint` is an OPTIONAL, already-classified string (see
    core/store.py:classify_arm_failure) a phase-A caller can pass when it ran the cheap
    cold-start-vs-genuinely-broken control probe — turns an opaque exception name into a
    legible "still warming up, retry" or "connection itself is unresponsive" signal.
    Every other call site keeps passing no hint and gets exactly today's behavior."""
    logger.warning("recall arm degraded: namespace=%s arm=%s %s: %s",
                   namespace, arm, type(exc).__name__, exc)
    if reasons is not None:
        entry = {"namespace": namespace, "arm": arm,
                 "error": type(exc).__name__,
                 "sqlstate": getattr(exc, "sqlstate", None)}
        if hint:
            entry["hint"] = hint
        reasons.append(entry)

# Belief-change supersession applies ONLY to SINGLE-VALUED attributes (a person has one
# current home/job/age — a new value replaces the old). MULTI-VALUED relations
# ('did_activity','met_person','visited','likes','owns') are ADDITIVE — a new martial art
# does NOT replace a previous one. Over-superseding list items corrupts aggregation +
# temporal recall, so default is ADDITIVE unless the predicate matches a single-valued cue.
_SINGLE_VALUED_CUES = ("live", "reside", "home", "based", "located", "current_",
                       "work_at", "works_at", "employ", "employer", "job_title", "occupation",
                       "role_at", "age", "marital", "married", "spouse", "status",
                       # work/ops functional attributes (field issue: ops facts were never
                       # superseded — the cue list was LoCoMo-personal-tuned). Each reviewed
                       # against multi-valued corruption: all describe ONE current state of a
                       # system/work item. did/visited/likes/met/owns stay additive.
                       "blocked",            # is_blocked_by / blocked_on — one current blocker state
                       "runs_on", "can_handle", "capacity", "deployed_", "version",
                       "recommended_action",
                       # value-attribute cues (field issue #10 retest: 'rate_limit' never
                       # superseded). Each reviewed against multi-valued corruption: all
                       # name ONE current scalar setting of a system. visited/did/met and
                       # the other additive relations stay out of this list.
                       "quota", "threshold", "timeout", "max_", "min_", "count_of")
# Exact-match-only cues: substring matching would be unsafe ('uses' is inside 'causes' /
# 'houses'; bare 'recommended' is inside plausibly multi-valued 'recommended_books').
_SINGLE_VALUED_EXACT = {"uses", "recommended", "recommendation"}
# Token-match-only cues: substring matching would be unsafe ('rate' is inside 'operates' /
# 'celebrates'; 'limit' is inside 'unlimited'), so these must match a whole _-separated
# predicate token ('rate_limit' → {'rate','limit'} → single-valued; 'operates_in' stays out).
_SINGLE_VALUED_TOKENS = {"rate", "limit"}

# QUANTIFIED-OBJECT default (issue #10 residual A, the safer general rule): same subject +
# IDENTICAL predicate + DIFFERENT object where the NEW object is a quantity ("200 rps",
# "100 requests per second", "$5,000", "30%") is a value UPDATE — single-valued by
# default regardless of the cue list. Guarded by _MULTI_VALUED_TOKENS: additive relations
# (visited/did/met/likes/owns...) must NEVER supersede, even with a quantified object
# ("ran 10 km" does not replace "ran 5 km" — separate events).
_QUANTIFIED_OBJ_RE = re.compile(
    r"^\s*(?:~|≈|<=|>=|<|>)?\s*[$€£]?\d[\d,.]*\s*(?:[a-zA-Z%/][\w\s/%.-]*)?$")
_MULTI_VALUED_TOKENS = {"visited", "visit", "did", "met", "meets", "likes", "liked",
                        "like", "owns", "own", "attended", "attends", "watched", "read",
                        "tried", "went", "ate", "played", "bought", "experienced",
                        "activity", "hobby", "enjoys"}

# Past-state markers: a statement asserting where things STOOD in the past ("Alice lived in
# Boston in 2019"), as opposed to a change-of-state assertion that implies a new CURRENT
# state ("Alice moved to Seattle last week" — 'moved' is deliberately NOT in this list).
# Used to gate backdated supersession — see _write_fact's bi-temporal note.
_HISTORICAL_RE = re.compile(
    r"\b(lived|resided|worked|was|were|used to|had been|formerly|previously|originally|"
    r"back in|at the time|grew up)\b", re.I)
# Change-of-state override for the historical gate: "the rate limit WAS CHANGED to 200"
# trips _HISTORICAL_RE on the bare 'was', but it asserts a new CURRENT value, not a past
# state (issue #10 retest case 4 verbatim). A passive change verb means belief change.
_CHANGE_OF_STATE_RE = re.compile(
    r"\b(?:was|were|has been|have been|is now|are now|got)\s+"
    r"(?:changed|updated|raised|increased|lowered|reduced|set|switched|moved|renamed|"
    r"bumped|upgraded|downgraded|adjusted|revised)\b", re.I)

# Reversal/negation cues: the NEW statement explicitly closes out a prior state. Kept
# high-precision (each must clearly assert that something stopped being true).
_REVERSAL_RE = re.compile(
    r"\b(no longer|not anymore|declined|rejected|not recommended|instead|switched to|"
    r"switching to|resolved|unblocked|cancelled|canceled|called off|reverted|rolled back)\b",
    re.I)

# Salient-token extraction for the negation overlap guard: content words only.
_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_-]{3,}")
_TOKEN_STOP = {"this", "that", "with", "from", "have", "been", "were", "will", "longer",
               "anymore", "after", "before", "into", "over", "under", "they", "them",
               "their", "there", "when", "what", "which", "while", "would", "could",
               "should", "about", "because", "very", "more", "most", "some", "such",
               "than", "then", "only", "also", "just", "like", "being", "does", "doing",
               "still", "instead", "switched", "switching", "resolved", "unblocked",
               "cancelled", "canceled", "called", "reverted", "rolled", "declined",
               "rejected", "recommended"}


def _salient_tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if t not in _TOKEN_STOP}


# --- query-specificity heuristic (issue #11) ----------------------------------------
# Broad status questions ("where are we with the deployment?") embed close to long
# discursive raw turns almost by construction — the turns mention everything — so the
# turn arm outranks the distilled facts that actually answer the question. The fix is
# a CHEAP, DETERMINISTIC classifier (no LLM, no embedding) that shifts ARM ORDER only:
# broad ⇒ facts/dossiers lead the context; specific/neutral ⇒ unchanged (turns stay
# competitive — verbatim questions NEED raw turns first). High-precision by design:
# 'specific' cues (quoted strings / verbatim verbs) always win over 'broad' cues, and
# anything without an explicit broad cue stays 'neutral' — LoCoMo-style single-hop /
# temporal questions must NOT trip the broad pathway. MEMNOS_BROAD_QUERY_TUNE=0 disables.
# Double/curly quotes only — single quotes false-positive on contractions ("what's …
# the team's plan" would scan as a quoted span between the two apostrophes).
_QUOTED_RE = re.compile(r'"[^"]{2,}"|“[^”]{2,}”')
_VERBATIM_RE = re.compile(
    r"\b(exactly|verbatim|quote[ds]?|quoting|word[ -]for[ -]word|literally|precisely|"
    r"say|says|said|saying|tell|tells|told|mention|mentions|mentioned|wrote|write|"
    r"phrase[d]?|wording|describe[d]?|express(ed)?)\b", re.I)
_BROAD_RE = re.compile(
    r"\b(where (are|do) we|where (do|does) (things|it|this|that) stand|"
    r"where do we stand|status|state of|progress|update[sd]? on|latest on|the latest|"
    r"overview|catch (me|us) up|summar(y|ies|ize[d]?|ise[d]?)|recap|big picture|"
    r"how (is|are) .{0,40}(going|coming along|shaping up|doing)|so far|"
    r"current(ly)? (state|situation)|bring me up to (speed|date)|fill me in|"
    r"what.{0,12}(going on|happening) with)\b", re.I)


# Question-word capitals are not entities ("Where are we…" — _PROPER would count 'Where').
_Q_CAPS = {"What", "Whats", "Where", "When", "Who", "Whom", "Whose", "Why", "How",
           "Which", "Give", "Tell", "Can", "Could", "Would", "Should", "Did", "Does",
           "The", "Are", "Is", "Was", "Were", "Has", "Have", "Any", "Please"}


def query_specificity(query: str) -> str:
    """'specific' | 'broad' | 'neutral'. Deterministic, ordered: verbatim/quoted cues
    beat broad cues; long or entity-dense questions never classify broad (a question
    naming 2+ proper entities is about THOSE things, not a status sweep)."""
    q = query or ""
    if _QUOTED_RE.search(q) or _VERBATIM_RE.search(q):
        return "specific"
    ents = {e for e in _PROPER.findall(q) if e not in _Q_CAPS}
    if _BROAD_RE.search(q) and len(q.split()) <= 20 and len(ents) <= 1:
        return "broad"
    return "neutral"


# --- list/aggregation intent (issue #2: the #11-class ranking gap) -------------------
# EXTENDS the #11 classifier (does NOT build a parallel one). The HDIG field case ("what
# projects does the MR reviewer monitor") FOUND the answer facts but ranked them 15-22,
# under raw-turn noise — a list/aggregation question whose crisp answer lives in distilled
# facts/dossiers, not in any single discursive turn. Such questions want the DISTILLED
# answer read first. Distinct from #11 'broad': those are status sweeps ('where are we'),
# these enumerate ('which / how many / what <plural> …'). High precision: a VERBATIM cue
# (query_specificity == 'specific') ALWAYS wins — "what exactly did X say" is not a list
# question even if phrased 'what …'. Entity-density does NOT veto here: "what projects does
# the HDIG MR reviewer monitor" names entities yet is squarely a list question.
_LIST_RE = re.compile(
    r"\b(how many|how much|which|what (?:projects?|agents?|services?|systems?|tools?|"
    r"tickets?|tasks?|items?|repos?|repositories|hosts?|servers?|clients?|vendors?|"
    r"people|teams?|things?|files?|components?|modules?|features?|pets?|hobbies|"
    r"languages?|frameworks?|members?|roles?|responsibilities|kinds?|types?|"
    r"activities|countries|places|cities)\b|"
    r"\b(list|enumerate|all of the|all the)\b|"
    r"\bwhat are\b|\bwho (?:all |are )\b)", re.I)


def query_intent(query: str) -> str:
    """'verbatim' | 'list' | 'broad' | 'neutral'. The unified non-LLM intent for the
    recall rank/render path. Ordered so the most answer-shape-decisive cue wins:
      verbatim (quoted / 'exactly'/'said'...)  -> raw turns stay first (today's behavior)
      list/aggregation ('which / how many …')  -> distilled facts lead (issue #2)
      broad status sweep ('where are we …')    -> facts lead (issue #11)
      neutral (single-hop / temporal)          -> turn-first (today's behavior)
    'verbatim' subsumes query_specificity=='specific' (the regression detector)."""
    spec = query_specificity(query)
    if spec == "specific":
        return "verbatim"
    if _LIST_RE.search(query or ""):
        return "list"
    if spec == "broad":
        return "broad"
    return "neutral"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# issue #12 phase 2: bound on how many of a hybrid recall's independent SQL arms
# (primary raw-turn + semantic, plus one raw+semantic pair per grounded/wide
# namespace) run AT ONCE via their own short-lived pooled connection. Default 2 —
# NOT #12 phase 1's rerank threads=min(4,cpu_count) precedent — because this cap
# multiplies POOL-WIDE connection pressure, not just one process's CPU: every
# simultaneous in-flight recall claims up to this many connections at once (see
# _dispatch_sql_jobs), so the real constraint is MEMNOS_POOL_MAX shared across every
# concurrent request the server is handling, not this one recall's own speed. 2
# covers the common case (no grounded/wide fan-out — just primary raw + semantic,
# where the issue's own profiling says most of the win lives) at the smallest
# possible pool cost; a deployment with headroom (bigger MEMNOS_POOL_MAX, low
# concurrent-request volume) can raise it.
def _sql_concurrency_cap() -> int:
    return max(1, _env_int("MEMNOS_RECALL_SQL_CONCURRENCY", 2))


def _pred_tokens(pred: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (pred or "").lower()) if t}


def _is_single_valued(pred: str) -> bool:
    if not pred:
        return False
    p = pred.lower()
    return (p in _SINGLE_VALUED_EXACT or any(cue in p for cue in _SINGLE_VALUED_CUES)
            or bool(_SINGLE_VALUED_TOKENS & _pred_tokens(p)))


def _supersedable(pred: str, obj: str | None) -> bool:
    """Should a new (subject, pred, obj) fact close out the prior value for the same
    subject+predicate? True when the predicate matches a single-valued cue, OR — the
    general value-update rule — when the NEW object is quantified (number/unit pattern)
    and the predicate is not a known additive/multi-valued relation."""
    if not pred:
        return False
    if _is_single_valued(pred):
        return True
    return bool(obj and _QUANTIFIED_OBJ_RE.match(obj)
                and not (_pred_tokens(pred) & _MULTI_VALUED_TOKENS))


def _negation_targets(stmt: str, subj: str | None, cands, neg_thresh: float):
    """Shared reversal/negation close-out GUARD (write path + namespace reconcile):
    from nearest-live candidates, yield the ones the reversal statement `stmt` closes.
    HIGH PRECISION: vector distance <= threshold AND subject agreement (when both sides
    carry one) AND ≥2 shared salient tokens of which at least one is NOT a subject-name
    token (same-entity-but-unrelated facts share only the entity name)."""
    new_toks = _salient_tokens(stmt)
    subj_toks = _salient_tokens(subj or "")
    for cand in cands:
        if cand["dist"] is None or float(cand["dist"]) > neg_thresh:
            continue
        cs = cand.get("subject_entity")
        if subj and cs and cs.lower() != subj.lower():
            continue
        cand_subj_toks = subj_toks | _salient_tokens(cs or "")
        overlap = new_toks & _salient_tokens(cand["statement"])
        if len(overlap) < 2 or not (overlap - cand_subj_toks):
            continue
        yield cand


def _dossier_text(x) -> str:
    """Coerce an LLM fact (string OR {date,event/statement/fact} object) into one clean
    sentence — guards against models that return objects despite the string-only prompt."""
    if isinstance(x, str):
        return x.strip()
    if isinstance(x, dict):
        body = (x.get("statement") or x.get("event") or x.get("fact")
                or x.get("text") or "").strip()
        date = str(x.get("date", "")).strip()
        if body and date and date not in body:
            return f"{body} ({date})"
        if body:
            return body
    return ""


class MemnosMemory:
    def __init__(self, store_or_dsn, embed_fn, *, reranker_model=brain_rerank.DEFAULT_RERANKER,
                 dim=1536, llm=None, extract_model="gpt-4o-mini", ensure_schema=False,
                 on_usage=None, extract_fn=None, redact=True, author=None):
        # store_or_dsn may be a pooled BrainStore (production), a DSN string (scripts), or
        # None — for phased callers (the server) that only use the no-DB methods
        # (extract_facts, recall_rank, ...) or pass conn_factory to the phased methods,
        # attaching a short-lived store per DB phase. Pool connections must NEVER be held
        # across LLM/embedding/network work.
        if store_or_dsn is None or isinstance(store_or_dsn, BrainStore):
            self.store = store_or_dsn
        else:
            self.store = BrainStore(store_or_dsn)
        self.embed = embed_fn
        self.reranker = reranker_model
        self.llm, self.extract_model = llm, extract_model
        # on_usage(model, prompt_tokens, completion_tokens): called after EVERY engine LLM
        # call (extraction + consolidation) so token cost is fully accounted — the server
        # records it to usage_ledger, the benchmark feeds it to the budget cap. No silent
        # untracked spend.
        self.on_usage = on_usage
        # extract_fn(text, date) -> [{subject,predicate,object,statement}]. Lets the COST-HEAVY
        # extraction run on a different (e.g. subscription-billed Claude CLI) backend while the
        # cheap-per-call answerer stays on a paid API — the provider mix. None = OpenAI path.
        self.extract_fn = extract_fn
        self.redact = redact          # strip secrets from text BEFORE it enters memory
        # author = the AUTHENTICATED principal's name (set by the server from the bearer
        # token, NEVER from a request body — non-spoofable) or 'system' for scheduled
        # jobs. Stamped as author_principal on every raw_turn/fact/episode this instance
        # writes. None (scripts/benchmarks) leaves the column NULL.
        self.author = author
        self.schema = self.store.create_schema(_TENANT, dim=dim) if ensure_schema else f"tenant_{_TENANT}"

    def _track(self, model, resp):
        if self.on_usage is not None:
            try:
                u = resp.usage
                self.on_usage(model, u.prompt_tokens, u.completion_tokens)
            except Exception:
                pass

    # --- WRITE -------------------------------------------------------------
    def remember(self, namespace: str, text: str, *, speaker=None, session_id=None,
                 observed_at=None, extract=True, memory_type=None) -> dict:
        """Store a message: raw turn (always) + STRUCTURED date-aware SPO fact
        extraction (optional, offline LLM). Each fact is stored with
        subject/predicate/object so belief-change SUPERSESSION fires (close out the
        prior value for the same subject+predicate) and the entity GRAPH is populated
        — parity with the validated phaseA engine. Returns ids + supersession count.

        NOTE for pooled callers (the server): this convenience method holds the store's
        connection across the LLM extraction call. Use the split phases below
        (remember_turn / extract_facts / write_facts) so a pool connection is never
        pinned for the seconds an LLM call takes — that starves the pool under
        concurrent sessions (field: 30s queueing, BrokenPipes)."""
        observed_at = observed_at or datetime.now(timezone.utc)
        tid, text, observed_at = self.remember_turn(namespace, text, speaker=speaker,
                                                    session_id=session_id, observed_at=observed_at,
                                                    memory_type=memory_type)
        n_facts = n_super = 0
        if extract and (self.llm is not None or self.extract_fn is not None):
            facts = self.extract_facts(text, observed_at, memory_type=memory_type)
            n_facts, n_super = self.write_facts(namespace, facts, observed_at, tid,
                                                memory_type=memory_type)
        return {"turn_id": tid, "facts": n_facts, "superseded": n_super}

    def remember_turn(self, namespace: str, text: str, *, speaker=None, session_id=None,
                      observed_at=None, vec=None, memory_type=None):
        """Phase 1 (fast, DB only): redact + store the verbatim raw turn. Returns
        (turn_id, redacted_text, observed_at) for the later extraction phases.

        `vec` lets a pooled caller pre-compute the embedding (a NETWORK call in OpenAI
        mode) BEFORE acquiring a pool connection. It must be the embedding of the
        REDACTED text (redact() is idempotent, so pre-redacting and embedding that is
        safe). Default (vec=None) embeds here — identical to the original behavior."""
        observed_at = observed_at or datetime.now(timezone.utc)
        if self.redact:
            from .redact import redact as _redact
            text, _ = _redact(text)             # strip secrets BEFORE storage + extraction
        tid = self.store.insert_raw_turn(self.schema, namespace, session_id, speaker,
                                         text, observed_at,
                                         vec if vec is not None else self.embed(text),
                                         author=self.author, memory_type=memory_type)
        return tid, text, observed_at

    def extract_facts(self, text, observed_at, *, memory_type=None):
        """Phase 2 (slow, NO database use): LLM fact extraction. Safe to run with no
        connection held — pure model I/O.

        CONSTRAINT BYPASS (issue #29): a constraint is a GUARDRAIL, not prose to
        summarize. LLM extraction paraphrases, splits, and can INVERT a rule's
        meaning or misattribute its actor (repro: "the agent must fix reviewer
        blockers" extracted as "the reviewer will fix blockers"). memory_type=
        'constraint' skips the LLM entirely — no facts are extracted, so nothing
        paraphrased ever gets stored. The verbatim text is already durable via
        remember_turn()'s raw_turn write (memory_type stamped there too), which
        pinned_constraints() reads directly — the raw turn IS the constraint."""
        if memory_type == "constraint":
            return []
        return self._extract(text, observed_at)

    def write_facts(self, namespace, facts, observed_at, turn_id, *, memory_type=None,
                    valid_anchor=None) -> tuple:
        """Phase 3 (fast, DB only): supersession + store + graph for extracted facts.
        `memory_type` = the source TURN's type — derived facts INHERIT it.

        `valid_anchor` (issue #42, default None): optional override for the EVENT-time
        (`valid_from`) fallback anchor ONLY — see `_write_fact` for why this is safe to
        decouple from `observed_at`. None (every caller except the server's write-behind
        replay path) preserves the original single-anchor behavior exactly."""
        n_facts = n_super = 0
        for f in facts:
            df, ds = self._write_fact(namespace, f, observed_at, source_turn_ids=[turn_id],
                                      memory_type=memory_type, valid_anchor=valid_anchor)
            n_facts += df; n_super += ds
        return n_facts, n_super

    def ingest_session(self, namespace: str, turns, *, session_date, session_id=None,
                       extract=True) -> dict:
        """BENCHMARKED ingest path (per-SESSION batch). Store each raw turn, then extract
        SPO facts from the WHOLE session at once (better pronoun / relative-date
        resolution than per-message), then supersede + graph-populate via the same
        `_write_fact` used by remember(). `turns` = [(speaker, text), ...].

        This is the SAME code the LoCoMo benchmark runs — there is one engine, not two.
        Feed sessions in chronological order so belief-change supersession closes the
        OLDER value first."""
        if self.redact:
            from .redact import redact as _redact
            turns = [(spk, _redact(txt)[0] if txt else txt) for spk, txt in turns]
        tids = []
        for ti, (spk, txt) in enumerate(turns):
            if not txt:
                continue
            tids.append(self.store.insert_raw_turn(self.schema, namespace, session_id, spk, txt,
                                                   session_date + timedelta(minutes=ti), self.embed(txt),
                                                   author=self.author))
        n_facts = n_super = 0
        if extract and self.llm is not None:
            content = f"SESSION DATE: {session_date}\n\n" + "\n".join(
                f"{s}: {t}" for s, t in turns if t)
            for f in self._extract(content, session_date):
                df, ds = self._write_fact(namespace, f, session_date, source_turn_ids=tids)
                n_facts += df; n_super += ds
        return {"turns": len(turns), "facts": n_facts, "superseded": n_super}

    def _write_fact(self, namespace, f, fallback_date, *, source_turn_ids=(),
                    memory_type=None, valid_anchor=None):
        """Write ONE SPO fact: near-duplicate collapse → absolute event date →
        belief-change supersession (SPO + reversal/negation close-out) → store with
        subject/predicate/object (+ provenance to its source turn) → populate the
        entity graph. Shared by remember() and ingest_session() so the write path can
        never drift between them. Returns (facts_written, superseded_count).

        BI-TEMPORAL SEMANTICS (chosen, documented): supersession is a BELIEF change, so
        it is keyed on the OBSERVATION axis (`fallback_date` = when we learned the fact;
        stored as semantic.observed_at), not on event order alone. A fact observed LATER
        supersedes an earlier-observed value even when its EVENT date backdates ("Alice
        moved to Seattle last week" arriving after "Alice lives in Austin": the knowledge
        is newer even though the move predates the Austin fact's valid_from stamp).
        The one exception is a backdated HISTORICAL statement — past-state wording
        ("Alice lived in Boston in 2019", _HISTORICAL_RE) with an event date older than
        the current value's valid_from: that describes the past, it does not change the
        current belief, so it must NOT supersede. valid_to on the closed fact is the new
        fact's event date (or observed date when no event date), clamped to never precede
        the closed fact's own valid_from. Deterministic; no LLM at this layer.

        `valid_anchor` (issue #42, default None → falls back to `fallback_date`, i.e. the
        ORIGINAL unchanged behavior for every caller except the server's write-behind
        replay path): the fallback anchor `parse_event_date` uses to resolve a RELATIVE
        date ("yesterday") when the statement carries no explicit absolute date. This is
        deliberately a SEPARATE knob from `fallback_date`/`obs` below — `obs` (observation
        axis, `known_at`) MUST always be the server's own write/replay-commit time (never
        client-influenced — see memnos_server.py's `_remember_phased` and
        offline_queue.py's `_post_remember` for the auth rationale: trusting a client
        timestamp there is a supersession-race backdating primitive). `valid_anchor` only
        affects the EVENT-time axis of THIS fact (and, via the existing GREATEST(...)
        clamp in supersede_predicate, the `valid_to` boundary stamped on whichever fact it
        supersedes) — orthogonal to which fact wins that race, which stays governed
        entirely by `obs`."""
        from .temporal import parse_event_date
        stmt = f["statement"]
        subj = (f["subject"][:100] if f["subject"] else None)
        pred = f["predicate"] or None
        obj = f["object"] or None
        date_anchor = valid_anchor if valid_anchor is not None else fallback_date
        ev = parse_event_date(stmt, date_anchor)             # relative → absolute event date
        obs = fallback_date                                  # observation/knowledge axis
        vec = self.embed(stmt)

        # WRITE-PATH DEDUPE (issue #10 density half): a verbatim/near restatement of a
        # LIVE fact does not insert a new row — it reinforces the existing one (salience
        # bump + restatements counter + provenance union). MEMNOS_DEDUPE_THRESHOLD=0 disables.
        dedupe_thresh = _env_float("MEMNOS_DEDUPE_THRESHOLD", 0.03)
        if dedupe_thresh > 0:
            dup = self.store.find_near_duplicate(self.schema, namespace, vec, subj,
                                                 dedupe_thresh)
            if dup is not None:
                self.store.bump_restatement(self.schema, dup["id"], source_turn_ids)
                return 0, 0

        # HISTORICAL gate: a past-state statement may only supersede when it carries an
        # EXPLICIT in-statement event date (parse_event_date with no fallback) — the SQL
        # guard then requires that date to be >= the old fact's valid_from. A historical
        # statement with NO parseable event date never supersedes (most conservative:
        # "Alice lived in Boston in 2019" — bare years don't parse — describes the past).
        hist = bool(_HISTORICAL_RE.search(stmt)) and not _CHANGE_OF_STATE_RE.search(stmt)
        explicit_ev = parse_event_date(stmt, None) if hist else None
        superseded_ids = []
        insert_kwargs = dict(subject=subj, predicate=pred, obj=obj, valid_from=ev,
                             salience=0.5, vec=vec, source_turn_ids=source_turn_ids,
                             author=self.author, memory_type=memory_type, observed_at=obs)
        if (subj and pred and _supersedable(pred, obj)       # belief-change ONLY for single-valued attrs
                and not (hist and explicit_ev is None)):     # (cue list OR quantified-object rule)
            # issue #60 — CONCURRENT/OUT-OF-ORDER WRITER GUARD. Two facts for the same
            # (namespace, subject, predicate) can commit out of OBSERVATION order: plain
            # concurrent /remember calls, or replayed write-behind writes landing on
            # different MEMNOS_INGEST_WORKERS threads (offline_queue.drain() explicitly
            # supports concurrent drainers). A Postgres advisory xact lock alone does NOT
            # fix this — it only serializes the two writers, it doesn't stop a smaller-obs
            # write from being inserted live AFTER a larger-obs write already committed.
            # The actual fix is `dominant_live_fact` below (the mirror image of
            # supersede_predicate); the lock is what makes that check race-safe instead of
            # a TOCTOU read — without it, two concurrent writers could each see "not
            # dominated" before either commits, and both still land live. Lock scope is
            # this fact only (acquired/released within this transaction), so it can't
            # deadlock against itself or hold across unrelated keys. It blocks under the
            # connection's normal statement_timeout (MEMNOS_STMT_TIMEOUT_MS, default 15s)
            # — contention on one subject+predicate should be rare and brief; if it isn't,
            # surfacing a clear timeout is preferable to the silent corruption this guards
            # against.
            lock_key = f"{namespace}\x01{subj.lower()}\x01{pred.lower()}"
            with self.store.conn.transaction():
                with self.store.conn.cursor() as c:
                    c.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (lock_key,))
                superseded_ids = self.store.supersede_predicate(
                    self.schema, namespace, subj, pred, obj, ev, observed_at=obs,
                    historical=hist, event_date=explicit_ev)
                dominant = self.store.dominant_live_fact(
                    self.schema, namespace, subj, pred, obj, obs,
                    historical=hist, event_date=explicit_ev)
                fid = self.store.insert_semantic(self.schema, namespace, "fact", stmt,
                                                 **insert_kwargs)
                if superseded_ids:
                    self.store.mark_superseded_by(self.schema, superseded_ids, fid)
                if dominant is not None:
                    # born already-superseded — a MORE RECENTLY observed fact is already
                    # live. Same clamp as the reversal/negation close-out below (ev or obs
                    # never precedes this fact's own start).
                    self.store.close_out(self.schema, namespace, fid,
                                         valid_to=(dominant["valid_from"] or obs),
                                         superseded_by=dominant["id"])
        else:
            fid = self.store.insert_semantic(self.schema, namespace, "fact", stmt,
                                             **insert_kwargs)
        n_super = len(superseded_ids)

        # REVERSAL/NEGATION close-out: extraction often emits reversals with no usable
        # predicate ("The zeta deployment is no longer blocked." → pred=None), so SPO
        # supersession structurally can't fire. When the NEW statement carries an explicit
        # reversal cue, close out the semantically-nearest LIVE facts — HIGH PRECISION:
        # require cue AND vector similarity AND salient-token overlap (≥2 shared content
        # words, at least one of which is NOT a subject-name token — same-entity-but-
        # unrelated facts share only the entity name) AND subject agreement when both
        # sides carry one. MEMNOS_NEGATION_THRESHOLD=0 disables.
        neg_thresh = _env_float("MEMNOS_NEGATION_THRESHOLD", 0.40)
        if n_super == 0 and neg_thresh > 0 and _REVERSAL_RE.search(stmt):
            cands = self.store.nearest_live_facts(self.schema, namespace, vec, k=8,
                                                  exclude_id=fid, observed_before=obs)
            for cand in _negation_targets(stmt, subj, cands, neg_thresh):
                n_super += self.store.close_out(self.schema, namespace, cand["id"],
                                                valid_to=(ev or obs), superseded_by=fid)

        # populate the ENTITY GRAPH from the SPO triple (every fact is an edge)
        if subj:
            se = self.store.upsert_entity(self.schema, namespace, subj)
            self.store.add_mention(self.schema, se, fid, "semantic")
            if obj:
                oe = self.store.upsert_entity(self.schema, namespace, obj[:100])
                self.store.add_mention(self.schema, oe, fid, "semantic")
                self.store.bump_edge(self.schema, namespace, se, oe)
        return 1, n_super

    def _extract(self, text, date):
        """EXHAUSTIVE statement-first extraction with optional SPO metadata.

        The `statement` is the retrieval unit (embedded + searched), so coverage matters
        most: capture EVERY fact, not just clean triples. subject/predicate are best-effort
        metadata that enable belief-change supersession when applicable — a fact that
        doesn't fit a triple is still captured (empty predicate). Measured: rigid
        SPO-only + 700-token cap under-extracted (~12 facts / 33-turn session); answers
        existed in raw turns but never became facts."""
        import json
        if self.extract_fn is not None:          # pluggable backend (e.g. Claude CLI, free via sub)
            try:
                return self.extract_fn(text, date)
            except Exception:
                return []
        try:
            r = self.llm.chat.completions.create(
                model=self.extract_model, temperature=0, max_tokens=2000,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content":
                           f"DATE: {date}. Extract EVERY atomic, self-contained FACT about any person, "
                           "project, or work item in this conversation — be EXHAUSTIVE, do not skip minor "
                           "details. Cover: hobbies & activities, experiences & events, preferences & "
                           "opinions, possessions, relationships & who they met, places been/lived, jobs & "
                           "education, plans, feelings/values, and decisions/outcomes/identifiers from work "
                           "discussions. PRESERVE identifiers VERBATIM in statements — ticket keys (ABC-123), "
                           "PR/MR numbers, version numbers, URLs, file/host names — never paraphrase them "
                           "away. List EACH distinct item separately (e.g. one fact per martial "
                           "art, per dessert, per country). RESOLVE relative dates ('yesterday','last "
                           "Saturday') to ABSOLUTE using DATE, and pronouns to named people. For each fact: "
                           "statement = a full self-contained sentence (with the date if known); subject = "
                           "the named person OR work item/system it's about; predicate = a short normalized "
                           "relation ('lives_in','works_at','did_activity','met_person','visited','likes'; "
                           "for work items: 'uses','status','is_blocked_by','can_handle','runs_on') or '' if "
                           "it doesn't fit; object = the value or ''. When a fact CHANGES or REVERSES a "
                           "previous state ('switched to','no longer','now handles'), fill subject and the "
                           "SAME predicate the prior fact would use, with the NEW value as object — e.g. 'we "
                           'switched the pipeline to MySQL\' -> {"subject":"pipeline","predicate":"uses",'
                           '"object":"MySQL","statement":"The pipeline uses MySQL."}. EVERY fact object MUST '
                           "have all four keys. ALWAYS include a statement even when "
                           'subject/predicate/object are empty. JSON {"facts":[{"subject":"","predicate":"",'
                           '"object":"","statement":"..."}]}.'},
                          {"role": "user", "content": text}])
            self._track(self.extract_model, r)
            out = []
            for f in json.loads(r.choices[0].message.content).get("facts", []):
                if isinstance(f, dict) and str(f.get("statement", "")).strip():
                    out.append({"subject": str(f.get("subject", "")).strip(),
                                "predicate": str(f.get("predicate", "")).strip().lower().replace(" ", "_"),
                                "object": str(f.get("object", "")).strip(),
                                "statement": str(f["statement"]).strip()})
            return out
        except Exception:
            return []

    # --- READ (no query-time LLM) -----------------------------------------
    def recall(self, namespace: str, query: str, *, k=40, raw_quota=11, fact_quota=8,
               entity_quota=10, subject=None) -> list[dict]:
        """Quota retrieval (raw turns ⊕ semantic facts) with BI-TEMPORAL awareness:
        for time-scoped questions, retrieve facts by EVENT TIME (valid_from window /
        current-vs-past / first-last ordering), lean on dated facts, and surface the
        date so the answerer can reason. No LLM at query time.

        Implemented as recall_fetch (DB only) + recall_rank (CPU only) so pooled callers
        can release the connection before the ~200ms cross-encoder rerank. This wrapper
        keeps the original single-call behavior for in-process users."""
        return self.recall_rank(query, self.recall_fetch(namespace, query, k=k),
                                raw_quota=raw_quota, fact_quota=fact_quota,
                                entity_quota=entity_quota, subject=subject)

    def recall_prefetch(self, namespace: str, query: str) -> dict:
        """DB phase A of recall (issue #12): every arm that does NOT need the query
        embedding — the 'now' watermark, temporal-intent analysis, query-entity
        extraction, and the timeline / entity-guarantee arms (JOIN/ILIKE scans, not
        cosine — at field scale these are the expensive non-vector SQL). Pooled callers
        run this on a short connection WHILE the embedding round-trip is in flight,
        then pass the result to recall_fetch(pre=...). recall_fetch with pre=None calls
        this itself, so in-process users (benchmark, scripts) are byte-identical.

        issue #41 fix C: the 'now' watermark and the timeline/entity-guarantee query are
        each a live per-arm DB call and can fail independently under load. A
        RECALL_ARM_FAILURES-class error degrades that ONE arm — the watermark falls back
        to wall-clock 'now' (the same fallback already used when the namespace has no
        rows), the guarantee arm is simply absent (b['dump']/b['tl'] stay unset, which
        recall_rank already treats as empty via .get()) — instead of failing the whole
        prefetch. Reasons collect into b['_degraded_reasons'] the same shape recall_fetch
        uses, so a caller passing this bundle via pre= merges them into one list."""
        from . import temporal as T
        reasons = []
        try:
            now = self.store.max_observed_at(self.schema, namespace) or datetime.now(timezone.utc)
        except RECALL_ARM_FAILURES as e:
            # issue #59: max_observed_at is the FIRST DB touch of a whole recall,
            # before the query embedding even arrives — the arm most likely to race a
            # cold connection/cache right after a restart. Classify before falling back.
            _record_arm_failure(reasons, namespace, "max_observed_at", e,
                                hint=classify_arm_failure(self.store.conn, e))
            now = datetime.now(timezone.utc)
        intent = T.analyze(query, now)
        if intent.temporal:
            logging.getLogger("memnos.temporal").debug(
                "temporal arm: query=%r matched=%r ents=%r",
                query[:80], getattr(intent, "matched_pattern", None), T.query_entities(query),
            )
        b = {"intent": intent, "ents": T.query_entities(query)}
        if not intent.temporal:
            if b["ents"]:
                # ENTITY-GUARANTEE arm: vector search misses items in list/aggregation
                # answers ('what martial arts' ≁ 'taekwondo'); 82% of eval failures were
                # retrieval misses. Guarantee the facts about the query's entities — the
                # same JOIN-not-cosine trick the timeline arm uses for temporal.
                try:
                    b["dump"] = self.store.timeline(self.schema, namespace, b["ents"],
                                                    order="desc", limit=20, current_only=True)
                except RECALL_ARM_FAILURES as e:
                    _record_arm_failure(reasons, namespace, "timeline", e,
                                        hint=classify_arm_failure(self.store.conn, e))
        else:
            # TEMPORAL: GUARANTEE the entity timeline (parity with the tested phaseA
            # engine). Vector search structurally misses dated evidence ('when did X'
            # ≁ 'I moved out') — a JOIN/range, not a cosine bet. This is the arm that
            # lifted temporal recall 12% → 70%.
            # current_only: "current/now/latest" with NO date range means the user wants
            # the present state, not a history walk — apply valid_to IS NULL filter.
            tl_current = intent.current and intent.start is None and intent.end is None
            try:
                b["tl"] = self.store.timeline(self.schema, namespace, b["ents"], start=intent.start,
                                              end=intent.end, order=intent.order or "asc", limit=12,
                                              current_only=tl_current)
            except RECALL_ARM_FAILURES as e:
                _record_arm_failure(reasons, namespace, "timeline", e,
                                    hint=classify_arm_failure(self.store.conn, e))
        if reasons:
            b["_degraded"] = True
            b["_degraded_reasons"] = reasons
        return b

    def _dispatch_sql_jobs(self, jobs, conn_factory):
        """issue #12 phase 2: fire every independent hybrid-recall SQL arm in `jobs`
        (each a 1-arg `lambda store: ...` callable) AT ONCE instead of one after
        another, then return a list, in the SAME order as `jobs`, of zero-arg
        resolvers the caller invokes to get each job's result (or have it re-raise
        the job's own RECALL_ARM_FAILURES-class error). This indirection is what
        keeps every degrade/dedup/ordering rule downstream byte-identical to the
        pre-#12 sequential code: concurrency only changes WHEN a query ran, never
        which try/except observes its outcome or what order results land in the
        bundle — callers resolve jobs[i] at the exact call site the old blocking
        `self.store.search_...(...)` call used to sit.

        `conn_factory` is None (in-process/test callers — scripts, benchmarks,
        tests/test_recall_arm_degrade.py's direct-DB harness — with no pool to draw
        extra connections from) or there's only one job: resolvers close over
        self.store and run sequentially/lazily on first call, unchanged from before
        this fix.

        `conn_factory` given (production, POOL.connection): jobs[0] reuses THIS
        request's already-checked-out phase-B connection (self.store) — no new
        checkout — and jobs[1:] each get their own short-lived pooled connection.
        Concurrency is bounded to MEMNOS_RECALL_SQL_CONCURRENCY (default 2, see
        _sql_concurrency_cap) so this ONE recall can't fan out past a couple of pool
        slots — the cap isn't about this recall's own speed, it's about how many
        connections EVERY simultaneously in-flight recall claims against the
        shared, server-wide MEMNOS_POOL_MAX; jobs past the cap simply queue for a
        freed worker/connection. A connection-acquisition failure (e.g.
        psycopg_pool.PoolTimeout — a psycopg.OperationalError subclass, itself a
        RECALL_ARM_FAILURES member) surfaces through that job's resolver exactly
        like a query failure, so a saturated pool degrades that ONE arm instead of
        the whole recall or the other concurrent arms.

        Each ephemeral connection's search_* call re-runs its own fts_clamp probe
        (core/store.py) — the SAME per-arm probe the sequential code already ran on
        every call, just now possibly on a freshly-grown pool connection rather
        than the one already-warm connection every arm shared before. fts_clamp's
        own control-probe path (core/store.py's _tsquery_within_bound) already
        tolerates exactly this "first statement on a cold connection" case with a
        1500ms budget — 30x the measured cold-start cost — so a probe that's
        genuinely slow on a brand-new connection degrades that arm, it doesn't
        stall or crash the recall."""
        if conn_factory is None or len(jobs) <= 1:
            return [functools.partial(job, self.store) for job in jobs]
        cap = max(1, min(len(jobs), _sql_concurrency_cap()))
        vtype = self.store.vtype if self.store is not None else None

        def _run_reused(job):
            return job(self.store)

        def _run_new(job):
            with conn_factory() as conn:
                return job(BrainStore(conn=conn, vtype=vtype))

        with ThreadPoolExecutor(max_workers=cap) as ex:
            futures = [ex.submit(_run_reused, jobs[0])]
            futures += [ex.submit(_run_new, job) for job in jobs[1:]]
            outcomes = []
            for f in futures:
                try:
                    outcomes.append((f.result(), None))
                except Exception as e:      # captured here, re-raised lazily below —
                    outcomes.append((None, e))   # same shape a live call would raise

        def _resolver(value, exc):
            def resolve():
                if exc is not None:
                    raise exc
                return value
            return resolve
        return [_resolver(v, e) for v, e in outcomes]

    def recall_fetch(self, namespace: str, query: str, *, k=40, qv=None,
                     extra_namespaces=(), pre=None, timings=None, deadline=None,
                     conn_factory=None) -> dict:
        """DB phase of recall: temporal intent + ALL store queries (raw, semantic,
        timeline/entity-guarantee arms). `qv` lets pooled callers pre-embed the query
        (network) before acquiring a connection. Returns a bundle for recall_rank.

        GROUNDED RECALL (0.1.6): `extra_namespaces` = linked knowledge namespaces the
        CALLER may read (the server checks both the link and the read grant). Their
        candidates are hybrid-searched per namespace, tagged with their source namespace
        ("_ns"), and merged into the bundle — ONE rerank covers everything. Primary-
        namespace rows stay untagged, so with no links the output is unchanged.

        Issue #12 (all optional, defaults = original behavior): `pre` = a
        recall_prefetch bundle computed while the embedding was in flight; `timings`
        = a dict the per-stage durations (sql_ms, staleness_ms) are accumulated into;
        `deadline` = a time.perf_counter() deadline — when already past it, the
        staleness annotation pass is skipped and the bundle is marked '_degraded'.
        `conn_factory` (issue #12 phase 2, e.g. POOL.connection): the primary raw +
        primary semantic arms, and every grounded namespace's raw+semantic pair, run
        CONCURRENTLY instead of sequentially — see _dispatch_sql_jobs. None (every
        caller before this fix) keeps the original one-at-a-time behavior exactly.

        issue #41 fix C: each store call below is an independent recall arm (primary
        raw-turn search, primary semantic search, and one pair per grounded namespace).
        A RECALL_ARM_FAILURES-class error (a canceled/timed-out query, a live DB-side
        error — see core/store.py's RECALL_ARM_FAILURES) degrades JUST that arm to an
        empty contribution rather than failing the whole recall; the OTHER arms' results
        are still returned. b['raw']/b['sem'] are always assigned a list (never left
        missing), since recall_rank indexes them directly. Failures collect into
        b['_degraded_reasons'] (namespace, arm, exception class, sqlstate) and flip
        b['_degraded'] — merged with anything recall_prefetch already recorded in `pre`.
        A bug in the calling code (e.g. psycopg.InterfaceError) is NOT in
        RECALL_ARM_FAILURES and still propagates/crashes loudly, same as before."""
        b = pre if pre is not None else self.recall_prefetch(namespace, query)
        intent = b["intent"]
        reasons = b.get("_degraded_reasons", [])
        if qv is None:
            qv = self.embed(query_clamp(query))   # #15 follow-up: bound a pathological query
        t_sql = time.perf_counter()

        jobs = [lambda store: store.search_raw_turns(self.schema, namespace, qv, query, k)]
        if not intent.temporal:
            jobs.append(lambda store: store.search_semantic(
                self.schema, namespace, qv, query, k, current_only=True))
        else:
            jobs.append(lambda store: store.search_semantic_temporal(
                self.schema, namespace, qv, query, k, start=intent.start, end=intent.end,
                current_only=intent.current, order=intent.order))
        for kns in extra_namespaces:
            jobs.append(lambda store, kns=kns: store.search_raw_turns(self.schema, kns, qv, query, k))
            jobs.append(lambda store, kns=kns: store.search_semantic(
                self.schema, kns, qv, query, k, current_only=True))
        resolve = iter(self._dispatch_sql_jobs(jobs, conn_factory))

        try:
            b["raw"] = next(resolve)()
        except RECALL_ARM_FAILURES as e:
            b["raw"] = []
            _record_arm_failure(reasons, namespace, "raw", e)
        if not intent.temporal:
            try:
                b["sem"] = next(resolve)()
            except RECALL_ARM_FAILURES as e:
                b["sem"] = []
                _record_arm_failure(reasons, namespace, "semantic", e)
        else:
            try:
                b["sem"] = next(resolve)()
            except RECALL_ARM_FAILURES as e:
                b["sem"] = []
                _record_arm_failure(reasons, namespace, "semantic_temporal", e)
        # grounded fan-out: per-knowledge-namespace fetches, merged for the SINGLE rerank
        for kns in extra_namespaces:
            try:
                for r in next(resolve)():
                    r["_ns"] = kns; b["raw"].append(r)
            except RECALL_ARM_FAILURES as e:
                _record_arm_failure(reasons, kns, "raw", e)
            try:
                for r in next(resolve)():
                    r["_ns"] = kns; b.setdefault("sem", []).append(r)
            except RECALL_ARM_FAILURES as e:
                _record_arm_failure(reasons, kns, "semantic", e)
        b["raw"] = self._dedup_candidates(b["raw"])   # issue #2: collapse cron-x10 dups
        t_stale = time.perf_counter()
        if deadline is not None and t_stale >= deadline:
            b["_degraded"] = True              # deadline hit: serve un-annotated turns
        else:
            self._mark_stale_turns(b["raw"])   # DB phase — see _mark_stale_turns
        if reasons:
            b["_degraded"] = True
            b["_degraded_reasons"] = reasons
        if timings is not None:
            done = time.perf_counter()
            timings["sql_ms"] = timings.get("sql_ms", 0.0) + (t_stale - t_sql) * 1000.0
            timings["staleness_ms"] = timings.get("staleness_ms", 0.0) + (done - t_stale) * 1000.0
        return b

    @staticmethod
    def _norm_content(s: str) -> str:
        """Whitespace/case-normalized content key for exact-duplicate collapse."""
        return re.sub(r"\s+", " ", (s or "").strip().lower())

    def _dedup_candidates(self, raw_rows) -> list:
        """RECALL-PATH DEDUPE (issue #2, DB phase): collapse near-identical RAW-TURN
        candidates BEFORE the cross-encoder. This is the cron-x10 killer — the HDIG field
        case had the SAME operational turn duplicated ~10x crowding ranks 1-4 above the
        answer-bearing facts. Two collapse rules, both deterministic and cheap:
          1. EXACT: whitespace/case-normalized content hash (no embedding needed) — kills
             literal duplicates regardless of vectors;
          2. NEAR: embedding cosine distance < the write-path dedupe threshold
             (MEMNOS_DEDUPE_THRESHOLD, 0.03) over ONLY the candidate ids (a bounded
             self-join, not a namespace scan).
        Groups are merged (union-find); ONE survivor is kept per group — highest base score,
        ties broken by most recent observed_at — with a `dup_count` annotation. Dups carry
        no extra signal, so accuracy risk is near zero; it also shrinks the pairs fed to the
        reranker. MEMNOS_RECALL_DEDUP=0 disables (returns the input unchanged)."""
        if not raw_rows or _env_float("MEMNOS_RECALL_DEDUP", 1.0) <= 0:
            return raw_rows
        n = len(raw_rows)
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]; x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        # rule 1: exact normalized-content collapse
        by_content = {}
        for i, r in enumerate(raw_rows):
            key = self._norm_content(r.get("content", ""))
            if key in by_content:
                union(i, by_content[key])
            else:
                by_content[key] = i
        # rule 2: embedding near-duplicate collapse (bounded self-join over candidate ids)
        thresh = _env_float("MEMNOS_DEDUPE_THRESHOLD", 0.03)
        id_to_idx = {r.get("id"): i for i, r in enumerate(raw_rows) if r.get("id") is not None}
        if thresh > 0 and len(id_to_idx) >= 2:
            try:
                for a, b in self.store.near_duplicate_pairs(self.schema, list(id_to_idx), thresh):
                    if a in id_to_idx and b in id_to_idx:
                        union(id_to_idx[a], id_to_idx[b])
            except Exception:                  # pre-migration / no-vector store: exact-only
                pass

        groups = {}
        for i in range(n):
            groups.setdefault(find(i), []).append(i)
        if len(groups) == n:                   # nothing collapsed — preserve identity
            return raw_rows

        def keyfn(idx):                        # survivor = highest score, then most recent
            r = raw_rows[idx]
            return (float(r.get("score") or 0.0),
                    (r.get("observed_at") or datetime.min.replace(tzinfo=timezone.utc)))

        survivors = []
        for members in groups.values():
            best = max(members, key=keyfn)
            row = raw_rows[best]
            if len(members) > 1:
                row["dup_count"] = len(members)
            survivors.append((best, row))
        survivors.sort(key=lambda t: t[0])     # keep the original retrieval order
        return [row for _, row in survivors]

    def _mark_stale_turns(self, raw_rows) -> None:
        """STALENESS pass over RETRIEVED turn rows (issue #10 residual B, DB phase): a
        raw turn is verbatim HISTORY with no supersession concept, so recall's top hits
        can lead with yesterday's state even after fact supersession works. Mark each
        retrieved turn whose derived semantic facts are ALL superseded — recall_rank then
        demotes it below current facts and render_context labels it
        '(said, superseded as of <date>)'. Turns with no facts, or with any live fact,
        are untouched. ONE batched query over the retrieved ids only — O(k)."""
        if not raw_rows:
            return
        try:
            stale = self.store.turn_supersession(self.schema,
                                                 [r.get("id") for r in raw_rows])
        except Exception:                      # pre-migration store: never break recall
            return
        for r in raw_rows:
            if r.get("id") in stale:
                r["_superseded"] = True
                r["_superseded_at"] = stale[r["id"]]

    @staticmethod
    def _demote_stale(turn_rows):
        """Split ranked turn rows into (fresh, stale): stale = turns whose derived facts
        are ALL superseded (marked by _mark_stale_turns). CONSERVATIVE ranking rule —
        current facts rank above stale turns at equal relevance, so the final ordering is
        fresh turns + facts + stale turns. Quotas are applied BEFORE the split, so the
        result set is identical to the pre-fix recall; only the order (and the
        superseded annotation) changes."""
        fresh = [r for r in turn_rows if not r.get("superseded")]
        return fresh, [r for r in turn_rows if r.get("superseded")]

    def recall_rank(self, query: str, b: dict, *, raw_quota=11, fact_quota=8,
                    entity_quota=10, use_rerank=True, subject=None) -> list[dict]:
        """CPU phase of recall: cross-encoder rerank + quota assembly over a
        recall_fetch bundle. No database access — safe with no connection held.
        `use_rerank=False` (issue #12 deadline path) skips the cross-encoder and keeps
        the retrieval (RRF) order — degraded but instant.
        Ranking is byte-identical to the pre-split recall(), except that STALE turns
        (all derived facts superseded) are annotated + demoted below current facts,
        plus the issue #11 broad-query tune (all three knobs zero-disable, and
        MEMNOS_BROAD_QUERY_TUNE=0 kills the whole tune):
          1. broad questions ('where are we with the deployment?') put facts/dossiers
             AHEAD of raw turns — same rows, same quotas, order only; specific/neutral
             queries keep the original turn-first order (query_specificity above);
          2. raw-turn scores get a mild LOG-LENGTH penalty at rank time (long
             discursive turns match everything; storage untouched);
          3. fact scores get a bounded reinforcement boost from restatements/salience
             (a fact restated 50x should outrank a turn that mentioned it once)."""
        intent = b["intent"]
        from . import temporal as T
        # --- issue #17: HARD SUBJECT SCOPE -----------------------------------------
        # A caller can pass subject="<entity>" to hard-filter facts to ONE entity's set —
        # the strongest disambiguation when the caller already knows the subject. Acts on
        # the fact arms only (raw turns are verbatim history, left intact). Gated by
        # MEMNOS_RECALL_ENTITY_SCOPE (default on); set =0 to ignore the param entirely.
        if subject and _env_float("MEMNOS_RECALL_ENTITY_SCOPE", 1.0) > 0:
            sl = subject.strip().lower()

            def _is_subject(it):
                se = (it.get("subject_entity") or "").strip().lower()
                return bool(sl) and (sl == se or sl in se or sl in (it.get("content") or "").lower())
            for key in ("sem", "dump", "tl"):
                if b.get(key):
                    b[key] = [it for it in b[key] if _is_subject(it)]
        tune = _env_float("MEMNOS_BROAD_QUERY_TUNE", 1.0) > 0
        len_pen = _env_float("MEMNOS_TURN_LENGTH_PENALTY", 0.15) if tune else 0.0
        sal_boost = _env_float("MEMNOS_SALIENCE_BOOST", 0.05) if tune else 0.0
        # issue #2: unified intent. list/aggregation ('which / how many …') leads with facts
        # (MEMNOS_RECALL_FACT_FIRST); #11 broad keeps its own facts-first. verbatim NEVER
        # leads with facts — raw turns stay first.
        qi = query_intent(query)
        fact_first = _env_float("MEMNOS_RECALL_FACT_FIRST", 1.0) > 0
        broad = (tune and qi == "broad") or (fact_first and qi == "list")
        # issue #2 layer 3: a SMALL, capped nudge for fact/semantic candidates over raw
        # turns of similar relevance — ONLY for non-verbatim classes (the gate). Bounded so
        # it cannot crater single_hop/verbatim. MEMNOS_RECALL_FACT_BOOST=0 disables.
        fb = _env_float("MEMNOS_RECALL_FACT_BOOST", 0.06)
        fact_boost = fb if qi != "verbatim" else 0.0
        # issue #22: operator-tunable PRECISION floor — candidates whose raw cross-encoder
        # score falls below MEMNOS_RERANK_MIN_SCORE are dropped entirely, before quotas are
        # applied (trades recall breadth for precision). Read on the RAW rerank() score, not
        # the rank-adjusted one (length penalty / salience / fact / entity boosts below) —
        # those are ranking heuristics, not the reranker's own confidence. Default 0.0
        # preserves today's behavior: rerank() scores are a sigmoid output in (0, 1), so a
        # >=0.0 floor never filters anything.
        min_rerank_score = _env_float("MEMNOS_RERANK_MIN_SCORE", 0.0)

        # --- issue #17: ENTITY-AWARE recall (subject disambiguation) ----------------
        # When the query names a known entity, two semantically-adjacent subjects in the
        # SAME namespace (e.g. an "Interoperability Gateway" service vs. a "record ID
        # crosswalk" task — both FHIR/interop-flavored, so their embeddings sit next to
        # each other) compete in one ranked pool. Vector/FTS similarity cannot separate
        # subject IDENTITY; the ENTITY BINDING (semantic.subject_entity) can, and the store
        # already captured it. This is a rank-time ARM, not a new retrieval:
        #   - BOOST a fact whose subject_entity / extracted entities match a query entity;
        #   - DEMOTE a fact that carries a COMPETING subject (a subject some OTHER candidate
        #     carries) while matching the query entity NOT AT ALL.
        # MATCHING SIGNAL (the #17 fix): match against the fact's ENTITY BINDING, never its
        # free-text content as a substring. The old `qe in content` clause let a single
        # generic token (e.g. "interoperability") inside a competing subject's prose make it
        # look on-topic, so in the shared-vocabulary case this arm targets it was a NO-OP
        # (ON == OFF). We now match query entities — as whole-word/phrase units, multi-word
        # phrases preferred over their split tokens — against subject_entity and the proper
        # nouns the fact's statement actually names (entity-level), via temporal.entity_match.
        # Bounded by design (same caps as fact_boost), verbatim-exempt. Kill switch:
        #   MEMNOS_RECALL_ENTITY_BOOST=0 disables the arm entirely.
        # PHRASES first (e.g. "Interoperability Gateway"), then fall back to single proper
        # nouns ONLY when no phrase exists — so split tokens can't bridge two subjects.
        qphrases = [e.lower() for e in T.query_entity_phrases(query)]
        qsingles = [e.lower() for e in (b.get("ents") or [])]
        qmatch = qphrases or qsingles          # prefer whole phrases when present
        eb = _env_float("MEMNOS_RECALL_ENTITY_BOOST", 0.12)
        entity_boost = eb if (qi != "verbatim" and qmatch and eb > 0) else 0.0

        def _fact_entities(it):
            """The fact's entity bindings: its subject_entity plus the proper-noun entities
            its statement actually names. Entity-level — NOT a free-text substring scan."""
            ents = []
            se = (it.get("subject_entity") or "").strip().lower()
            if se:
                ents.append(se)
            ents.extend(e.lower() for e in T.query_entities(it.get("content") or ""))
            return ents

        def _hits_query(it):
            fe = _fact_entities(it)
            return any(T.entity_match(qe, fce) for qe in qmatch for fce in fe)

        # the set of SUBJECTS other facts in the pool carry but the query did NOT match —
        # a fact whose subject is one of these (and matches no query entity) is off-topic.
        competing = set()
        if entity_boost > 0:
            for it in (b.get("sem") or []):
                se = (it.get("subject_entity") or "").strip().lower()
                if se and not any(T.entity_match(qe, se) for qe in qmatch):
                    competing.add(se)

        def _fact_entity_factor(it):
            """+boost if the fact's entity binding matches a query entity; -demote if it
            carries only a competing subject and matches no query entity. 1.0 otherwise."""
            if entity_boost <= 0:
                return 1.0
            if _hits_query(it):
                return 1.0 + min(0.20, entity_boost)
            se = (it.get("subject_entity") or "").strip().lower()
            if se and se in competing:                    # off-subject neighbor: demote
                return 1.0 / (1.0 + min(0.20, entity_boost))
            return 1.0

        def adjust(s, it, kind):
            # rank-time score shaping — content/storage untouched, bounded by design
            if kind == "turn" and len_pen > 0:
                s /= 1.0 + len_pen * max(0.0, math.log(max(len(it["content"]), 1) / 400.0))
            if kind == "fact" and sal_boost > 0:
                s *= 1.0 + min(0.25, sal_boost * (math.log1p(it.get("restatements") or 0)
                                                  + max(0.0, (it.get("salience") or 0.0) - 0.5)))
            if kind == "fact" and fact_boost > 0:         # gated, bounded fact preference (#2)
                s *= 1.0 + min(0.10, fact_boost)
            if kind == "fact" and entity_boost > 0:       # #17: entity-aware subject scope
                s *= _fact_entity_factor(it)
            return s

        rq = query_clamp(query)                    # #15 follow-up: bound the reranker's query side
        def rr(items, kind):
            if not items:
                return []
            if use_rerank:
                order = brain_rerank.rerank(rq, [c["content"] for c in items], self.reranker)
            else:                                  # deadline-degraded: retrieval order
                order = [(i, 1.0 / (1.0 + i)) for i in range(len(items))]
            if min_rerank_score > 0.0:             # issue #22: precision floor
                order = [(i, s) for i, s in order if s >= min_rerank_score]
            scored = []
            for i, s in order:
                s = adjust(float(s), items[i], kind)
                row = {"content": items[i]["content"], "kind": kind, "score": s}
                if kind == "fact" and items[i].get("valid_from"):
                    row["date"] = items[i]["valid_from"].date().isoformat()
                if items[i].get("author"):                    # who wrote this memory
                    row["author"] = items[i]["author"]
                if items[i].get("memory_type"):               # typed memory (0.1.6)
                    row["type"] = items[i]["memory_type"]
                if items[i].get("memory_type") == "inferred":  # issue #24: derived conclusion
                    if items[i].get("inference_confidence"):
                        row["confidence"] = items[i]["inference_confidence"]
                    if items[i].get("inference_basis"):
                        row["basis"] = items[i]["inference_basis"]
                if items[i].get("_ns"):                       # grounded: source namespace
                    row["namespace"] = items[i]["_ns"]
                if kind == "turn" and items[i].get("_superseded"):   # stale turn (issue #10 B)
                    row["superseded"] = True
                    if items[i].get("_superseded_at"):
                        row["superseded_at"] = (items[i]["_superseded_at"]
                                                .astimezone(timezone.utc).date().isoformat())
                if items[i].get("dup_count"):                  # issue #2: collapsed duplicates
                    row["dup_count"] = items[i]["dup_count"]
                scored.append(row)
            scored.sort(key=lambda r: -r["score"])            # re-sort on adjusted score
            return scored

        if not intent.temporal:
            if not b["ents"]:
                sem_rows = rr(b.get("sem") or [], "fact")
                fresh, stale = self._demote_stale(rr(b["raw"], "turn")[:raw_quota])
                if broad:                                     # facts lead on broad questions
                    return sem_rows[:fact_quota] + fresh + stale
                return fresh + sem_rows[:fact_quota] + stale
            # Entity path (issue #10 follow-up): entity-dump + semantic facts unified through
            # the cross-encoder reranker so every fact carries a real score. Previously the
            # dossier rows bypassed rr() and arrived score=None, always sorting below turns.
            dump_rows = b.get("dump") or []
            dump_contents = {r["content"] for r in dump_rows if r.get("content")}
            sem_for_entity = [r for r in (b.get("sem") or [])
                              if r.get("content") not in dump_contents]
            # entity-dump rows first → their position preserved when the cross-encoder ties
            all_fact_cands = dump_rows + sem_for_entity
            all_facts_ranked = rr(all_fact_cands, "fact")
            fresh, stale = self._demote_stale(rr(b["raw"], "turn")[:raw_quota])
            cap = fact_quota + entity_quota                   # same total-fact budget
            if broad:                                         # facts/dossiers lead
                return all_facts_ranked[:cap] + fresh + stale
            return fresh + all_facts_ranked[:cap] + stale

        # Deduplicate timeline candidates, then score through rr() so every temporal fact
        # carries a real cross-encoder score (previously tl_rows bypassed rr() → score=None).
        tl_cands, tl_seen = [], set()
        for r in b.get("tl", []):
            c = r["content"]
            if c not in tl_seen:
                tl_seen.add(c)
                tl_cands.append(r)
        tl_rows = rr(tl_cands, "fact")
        sem_rows = [r for r in rr(b["sem"], "fact") if r["content"] not in tl_seen]
        # small raw + GUARANTEED timeline (scored) + a few reranked relevance facts
        fresh, stale = self._demote_stale(rr(b["raw"], "turn")[:5])
        return fresh + tl_rows[:12] + sem_rows[:6] + stale

    def recall_wide(self, namespaces, query, *, k=40, raw_quota=11, fact_quota=8) -> list[dict]:
        """WIDEN recall across multiple permissible namespaces (the agent's default + the
        others its key can read). Reuses the single-namespace hybrid search per namespace,
        then GLOBALLY reranks the merged candidates with the cross-encoder — so the best
        memories surface regardless of which namespace they live in. No LLM at query time.
        Each result is tagged with its source namespace. Split as fetch (DB) + rank (CPU)
        so pooled callers release the connection before the rerank."""
        raw_c, sem_c = self.recall_wide_fetch(namespaces, query, k=k)
        return self.recall_wide_rank(query, raw_c, sem_c, raw_quota=raw_quota,
                                     fact_quota=fact_quota)

    def recall_wide_fetch(self, namespaces, query, *, k=40, qv=None, timings=None,
                          deadline=None, degraded_reasons=None, conn_factory=None):
        """DB phase of recall_wide: per-namespace hybrid search + RRF-score candidate cap.
        `timings`/`deadline` — same issue #12 semantics as recall_fetch (per-stage
        accumulation; past-deadline skips the staleness pass). `conn_factory` (issue
        #12 phase 2, e.g. POOL.connection) runs every namespace's raw+semantic pair
        concurrently instead of one namespace at a time — see _dispatch_sql_jobs.
        None (every caller before this fix) keeps one-namespace-at-a-time exactly.

        issue #41 fix C: each namespace's raw/semantic query is an independent arm. A
        RECALL_ARM_FAILURES-class error degrades JUST that (namespace, arm) pair — the
        loop continues, so every OTHER namespace's results are unaffected. `degraded_
        reasons`, if passed, is a list mutated in place (same convention as `timings`)
        with one {namespace, arm, error, sqlstate} entry per failure; the caller decides
        whether a non-empty list means the response is degraded=true."""
        if not namespaces:
            return [], []
        if qv is None:
            qv = self.embed(query_clamp(query))   # #15 follow-up: bound a pathological query
        t_sql = time.perf_counter()
        jobs, job_meta = [], []
        for ns in namespaces:
            jobs.append(lambda store, ns=ns: store.search_raw_turns(self.schema, ns, qv, query, k))
            job_meta.append((ns, "raw"))
            jobs.append(lambda store, ns=ns: store.search_semantic(
                self.schema, ns, qv, query, k, current_only=True))
            job_meta.append((ns, "semantic"))
        resolvers = self._dispatch_sql_jobs(jobs, conn_factory)
        raw_c, sem_c = [], []
        for (ns, arm), resolve in zip(job_meta, resolvers):
            try:
                rows = resolve()
            except RECALL_ARM_FAILURES as e:
                _record_arm_failure(degraded_reasons, ns, arm, e)
                continue
            dest = raw_c if arm == "raw" else sem_c
            for r in rows:
                r["_ns"] = ns; dest.append(r)
        # cap candidates by RRF score before the (CPU) cross-encoder rerank
        raw_c = sorted(raw_c, key=lambda x: x.get("score", 0), reverse=True)[:60]
        sem_c = sorted(sem_c, key=lambda x: x.get("score", 0), reverse=True)[:60]
        raw_c = self._dedup_candidates(raw_c)   # issue #2: collapse cron-x10 dups
        t_stale = time.perf_counter()
        if deadline is None or t_stale < deadline:
            self._mark_stale_turns(raw_c)      # DB phase — stale-turn annotation
        if timings is not None:
            done = time.perf_counter()
            timings["sql_ms"] = timings.get("sql_ms", 0.0) + (t_stale - t_sql) * 1000.0
            timings["staleness_ms"] = timings.get("staleness_ms", 0.0) + (done - t_stale) * 1000.0
        return raw_c, sem_c

    def recall_wide_rank(self, query, raw_c, sem_c, *, raw_quota=11, fact_quota=8,
                         use_rerank=True) -> list[dict]:
        """CPU phase of recall_wide: global cross-encoder rerank. No database access.
        `use_rerank=False` (deadline path) keeps retrieval order — degraded but instant.
        Carries the same issue #11 tune as recall_rank: length-normalized turn scores,
        restatement/salience-boosted fact scores, facts-first order on broad queries."""
        tune = _env_float("MEMNOS_BROAD_QUERY_TUNE", 1.0) > 0
        len_pen = _env_float("MEMNOS_TURN_LENGTH_PENALTY", 0.15) if tune else 0.0
        sal_boost = _env_float("MEMNOS_SALIENCE_BOOST", 0.05) if tune else 0.0
        qi = query_intent(query)                              # issue #2 unified intent
        fact_first = _env_float("MEMNOS_RECALL_FACT_FIRST", 1.0) > 0
        broad = (tune and qi == "broad") or (fact_first and qi == "list")
        fb = _env_float("MEMNOS_RECALL_FACT_BOOST", 0.06)
        fact_boost = fb if qi != "verbatim" else 0.0
        rq = query_clamp(query)                    # #15 follow-up: bound the reranker's query side
        # issue #22: same precision floor as recall_rank (see that docstring) — filters on
        # the RAW rerank() score, before the length/salience/fact adjustments below.
        min_rerank_score = _env_float("MEMNOS_RERANK_MIN_SCORE", 0.0)

        def rr(items, kind, quota):
            if not items:
                return []
            if use_rerank:
                order = brain_rerank.rerank(rq, [c["content"] for c in items], self.reranker)
            else:                                  # deadline-degraded: retrieval order
                order = [(i, 1.0 / (1.0 + i)) for i in range(len(items))]
            if min_rerank_score > 0.0:             # issue #22: precision floor
                order = [(i, s) for i, s in order if s >= min_rerank_score]
            scored = []
            for i, s in order:
                it = items[i]
                s = float(s)
                if kind == "turn" and len_pen > 0:            # rank-time length norm (issue #11)
                    s /= 1.0 + len_pen * max(0.0, math.log(max(len(it["content"]), 1) / 400.0))
                if kind == "fact" and sal_boost > 0:          # bounded reinforcement boost
                    s *= 1.0 + min(0.25, sal_boost * (math.log1p(it.get("restatements") or 0)
                                                      + max(0.0, (it.get("salience") or 0.0) - 0.5)))
                if kind == "fact" and fact_boost > 0:         # gated, bounded fact preference (#2)
                    s *= 1.0 + min(0.10, fact_boost)
                row = {"content": it["content"], "kind": kind, "score": s, "namespace": it["_ns"]}
                if kind == "fact" and it.get("valid_from"):
                    row["date"] = it["valid_from"].date().isoformat()
                if it.get("author"):
                    row["author"] = it["author"]
                if it.get("memory_type"):
                    row["type"] = it["memory_type"]
                if it.get("memory_type") == "inferred":        # issue #24: derived conclusion
                    if it.get("inference_confidence"):
                        row["confidence"] = it["inference_confidence"]
                    if it.get("inference_basis"):
                        row["basis"] = it["inference_basis"]
                if kind == "turn" and it.get("_superseded"):   # stale turn (issue #10 B)
                    row["superseded"] = True
                    if it.get("_superseded_at"):
                        row["superseded_at"] = (it["_superseded_at"]
                                                .astimezone(timezone.utc).date().isoformat())
                if it.get("dup_count"):                        # issue #2: collapsed duplicates
                    row["dup_count"] = it["dup_count"]
                scored.append(row)
            scored.sort(key=lambda r: -r["score"])            # re-sort on adjusted score
            return scored[:quota]

        fresh, stale = self._demote_stale(rr(raw_c, "turn", raw_quota))
        facts = rr(sem_c, "fact", fact_quota)
        if broad:                                             # facts lead on broad questions
            return facts + fresh + stale
        return fresh + facts + stale

    @staticmethod
    def render_context(rows, *, max_chars=9000, viewer=None, query=None) -> str:
        """Format ALREADY-RETRIEVED recall rows into a paste-ready context block.
        Lets callers that need both memories AND context (the /recall endpoint) run the
        retrieval+rerank pipeline ONCE instead of twice — measured: halves /recall
        latency at field scale. Rows from recall() (no namespace tag) and recall_wide()
        (tagged with source namespace) both render correctly.

        ATTRIBUTION RULE: a line is tagged '(by <author>)' ONLY when the row's author
        differs from `viewer` (the calling principal's name). Single-user contexts —
        where everything was written by the caller — stay clean; memories written by
        OTHER principals (bots, teammates) into a shared namespace are visibly
        attributed. viewer=None (legacy callers) never tags.

        TYPED MEMORIES (0.1.6): a row's `type` replaces the generic fact/said label —
        '- (decision, 2026-06-10, by arch-agent) ...'. PINNED constraint rows render as
        'CONSTRAINT: ...' lines; the server puts them first in `rows`, so they lead the
        context block ahead of every ranked result.

        FACT-FIRST RENDERING (issue #2): when `query` is a list/aggregation or broad-status
        question, lead with distilled FACTS/dossiers ('what's known') and put raw turns
        below as supporting evidence — so the crisp answer is read FIRST regardless of the
        rerank float order (the HDIG case: answer facts that floated to rank 15-22). Pins
        always stay first; verbatim/neutral queries keep the rerank order. query=None
        (legacy callers) is unchanged. MEMNOS_RECALL_FACT_FIRST=0 disables."""
        if (query and _env_float("MEMNOS_RECALL_FACT_FIRST", 1.0) > 0
                and query_intent(query) in ("list", "broad")):
            pins = [r for r in rows if r.get("pinned")]
            rest = [r for r in rows if not r.get("pinned")]
            facts = [r for r in rest if r.get("kind") == "fact"]
            turns = [r for r in rest if r.get("kind") != "fact"]
            rows = pins + facts + turns
        out, used = [], 0
        for r in rows:
            tag = f" [{r['namespace']}]" if r.get("namespace") else ""
            by = (f", by {r['author']}"
                  if viewer is not None and r.get("author") and r["author"] != viewer else "")
            if r.get("pinned"):
                line = f"CONSTRAINT:{tag} {r['content']}"
            else:
                label = r.get("type") or ("fact" if r["kind"] == "fact" else "said")
                if label == "inferred" and r.get("confidence"):   # issue #24
                    label += f", confidence={r['confidence']}"
                if r.get("superseded"):        # stale turn — show the transition (issue #10 B)
                    label += (f", superseded as of {r['superseded_at']}"
                              if r.get("superseded_at") else ", superseded")
                if r.get("dup_count"):         # issue #2: N near-identical turns collapsed to 1
                    label += f", x{r['dup_count']}"
                d = f", {r['date']}" if (r["kind"] == "fact" and r.get("date")) else ""
                line = f"- ({label}{d}{by}){tag} {r['content']}"
            if used + len(line) > max_chars:
                break
            out.append(line); used += len(line)
        return "\n".join(out)

    def context_wide(self, namespaces, query, *, max_chars=9000, **kw) -> str:
        return self.render_context(self.recall_wide(namespaces, query, **kw),
                                   max_chars=max_chars, query=query)

    def context(self, namespace: str, query: str, *, max_chars=9000, **kw) -> str:
        return self.render_context(self.recall(namespace, query, **kw),
                                   max_chars=max_chars, query=query)

    # --- consolidation (offline; call on idle / schedule) -----------------
    def consolidate(self, namespace: str, max_entities=25, max_dossier=6,
                    conn_factory=None, infer=False) -> dict:
        """Build entity dossiers (offline multi-hop pre-join) from stored facts. Groups
        facts by SUBJECT entity (SPO) + proper nouns in the statement — same clustering
        as the benchmarked phaseA ingest.

        PHASED for pooled callers: read phase (facts, DB) → LLM phase (per-entity dossier
        generation + embeddings, MINUTES of model I/O, NO connection) → write phase
        (supersession + inserts, DB). `conn_factory` (e.g. POOL.connection) makes each DB
        phase use its own short-lived connection so a pool slot is never pinned across the
        LLM phase. Default (None) runs everything on self.store — identical behavior for
        scripts/launchd jobs that hold one plain non-pooled connection."""
        import json
        from collections import defaultdict

        def _read(store):
            with store.conn.cursor() as c:
                c.execute(f"SELECT id, statement, subject_entity, valid_from, source_turn_ids "
                          f"FROM {self.schema}.semantic WHERE namespace=%s AND kind='fact'",
                          (namespace,))
                return c.fetchall()

        if conn_factory is not None:                          # READ phase (short conn)
            with conn_factory() as conn:
                facts = _read(BrainStore(conn=conn))
        else:
            facts = _read(self.store)

        ent = defaultdict(list)
        ent_src = defaultdict(set)                           # dossier provenance = union of source facts' turns
        for row in facts:
            ents = set(_PROPER.findall(row["statement"]))
            if row.get("subject_entity"):
                ents.add(row["subject_entity"])
            for e in ents:
                ent[e].append((row["id"], row["valid_from"], row["statement"]))
                ent_src[e].update(row.get("source_turn_ids") or [])
        clusters = sorted(((e, fs) for e, fs in ent.items() if len(fs) >= 3),
                          key=lambda x: -len(x[1]))[:max_entities]

        # LLM phase — NO connection required: dossier generation + embeddings only.
        results = []                                         # (entity, vf, [(fact, vec)...])
        infer_results = []                                   # (entity, vf, [conclusion dict, ...]) — issue #24
        for e, fs in clusters:
            if self.llm is None:
                break
            try:
                r = self.llm.chat.completions.create(
                    model=self.extract_model, temperature=0, max_tokens=500,
                    response_format={"type": "json_object"},
                    messages=[{"role": "system", "content":
                               "Consolidate EVERYTHING known about ONE subject into durable facts, DERIVING "
                               "facts that require COMBINING inputs ('A works at B' + 'B in C' => 'A works in "
                               "C'). PRESERVE every distinct item the subject has done/owns/likes — do NOT "
                               "collapse a list (e.g. keep BOTH 'kickboxing' AND 'taekwondo'). On genuine "
                               "value conflict keep the most recent (dates given). Keep dates inline. Each "
                               "fact MUST be a single self-contained SENTENCE (a string, not an object). "
                               'JSON {"facts":["...", "..."]}.'},
                              {"role": "user", "content": f"Subject: {e}\n- " +
                               "\n- ".join(f for _, _, f in fs[:40])}])
                self._track(self.extract_model, r)
                vf = max((d for _, d, _ in fs if d), default=None)
                items = []
                for f in [_dossier_text(x) for x in json.loads(r.choices[0].message.content).get("facts", [])][:max_dossier]:
                    if not f:
                        continue
                    items.append((f, self.embed(f)))
                if items:
                    results.append((e, vf, items))
            except Exception:
                continue
            if infer:
                try:
                    concls = infer_conclusions(e, [(fid, stmt) for fid, _, stmt in fs], self.llm, self.extract_model)
                    if concls:
                        infer_results.append((e, vf, concls))
                except Exception:
                    continue

        n = 0
        n_inferred = 0

        def _write(store):
            nonlocal n, n_inferred
            for e, vf, items in results:
                try:
                    for f, vec in items:
                        # belief-change supersession: close out the prior value for this subject
                        store.supersede_subject(self.schema, namespace, e[:100], vec, vf)
                        store.insert_semantic(self.schema, namespace, "dossier", f,
                                              subject=e[:100], valid_from=vf, salience=0.8, vec=vec,
                                              source_turn_ids=sorted(ent_src.get(e, ())),
                                              author=self.author)
                        n += 1
                except Exception:
                    continue
            CONF_WEIGHT = {"low": 0.3, "medium": 0.6, "high": 0.9}
            for e, vf, concls in infer_results:
                try:
                    store.supersede_inferred(self.schema, namespace, e[:100], vf)
                    for item in concls:
                        vec = self.embed(item["conclusion"])
                        store.insert_semantic(
                            self.schema, namespace, "inferred", item["conclusion"],
                            subject=e[:100], valid_from=vf, confidence=CONF_WEIGHT[item["confidence"]],
                            salience=0.7, vec=vec, source_turn_ids=sorted(ent_src.get(e, ())),
                            author=self.author, memory_type="inferred",
                            inference_confidence=item["confidence"], inference_basis=item["basis"],
                            source_fact_ids=item["supporting_fact_ids"])
                        n_inferred += 1
                except Exception:
                    continue

        if conn_factory is not None:                          # WRITE phase (short conn)
            with conn_factory() as conn:
                _write(BrainStore(conn=conn))
        else:
            _write(self.store)
        return {"dossiers": n, "inferred": n_inferred}

    def segment_episodes(self, namespace: str, *, gap_minutes=30, max_episodes=200,
                         summary_fn=None, conn_factory=None) -> dict:
        """EPISODIC tier (hippocampus): segment uncovered raw turns into coherent episodes —
        boundary on session_id change OR a time gap > gap_minutes — with an extractive
        summary (no LLM; pass summary_fn for an optional LLM summary), time span, and
        two-level provenance (fact → episode → turn). Incremental + idempotent: only turns
        not already in an episode are segmented.

        TYPED MEMORIES: an episode INHERITS a memory_type only when ALL of its source
        turns carry the same non-null type (unanimous — conservative by design: a mixed
        or partly-typed group is not 'a decision episode', so it stays NULL).

        PHASED for pooled callers: read turns (DB) → summaries + embeddings (CPU/network,
        NO connection: up to max_episodes embedding calls) → inserts (DB). `conn_factory`
        gives each DB phase its own short-lived connection; default (None) uses self.store
        throughout — same results either way."""
        import re

        if conn_factory is not None:                          # READ phase (short conn)
            with conn_factory() as conn:
                turns = BrainStore(conn=conn).uncovered_raw_turns(self.schema, namespace)
        else:
            turns = self.store.uncovered_raw_turns(self.schema, namespace)
        if not turns:
            return {"episodes": 0}
        groups, cur, prev = [], [], None
        for t in turns:
            if cur:
                gap = (t["observed_at"] - prev["observed_at"]).total_seconds() / 60.0
                if t.get("session_id") != prev.get("session_id") or gap > gap_minutes:
                    groups.append(cur); cur = []
            cur.append(t); prev = t
        if cur:
            groups.append(cur)

        # SUMMARY + EMBED phase — NO connection required (embedding is a network call
        # in OpenAI mode; summary_fn may be an LLM).
        prepared = []                              # (group, body, summary, vec, mtype)
        for g in groups[:max_episodes]:
            body = "\n".join(f"{(r['speaker'] or 'user')}: {r['text']}" for r in g)
            if summary_fn:
                summary = summary_fn(body)
            else:
                joined = " ".join(r["text"] for r in g)
                sents = re.split(r"(?<=[.!?])\s+", joined)
                summary = (" ".join(sents[:2])[:400] or joined[:400])
            vec = self.embed(summary) if summary else None
            # UNANIMOUS type inheritance (conservative): one non-null type across ALL
            # source turns → the episode carries it; any mix / any untyped turn → NULL.
            mtypes = {r.get("memory_type") for r in g}
            mtype = mtypes.pop() if (len(mtypes) == 1 and None not in mtypes) else None
            prepared.append((g, body, summary, vec, mtype))

        n = 0

        def _write(store):
            nonlocal n
            for g, body, summary, vec, mtype in prepared:
                sids = [r["id"] for r in g]
                eid = store.insert_episodic(
                    self.schema, namespace, g[0].get("session_id"), body, summary=summary,
                    t_start=g[0]["observed_at"], t_end=g[-1]["observed_at"], observed_at=g[-1]["observed_at"],
                    salience=min(1.0, 0.3 + 0.1 * len(g)), source_turn_ids=sids, vec=vec,
                    author=self.author, memory_type=mtype)
                store.link_episode_provenance(self.schema, namespace, eid, sids)
                n += 1

        if conn_factory is not None:                          # WRITE phase (short conn)
            with conn_factory() as conn:
                _write(BrainStore(conn=conn))
        else:
            _write(self.store)
        return {"episodes": n}




def generate_entity_dossier(entity_name: str, facts: list[str], llm, model: str) -> str:
    """Generate a 2-4 sentence summary paragraph about an entity from its related facts.
    Uses the same LLM pattern as consolidate(). Returns the generated text, or an empty
    string if the LLM call fails. No em-dashes used in the prompt (issue #23 constraint)."""
    import json
    if not facts or llm is None:
        return ""
    fact_lines = "\n- ".join(f[:500] for f in facts[:30])
    prompt = (
        "You are summarising what is known about a single entity from a memory system. "
        "Write 2 to 4 sentences that capture the most important, durable facts about the entity. "
        "Be specific: include names, roles, relationships, and notable attributes. "
        "Do NOT use bullet points, lists, or section headers. "
        "Write in present tense for current facts and past tense for past events. "
        "Do not use the word 'dossier'. Output plain prose only."
    )
    user_msg = f"Entity: {entity_name}\n\nKnown facts:\n- {fact_lines}"
    try:
        r = llm.chat.completions.create(
            model=model, temperature=0, max_tokens=300,
            messages=[{"role": "system", "content": prompt},
                      {"role": "user", "content": user_msg}])
        text = (r.choices[0].message.content or "").strip()
        return text if len(text) >= 10 else ""
    except Exception:
        return ""


def infer_conclusions(entity_name: str, facts: list[tuple[int, str]], llm, model: str) -> list[dict]:
    """Derive implicit conclusions from a pattern of stated facts about one entity
    (issue #24). `facts` = [(fact_id, statement), ...]. Returns a list of
    {"conclusion": str, "confidence": "low"|"medium"|"high", "basis": str,
    "supporting_fact_ids": [int, ...]}, or [] on any failure / no LLM / <3 facts.

    PERSON/AGENT SCOPING: consolidate()'s clustering groups facts under ANY entity
    mentioned 3+ times (subject_entity, which the extractor assigns to tools/topics/
    products just as readily as to people — "Curves panel", "Fitbit Inspire HR" show
    up as subjects as often as "Alice" does). Deriving a "pattern" for a tool entity
    produces coherent-sounding trivia about the tool, not a preference — useless noise
    at recall time. The prompt below gates on person/agent-hood itself (no hardcoded
    identity string, so it works for "user", "Alice", or any named individual) rather
    than the caller pre-filtering, since only the LLM can tell from the fact content
    whether the subject IS a person/agent.

    The LLM is asked for 1-based INDICES into the numbered fact list (not raw IDs,
    which it would frequently hallucinate) — indices are translated back to real
    semantic.id values locally after parsing, so provenance is never LLM-authored."""
    import json
    if not facts or llm is None or len(facts) < 3:
        return []
    numbered = "\n".join(f"{i+1}. {stmt[:400]}" for i, (_, stmt) in enumerate(facts[:40]))
    try:
        r = llm.chat.completions.create(
            model=model, temperature=0, max_tokens=500,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content":
                       "Given numbered facts about ONE subject, first decide: is this "
                       "subject a PERSON or AGENT (a user, a named individual) whose own "
                       "behavior, choices, or preferences these facts describe — as "
                       "opposed to a tool, product, feature, place, organization, or topic "
                       "that facts merely explain or describe? If the subject is NOT a "
                       "person/agent, return {\"conclusions\":[]} immediately — do not "
                       "derive conclusions about what a tool or topic IS or does. "
                       "Otherwise, identify PATTERNS across MULTIPLE facts and derive "
                       "conclusions that are clearly supported "
                       "(do NOT restate a single fact as a conclusion; do NOT speculate "
                       "beyond what the pattern supports). Return only conclusions backed "
                       "by at least 2 of the numbered facts. For each: the conclusion as a "
                       "single sentence, a confidence level (low, medium, or high), a "
                       "one-line basis explaining the reasoning, and the 1-based indices "
                       "of the supporting facts. "
                       'JSON {"conclusions":[{"conclusion":"...", "confidence":"medium", '
                       '"basis":"...", "supporting_indices":[1,3]}]}'},
                      {"role": "user", "content": f"Subject: {entity_name}\n{numbered}"}])
        data = json.loads(r.choices[0].message.content)
        out = []
        for item in data.get("conclusions", []):
            conclusion = (item.get("conclusion") or "").strip()
            if not conclusion:
                continue
            conf = str(item.get("confidence") or "medium").strip().lower()
            if conf not in ("low", "medium", "high"):
                conf = "medium"
            idxs = item.get("supporting_indices") or []
            fact_ids = [facts[i - 1][0] for i in idxs
                        if isinstance(i, int) and 1 <= i <= len(facts)]
            if len(fact_ids) < 2:              # not actually a cross-fact pattern
                continue
            out.append({"conclusion": conclusion, "confidence": conf,
                        "basis": (item.get("basis") or "")[:300],
                        "supporting_fact_ids": fact_ids})
        return out
    except Exception:
        return []

# --- namespace reconcile (issue #10 residual C) ------------------------------------
def reconcile_namespace(store, namespace: str, *, schema: str = f"tenant_{_TENANT}",
                        limit: int | None = None) -> dict:
    """BACKFILL for pre-fix contradiction debt: namespaces ingested before the bf78b2e
    write-path fix hold contradicting LIVE facts that the fixed write path would have
    closed at write time. Walk the namespace's live facts NEWEST-FIRST (observation
    axis) and apply the SAME deterministic write-time logic pairwise against older live
    facts: dedupe -> SPO supersession (cue list + quantified-object rule) -> reversal/
    negation close-out via nearest stored embeddings. Embedding-only — stored vectors,
    NO LLM, no new embedding calls.

    The CALLER owns the transaction: run on a non-autocommit connection, then COMMIT
    for a real run or ROLLBACK for --dry-run — the mutations (and therefore the
    reported counts) are identical by construction. `limit` caps the number of facts
    WALKED per run (newest first), bounding the work for huge namespaces.

    Dedupe direction (backfill twin of the write-path rule "a restatement reinforces,
    never inserts"): the OLDER fact is kept (restatements + salience bump + provenance
    union of the newer fact's source turns) and the NEWER duplicate row is expired —
    converging on the exact end-state the fixed write path would have produced."""
    from .temporal import parse_event_date
    dedupe_thresh = _env_float("MEMNOS_DEDUPE_THRESHOLD", 0.03)
    neg_thresh = _env_float("MEMNOS_NEGATION_THRESHOLD", 0.40)
    facts = store.live_facts_newest_first(schema, namespace, limit=limit)
    out = {"facts_scanned": len(facts), "deduped": 0, "closed": 0}
    for f in facts:
        if not store.is_live(schema, namespace, f["id"]):   # closed earlier in this walk
            continue
        stmt, subj, pred, obj = (f["statement"], f.get("subject_entity"),
                                 f.get("predicate"), f.get("object"))
        ev = f.get("valid_from") or f.get("observed_at")
        # (a) DEDUPE: f restates an older live fact -> reinforce the older, expire f
        if dedupe_thresh > 0:
            dup = store.older_near_duplicate(schema, namespace, f["id"], dedupe_thresh)
            if dup is not None:
                store.bump_restatement(schema, dup["id"], f.get("source_turn_ids") or [])
                store.expire(schema, namespace, f["id"])
                out["deduped"] += 1
                continue
        # (b) SPO supersession — identical gate to _write_fact
        hist = bool(_HISTORICAL_RE.search(stmt)) and not _CHANGE_OF_STATE_RE.search(stmt)
        explicit_ev = parse_event_date(stmt, None) if hist else None
        n = 0
        if (subj and pred and _supersedable(pred, obj) and ev is not None
                and not (hist and explicit_ev is None)):
            ids = store.supersede_predicate(schema, namespace, subj, pred, obj, ev,
                                            observed_at=f.get("observed_at"),
                                            historical=hist, event_date=explicit_ev)
            store.mark_superseded_by(schema, ids, f["id"])
            n += len(ids)
        # (c) REVERSAL/NEGATION close-out — same guards as _write_fact
        if n == 0 and neg_thresh > 0 and _REVERSAL_RE.search(stmt):
            cands = store.nearest_live_facts_to(schema, namespace, f["id"], k=8)
            for cand in _negation_targets(stmt, subj, cands, neg_thresh):
                n += store.close_out(schema, namespace, cand["id"],
                                     valid_to=ev or datetime.now(timezone.utc),
                                     superseded_by=f["id"])
        out["closed"] += n
    return out
