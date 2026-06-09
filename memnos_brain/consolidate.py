"""B2 — CONSOLIDATION ("sleep") pass. The core accuracy fix.

Offline, the brain replays episodes and writes durable SEMANTIC memory. We do the
same: episodic events → (1) decontextualized propositions, (2) per-entity dossiers
that PRE-JOIN multi-hop facts ("A works at B" + "B in C" => "A works in C"). Every
semantic fact keeps provenance back to its episodic evidence (auditable — consolidation
hallucinates) and a valid_from for bi-temporal recall. New facts SUPERSEDE
contradicted ones (set valid_to, never delete).

LLM is used HERE (offline) — never at query time. Calls run concurrently; pass a
metered client to enforce a budget.
"""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from .store import BrainStore

PROP_SYS = (
    "Convert this dated conversation EVENT into atomic, self-contained FACTS. "
    "Resolve pronouns and references to explicit named entities. Attach the date when "
    "relevant. Each fact must be understandable with NO access to the conversation. "
    'Return JSON {"facts": ["...", ...]} — short declarative sentences, no commentary.')

DOSSIER_SYS = (
    "You consolidate everything known about ONE subject into durable, CURRENT facts. "
    "Critically, DERIVE facts that require COMBINING multiple inputs "
    "(e.g. 'Alice works at Boeing' + 'Boeing is in Seattle' => 'Alice works in Seattle'). "
    "When inputs conflict, keep the MOST RECENT (dates are given) and drop the stale one. "
    "Preserve dates. Return JSON {\"facts\": [\"...\", ...]} of standalone sentences.")


def _facts(cli, model, sys_prompt, content, meter):
    r = cli.chat.completions.create(
        model=model, temperature=0, max_tokens=700,
        response_format={"type": "json_object"},
        messages=[{"role": "system", "content": sys_prompt},
                  {"role": "user", "content": content}])
    if meter is not None:
        meter.record("consolidate", model, r.usage.prompt_tokens, r.usage.completion_tokens)
    try:
        return [str(x).strip() for x in json.loads(r.choices[0].message.content).get("facts", [])
                if str(x).strip()]
    except (json.JSONDecodeError, ValueError, AttributeError):
        return []


class Consolidator:
    def __init__(self, store: BrainStore, schema: str, ns: str, llm, model: str,
                 embed_fn, meter=None, workers: int = 8,
                 max_entities: int = 30, min_episodes: int = 3, max_facts_per_dossier: int = 8):
        self.store, self.schema, self.ns = store, schema, ns
        self.llm, self.model, self.embed = llm, model, embed_fn
        self.meter, self.workers = meter, workers
        self.max_entities, self.min_episodes = max_entities, min_episodes
        self.max_facts_per_dossier = max_facts_per_dossier
        self._wlock = threading.Lock()      # serialize DB writes (single conn)
        self._seen = set()                  # statement-level dedup within a run

    # --- pass 1: episode -> propositions ---------------------------------
    def _propositions(self, episodes):
        def one(ep):
            content = (f"[date: {ep['t_start']}]\n{ep['text']}")
            facts = _facts(self.llm, self.model, PROP_SYS, content, self.meter)
            return ep, facts
        out = []
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            for ep, facts in ex.map(one, episodes):
                out.append((ep, facts))
        return out   # [(episode, [fact,...])]

    # --- pass 2: entity -> dossier (multi-hop pre-join) ------------------
    def _dossiers(self, ent_clusters, prop_by_ep):
        def one(item):
            name, ep_ids = item["name"], item["ep_ids"]
            facts_in = []
            for eid in ep_ids:
                facts_in += prop_by_ep.get(eid, [])
            if len(facts_in) < 3:
                return name, ep_ids, []
            content = f"Subject: {name}\nKnown facts (dated):\n- " + "\n- ".join(facts_in[:50])
            return name, ep_ids, _facts(self.llm, self.model, DOSSIER_SYS, content, self.meter)
        out = []
        with ThreadPoolExecutor(max_workers=self.workers) as ex:
            for name, ep_ids, facts in ex.map(one, ent_clusters):
                out.append((name, ep_ids, facts))
        return out

    def _write(self, kind, statement, ep_ids, valid_from, salience, subject=None):
        key = " ".join(statement.lower().split())
        with self._wlock:
            if key in self._seen:           # dedup identical consolidated statements
                return 0
            self._seen.add(key)
        vec = self.embed(statement)
        with self._wlock:
            n_super = self.store.supersede_similar(self.schema, self.ns, vec, subject, valid_from)
            if subject:        # belief-change: close out the prior value for this subject
                n_super += self.store.supersede_subject(self.schema, self.ns, subject, vec, valid_from)
            sid = self.store.insert_semantic(
                self.schema, self.ns, kind, statement, subject=subject,
                valid_from=valid_from, salience=salience, vec=vec)
            self.store.add_provenance(self.schema, sid, ep_ids)
            # link semantic fact to the entity graph too
            if subject:
                eid = self.store.upsert_entity(self.schema, self.ns, subject[:100], vec=self.embed(subject))
                self.store.add_mention(self.schema, eid, sid, "semantic")
        return n_super

    def run(self) -> dict:
        episodes = self.store.fetch_episodes(self.schema, self.ns, only_unconsolidated=True)
        if not episodes:
            return {"episodes": 0, "propositions": 0, "dossiers": 0, "superseded": 0}

        # PASS 1 — propositions per episode
        prop_results = self._propositions(episodes)
        prop_by_ep, n_prop, superseded = {}, 0, 0
        for ep, facts in prop_results:
            prop_by_ep[ep["id"]] = facts

        # PASS 2 — entity dossiers (the multi-hop pre-join); cap entities to cut noise
        clusters = self.store.entity_episodes(self.schema, self.ns,
                                              min_episodes=self.min_episodes)[: self.max_entities]
        dossiers = self._dossiers(clusters, prop_by_ep)

        # batch-embed all fact statements up front if the embedder supports it (fast OpenAI path)
        all_facts = [f for _, fs in prop_results for f in fs]
        for _, _, fs in dossiers:
            all_facts += fs[: self.max_facts_per_dossier]
        if hasattr(self.embed, "prime"):
            self.embed.prime(all_facts)

        for ep, facts in prop_results:
            for f in facts:
                superseded += self._write("proposition", f, [ep["id"]], ep["t_start"],
                                          float(ep["salience"]))
                n_prop += 1
        n_dos = 0
        ep_time = {e["id"]: e["t_start"] for e in episodes}
        for name, ep_ids, facts in dossiers:
            vf = max((ep_time.get(i) for i in ep_ids if ep_time.get(i)), default=None)
            for f in facts[: self.max_facts_per_dossier]:
                superseded += self._write("dossier", f, list(ep_ids), vf, 0.8, subject=name)
                n_dos += 1

        self.store.mark_consolidated(self.schema, [e["id"] for e in episodes])
        return {"episodes": len(episodes), "propositions": n_prop, "dossiers": n_dos,
                "superseded": superseded}
