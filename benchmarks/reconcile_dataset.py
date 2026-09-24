"""Deterministic, realistic-shaped fact dataset for `memnos namespace reconcile` perf and
equivalence work (issue #155). Shared by `benchmarks/reconcile_perf.py` (the local
before/after benchmark whose numbers go in the PR) and `tests/test_reconcile_perf.py`
(the smaller CI-safe version).

The facts are ordinary ops / personal-memory statements with REAL local embeddings
(fastembed `BAAI/bge-small-en-v1.5`, 384-d — the same model memnos' free local mode
uses; no OpenAI, no network after the model is cached). The generator deliberately
plants the three kinds of pre-fix "contradiction debt" reconcile exists to clean up —
SPO value changes left live, verbatim restatements stored as second rows, reversal
statements that never closed their target — among a majority of unrelated facts, and
can pad the SAME table with distractor namespaces (production's semantic table holds
many namespaces, which is exactly what made the old full-scan query expensive).

Every row gets a unique `observed_at` within its namespace (1-second steps), so two
namespaces seeded from the same spec can be compared row-for-row on that key even
though their ids differ.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from core.store import vlit

_SERVICES = ["billing", "auth", "search", "ledger", "gateway", "notifier", "catalog",
             "scheduler", "ingest", "reports", "payments", "inventory", "profile",
             "checkout", "analytics", "mailer", "exporter", "indexer", "archiver", "sync"]
_ENVS = ["production", "staging", "canary", "sandbox"]
_PEOPLE = ["Aisha", "Bilal", "Carmen", "Deepak", "Elena", "Farid", "Grace", "Hassan",
           "Ines", "Jamal", "Kavya", "Liam", "Maryam", "Noah", "Omar", "Priya", "Quinn",
           "Rania", "Samir", "Tariq", "Uma", "Victor", "Wafa", "Xavier", "Yusuf", "Zara"]
_CITIES = ["Austin", "Seattle", "Denver", "Chicago", "Boston", "Dallas", "Frisco",
           "Toronto", "London", "Dubai", "Chennai", "Karachi", "Istanbul", "Madrid"]
_COMPANIES = ["Acme", "Globex", "Initech", "Umbrella", "Hooli", "Stark", "Wayne",
              "Wonka", "Tyrell", "Cyberdyne"]
_TITLES = ["staff engineer", "engineering manager", "product lead", "data scientist",
           "site reliability engineer", "principal architect", "designer", "analyst"]
_HOSTS = ["host3", "host11", "host21", "host22", "host24", "host27", "host28", "host30"]
_BLOCKERS = ["database migration", "security review", "vendor contract",
             "load test sign-off", "DNS cutover", "budget approval", "API key rotation"]
_HOBBIES = ["chess", "hiking", "calligraphy", "cycling", "pottery", "archery",
            "photography", "gardening", "swimming", "baking"]
_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]


def _unique_fact(rng: random.Random, i: int):
    """An ordinary, non-contradicting fact (the bulk of any real namespace)."""
    kind = rng.randrange(8)
    svc, env = rng.choice(_SERVICES), rng.choice(_ENVS)
    p, p2 = rng.choice(_PEOPLE), rng.choice(_PEOPLE)
    if kind == 0:
        return (f"The {svc} team holds its standup every {rng.choice(_DAYS)} at "
                f"{rng.randint(8, 11)}:{rng.choice(['00', '15', '30', '45'])} (note {i}).",
                f"{svc} team", "meets_on", None)
    if kind == 1:
        return (f"{p} reviewed pull request #{1000 + i} for the {svc} service.",
                p, "reviewed", f"PR #{1000 + i}")
    if kind == 2:
        return (f"{p} likes {rng.choice(_HOBBIES)} and practices it on weekends "
                f"(mentioned in chat {i}).", p, "likes", None)
    if kind == 3:
        return (f"Incident {i}: the {svc} {env} deployment returned HTTP 5xx for "
                f"{rng.randint(2, 50)} minutes before recovering.", f"{svc} {env}",
                "had_incident", f"incident {i}")
    if kind == 4:
        return (f"{p} and {p2} paired on the {svc} refactor ticket {i}.",
                p, "paired_with", p2)
    if kind == 5:
        return (f"Ticket {i} asks for a new report in the {svc} dashboard showing "
                f"{rng.choice(['weekly', 'monthly', 'quarterly'])} totals.",
                f"ticket {i}", "requests", None)
    if kind == 6:
        return (f"{p} visited {rng.choice(_CITIES)} for a conference in "
                f"{rng.choice(['spring', 'summer', 'autumn', 'winter'])} (trip {i}).",
                p, "visited", rng.choice(_CITIES))
    return (f"Runbook note {i}: restart the {svc} worker on {rng.choice(_HOSTS)} if the "
            f"queue depth exceeds {rng.randint(100, 9000)}.", f"{svc} worker",
            "runbook_note", None)


def _spo_chain(rng: random.Random, uid: int):
    """2-3 successive values of ONE single-valued attribute — the older ones are debt."""
    kind = rng.randrange(6)
    svc, env = rng.choice(_SERVICES), rng.choice(_ENVS)
    p = f"{rng.choice(_PEOPLE)} {uid}"
    n = rng.choice((2, 2, 3))
    out = []
    if kind == 0:
        subj = f"{svc} {env} API {uid}"
        for v in rng.sample([50, 100, 200, 400, 800, 1600], n):
            out.append((f"The {subj} rate limit is {v} requests per second.",
                        subj, "rate_limit", f"{v} requests per second"))
    elif kind == 1:
        subj = f"{svc} {env} service {uid}"
        for v in rng.sample(_HOSTS, n):
            out.append((f"The {subj} runs on {v}.", subj, "runs_on", v))
    elif kind == 2:
        for v in rng.sample(_CITIES, n):
            out.append((f"{p} lives in {v}.", p, "lives_in", v))
    elif kind == 3:
        for v in rng.sample(_COMPANIES, n):
            out.append((f"{p} works at {v}.", p, "works_at", v))
    elif kind == 4:
        subj = f"{svc} {env} release {uid}"
        for v in rng.sample(["1.4.2", "1.5.0", "2.0.1", "2.1.0", "3.0.0"], n):
            out.append((f"The {subj} is deployed at version {v}.", subj, "version", v))
    else:
        subj = f"{svc} {env} job {uid}"
        for v in rng.sample([30, 60, 90, 120, 300], n):
            out.append((f"The {subj} timeout is {v} seconds.", subj, "timeout",
                        f"{v} seconds"))
    return out


def _reversal_pair(rng: random.Random, uid: int):
    """A blocked/decided state followed by an explicit reversal of it."""
    proj = f"Project {rng.choice(_SERVICES).title()}-{uid}"
    if rng.random() < 0.5:
        b = rng.choice(_BLOCKERS)
        return [(f"{proj} is blocked by the {b}.", proj, "is_blocked_by", b),
                (f"{proj} is no longer blocked by the {b}.", proj, "", "")]
    tool = rng.choice(["Kafka", "Redis", "RabbitMQ", "Postgres", "Mongo"])
    return [(f"{proj} will use {tool} for its event queue.", proj, "plans_to_use", tool),
            (f"{proj} will no longer use {tool} for its event queue.", proj, "", "")]


def build_spec(n: int, seed: int = 155) -> list[tuple]:
    """~n facts, oldest-first: (statement, subject, predicate, object). Roughly 70%
    unrelated facts, 15% SPO chains, 7% reversal pairs, 8% restatement duplicates — the
    debt is interleaved in time with the unrelated facts, as it is in a real namespace.
    ~15% of ordinary facts and ~30% of reversal statements carry NO subject (as LLM
    extraction often produces) — those anchors cannot use the subject index, so their
    dedupe lookup is the one that exercises sem_hnsw."""
    rng = random.Random(seed)
    # subject-less facts come from a SEPARATE rng so the statements (and therefore the
    # embedding cache) are identical whatever the null-subject mix is
    nrng = random.Random(seed + 1)
    groups: list[list[tuple]] = []
    total, uid = 0, 0
    while total < n:
        uid += 1
        r = rng.random()
        if r < 0.70:
            g = [_unique_fact(rng, uid)]
        elif r < 0.85:
            g = _spo_chain(rng, uid)
        elif r < 0.92:
            g = _reversal_pair(rng, uid)
            if nrng.random() < 0.3:                # extraction often emits reversals with
                s0, _, p0, o0 = g[1]               # no usable subject/predicate at all
                g = [g[0], (s0, None, p0, o0)]
        else:
            base = _unique_fact(rng, uid)
            g = [base, base]                       # verbatim restatement, stored twice
        if r < 0.70 or r >= 0.92:
            if nrng.random() < 0.15:               # ~15% of ordinary facts carry no subject
                g = [(s0, None, p0, o0) for s0, _, p0, o0 in g]
        groups.append(g)
        total += len(g)
    # interleave: each group's members keep their relative order, spread across time
    slots: list[tuple[float, int, tuple]] = []
    for g in groups:
        start = rng.random()
        for j, fact in enumerate(g):
            slots.append((start + j * rng.uniform(0.001, 0.05), len(slots), fact))
    slots.sort()
    return [f for _, _, f in slots][:n]


def embed_texts(texts: list[str]) -> dict[str, list[float]]:
    """Real local embeddings — the SAME model memnos' local mode uses
    (core.local_models.EMBED_MODEL, bge-small 384-d), but with all CPU threads: thread
    count changes throughput only, never the vectors."""
    import os
    from fastembed import TextEmbedding
    from core.local_models import EMBED_MODEL
    model = TextEmbedding(model_name=EMBED_MODEL, threads=os.cpu_count())
    uniq = sorted(set(texts))
    vecs = model.embed(uniq, batch_size=256)
    return {t: v.tolist() for t, v in zip(uniq, vecs)}


def seed_namespace(conn, schema: str, ns: str, spec: list[tuple], emb: dict,
                   vtype: str = "halfvec", t0: datetime | None = None,
                   tile: int = 1) -> None:
    """Bulk-insert `spec` into `ns` as LIVE facts (the pre-fix write path stored them
    without closing anything). observed_at = valid_from = t0 + i seconds (unique).

    tile=4 stores each 384-d vector repeated 4x as a 1536-d vector — production's
    (OpenAI text-embedding-3-small) width and on-disk size — while leaving every cosine
    distance EXACTLY unchanged (concatenating a vector with itself scales the dot
    product and both norms by the same factor), so the data stays semantically real."""
    t0 = t0 or datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i, (stmt, subj, pred, obj) in enumerate(spec):
        ts = t0 + timedelta(seconds=i)
        rows.append((ns, stmt, subj, pred, obj, ts, ts, vlit(list(emb[stmt]) * tile)))
    with conn.cursor() as c:
        c.executemany(
            f"INSERT INTO {schema}.semantic(namespace,kind,statement,subject_entity,"
            f"predicate,object,valid_from,observed_at,salience,embedding) "
            f"VALUES(%s,'fact',%s,%s,%s,%s,%s,%s,0.5,%s::{vtype})", rows)


def snapshot(conn, schema: str, ns: str) -> dict:
    """Outcome of a reconcile run, keyed by the unique observed_at so two namespaces
    seeded from one spec compare row-for-row: {obs: (closed, expired, superseded_by's
    obs, restatements, has_exact_twin)}. has_exact_twin = another row in the namespace
    has the IDENTICAL embedding (distance 0) — the only way two dedupe candidates can
    tie exactly on distance."""
    with conn.cursor() as c:
        c.execute(
            f"SELECT s.observed_at AS k, s.valid_to IS NOT NULL AS closed, "
            f"s.expired_at IS NOT NULL AS expired, b.observed_at AS by_k, s.restatements, "
            f"count(*) OVER (PARTITION BY s.statement) > 1 AS twin "
            f"FROM {schema}.semantic s LEFT JOIN {schema}.semantic b ON b.id=s.superseded_by "
            f"WHERE s.namespace=%s", (ns,))
        return {r["k"]: (r["closed"], r["expired"], r["by_k"], r["restatements"], r["twin"])
                for r in c.fetchall()}


def compare(old: dict, new: dict) -> dict:
    """decision_diffs: rows whose close / expire / superseded_by outcome differs.
    restatement_diffs: rows whose outcome is identical but whose restatements counter
    differs; `untied` counts those NOT explained by an exact-distance tie (a row with an
    identical twin — which of two identical older copies absorbs a restatement is
    arbitrary in the old LIMIT-1 SQL; the new code deterministically picks the oldest)."""
    dec = [k for k in old if old[k][:3] != new.get(k, (None,) * 5)[:3]]
    rst = [k for k in old if k not in dec and old[k][3] != new[k][3]]
    untied = [k for k in rst if not old[k][4]]
    # direction: did the new code leave live a fact the old closed/expired (did LESS —
    # the conservative direction), or close/expire one the old left live (did MORE)?
    less = [k for k in dec if (old[k][0] or old[k][1]) and not (new[k][0] or new[k][1])]
    more = [k for k in dec if (new[k][0] or new[k][1]) and not (old[k][0] or old[k][1])]
    return {"rows": len(old), "decision_diffs": dec, "restatement_diffs": rst,
            "untied_restatement_diffs": untied, "new_did_less": less, "new_did_more": more}
