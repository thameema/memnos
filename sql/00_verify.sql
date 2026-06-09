-- POC-1 smoke test: prove pgvector HNSW + tsvector full-text both work via SQL
-- in a single Postgres engine (the thing ArcadeDB could not do over HTTP).

CREATE EXTENSION IF NOT EXISTS vector;

\echo '== pgvector version =='
SELECT extversion FROM pg_extension WHERE extname = 'vector';

DROP TABLE IF EXISTS _poc_probe;
CREATE TABLE _poc_probe (
    id        bigserial PRIMARY KEY,
    body      text,
    fts       tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(body, ''))) STORED,
    embedding vector(4)              -- tiny dim for the smoke test
);

INSERT INTO _poc_probe (body, embedding) VALUES
    ('the acme merger is handled by the m&a team', '[0.10, 0.20, 0.30, 0.40]'),
    ('jane moved to the m&a team in march',        '[0.11, 0.19, 0.31, 0.39]'),
    ('bob is jane''s paralegal',                    '[0.90, 0.80, 0.10, 0.05]');

\echo '== create HNSW vector index (cosine) =='
CREATE INDEX _poc_hnsw ON _poc_probe USING hnsw (embedding vector_cosine_ops);

\echo '== create GIN full-text index =='
CREATE INDEX _poc_gin ON _poc_probe USING gin (fts);

\echo '== HNSW vector search (nearest to the merger vector) =='
SELECT id, body, embedding <=> '[0.10,0.20,0.30,0.40]' AS cosine_dist
FROM _poc_probe
ORDER BY embedding <=> '[0.10,0.20,0.30,0.40]'
LIMIT 3;

\echo '== full-text search (BM25-ish via ts_rank) for "m&a team" =='
SELECT id, body, ts_rank(fts, websearch_to_tsquery('english', 'm&a team')) AS rank
FROM _poc_probe
WHERE fts @@ websearch_to_tsquery('english', 'm&a team')
ORDER BY rank DESC;

\echo '== confirm both index types exist =='
SELECT indexname, indexdef FROM pg_indexes WHERE tablename = '_poc_probe' AND indexname LIKE '_poc%';

DROP TABLE _poc_probe;
\echo '== POC-1 OK: HNSW + tsvector both work in one engine =='
