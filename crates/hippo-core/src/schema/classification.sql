-- Coalescing classification work and owned topic results. Original node fields stay unchanged.
CREATE TABLE IF NOT EXISTS knowledge_node_classifications (
    node_id INTEGER PRIMARY KEY REFERENCES knowledge_nodes (id) ON DELETE CASCADE,
    node_uuid TEXT NOT NULL,
    requested_revision INTEGER NOT NULL CHECK (requested_revision > 0),
    desired_input_hash TEXT NOT NULL,
    recipe_hash TEXT NOT NULL,
    taxonomy_version TEXT NOT NULL,
    model_id TEXT NOT NULL,
    prompt_hash TEXT NOT NULL,
    threshold_hash TEXT NOT NULL,
    input_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'processing', 'ready', 'failed', 'skipped')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at INTEGER NOT NULL DEFAULT 0,
    lease_token TEXT,
    lease_expires_at INTEGER,
    applied_revision INTEGER,
    applied_input_hash TEXT,
    applied_recipe_hash TEXT,
    accepted_topics_json TEXT NOT NULL DEFAULT '[]'
    CHECK (json_valid(accepted_topics_json) AND json_type(accepted_topics_json) = 'array'),
    probabilities_json TEXT NOT NULL DEFAULT '{}'
    CHECK (json_valid(probabilities_json) AND json_type(probabilities_json) = 'object'),
    returned_model_id TEXT,
    error TEXT,
    enqueued_at INTEGER NOT NULL,
    started_at INTEGER,
    applied_at INTEGER,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_classification_claim
ON knowledge_node_classifications (status, next_attempt_at, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_classification_recipe
ON knowledge_node_classifications (recipe_hash, status);
