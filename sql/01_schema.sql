-- POC-2: schema-per-tenant template (no AGE — graph is typed relational tables).
-- create_tenant_schema('acme') provisions an isolated tenant: schema tenant_acme
-- with memory + bi-temporal fact + entity + relation + mentions, and the indexes
-- that power hybrid retrieval (HNSW vector + GIN full-text + FK 1-hop).
--
-- Embedding dim = 1536 (text-embedding-3-small), stored as halfvec (½ the RAM).

CREATE EXTENSION IF NOT EXISTS vector;

CREATE OR REPLACE FUNCTION create_tenant_schema(tenant text, dim int DEFAULT 1536)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE s text := format('tenant_%s', tenant);
BEGIN
  EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', s);

  -- episodes (raw verbatim turns — provenance / source of truth) -----------
  EXECUTE format($f$
    CREATE TABLE IF NOT EXISTS %I.episode (
      id          bigserial PRIMARY KEY,
      namespace   text NOT NULL,
      role        text NOT NULL DEFAULT 'user',   -- user | assistant | file
      content     text NOT NULL,                   -- RAW text, stored once, verbatim
      created_at  timestamptz NOT NULL DEFAULT now()
    )$f$, s);

  -- entities (graph nodes) ------------------------------------------------
  EXECUTE format($f$
    CREATE TABLE IF NOT EXISTS %I.entity (
      id           bigserial PRIMARY KEY,
      namespace    text NOT NULL,
      name         text NOT NULL,
      entity_type  text NOT NULL DEFAULT 'CONCEPT',
      created_at   timestamptz NOT NULL DEFAULT now(),
      superseded_at timestamptz,
      UNIQUE (namespace, name)
    )$f$, s);

  -- memories (the primary unit) -------------------------------------------
  EXECUTE format($f$
    CREATE TABLE IF NOT EXISTS %1$I.memory (
      id           bigserial PRIMARY KEY,
      namespace    text NOT NULL,
      content      text NOT NULL,
      embedding    halfvec(%2$s),
      fts          tsvector GENERATED ALWAYS AS (to_tsvector('english', coalesce(content,''))) STORED,
      source       text NOT NULL DEFAULT 'api',
      author       text NOT NULL DEFAULT '',
      memory_type  text NOT NULL DEFAULT 'fact',
      user_id      text NOT NULL DEFAULT '',
      agent_id     text NOT NULL DEFAULT '',
      session_id   text NOT NULL DEFAULT '',
      created_at   timestamptz NOT NULL DEFAULT now(),
      superseded_at timestamptz,
      confidence   real NOT NULL DEFAULT 1.0,
      source_episode_id bigint              -- provenance: which raw episode this came from
    )$f$, s, dim);

  -- bi-temporal facts (P1) ------------------------------------------------
  EXECUTE format($f$
    CREATE TABLE IF NOT EXISTS %I.fact (
      id            bigserial PRIMARY KEY,
      namespace     text NOT NULL,
      subject       text NOT NULL,
      predicate     text NOT NULL,
      object        text NOT NULL,
      valid_at      timestamptz,           -- EVENT axis: true-from
      invalid_at    timestamptz,           -- EVENT axis: true-until
      created_at    timestamptz NOT NULL DEFAULT now(),   -- SYSTEM axis
      superseded_at timestamptz,                          -- SYSTEM axis (expired)
      invalidated_by bigint,
      source_memory_id bigint,
      source_episode_id bigint            -- provenance back to the raw turn
    )$f$, s);

  -- relations (graph edges, typed) ----------------------------------------
  EXECUTE format($f$
    CREATE TABLE IF NOT EXISTS %I.relation (
      id          bigserial PRIMARY KEY,
      namespace   text NOT NULL,
      src_entity  bigint NOT NULL REFERENCES %I.entity(id),
      dst_entity  bigint NOT NULL REFERENCES %I.entity(id),
      rel_type    text NOT NULL,
      created_at  timestamptz NOT NULL DEFAULT now(),
      superseded_at timestamptz
    )$f$, s, s, s);

  -- mentions (memory ↔ entity link, the 1-hop bridge) ---------------------
  EXECUTE format($f$
    CREATE TABLE IF NOT EXISTS %I.mentions (
      memory_id  bigint NOT NULL REFERENCES %I.memory(id) ON DELETE CASCADE,
      entity_id  bigint NOT NULL REFERENCES %I.entity(id) ON DELETE CASCADE,
      PRIMARY KEY (memory_id, entity_id)
    )$f$, s, s, s);

  -- indexes: HNSW vector + GIN full-text + namespace + FK 1-hop -----------
  EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.memory USING hnsw (embedding halfvec_cosine_ops)', s||'_mem_hnsw', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.memory USING gin (fts)', s||'_mem_gin', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.memory (namespace)', s||'_mem_ns', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.mentions (entity_id)', s||'_men_ent', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.fact (namespace, subject)', s||'_fact_subj', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.relation (src_entity)', s||'_rel_src', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.episode (namespace)', s||'_ep_ns', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS %I ON %I.memory (source_episode_id)', s||'_mem_ep', s);
END $$;
