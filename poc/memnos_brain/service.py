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
                 on_usage=None):
        # store_or_dsn may be a pooled BrainStore (production) or a DSN string (scripts).
        self.store = store_or_dsn if isinstance(store_or_dsn, BrainStore) else BrainStore(store_or_dsn)
        self.embed = embed_fn
        self.reranker = reranker_model
        self.llm, self.extract_model = llm, extract_model
        # on_usage(model, prompt_tokens, completion_tokens): called after EVERY engine LLM
        # call (extraction + consolidation) so token cost is fully accounted — the server
        # records it to usage_ledger, the benchmark feeds it to the budget cap. No silent
        # untracked spend.
        self.on_usage = on_usage
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
                 observed_at=None, extract=True) -> dict:
        """Store a message: raw turn (always) + STRUCTURED date-aware SPO fact
        extraction (optional, offline LLM). Each fact is stored with
        subject/predicate/object so belief-change SUPERSESSION fires (close out the
        prior value for the same subject+predicate) and the entity GRAPH is populated
        — parity with the validated phaseA engine. Returns ids + supersession count."""
        observed_at = observed_at or datetime.now(timezone.utc)
        tid = self.store.insert_raw_turn(self.schema, namespace, session_id, speaker,
                                         text, observed_at, self.embed(text))
        n_facts = n_super = 0
        if extract and self.llm is not None:
            for f in self._extract(text, observed_at):
                df, ds = self._write_fact(namespace, f, observed_at)
                n_facts += df; n_super += ds
        return {"turn_id": tid, "facts": n_facts, "superseded": n_super}

    def ingest_session(self, namespace: str, turns, *, session_date, session_id=None,
                       extract=True) -> dict:
        """BENCHMARKED ingest path (per-SESSION batch). Store each raw turn, then extract
        SPO facts from the WHOLE session at once (better pronoun / relative-date
        resolution than per-message), then supersede + graph-populate via the same
        `_write_fact` used by remember(). `turns` = [(speaker, text), ...].

        This is the SAME code the LoCoMo benchmark runs — there is one engine, not two.
        Feed sessions in chronological order so belief-change supersession closes the
        OLDER value first."""
        for ti, (spk, txt) in enumerate(turns):
            if not txt:
                continue
            self.store.insert_raw_turn(self.schema, namespace, session_id, spk, txt,
                                       session_date + timedelta(minutes=ti), self.embed(txt))
        n_facts = n_super = 0
        if extract and self.llm is not None:
            content = f"SESSION DATE: {session_date}\n\n" + "\n".join(
                f"{s}: {t}" for s, t in turns if t)
            for f in self._extract(content, session_date):
                df, ds = self._write_fact(namespace, f, session_date)
                n_facts += df; n_super += ds
        return {"turns": len(turns), "facts": n_facts, "superseded": n_super}

    def _write_fact(self, namespace, f, fallback_date):
        """Write ONE SPO fact: absolute event date → belief-change supersession → store
        with subject/predicate/object → populate the entity graph. Shared by remember()
        and ingest_session() so the write path can never drift between them.
        Returns (facts_written, superseded_count)."""
        from .temporal import parse_event_date
        stmt = f["statement"]
        subj = (f["subject"][:100] if f["subject"] else None)
        pred = f["predicate"] or None
        obj = f["object"] or None
        ev = parse_event_date(stmt, fallback_date)          # relative → absolute event date
        n_super = 0
        if subj and pred:                                    # belief-change supersession
            n_super = self.store.supersede_predicate(self.schema, namespace, subj, pred, obj, ev)
        vec = self.embed(stmt)
        fid = self.store.insert_semantic(self.schema, namespace, "fact", stmt,
                                         subject=subj, predicate=pred, obj=obj,
                                         valid_from=ev, salience=0.5, vec=vec)
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
        """Structured SPO extraction: [{subject, predicate, object, statement}] —
        enables same-(subject,predicate) belief-change supersession in production."""
        import json
        try:
            r = self.llm.chat.completions.create(
                model=self.extract_model, temperature=0, max_tokens=700,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content":
                           f"DATE: {date}. Extract atomic, self-contained FACTS as STRUCTURED triples. "
                           "RESOLVE relative dates ('yesterday','last Saturday') to ABSOLUTE using DATE, and "
                           "pronouns to named people. For each fact give: subject (the named entity it's "
                           "about), predicate (a short normalized relation like 'lives_in','works_at',"
                           "'owns_pet','favorite_food','job_title'), object (the value), and statement (a "
                           "full self-contained sentence with the date). "
                           'JSON {"facts":[{"subject":"...","predicate":"...","object":"...","statement":"..."}]}.'},
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
    def recall(self, namespace: str, query: str, *, k=40, raw_quota=11, fact_quota=8) -> list[dict]:
        """Quota retrieval (raw turns ⊕ semantic facts) with BI-TEMPORAL awareness:
        for time-scoped questions, retrieve facts by EVENT TIME (valid_from window /
        current-vs-past / first-last ordering), lean on dated facts, and surface the
        date so the answerer can reason. No LLM at query time."""
        from . import temporal as T
        now = self.store.max_observed_at(self.schema, namespace) or datetime.now(timezone.utc)
        intent = T.analyze(query, now)
        qv = self.embed(query)
        raw = self.store.search_raw_turns(self.schema, namespace, qv, query, k)

        def rr(items, kind):
            if not items:
                return []
            order = brain_rerank.rerank(query, [c["content"] for c in items], self.reranker)
            out = []
            for i, s in order:
                row = {"content": items[i]["content"], "kind": kind, "score": float(s)}
                if kind == "fact" and items[i].get("valid_from"):
                    row["date"] = items[i]["valid_from"].date().isoformat()
                out.append(row)
            return out

        if not intent.temporal:
            sem = self.store.search_semantic(self.schema, namespace, qv, query, k)
            return rr(raw, "turn")[:raw_quota] + rr(sem, "fact")[:fact_quota]

        # --- TEMPORAL: GUARANTEE the entity timeline in context (parity with the tested
        # phaseA engine). Vector search structurally misses dated evidence ('when did X'
        # ≁ 'I moved out'), so we GUARANTEE the entity facts sorted by event time — a
        # JOIN/range, not a cosine bet — then add a few reranked relevance facts. This is
        # the arm that lifted temporal recall 12% → 70%.
        ents = T.query_entities(query)
        tl = self.store.timeline(self.schema, namespace, ents, start=intent.start,
                                 end=intent.end, order=intent.order or "asc", limit=12)
        tl_rows, tl_seen = [], set()
        for r in tl:
            c = r["content"]
            if c in tl_seen:
                continue
            tl_seen.add(c)
            tl_rows.append({"content": c, "kind": "fact",
                            "date": r["valid_from"].date().isoformat() if r.get("valid_from") else None})
        sem = self.store.search_semantic_temporal(
            self.schema, namespace, qv, query, k,
            start=intent.start, end=intent.end, current_only=intent.current, order=intent.order)
        sem_rows = [r for r in rr(sem, "fact") if r["content"] not in tl_seen]
        # small raw + GUARANTEED timeline + a few reranked relevance facts
        return rr(raw, "turn")[:5] + tl_rows[:12] + sem_rows[:6]

    def context(self, namespace: str, query: str, *, max_chars=9000, **kw) -> str:
        out, used = [], 0
        for r in self.recall(namespace, query, **kw):
            if r["kind"] == "fact":
                d = f", {r['date']}" if r.get("date") else ""
                line = f"- (fact{d}) {r['content']}"
            else:
                line = f"- (said) {r['content']}"
            if used + len(line) > max_chars:
                break
            out.append(line); used += len(line)
        return "\n".join(out)

    # --- consolidation (offline; call on idle / schedule) -----------------
    def consolidate(self, namespace: str, max_entities=25, max_dossier=6) -> dict:
        """Build entity dossiers (offline multi-hop pre-join) from stored facts. Groups
        facts by SUBJECT entity (SPO) + proper nouns in the statement — same clustering
        as the benchmarked phaseA ingest."""
        import json
        from collections import defaultdict
        with self.store.conn.cursor() as c:
            c.execute(f"SELECT statement, subject_entity, valid_from FROM {self.schema}.semantic "
                      f"WHERE namespace=%s AND kind='fact'", (namespace,))
            facts = c.fetchall()
        ent = defaultdict(list)
        for row in facts:
            ents = set(_PROPER.findall(row["statement"]))
            if row.get("subject_entity"):
                ents.add(row["subject_entity"])
            for e in ents:
                ent[e].append((row["valid_from"], row["statement"]))
        clusters = sorted(((e, fs) for e, fs in ent.items() if len(fs) >= 3),
                          key=lambda x: -len(x[1]))[:max_entities]
        n = 0
        for e, fs in clusters:
            if self.llm is None:
                break
            try:
                r = self.llm.chat.completions.create(
                    model=self.extract_model, temperature=0, max_tokens=500,
                    response_format={"type": "json_object"},
                    messages=[{"role": "system", "content":
                               "Consolidate durable CURRENT facts about the subject, DERIVING multi-input "
                               "joins (A@B + B in C => A in C). On conflict keep the most recent. Keep dates "
                               "inline in each sentence. Each fact MUST be a single self-contained SENTENCE "
                               '(a string, not an object). JSON {"facts":["...", "..."]}.'},
                              {"role": "user", "content": f"Subject: {e}\n- " +
                               "\n- ".join(f for _, f in fs[:40])}])
                self._track(self.extract_model, r)
                vf = max((d for d, _ in fs if d), default=None)
                for f in [_dossier_text(x) for x in json.loads(r.choices[0].message.content).get("facts", [])][:max_dossier]:
                    if not f:
                        continue
                    vec = self.embed(f)
                    # belief-change supersession: close out the prior value for this subject
                    self.store.supersede_subject(self.schema, namespace, e[:100], vec, vf)
                    self.store.insert_semantic(self.schema, namespace, "dossier", f,
                                               subject=e[:100], valid_from=vf, salience=0.8, vec=vec)
                    n += 1
            except Exception:
                continue
        return {"dossiers": n}
