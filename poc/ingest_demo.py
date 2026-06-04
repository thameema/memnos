"""Ingest a real 1000-word input + 5000-word LLM reply and show storage growth.

episode (raw verbatim) -> memory chunks (embedded) -> facts -> entities -> mentions,
with provenance (source_episode_id). Run twice-over with size measurement around it.
"""
import random
import sys

sys.path.insert(0, ".")
import psycopg
from memnos_poc.embedder import embed  # 1536-d stand-in (size is what matters here)

DSN = "postgresql://memnos:memnos_poc@localhost:5433/memnos"
SCHEMA = "tenant_demo"
NS = "demo:ns"

VOCAB = ("acme merger m&a team jane bob postgres pgvector decision rationale architecture "
         "latency tenant namespace embedding retrieval fact entity episode provenance audit "
         "litigation paralegal contract globex redis outage cto billing module deposition").split()


def make_text(nwords, seed):
    random.seed(seed)
    return " ".join(random.choice(VOCAB) for _ in range(nwords))


def chunk(text, size_words=225):
    w = text.split()
    return [" ".join(w[i:i+size_words]) for i in range(0, len(w), size_words)]


def main():
    conn = psycopg.connect(DSN, autocommit=True)
    c = conn.cursor()

    # input 1000 words, output 5000 words
    user_text = make_text(1000, 1)
    asst_text = make_text(5000, 2)

    # --- episodes: raw verbatim, stored once ---
    c.execute(f"INSERT INTO {SCHEMA}.episode(namespace,role,content) VALUES(%s,'user',%s) RETURNING id", (NS, user_text))
    ep_in = c.fetchone()[0]
    c.execute(f"INSERT INTO {SCHEMA}.episode(namespace,role,content) VALUES(%s,'assistant',%s) RETURNING id", (NS, asst_text))
    ep_out = c.fetchone()[0]

    # --- memory chunks: embed both sides (the 'embed everything' case) ---
    n_chunks = 0
    for ep_id, text in [(ep_in, user_text), (ep_out, asst_text)]:
        for ch in chunk(text):
            vec = "[" + ",".join(f"{x:.5f}" for x in embed(ch)) + "]"
            c.execute(f"INSERT INTO {SCHEMA}.memory(namespace,content,embedding,source_episode_id) "
                      f"VALUES(%s,%s,%s::halfvec,%s)", (NS, ch, vec, ep_id))
            n_chunks += 1

    # --- entities (deduped) ---
    ent_ids = []
    for i in range(40):
        c.execute(f"INSERT INTO {SCHEMA}.entity(namespace,name,entity_type) VALUES(%s,%s,'CONCEPT') "
                  f"ON CONFLICT (namespace,name) DO UPDATE SET name=EXCLUDED.name RETURNING id", (NS, f"ent_{i}"))
        ent_ids.append(c.fetchone()[0])

    # --- facts from BOTH episodes (OpenIE on input + output) ---
    n_facts = 0
    for ep_id in (ep_in, ep_out):
        k = 20 if ep_id == ep_in else 50   # the denser reply yields more facts
        for j in range(k):
            s, o = random.choice(VOCAB), random.choice(VOCAB)
            c.execute(f"INSERT INTO {SCHEMA}.fact(namespace,subject,predicate,object,source_episode_id) "
                      f"VALUES(%s,%s,'relates_to',%s,%s)", (NS, s, o, ep_id))
            n_facts += 1

    # --- mentions: link each memory to ~2 entities ---
    c.execute(f"SELECT id FROM {SCHEMA}.memory WHERE namespace=%s", (NS,))
    for (mid,) in c.fetchall():
        for eid in random.sample(ent_ids, 2):
            c.execute(f"INSERT INTO {SCHEMA}.mentions(memory_id,entity_id) VALUES(%s,%s) ON CONFLICT DO NOTHING", (mid, eid))

    print(f"ingested: 2 episodes (1000w + 5000w), {n_chunks} memory chunks, "
          f"{n_facts} facts, {len(ent_ids)} entities")
    conn.close()


if __name__ == "__main__":
    main()
