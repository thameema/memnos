-- memnos brain-inspired memory — tenant schema (B1)
-- Dual long-term store (episodic + semantic) + associative graph + provenance.
-- One ACID engine. halfvec embeddings on pgvector >= 0.7; falls back to full-precision
-- `vector` on pgvector 0.6 (what Debian/Ubuntu apt ships) so a clean apt install works
-- with no source build. The embedding column type + HNSW ops class are passed in by the
-- caller (vtype/vops), which feature-detects the installed pgvector. halfvec is purely a
-- storage optimization; queries are identical on either type.
CREATE EXTENSION IF NOT EXISTS vector;

CREATE OR REPLACE FUNCTION create_brain_schema(tenant text, dim int DEFAULT 1536,
                                               vtype text DEFAULT 'halfvec',
                                               vops  text DEFAULT 'halfvec_cosine_ops')
RETURNS void LANGUAGE plpgsql AS $fn$
DECLARE s text := 'tenant_' || tenant;
        coltype text := vtype || '(' || dim || ')';
BEGIN
  IF vtype NOT IN ('halfvec', 'vector') THEN
    RAISE EXCEPTION 'unsupported embedding vector type: %', vtype;
  END IF;
  IF vops NOT IN ('halfvec_cosine_ops', 'vector_cosine_ops') THEN
    RAISE EXCEPTION 'unsupported vector ops class: %', vops;
  END IF;
  EXECUTE format('CREATE SCHEMA IF NOT EXISTS %I', s);

  -- SENSORY / verbatim log — provenance floor, append-only
  EXECUTE format($t$
    CREATE TABLE IF NOT EXISTS %I.raw_turns(
      id bigserial PRIMARY KEY,
      namespace text NOT NULL,
      session_id text,
      speaker text,
      text text NOT NULL,
      observed_at timestamptz NOT NULL DEFAULT now(),
      embedding %s,
      fts tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
    )$t$, s, coltype);

  -- EPISODIC — hippocampus: sharp, recent, verbatim, event-segmented
  EXECUTE format($t$
    CREATE TABLE IF NOT EXISTS %I.episodic(
      id bigserial PRIMARY KEY,
      namespace text NOT NULL,
      session_id text,
      text text NOT NULL,
      summary text,
      t_start timestamptz,
      t_end timestamptz,
      observed_at timestamptz NOT NULL DEFAULT now(),
      salience real NOT NULL DEFAULT 0,
      last_access timestamptz,
      access_count int NOT NULL DEFAULT 0,
      consolidated boolean NOT NULL DEFAULT false,
      source_turn_ids bigint[],
      embedding %s,
      fts tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED
    )$t$, s, coltype);

  -- SEMANTIC — neocortex: durable, deduped, multi-hop pre-joined, FULLY BI-TEMPORAL.
  --   valid time   (application/event): valid_from .. valid_to  (when the fact is TRUE)
  --   system time  (transaction/audit): created_at, expired_at (when we KNEW / removed it)
  -- Supersession sets valid_to (belief changed); corrections set expired_at (system-removed);
  -- nothing is ever hard-deleted → full history + "as-of" queries on either axis.
  EXECUTE format($t$
    CREATE TABLE IF NOT EXISTS %I.semantic(
      id bigserial PRIMARY KEY,
      namespace text NOT NULL,
      kind text NOT NULL,                 -- proposition | dossier | summary
      statement text NOT NULL,
      subject_entity text,
      predicate text,
      object text,
      valid_from timestamptz,             -- valid_at: when the fact became true   (valid/app time)
      valid_to timestamptz,               -- invalid_at: NULL = still true (supersession sets this)
      created_at timestamptz NOT NULL DEFAULT now(),   -- system time: when first recorded (immutable)
      expired_at timestamptz,             -- system time: NULL = live; set on correction (never deleted)
      observed_at timestamptz NOT NULL DEFAULT now(),  -- legacy/event-observed (kept)
      confidence real DEFAULT 1.0,
      salience real NOT NULL DEFAULT 0,
      embedding %s,
      fts tsvector GENERATED ALWAYS AS (to_tsvector('english', statement)) STORED
    )$t$, s, coltype);
  -- PROVENANCE (inline): the raw_turn(s) a fact was extracted from / a dossier derived
  -- from. Auditable evidence chain — "why do you believe this?" (additive, rolling-safe).
  EXECUTE format('ALTER TABLE %I.semantic ADD COLUMN IF NOT EXISTS source_turn_ids bigint[]', s);

  -- AUTHOR ATTRIBUTION (0.1.6): name of the AUTHENTICATED principal that wrote the row.
  -- Stamped server-side from the bearer token — never read from a request body, so it
  -- is non-spoofable. NULL on legacy rows / direct-DB writes. (additive, rolling-safe)
  EXECUTE format('ALTER TABLE %I.raw_turns ADD COLUMN IF NOT EXISTS author_principal text', s);
  EXECUTE format('ALTER TABLE %I.episodic  ADD COLUMN IF NOT EXISTS author_principal text', s);
  EXECUTE format('ALTER TABLE %I.semantic  ADD COLUMN IF NOT EXISTS author_principal text', s);

  -- SUPERSESSION LINKAGE + RESTATEMENTS (write-path supersession fixes; additive,
  -- rolling-safe): superseded_by = the id of the fact that closed this one out (set by
  -- SPO supersession and the reversal/negation close-out — auditable belief-change
  -- chain). restatements = how many times a near-duplicate of this fact was re-asserted
  -- and collapsed into it instead of inserted (write-path dedupe; density signal).
  EXECUTE format('ALTER TABLE %I.semantic ADD COLUMN IF NOT EXISTS superseded_by bigint', s);
  EXECUTE format('ALTER TABLE %I.semantic ADD COLUMN IF NOT EXISTS restatements int NOT NULL DEFAULT 0', s);

  -- TYPED MEMORIES (0.1.6): optional classification of a memory —
  -- decision | incident | constraint | skill | fact. NULL = untyped (legacy/plain).
  -- Facts extracted from a typed turn INHERIT the turn's type. type='constraint'
  -- memories are PINNED into every /recall on their namespace (additive, rolling-safe).
  EXECUTE format('ALTER TABLE %I.raw_turns ADD COLUMN IF NOT EXISTS memory_type text', s);
  EXECUTE format('ALTER TABLE %I.semantic  ADD COLUMN IF NOT EXISTS memory_type text', s);
  -- Episodes INHERIT a type only when ALL their source turns share one non-null type
  -- (unanimous — conservative; mixed/partly-typed groups stay NULL).
  EXECUTE format('ALTER TABLE %I.episodic  ADD COLUMN IF NOT EXISTS memory_type text', s);

  -- ASSOCIATIVE GRAPH
  EXECUTE format($t$
    CREATE TABLE IF NOT EXISTS %I.entities(
      id bigserial PRIMARY KEY,
      namespace text NOT NULL,
      name text NOT NULL,
      embedding %s,
      UNIQUE(namespace, name)
    )$t$, s, coltype);

  EXECUTE format($t$
    CREATE TABLE IF NOT EXISTS %I.mentions(
      entity_id bigint NOT NULL,
      memory_id bigint NOT NULL,
      memory_kind text NOT NULL,          -- episodic | semantic
      UNIQUE(entity_id, memory_id, memory_kind)
    )$t$, s);

  EXECUTE format($t$
    CREATE TABLE IF NOT EXISTS %I.edges(
      id bigserial PRIMARY KEY,
      namespace text NOT NULL,
      src_entity bigint NOT NULL,
      dst_entity bigint NOT NULL,
      weight real DEFAULT 1.0,
      UNIQUE(namespace, src_entity, dst_entity)
    )$t$, s);

  -- PROVENANCE — every semantic fact links to its episodic evidence (auditable;
  -- consolidation can hallucinate, so this is non-negotiable)
  EXECUTE format($t$
    CREATE TABLE IF NOT EXISTS %I.provenance(
      semantic_id bigint NOT NULL,
      episodic_id bigint NOT NULL,
      UNIQUE(semantic_id, episodic_id)
    )$t$, s);

  -- INDEXES (HNSW vector + GIN fts + temporal + graph)
  EXECUTE format('CREATE INDEX IF NOT EXISTS raw_hnsw ON %I.raw_turns USING hnsw (embedding %s)', s, vops);
  -- raw_turns BM25 arm: episodic/semantic always had GIN fts indexes but raw_turns did
  -- not, so every /recall row-filtered the whole namespace (linear with data size)
  EXECUTE format('CREATE INDEX IF NOT EXISTS raw_fts  ON %I.raw_turns USING gin (fts)', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS epi_hnsw ON %I.episodic USING hnsw (embedding %s)', s, vops);
  EXECUTE format('CREATE INDEX IF NOT EXISTS epi_fts  ON %I.episodic USING gin (fts)', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS epi_time ON %I.episodic (observed_at)', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS sem_hnsw ON %I.semantic USING hnsw (embedding %s)', s, vops);
  EXECUTE format('CREATE INDEX IF NOT EXISTS sem_fts  ON %I.semantic USING gin (fts)', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS sem_valid ON %I.semantic (valid_from, valid_to)', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS sem_live ON %I.semantic (valid_from) WHERE expired_at IS NULL AND valid_to IS NULL', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS ent_hnsw ON %I.entities USING hnsw (embedding %s)', s, vops);
  EXECUTE format('CREATE INDEX IF NOT EXISTS men_ent  ON %I.mentions (entity_id)', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS men_mem  ON %I.mentions (memory_id)', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS prov_sem ON %I.provenance (semantic_id)', s);
  -- namespace b-trees: every recall/list/stat filters or groups by namespace; without
  -- these, namespace queries seq-scan embedding-heavy tables (field: 2-min admin loads
  -- + 4-8s blocked writes at 35k turns / 95k facts)
  EXECUTE format('CREATE INDEX IF NOT EXISTS raw_ns ON %I.raw_turns (namespace)', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS epi_ns ON %I.episodic (namespace)', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS sem_ns ON %I.semantic (namespace)', s);
  -- SUPERSESSION indexes (issue #8): every single-valued fact write runs
  -- supersede_predicate (lower(subject)+lower(predicate) over live rows) and
  -- consolidation runs supersede_subject/_similar (exact subject over live rows).
  -- Without these each call seq-scans the whole embedding-heavy namespace heap
  -- (measured: 95K rows filtered + ~195K buffers touched PER FACT WRITE). Partial
  -- btrees over live rows make both an index scan; UPDATE semantics are unchanged.
  EXECUTE format('CREATE INDEX IF NOT EXISTS sem_supersede_pred ON %I.semantic '
                 '(namespace, lower(subject_entity), lower(predicate)) '
                 'WHERE valid_to IS NULL AND expired_at IS NULL', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS sem_supersede_subj ON %I.semantic '
                 '(namespace, subject_entity) '
                 'WHERE valid_to IS NULL AND expired_at IS NULL', s);
  -- TYPED-MEMORY indexes: pinned-constraint fetch + type filters scan by
  -- (namespace, memory_type); partial — typed rows are a small minority.
  EXECUTE format('CREATE INDEX IF NOT EXISTS raw_mtype ON %I.raw_turns '
                 '(namespace, memory_type) WHERE memory_type IS NOT NULL', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS sem_mtype ON %I.semantic '
                 '(namespace, memory_type) WHERE memory_type IS NOT NULL', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS epi_mtype ON %I.episodic '
                 '(namespace, memory_type) WHERE memory_type IS NOT NULL', s);
  -- STALE-TURN lookup (issue #10 residual B): recall checks, for the retrieved turn ids
  -- only, whether ALL semantic facts derived from a turn are superseded
  -- (semantic.source_turn_ids @> ARRAY[turn_id]). GIN makes that containment probe an
  -- index scan; additive, rolling-safe.
  EXECUTE format('CREATE INDEX IF NOT EXISTS sem_src_turns ON %I.semantic '
                 'USING gin (source_turn_ids)', s);
END
$fn$;
