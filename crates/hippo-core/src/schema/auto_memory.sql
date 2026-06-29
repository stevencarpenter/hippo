-- Claude Code auto-memory tables (schema v19). Single source of truth for
-- migration DDL and fresh-install schema assembly in storage.rs.

CREATE TABLE IF NOT EXISTS memory_documents (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL DEFAULT 'claude-auto-memory' CHECK (source_kind = 'claude-auto-memory'),
    repository TEXT NOT NULL,
    logical_path TEXT NOT NULL,
    source_path TEXT NOT NULL,
    current_revision_id INTEGER REFERENCES memory_revisions(id) ON DELETE SET NULL,
    active_revision_id INTEGER REFERENCES memory_revisions(id) ON DELETE SET NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'tombstoned', 'unavailable')),
    projection_status TEXT NOT NULL DEFAULT 'pending' CHECK (projection_status IN ('pending', 'processing', 'ready', 'failed', 'stale')),
    last_error TEXT,
    observed_at INTEGER NOT NULL,
    tombstoned_at INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (source_kind, repository, logical_path)
) STRICT;

CREATE TABLE IF NOT EXISTS memory_revisions (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES memory_documents(id) ON DELETE CASCADE,
    revision_number INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    source_hash TEXT NOT NULL,
    redacted_content TEXT,
    source_mtime_ms INTEGER NOT NULL,
    source_size INTEGER NOT NULL,
    change_kind TEXT NOT NULL DEFAULT 'create' CHECK (change_kind IN ('create', 'update', 'rename', 'delete')),
    summary TEXT,
    diff_text TEXT,
    chunker_name TEXT NOT NULL,
    chunker_version INTEGER NOT NULL DEFAULT 1,
    chunker_config_json TEXT NOT NULL DEFAULT '{}',
    enrichment_model TEXT,
    enrichment_version INTEGER NOT NULL DEFAULT 1,
    enriched_at INTEGER,
    created_at INTEGER NOT NULL,
    UNIQUE (document_id, revision_number)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_memory_revisions_document_created ON memory_revisions(document_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memory_revisions_hash ON memory_revisions(document_id, content_hash);

CREATE TABLE IF NOT EXISTS memory_chunks (
    id INTEGER PRIMARY KEY,
    revision_id INTEGER NOT NULL REFERENCES memory_revisions(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    heading_path TEXT NOT NULL DEFAULT '',
    start_offset INTEGER NOT NULL DEFAULT 0,
    end_offset INTEGER NOT NULL DEFAULT 0,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    token_count INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    UNIQUE (revision_id, ordinal)
) STRICT;

CREATE TABLE IF NOT EXISTS memory_enrichment_queue (
    id INTEGER PRIMARY KEY,
    revision_id INTEGER NOT NULL UNIQUE REFERENCES memory_revisions(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'processing', 'done', 'failed', 'skipped')),
    priority INTEGER NOT NULL DEFAULT 5,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 5,
    error_message TEXT,
    locked_at INTEGER,
    locked_by TEXT,
    enqueued_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
) STRICT;
CREATE INDEX IF NOT EXISTS idx_memory_queue_pending ON memory_enrichment_queue(status, priority, enqueued_at) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS knowledge_node_memory_chunks (
    knowledge_node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id) ON DELETE CASCADE,
    memory_chunk_id INTEGER NOT NULL REFERENCES memory_chunks(id) ON DELETE CASCADE,
    PRIMARY KEY (knowledge_node_id, memory_chunk_id)
) STRICT;
