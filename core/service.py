"""Phase C — memnos memory SERVICE: a clean remember/recall API over the engine,
plus the Anthropic memory-tool op surface. This is what an MCP server / Claude Code
hooks call. No query-time LLM on recall (quota retrieval + rerank).

Multi-tenant: ONE schema (tenant_memnos) with a `namespace` column per the design
(namespace = user/team/agent scope). recall() = Phase A quota retrieval.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone, timedelta

from .store import BrainStore
from . import rerank as brain_rerank

_TENANT = "memnos"
_PROPER = re.compile(r"\b[A-Z][a-zA-Z]{2,}\b")

# Belief-change supersession applies ONLY to SINGLE-VALUED attributes (a person has one
# current home/job/age — a new value replaces the old). MULTI-VALUED relations
# ('did_activity','met_person','visited','likes','owns') are ADDITIVE — a new martial art
# does NOT replace a previous one. Over-superseding list items corrupts aggregation +
# temporal recall, so default is ADDITIVE unless the predicate matches a single-valued cue.
_SINGLE_VALUED_CUES = ("live", "reside", "home", "based", "located", "current_city",
                       "work_at", "works_at", "employ", "employer", "job_title", "occupation",
                       "role_at", "age", "marital", "married", "spouse", "status")


def _is_single_valued(pred: str) -> bool:
    return bool(pred) and any(cue in pred for cue in _SINGLE_VALUED_CUES)


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
            facts = self.extract_facts(text, observed_at)
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

    def extract_facts(self, text, observed_at):
        """Phase 2 (slow, NO database use): LLM fact extraction. Safe to run with no
        connection held — pure model I/O."""
        return self._extract(text, observed_at)

    def write_facts(self, namespace, facts, observed_at, turn_id, *, memory_type=None) -> tuple:
        """Phase 3 (fast, DB only): supersession + store + graph for extracted facts.
        `memory_type` = the source TURN's type — derived facts INHERIT it."""
        n_facts = n_super = 0
        for f in facts:
            df, ds = self._write_fact(namespace, f, observed_at, source_turn_ids=[turn_id],
                                      memory_type=memory_type)
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
                    memory_type=None):
        """Write ONE SPO fact: absolute event date → belief-change supersession → store
        with subject/predicate/object (+ provenance to its source turn) → populate the
        entity graph. Shared by remember() and ingest_session() so the write path can
        never drift between them. Returns (facts_written, superseded_count)."""
        from .temporal import parse_event_date
        stmt = f["statement"]
        subj = (f["subject"][:100] if f["subject"] else None)
        pred = f["predicate"] or None
        obj = f["object"] or None
        ev = parse_event_date(stmt, fallback_date)          # relative → absolute event date
        n_super = 0
        if subj and pred and _is_single_valued(pred):        # belief-change ONLY for single-valued attrs
            n_super = self.store.supersede_predicate(self.schema, namespace, subj, pred, obj, ev)
        vec = self.embed(stmt)
        fid = self.store.insert_semantic(self.schema, namespace, "fact", stmt,
                                         subject=subj, predicate=pred, obj=obj,
                                         valid_from=ev, salience=0.5, vec=vec,
                                         source_turn_ids=source_turn_ids, author=self.author,
                                         memory_type=memory_type)
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
                           "the named person it's about; predicate = a short normalized relation "
                           "('lives_in','works_at','did_activity','met_person','visited','likes') or '' if it "
                           "doesn't fit; object = the value or ''. ALWAYS include a statement even when "
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
               entity_quota=10) -> list[dict]:
        """Quota retrieval (raw turns ⊕ semantic facts) with BI-TEMPORAL awareness:
        for time-scoped questions, retrieve facts by EVENT TIME (valid_from window /
        current-vs-past / first-last ordering), lean on dated facts, and surface the
        date so the answerer can reason. No LLM at query time.

        Implemented as recall_fetch (DB only) + recall_rank (CPU only) so pooled callers
        can release the connection before the ~200ms cross-encoder rerank. This wrapper
        keeps the original single-call behavior for in-process users."""
        return self.recall_rank(query, self.recall_fetch(namespace, query, k=k),
                                raw_quota=raw_quota, fact_quota=fact_quota,
                                entity_quota=entity_quota)

    def recall_fetch(self, namespace: str, query: str, *, k=40, qv=None,
                     extra_namespaces=()) -> dict:
        """DB phase of recall: temporal intent + ALL store queries (raw, semantic,
        timeline/entity-guarantee arms). `qv` lets pooled callers pre-embed the query
        (network) before acquiring a connection. Returns a bundle for recall_rank.

        GROUNDED RECALL (0.1.6): `extra_namespaces` = linked knowledge namespaces the
        CALLER may read (the server checks both the link and the read grant). Their
        candidates are hybrid-searched per namespace, tagged with their source namespace
        ("_ns"), and merged into the bundle — ONE rerank covers everything. Primary-
        namespace rows stay untagged, so with no links the output is unchanged."""
        from . import temporal as T
        now = self.store.max_observed_at(self.schema, namespace) or datetime.now(timezone.utc)
        intent = T.analyze(query, now)
        if qv is None:
            qv = self.embed(query)
        b = {"intent": intent, "ents": T.query_entities(query)}
        b["raw"] = self.store.search_raw_turns(self.schema, namespace, qv, query, k)
        if not intent.temporal:
            b["sem"] = self.store.search_semantic(self.schema, namespace, qv, query, k)
            if b["ents"]:
                # ENTITY-GUARANTEE arm: vector search misses items in list/aggregation
                # answers ('what martial arts' ≁ 'taekwondo'); 82% of eval failures were
                # retrieval misses. Guarantee the facts about the query's entities — the
                # same JOIN-not-cosine trick the timeline arm uses for temporal.
                b["dump"] = self.store.timeline(self.schema, namespace, b["ents"],
                                                order="desc", limit=20)
        else:
            # TEMPORAL: GUARANTEE the entity timeline (parity with the tested phaseA
            # engine). Vector search structurally misses dated evidence ('when did X'
            # ≁ 'I moved out') — a JOIN/range, not a cosine bet. This is the arm that
            # lifted temporal recall 12% → 70%.
            b["tl"] = self.store.timeline(self.schema, namespace, b["ents"], start=intent.start,
                                          end=intent.end, order=intent.order or "asc", limit=12)
            b["sem"] = self.store.search_semantic_temporal(
                self.schema, namespace, qv, query, k,
                start=intent.start, end=intent.end, current_only=intent.current,
                order=intent.order)
        # grounded fan-out: per-knowledge-namespace fetches, merged for the SINGLE rerank
        for kns in extra_namespaces:
            for r in self.store.search_raw_turns(self.schema, kns, qv, query, k):
                r["_ns"] = kns; b["raw"].append(r)
            for r in self.store.search_semantic(self.schema, kns, qv, query, k):
                r["_ns"] = kns; b.setdefault("sem", []).append(r)
        return b

    def recall_rank(self, query: str, b: dict, *, raw_quota=11, fact_quota=8,
                    entity_quota=10) -> list[dict]:
        """CPU phase of recall: cross-encoder rerank + quota assembly over a
        recall_fetch bundle. No database access — safe with no connection held.
        Ranking is byte-identical to the pre-split recall()."""
        intent = b["intent"]

        def rr(items, kind):
            if not items:
                return []
            order = brain_rerank.rerank(query, [c["content"] for c in items], self.reranker)
            out = []
            for i, s in order:
                row = {"content": items[i]["content"], "kind": kind, "score": float(s)}
                if kind == "fact" and items[i].get("valid_from"):
                    row["date"] = items[i]["valid_from"].date().isoformat()
                if items[i].get("author"):                    # who wrote this memory
                    row["author"] = items[i]["author"]
                if items[i].get("memory_type"):               # typed memory (0.1.6)
                    row["type"] = items[i]["memory_type"]
                if items[i].get("_ns"):                       # grounded: source namespace
                    row["namespace"] = items[i]["_ns"]
                out.append(row)
            return out

        if not intent.temporal:
            sem_rows = rr(b["sem"], "fact")
            if not b["ents"]:
                return rr(b["raw"], "turn")[:raw_quota] + sem_rows[:fact_quota]
            seen, eg = set(), []
            for r in b.get("dump", []):
                c = r["content"]
                if c in seen:
                    continue
                seen.add(c)
                row = {"content": c, "kind": "fact",
                       "date": r["valid_from"].date().isoformat() if r.get("valid_from") else None}
                if r.get("author"):
                    row["author"] = r["author"]
                if r.get("memory_type"):
                    row["type"] = r["memory_type"]
                eg.append(row)
            sem_rows = [r for r in sem_rows if r["content"] not in seen]
            return rr(b["raw"], "turn")[:raw_quota] + eg[:entity_quota] + sem_rows[:fact_quota]

        tl_rows, tl_seen = [], set()
        for r in b.get("tl", []):
            c = r["content"]
            if c in tl_seen:
                continue
            tl_seen.add(c)
            row = {"content": c, "kind": "fact",
                   "date": r["valid_from"].date().isoformat() if r.get("valid_from") else None}
            if r.get("author"):
                row["author"] = r["author"]
            if r.get("memory_type"):
                row["type"] = r["memory_type"]
            tl_rows.append(row)
        sem_rows = [r for r in rr(b["sem"], "fact") if r["content"] not in tl_seen]
        # small raw + GUARANTEED timeline + a few reranked relevance facts
        return rr(b["raw"], "turn")[:5] + tl_rows[:12] + sem_rows[:6]

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

    def recall_wide_fetch(self, namespaces, query, *, k=40, qv=None):
        """DB phase of recall_wide: per-namespace hybrid search + RRF-score candidate cap."""
        if not namespaces:
            return [], []
        if qv is None:
            qv = self.embed(query)
        raw_c, sem_c = [], []
        for ns in namespaces:
            for r in self.store.search_raw_turns(self.schema, ns, qv, query, k):
                r["_ns"] = ns; raw_c.append(r)
            for r in self.store.search_semantic(self.schema, ns, qv, query, k):
                r["_ns"] = ns; sem_c.append(r)
        # cap candidates by RRF score before the (CPU) cross-encoder rerank
        raw_c = sorted(raw_c, key=lambda x: x.get("score", 0), reverse=True)[:60]
        sem_c = sorted(sem_c, key=lambda x: x.get("score", 0), reverse=True)[:60]
        return raw_c, sem_c

    def recall_wide_rank(self, query, raw_c, sem_c, *, raw_quota=11, fact_quota=8) -> list[dict]:
        """CPU phase of recall_wide: global cross-encoder rerank. No database access."""

        def rr(items, kind, quota):
            if not items:
                return []
            order = brain_rerank.rerank(query, [c["content"] for c in items], self.reranker)
            out = []
            for i, s in order[:quota]:
                it = items[i]
                row = {"content": it["content"], "kind": kind, "score": float(s), "namespace": it["_ns"]}
                if kind == "fact" and it.get("valid_from"):
                    row["date"] = it["valid_from"].date().isoformat()
                if it.get("author"):
                    row["author"] = it["author"]
                if it.get("memory_type"):
                    row["type"] = it["memory_type"]
                out.append(row)
            return out

        return rr(raw_c, "turn", raw_quota) + rr(sem_c, "fact", fact_quota)

    @staticmethod
    def render_context(rows, *, max_chars=9000, viewer=None) -> str:
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
        context block ahead of every ranked result."""
        out, used = [], 0
        for r in rows:
            tag = f" [{r['namespace']}]" if r.get("namespace") else ""
            by = (f", by {r['author']}"
                  if viewer is not None and r.get("author") and r["author"] != viewer else "")
            if r.get("pinned"):
                line = f"CONSTRAINT:{tag} {r['content']}"
            else:
                label = r.get("type") or ("fact" if r["kind"] == "fact" else "said")
                d = f", {r['date']}" if (r["kind"] == "fact" and r.get("date")) else ""
                line = f"- ({label}{d}{by}){tag} {r['content']}"
            if used + len(line) > max_chars:
                break
            out.append(line); used += len(line)
        return "\n".join(out)

    def context_wide(self, namespaces, query, *, max_chars=9000, **kw) -> str:
        return self.render_context(self.recall_wide(namespaces, query, **kw), max_chars=max_chars)

    def context(self, namespace: str, query: str, *, max_chars=9000, **kw) -> str:
        return self.render_context(self.recall(namespace, query, **kw), max_chars=max_chars)

    # --- consolidation (offline; call on idle / schedule) -----------------
    def consolidate(self, namespace: str, max_entities=25, max_dossier=6,
                    conn_factory=None) -> dict:
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
                c.execute(f"SELECT statement, subject_entity, valid_from, source_turn_ids "
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
                ent[e].append((row["valid_from"], row["statement"]))
                ent_src[e].update(row.get("source_turn_ids") or [])
        clusters = sorted(((e, fs) for e, fs in ent.items() if len(fs) >= 3),
                          key=lambda x: -len(x[1]))[:max_entities]

        # LLM phase — NO connection required: dossier generation + embeddings only.
        results = []                                         # (entity, vf, [(fact, vec)...])
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
                               "\n- ".join(f for _, f in fs[:40])}])
                self._track(self.extract_model, r)
                vf = max((d for d, _ in fs if d), default=None)
                items = []
                for f in [_dossier_text(x) for x in json.loads(r.choices[0].message.content).get("facts", [])][:max_dossier]:
                    if not f:
                        continue
                    items.append((f, self.embed(f)))
                if items:
                    results.append((e, vf, items))
            except Exception:
                continue

        n = 0

        def _write(store):
            nonlocal n
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

        if conn_factory is not None:                          # WRITE phase (short conn)
            with conn_factory() as conn:
                _write(BrainStore(conn=conn))
        else:
            _write(self.store)
        return {"dossiers": n}

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
