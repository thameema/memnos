"""PG ingest pipeline: episode → gate → extract → chunk/embed → memory + facts + mentions.

Embeddings: local bge-small (free). Extraction: OpenAI (pluggable, metered).
Every OpenAI call goes through the CostMeter so a run respects the budget cap.
"""
from __future__ import annotations

import json

from memnos_poc import local_models
from memnos_poc.usage import CostMeter

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "entities": {"type": "array", "items": {"type": "object",
            "properties": {"name": {"type": "string"}, "type": {"type": "string"}}, "required": ["name", "type"]}},
        "facts": {"type": "array", "items": {"type": "object",
            "properties": {"subject": {"type": "string"}, "predicate": {"type": "string"}, "object": {"type": "string"}},
            "required": ["subject", "predicate", "object"]}},
    }, "required": ["entities", "facts"]}

_SYS = ("Extract entities (name,type) and subject-predicate-object facts from the text. "
        "Lowercase names. Return JSON only.")


def _embed_literal(text: str) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in local_models.embed(text)) + "]"


def extract(openai_client, model: str, text: str, meter: CostMeter):
    r = openai_client.chat.completions.create(
        model=model, temperature=0, response_format={"type": "json_object"},
        messages=[{"role": "system", "content": _SYS + " Schema: " + json.dumps(_EXTRACT_SCHEMA)},
                  {"role": "user", "content": text}])
    meter.record("extract", model, r.usage.prompt_tokens, r.usage.completion_tokens)
    try:
        d = json.loads(r.choices[0].message.content)
        return d.get("entities", []), d.get("facts", [])
    except (json.JSONDecodeError, ValueError):
        return [], []


def ingest_turn(storage, schema, ns, role, text, *, embed_fn, openai_client=None, extract_model=None,
                meter: CostMeter, do_extract: bool = True, gate_min_chars: int = 12) -> dict:
    """Ingest one turn. Returns counts. Stores: episode (raw) + memory (embedded)
    + extracted entities/facts + mentions (memory↔entity). ``embed_fn`` is the
    pluggable embedder (OpenAI or local)."""
    if len(text.strip()) < gate_min_chars:      # gate: skip trivial turns
        return {"skipped": True}

    ep = storage.insert_episode(schema, ns, role, text)
    mid = storage.insert_memory(schema, ns, text, embed_fn(text), entity_ids=())

    n_ent = n_fact = 0
    if do_extract and openai_client is not None:
        entities, facts = extract(openai_client, extract_model, text, meter)
        ent_ids = []
        for e in entities:
            name = str(e.get("name", "")).strip().lower()
            if name:
                ent_ids.append(storage.insert_entity(schema, ns, name[:100],
                                                      str(e.get("type", "CONCEPT")).upper()[:32]))
        # link the memory to its mentioned entities (the 1-hop bridge)
        with storage.conn.cursor() as c:
            for eid in ent_ids:
                c.execute(f"INSERT INTO {schema}.mentions(memory_id,entity_id) VALUES(%s,%s) "
                          f"ON CONFLICT DO NOTHING", (mid, eid))
        for f in facts:
            s, p, o = (str(f.get(k, "")).strip().lower() for k in ("subject", "predicate", "object"))
            if s and p and o:
                storage.insert_fact(schema, ns, s[:100], p[:60], o[:200], source_episode_id=ep)
                n_fact += 1
        n_ent = len(ent_ids)
    return {"episode": ep, "memory": mid, "entities": n_ent, "facts": n_fact}
