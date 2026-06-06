"""Phase C — memnos memory SERVICE: a clean remember/recall API over the engine,
plus the Anthropic memory-tool op surface. This is what an MCP server / Claude Code
hooks call. No query-time LLM on recall (quota retrieval + rerank).

Multi-tenant: ONE schema (tenant_memnos) with a `namespace` column per the design
(namespace = user/team/agent scope). recall() = Phase A quota retrieval.
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timezone

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
                 dim=1536, llm=None, extract_model="gpt-4o-mini", ensure_schema=False):
        # store_or_dsn may be a pooled BrainStore (production) or a DSN string (scripts).
        self.store = store_or_dsn if isinstance(store_or_dsn, BrainStore) else BrainStore(store_or_dsn)
        self.embed = embed_fn
        self.reranker = reranker_model
        self.llm, self.extract_model = llm, extract_model
        self.schema = self.store.create_schema(_TENANT, dim=dim) if ensure_schema else f"tenant_{_TENANT}"

    # --- WRITE -------------------------------------------------------------
    def remember(self, namespace: str, text: str, *, speaker=None, session_id=None,
                 observed_at=None, extract=True) -> dict:
        """Store a message: raw turn (always) + STRUCTURED date-aware SPO fact
        extraction (optional, offline LLM). Each fact is stored with
        subject/predicate/object so belief-change SUPERSESSION fires (close out the
        prior value for the same subject+predicate) and the entity GRAPH is populated
        — parity with the validated phaseA engine. Returns ids + supersession count."""
        from .temporal import parse_event_date
        observed_at = observed_at or datetime.now(timezone.utc)
        tid = self.store.insert_raw_turn(self.schema, namespace, session_id, speaker,
                                         text, observed_at, self.embed(text))
        n_facts = n_super = 0
        if extract and self.llm is not None:
            for f in self._extract(text, observed_at):
                stmt = f["statement"]
                subj = (f["subject"][:100] if f["subject"] else None)
                pred = f["predicate"] or None
                obj = f["object"] or None
                ev = parse_event_date(stmt, observed_at)        # relative → absolute event date
                if subj and pred:                                # belief-change supersession
                    n_super += self.store.supersede_predicate(self.schema, namespace, subj, pred, obj, ev)
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
                n_facts += 1
        return {"turn_id": tid, "facts": n_facts, "superseded": n_super}

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
    def recall(self, namespace: str, query: str, *, k=40, raw_quota=8, fact_quota=6) -> list[dict]:
        """Quota retrieval (raw turns ⊕ semantic facts) with BI-TEMPORAL awareness:
        for time-scoped questions, retrieve facts by EVENT TIME (valid_from window /
        current-vs-past / first-last ordering), lean on dated facts, and surface the
        date so the answerer can reason. No LLM at query time."""
        from . import temporal as T
        now = self.store.max_observed_at(self.schema, namespace) or datetime.now(timezone.utc)
        intent = T.analyze(query, now)
        qv = self.embed(query)
        raw = self.store.search_raw_turns(self.schema, namespace, qv, query, k)
        if intent.temporal:
            sem = self.store.search_semantic_temporal(
                self.schema, namespace, qv, query, k,
                start=intent.start, end=intent.end, current_only=intent.current, order=intent.order)
            fact_quota = max(fact_quota, 12)        # temporal leans on dated facts
            raw_quota = min(raw_quota, 6)
        else:
            sem = self.store.search_semantic(self.schema, namespace, qv, query, k)

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
        return rr(raw, "turn")[:raw_quota] + rr(sem, "fact")[:fact_quota]

    def context(self, namespace: str, query: str, *, max_chars=4000, **kw) -> str:
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
    def consolidate(self, namespace: str, max_entities=30, max_dossier=6) -> dict:
        """Build entity dossiers (offline multi-hop pre-join) from stored facts."""
        import json
        from collections import defaultdict
        with self.store.conn.cursor() as c:
            c.execute(f"SELECT statement, valid_from FROM {self.schema}.semantic "
                      f"WHERE namespace=%s AND kind='fact'", (namespace,))
            facts = c.fetchall()
        ent = defaultdict(list)
        for row in facts:
            for e in set(_PROPER.findall(row["statement"])):
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
