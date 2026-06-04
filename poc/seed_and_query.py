"""POC-4: seed the Acme/Jane/Bob example and run hybrid retrieval.

Demonstrates that the 1-hop graph arm surfaces a relevant memory that neither
vector nor full-text search finds on its own — proving the bridge earns its keep.
"""
import sys
sys.path.insert(0, ".")
from memnos_poc.embedder import embed
from memnos_poc.storage import PgStorage

DSN = "postgresql://memnos:memnos_poc@localhost:5433/memnos"
SCHEMA = "tenant_acme"
NS = "acme:eng:projectM"


def main():
    s = PgStorage(DSN)
    s.create_tenant("acme")

    # entities
    acme = s.insert_entity(SCHEMA, NS, "acme", "ORG")
    mna = s.insert_entity(SCHEMA, NS, "m&a team", "ORG")
    jane = s.insert_entity(SCHEMA, NS, "jane", "PERSON")
    bob = s.insert_entity(SCHEMA, NS, "bob", "PERSON")
    lit = s.insert_entity(SCHEMA, NS, "litigation team", "ORG")

    # memories (content, mentioned-entities)
    rows = [
        ("The Acme merger is handled by the M&A team.", [acme, mna]),
        ("Jane moved to the M&A team in March.", [jane, mna]),       # graph-only hit
        ("Bob is Jane's paralegal.", [bob, jane]),                    # 2 hops away
        ("Jane used to be on the Litigation team.", [jane, lit]),
        ("Acme's headquarters are in Chicago.", [acme]),
        ("The office coffee machine was repaired last week.", []),    # distractor
    ]
    for content, ents in rows:
        s.insert_memory(SCHEMA, NS, content, embed(content), entity_ids=ents)

    # relations (typed graph edges) — not needed for this query but proves writes
    s.insert_relation(SCHEMA, NS, jane, mna, "member_of")
    s.insert_relation(SCHEMA, NS, bob, jane, "assists")

    query = "who handles the acme merger"
    print(f"\nQUERY: {query!r}\n")
    # k=2 per arm: with only 6 rows, a large k would let the vector arm return
    # everything and mask per-arm differences. Production uses k~20 over millions.
    results = s.hybrid_search(SCHEMA, NS, query, embed(query), k=2, top_k=6)
    print(f"{'score':>7}  {'V':1} {'F':1} {'G':1}  content")
    print("-" * 70)
    for r in results:
        flags = ("•" if r["in_vec"] else " ", "•" if r["in_fts"] else " ", "•" if r["in_graph"] else " ")
        print(f"{r['score']:>7}  {flags[0]} {flags[1]} {flags[2]}  {r['content']}")
    print("\nV=vector  F=full-text  G=1-hop graph")

    graph_only = [r for r in results if r["in_graph"] and not r["in_vec"] and not r["in_fts"]]
    print(f"\nGraph-only finds (missed by vector+FTS): "
          f"{[r['content'] for r in graph_only] or 'none'}")


if __name__ == "__main__":
    main()
