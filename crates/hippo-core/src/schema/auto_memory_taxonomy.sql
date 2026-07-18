-- Claude auto-memory taxonomy (schema v20): deterministic categories, model
-- categories, and index-to-topic links. Concatenated at migrate/install time.

CREATE TABLE IF NOT EXISTS memory_document_categories (
    id INTEGER PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES memory_documents(id) ON DELETE CASCADE,
    category TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('filename', 'model')),
    confidence REAL,
    model TEXT,
    enrichment_version INTEGER,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (document_id, category, source)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_memory_document_categories_category
    ON memory_document_categories(category);
CREATE INDEX IF NOT EXISTS idx_memory_document_categories_document
    ON memory_document_categories(document_id);

CREATE TABLE IF NOT EXISTS memory_document_links (
    id INTEGER PRIMARY KEY,
    source_document_id INTEGER NOT NULL REFERENCES memory_documents(id) ON DELETE CASCADE,
    source_revision_id INTEGER NOT NULL REFERENCES memory_revisions(id) ON DELETE CASCADE,
    target_document_id INTEGER REFERENCES memory_documents(id) ON DELETE SET NULL,
    target_logical_path TEXT NOT NULL,
    anchor_text TEXT NOT NULL DEFAULT '',
    resolution TEXT NOT NULL CHECK (
        resolution IN ('resolved', 'unresolved', 'external', 'ambiguous', 'circular')
    ),
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    UNIQUE (source_document_id, source_revision_id, target_logical_path)
) STRICT;
CREATE INDEX IF NOT EXISTS idx_memory_document_links_target
    ON memory_document_links(target_logical_path, resolution);
CREATE INDEX IF NOT EXISTS idx_memory_document_links_source
    ON memory_document_links(source_document_id);
