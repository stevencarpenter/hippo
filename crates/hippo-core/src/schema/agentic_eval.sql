-- v28 (agent evaluation, Phase 1). Idempotent; shared by the fresh schema and
-- the v27→v28 migration. The agentic_sessions columns are added separately
-- (inline in schema.sql, `add_column_if_missing` in the migration).
CREATE INDEX IF NOT EXISTS idx_agentic_sessions_agent ON agentic_sessions(agent) WHERE agent <> '';
CREATE INDEX IF NOT EXISTS idx_agentic_sessions_model ON agentic_sessions(model) WHERE model <> '';
-- One row per session (not per segment): totals a harness reports for the
-- whole session.
CREATE TABLE IF NOT EXISTS agentic_session_facts (
    session_id     TEXT    NOT NULL,
    harness        TEXT    NOT NULL,
    cost_usd       REAL,
    duration_ms    INTEGER,
    tool_duration_ms INTEGER,
    lines_added    INTEGER,
    lines_removed  INTEGER,
    title          TEXT,
    cli_version    TEXT,
    pr_links_json  TEXT    NOT NULL DEFAULT '[]',
    updated_at     INTEGER NOT NULL,
    PRIMARY KEY (session_id, harness)
);
