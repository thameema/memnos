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

  -- CONSTRAINT SUBJECT + RETIREMENT (issues #83/#84, epic #70 items 2 + 5): an
  -- OPTIONAL, AUTHOR-SUPPLIED grouping key for memory_type='constraint' rows — NEVER
  -- LLM-inferred (issue #29: extraction is fully bypassed for constraints because it
  -- can paraphrase or misattribute a guardrail's meaning; deriving a subject/predicate
  -- via the same extraction path would carry the identical risk, just for the
  -- MATCHING key instead of the stored text — so constraint_subject is a value the
  -- caller declares, same trust model as the constraint text itself). Two constraints
  -- only ever compete for precedence (#83, cross-namespace) or supersession (#84,
  -- same-namespace) when they share BOTH memory_type='constraint' AND a non-null,
  -- equal constraint_subject. Untagged (NULL) constraints — every constraint written
  -- before this change, and every one whose author doesn't tag it — never group and
  -- are therefore never auto-suppressed or auto-retired: a conservative default that
  -- leaves existing behavior for every untagged write exactly as it was.
  -- Stored NORMALIZED (stripped + lowercased by MemnosMemory.remember_turn before it
  -- ever reaches insert_raw_turn) — the same case-insensitive-identity convention this
  -- schema already uses for subject_entity/predicate (see sem_supersede_pred's
  -- lower(...) below), so 'Deploy-Policy' and 'deploy-policy' are the SAME group.
  EXECUTE format('ALTER TABLE %I.raw_turns ADD COLUMN IF NOT EXISTS constraint_subject text', s);
  EXECUTE format('ALTER TABLE %I.semantic  ADD COLUMN IF NOT EXISTS constraint_subject text', s);
  EXECUTE format('ALTER TABLE %I.episodic  ADD COLUMN IF NOT EXISTS constraint_subject text', s);
  -- RETIREMENT (issue #84): stamped by BrainStore.retire_constraints() when a NEWER
  -- constraint with the same (namespace, constraint_subject) is written. NULL = live.
  -- constraint_retired_by = "{kind}:{id}" of the successor row (kind disambiguates —
  -- fact/turn/episode ids are independent bigserial sequences, so a bare id could
  -- collide across kinds; same convention epic #70 item 3's per-injection audit uses).
  -- pinned_constraints() excludes retired rows outright, so a retired constraint stops
  -- injecting immediately, no separate expiry sweep needed.
  --
  -- Deliberately a SEPARATE mechanism from valid_to/expired_at just above: those drive
  -- belief-change supersession (supersede_predicate/dominant_live_fact) keyed on
  -- subject_entity/predicate, which constraint rows never populate (issue #29 again —
  -- that's the extraction-derived structure the bypass exists to avoid). Routing
  -- constraints through that pipeline would require either inferring subject_entity/
  -- predicate for them (the #29 risk) or giving raw_turns bi-temporal columns it has
  -- never had. This is issue #84's option B: a parallel, constraint-scoped path that
  -- extends the #29 bypass rather than reversing it — extract_facts() still returns
  -- [] for memory_type='constraint', the verbatim text is still stored unmodified in
  -- raw_turns; retirement adds metadata ALONGSIDE that row, never re-reads or
  -- rewrites its text.
  EXECUTE format('ALTER TABLE %I.raw_turns ADD COLUMN IF NOT EXISTS constraint_retired_at timestamptz', s);
  EXECUTE format('ALTER TABLE %I.semantic  ADD COLUMN IF NOT EXISTS constraint_retired_at timestamptz', s);
  EXECUTE format('ALTER TABLE %I.episodic  ADD COLUMN IF NOT EXISTS constraint_retired_at timestamptz', s);
  EXECUTE format('ALTER TABLE %I.raw_turns ADD COLUMN IF NOT EXISTS constraint_retired_by text', s);
  EXECUTE format('ALTER TABLE %I.semantic  ADD COLUMN IF NOT EXISTS constraint_retired_by text', s);
  EXECUTE format('ALTER TABLE %I.episodic  ADD COLUMN IF NOT EXISTS constraint_retired_by text', s);

  -- VERSION SCOPING (issue #106): an OPTIONAL dotted-numeric version WINDOW
  -- (constraint_since <= version < constraint_until) a corpus_ingest()-extracted
  -- constraint is in force for. NULL constraint_since = always applied from the
  -- beginning; NULL constraint_until = never expires. Set uniformly on every
  -- constraint extracted from one corpus_ingest() call (the `since`/`until` args are
  -- doc-level, not per-sentence). Deliberately a THIRD, independent expiry axis on
  -- this table, distinct from both `expired_at` (system-time correction/removal,
  -- above) and `constraint_retired_at` (issue #84 same-namespace supersession,
  -- above) — a constraint can be system-live and un-retired yet still be
  -- version-expired (`status: "expired"` in corpus_check's output), which is the
  -- whole point of this feature: stale architecture rules stop being enforced on a
  -- newer release branch without being deleted or supersession-retired. Read/parsed
  -- by BrainStore.corpus_check() (core/store.py); NEVER written outside
  -- ingest_constraints() (author-supplied, like constraint_subject — see the
  -- CONSTRAINT SUBJECT comment above for why LLM-inferred values are avoided here).
  EXECUTE format('ALTER TABLE %I.semantic ADD COLUMN IF NOT EXISTS constraint_since text', s);
  EXECUTE format('ALTER TABLE %I.semantic ADD COLUMN IF NOT EXISTS constraint_until text', s);

  -- INFERENTIAL MEMORY (issue #24): LLM-derived conclusions from patterns across stated
  -- facts, written with kind='inferred' + memory_type='inferred' (a distinct kind, so
  -- they never get swept into the kind='fact' reversal/negation-close-out queries that
  -- are tuned for stated facts only). inference_confidence/basis are the human-readable
  -- level + one-line justification; source_fact_ids = the semantic.id values of the
  -- stated facts that support the inference (separate from source_turn_ids, which points
  -- at raw verbatim turns).
  EXECUTE format('ALTER TABLE %I.semantic ADD COLUMN IF NOT EXISTS inference_confidence text', s);
  EXECUTE format('ALTER TABLE %I.semantic ADD COLUMN IF NOT EXISTS inference_basis text', s);
  EXECUTE format('ALTER TABLE %I.semantic ADD COLUMN IF NOT EXISTS source_fact_ids bigint[]', s);

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
  -- CONSTRAINT SUBJECT lookup (issues #83/#84): retire_constraints() UPDATEs, and
  -- pinned_constraints() reads, LIVE constraint rows by (namespace, constraint_subject)
  -- — partial on the same "typed rows are a minority" + "live only" logic as the
  -- indexes just above and sem_supersede_pred's live-rows pattern.
  EXECUTE format('CREATE INDEX IF NOT EXISTS raw_cons_subj ON %I.raw_turns '
                 '(namespace, constraint_subject) '
                 'WHERE memory_type=''constraint'' AND constraint_retired_at IS NULL', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS sem_cons_subj ON %I.semantic '
                 '(namespace, constraint_subject) '
                 'WHERE memory_type=''constraint'' AND constraint_retired_at IS NULL', s);
  EXECUTE format('CREATE INDEX IF NOT EXISTS epi_cons_subj ON %I.episodic '
                 '(namespace, constraint_subject) '
                 'WHERE memory_type=''constraint'' AND constraint_retired_at IS NULL', s);
  -- STALE-TURN lookup (issue #10 residual B): recall checks, for the retrieved turn ids
  -- only, whether ALL semantic facts derived from a turn are superseded
  -- (semantic.source_turn_ids @> ARRAY[turn_id]). GIN makes that containment probe an
  -- index scan; additive, rolling-safe.
  EXECUTE format('CREATE INDEX IF NOT EXISTS sem_src_turns ON %I.semantic '
                 'USING gin (source_turn_ids)', s);
  -- ENTITY DOSSIERS (issue #23): one generated summary paragraph per entity,
  -- stored per-tenant so it is namespace-scoped and shares the pool/conn.
  -- Upserted on (entity_id, namespace) so a re-run replaces the prior text
  -- (idempotent). model_used records which LLM generated the text for audit.
  EXECUTE format($t$
    CREATE TABLE IF NOT EXISTS %I.entity_dossiers(
      id bigserial PRIMARY KEY,
      entity_id bigint NOT NULL,
      namespace text NOT NULL,
      dossier_text text NOT NULL,
      generated_at timestamptz NOT NULL DEFAULT now(),
      model_used text
    )$t$, s);
  EXECUTE format('CREATE UNIQUE INDEX IF NOT EXISTS entity_dossiers_entity_ns '
                 'ON %I.entity_dossiers(entity_id, namespace)', s);
END
$fn$;
