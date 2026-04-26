CREATE TABLE IF NOT EXISTS sessions
(
    id INTEGER PRIMARY KEY,
    start_time INTEGER NOT NULL,
    end_time INTEGER,
    terminal TEXT,
    shell TEXT NOT NULL,
    hostname TEXT NOT NULL,
    username TEXT NOT NULL,
    summary TEXT,
    created_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
);

CREATE TABLE IF NOT EXISTS env_snapshots
(
    id INTEGER PRIMARY KEY,
    content_hash TEXT NOT NULL UNIQUE,
    env_json TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
);

CREATE TABLE IF NOT EXISTS events
(
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions (id),
    timestamp INTEGER NOT NULL,
    command TEXT NOT NULL,
    stdout TEXT,
    stderr TEXT,
    stdout_truncated INTEGER DEFAULT 0,
    stderr_truncated INTEGER DEFAULT 0,
    exit_code INTEGER,
    duration_ms INTEGER NOT NULL,
    cwd TEXT NOT NULL,
    hostname TEXT NOT NULL,
    shell TEXT NOT NULL,
    git_repo TEXT,
    git_branch TEXT,
    git_commit TEXT,
    git_dirty INTEGER,
    env_snapshot_id INTEGER REFERENCES env_snapshots (id),
    envelope_id TEXT,
    -- source_kind groups events by their origin so queries can separate
    -- real shell activity from synthesized rows (e.g. Claude tool calls).
    -- 'shell' = native shell hook; 'claude-tool' = tool call derived from
    -- a Claude Code session. New sources (cursor, codex, etc.) take their
    -- own label rather than reusing 'shell'.
    source_kind TEXT NOT NULL DEFAULT 'shell',
    -- tool_name is set only when source_kind != 'shell'. Holds the exact
    -- tool name as the upstream producer reported it (e.g. 'Bash', 'Agent',
    -- 'mcp__github__create_pull_request'). Used by the enrichment policy
    -- to decide whether to enrich or skip a row.
    tool_name TEXT,
    enriched INTEGER NOT NULL DEFAULT 0,
    redaction_count INTEGER NOT NULL DEFAULT 0,
    archived_at INTEGER,
    -- probe_tag is set only on synthetic probe rows injected for health
    -- checking; NULL on all real events.
    probe_tag TEXT,
    created_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
);

CREATE TABLE IF NOT EXISTS entities
(
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL CHECK (type IN (
        'project', 'file', 'tool', 'service', 'repo', 'host', 'person',
        'concept', 'domain'
    )),
    name TEXT NOT NULL,
    canonical TEXT,
    metadata TEXT,
    first_seen INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    last_seen INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    created_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    UNIQUE (type, canonical)
);

CREATE TABLE IF NOT EXISTS relationships
(
    id INTEGER PRIMARY KEY,
    from_entity_id INTEGER NOT NULL REFERENCES entities (id),
    to_entity_id INTEGER NOT NULL REFERENCES entities (id),
    relationship TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    evidence_count INTEGER NOT NULL DEFAULT 1,
    first_seen INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    last_seen INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    UNIQUE (from_entity_id, to_entity_id, relationship)
);

-- NOTE: event_entities is not yet populated by the enrichment pipeline.
-- Populating it correctly requires per-event entity attribution at enrichment
-- time, which the current batch-level enrichment contract does not support.
-- Reserved for future per-event graph features. See ARCH-R12.
CREATE TABLE IF NOT EXISTS event_entities
(
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL REFERENCES events (id),
    entity_id INTEGER NOT NULL REFERENCES entities (id),
    role TEXT NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    UNIQUE (event_id, entity_id, role)
);

CREATE TABLE IF NOT EXISTS knowledge_nodes
(
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL UNIQUE,
    content TEXT NOT NULL,
    embed_text TEXT NOT NULL,
    node_type TEXT NOT NULL DEFAULT 'observation',
    outcome TEXT,
    tags TEXT,
    enrichment_model TEXT,
    enrichment_version INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    updated_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
);

CREATE TABLE IF NOT EXISTS knowledge_node_entities
(
    knowledge_node_id INTEGER NOT NULL REFERENCES knowledge_nodes (id),
    entity_id INTEGER NOT NULL REFERENCES entities (id),
    PRIMARY KEY (knowledge_node_id, entity_id)
);

CREATE TABLE IF NOT EXISTS knowledge_node_events
(
    knowledge_node_id INTEGER NOT NULL REFERENCES knowledge_nodes (id),
    event_id INTEGER NOT NULL REFERENCES events (id),
    PRIMARY KEY (knowledge_node_id, event_id)
);

CREATE TABLE IF NOT EXISTS enrichment_queue
(
    id INTEGER PRIMARY KEY,
    event_id INTEGER NOT NULL UNIQUE REFERENCES events (id),
    status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'processing', 'done', 'failed', 'skipped')),
    priority INTEGER NOT NULL DEFAULT 5,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 5,
    error_message TEXT,
    locked_at INTEGER,
    locked_by TEXT,
    created_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    updated_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
);

-- Claude Code session segments (conversations, not shell commands)
CREATE TABLE IF NOT EXISTS claude_sessions
(
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    project_dir TEXT NOT NULL,
    cwd TEXT NOT NULL,
    git_branch TEXT,
    segment_index INTEGER NOT NULL,
    start_time INTEGER NOT NULL,
    end_time INTEGER NOT NULL,
    summary_text TEXT NOT NULL,
    tool_calls_json TEXT,
    user_prompts_json TEXT,
    message_count INTEGER NOT NULL,
    token_count INTEGER,
    source_file TEXT NOT NULL,
    is_subagent INTEGER NOT NULL DEFAULT 0,
    parent_session_id TEXT,
    enriched INTEGER NOT NULL DEFAULT 0,
    -- probe_tag is set only on synthetic probe rows; NULL on all real sessions.
    probe_tag TEXT,
    created_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    UNIQUE (session_id, segment_index)
);

CREATE TABLE IF NOT EXISTS knowledge_node_claude_sessions
(
    knowledge_node_id INTEGER NOT NULL REFERENCES knowledge_nodes (id),
    claude_session_id INTEGER NOT NULL REFERENCES claude_sessions (id),
    PRIMARY KEY (knowledge_node_id, claude_session_id)
);

CREATE TABLE IF NOT EXISTS claude_enrichment_queue
(
    id INTEGER PRIMARY KEY,
    claude_session_id INTEGER NOT NULL UNIQUE REFERENCES claude_sessions (id),
    status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'processing', 'done', 'failed', 'skipped')),
    priority INTEGER NOT NULL DEFAULT 5,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 5,
    error_message TEXT,
    locked_at INTEGER,
    locked_by TEXT,
    created_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    updated_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
);

CREATE INDEX IF NOT EXISTS idx_events_session ON events (session_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp ON events (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_events_git_repo ON events (git_repo)
WHERE git_repo IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_enriched ON events (enriched)
WHERE enriched = 0;
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_envelope_id ON events (envelope_id)
WHERE envelope_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_source_kind ON events (source_kind)
WHERE source_kind != 'shell';
CREATE INDEX IF NOT EXISTS idx_entities_type_name ON entities (type, name);
CREATE INDEX IF NOT EXISTS idx_entities_canonical ON entities (canonical)
WHERE canonical IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_relationships_from ON relationships (from_entity_id, relationship);
CREATE INDEX IF NOT EXISTS idx_relationships_to ON relationships (to_entity_id, relationship);
CREATE INDEX IF NOT EXISTS idx_event_entities_entity ON event_entities (entity_id, event_id);
CREATE INDEX IF NOT EXISTS idx_kn_entities_entity ON knowledge_node_entities (entity_id);
CREATE INDEX IF NOT EXISTS idx_queue_pending ON enrichment_queue (status, priority)
WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_claude_sessions_cwd ON claude_sessions (cwd);
CREATE INDEX IF NOT EXISTS idx_claude_sessions_session ON claude_sessions (session_id);
CREATE INDEX IF NOT EXISTS idx_claude_sessions_start_time ON claude_sessions (start_time DESC);
CREATE INDEX IF NOT EXISTS idx_claude_queue_pending ON claude_enrichment_queue (status, priority)
WHERE status = 'pending';

-- Browser activity events (captured via Firefox extension)
CREATE TABLE IF NOT EXISTS browser_events
(
    id INTEGER PRIMARY KEY,
    `timestamp` INTEGER NOT NULL,
    url TEXT NOT NULL,
    title TEXT,
    `domain` TEXT NOT NULL,
    dwell_ms INTEGER NOT NULL,
    scroll_depth REAL,
    extracted_text TEXT,
    search_query TEXT,
    referrer TEXT,
    content_hash TEXT,
    envelope_id TEXT,
    enriched INTEGER NOT NULL DEFAULT 0,
    -- probe_tag is set only on synthetic probe rows; NULL on all real events.
    probe_tag TEXT,
    created_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
);

CREATE TABLE IF NOT EXISTS browser_enrichment_queue
(
    id INTEGER PRIMARY KEY,
    browser_event_id INTEGER NOT NULL UNIQUE REFERENCES browser_events (id),
    status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'processing', 'done', 'failed', 'skipped')),
    priority INTEGER NOT NULL DEFAULT 5,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 5,
    error_message TEXT,
    locked_at INTEGER,
    locked_by TEXT,
    created_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    updated_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
);

CREATE TABLE IF NOT EXISTS knowledge_node_browser_events
(
    knowledge_node_id INTEGER NOT NULL REFERENCES knowledge_nodes (id),
    browser_event_id INTEGER NOT NULL REFERENCES browser_events (id),
    PRIMARY KEY (knowledge_node_id, browser_event_id)
);

CREATE INDEX IF NOT EXISTS idx_browser_events_timestamp ON browser_events (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_browser_events_domain ON browser_events (domain);
CREATE UNIQUE INDEX IF NOT EXISTS idx_browser_events_envelope_id ON browser_events (envelope_id)
WHERE envelope_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_browser_events_enriched ON browser_events (enriched)
WHERE enriched = 0;
CREATE INDEX IF NOT EXISTS idx_browser_queue_pending ON browser_enrichment_queue (status, priority)
WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_browser_events_ts_domain ON browser_events (timestamp, domain);

-- ─── v5: GitHub Actions ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS workflow_runs (
    id INTEGER PRIMARY KEY,
    repo TEXT NOT NULL,
    head_sha TEXT NOT NULL,
    head_branch TEXT,
    event TEXT NOT NULL,
    status TEXT NOT NULL,
    conclusion TEXT,
    started_at INTEGER,
    completed_at INTEGER,
    html_url TEXT NOT NULL,
    actor TEXT,
    raw_json TEXT NOT NULL,
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    enriched INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_sha ON workflow_runs (head_sha);
CREATE INDEX IF NOT EXISTS idx_workflow_runs_repo_started ON workflow_runs (repo, started_at);

CREATE TABLE IF NOT EXISTS workflow_jobs (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES workflow_runs (id) ON DELETE CASCADE,
    `name` TEXT NOT NULL,
    status TEXT NOT NULL,
    conclusion TEXT,
    started_at INTEGER,
    completed_at INTEGER,
    runner_name TEXT,
    raw_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_jobs_run ON workflow_jobs (run_id);

CREATE TABLE IF NOT EXISTS workflow_annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES workflow_jobs (id) ON DELETE CASCADE,
    `level` TEXT NOT NULL,
    tool TEXT,
    rule_id TEXT,
    `path` TEXT,
    start_line INTEGER,
    message TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_annotations_job ON workflow_annotations (job_id);
CREATE INDEX IF NOT EXISTS idx_workflow_annotations_tool_rule ON workflow_annotations (tool, rule_id);

CREATE TABLE IF NOT EXISTS workflow_log_excerpts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES workflow_jobs (id) ON DELETE CASCADE,
    step_name TEXT,
    excerpt TEXT NOT NULL,
    truncated INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_workflow_log_excerpts_job ON workflow_log_excerpts (job_id);

CREATE TABLE IF NOT EXISTS sha_watchlist (
    sha TEXT NOT NULL,
    repo TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    terminal_status TEXT,
    notified INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (sha, repo)
);
CREATE INDEX IF NOT EXISTS idx_sha_watchlist_expires ON sha_watchlist (expires_at);

-- 1:1 with workflow_runs; no separate id surrogate, run_id is the PK.
CREATE TABLE IF NOT EXISTS workflow_enrichment_queue (
    run_id INTEGER PRIMARY KEY REFERENCES workflow_runs (id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending', 'processing', 'done', 'failed', 'skipped')),
    priority INTEGER NOT NULL DEFAULT 5,
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 5,
    error_message TEXT,
    locked_at INTEGER,
    locked_by TEXT,
    enqueued_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_workflow_queue_pending ON workflow_enrichment_queue (status, priority);

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL,
    tool TEXT NOT NULL DEFAULT '',
    rule_id TEXT NOT NULL DEFAULT '',
    path_prefix TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    fix_hint TEXT,
    occurrences INTEGER NOT NULL DEFAULT 1,
    first_seen_at INTEGER NOT NULL,
    last_seen_at INTEGER NOT NULL,
    UNIQUE (repo, tool, rule_id, path_prefix)
);
CREATE INDEX IF NOT EXISTS idx_lessons_repo ON lessons (repo);

CREATE TABLE IF NOT EXISTS lesson_pending (
    repo TEXT NOT NULL,
    tool TEXT NOT NULL DEFAULT '',
    rule_id TEXT NOT NULL DEFAULT '',
    path_prefix TEXT NOT NULL DEFAULT '',
    count INTEGER NOT NULL DEFAULT 1,
    first_seen_at INTEGER NOT NULL,
    UNIQUE (repo, tool, rule_id, path_prefix)
);

CREATE TABLE IF NOT EXISTS knowledge_node_workflow_runs (
    knowledge_node_id INTEGER NOT NULL REFERENCES knowledge_nodes (id),
    run_id INTEGER NOT NULL REFERENCES workflow_runs (id) ON DELETE CASCADE,
    PRIMARY KEY (knowledge_node_id, run_id)
);

CREATE TABLE IF NOT EXISTS knowledge_node_lessons (
    knowledge_node_id INTEGER NOT NULL REFERENCES knowledge_nodes (id),
    lesson_id INTEGER NOT NULL REFERENCES lessons (id) ON DELETE CASCADE,
    PRIMARY KEY (knowledge_node_id, lesson_id)
);

-- ─── v6: FTS5 index + sqlite-vec vec0 table ──────────────────────────
--
-- FTS5 full-text index over knowledge_nodes. Summary is extracted from
-- knowledge_nodes.content (JSON blob) via json_extract in the triggers.
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    summary,
    embed_text,
    content,
    tokenize = 'porter unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS knowledge_nodes_fts_ai
AFTER INSERT ON knowledge_nodes
BEGIN
    INSERT INTO knowledge_fts (rowid, summary, embed_text, content)
    VALUES (
        NEW.id,
        COALESCE(CASE WHEN json_valid(NEW.content) THEN json_extract(NEW.content, '$.summary') END, ''),
        NEW.embed_text,
        NEW.content
    );
END;

CREATE TRIGGER IF NOT EXISTS knowledge_nodes_fts_ad
AFTER DELETE ON knowledge_nodes
BEGIN
    DELETE FROM knowledge_fts WHERE rowid = OLD.id;
END;

CREATE TRIGGER IF NOT EXISTS knowledge_nodes_fts_au
AFTER UPDATE ON knowledge_nodes
BEGIN
    DELETE FROM knowledge_fts WHERE rowid = OLD.id;
    INSERT INTO knowledge_fts (rowid, summary, embed_text, content)
    VALUES (
        NEW.id,
        COALESCE(CASE WHEN json_valid(NEW.content) THEN json_extract(NEW.content, '$.summary') END, ''),
        NEW.embed_text,
        NEW.content
    );
END;

-- NOTE: the vec0 `knowledge_vectors` virtual table is NOT created here.
-- vec0 is provided by the runtime-loadable sqlite-vec extension, which only
-- the Python brain loads. The brain creates `knowledge_vectors` idempotently
-- on boot via hippo_brain.vector_store.ensure_vec_table().

-- ─── v8: source_health table for capture reliability monitoring ───────
--
-- Tracks per-source liveness, probe results, and rolling event counts so
-- `hippo doctor` can surface capture gaps without scanning the full events
-- table on every invocation.
CREATE TABLE IF NOT EXISTS source_health (
    source                 TEXT PRIMARY KEY,
    last_event_ts          INTEGER,
    last_success_ts        INTEGER,
    last_error_ts          INTEGER,
    last_error_msg         TEXT,
    consecutive_failures   INTEGER NOT NULL DEFAULT 0,
    events_last_1h         INTEGER NOT NULL DEFAULT 0,
    events_last_24h        INTEGER NOT NULL DEFAULT 0,
    expected_min_per_hour  INTEGER,
    probe_ok               INTEGER,
    probe_lag_ms           INTEGER,
    probe_last_run_ts      INTEGER,
    last_heartbeat_ts      INTEGER,
    updated_at             INTEGER NOT NULL
);

-- Pre-seed one row per known source so health checks always have a row to
-- UPDATE rather than needing INSERT-or-UPDATE logic. last_event_ts is NULL
-- on fresh databases because the event tables are empty.
INSERT OR IGNORE INTO source_health (source, last_event_ts, updated_at) VALUES
    ('shell',         (SELECT MAX(timestamp)  FROM events          WHERE source_kind = 'shell'),   unixepoch('now') * 1000),
    ('claude-tool',   (SELECT MAX(timestamp)  FROM events          WHERE source_kind = 'claude-tool'), unixepoch('now') * 1000),
    ('claude-session',(SELECT MAX(start_time) FROM claude_sessions),                               unixepoch('now') * 1000),
    ('browser',       (SELECT MAX(timestamp)  FROM browser_events),                                unixepoch('now') * 1000);

-- ─── v9: capture_alarms table for watchdog invariant violations ────────
--
-- Written by `hippo watchdog run` when an invariant (I-1..I-10) fires.
-- Rate-limited per invariant per sliding window (default 60 min).
-- Acked via `hippo alarms ack <id>` (T-2).
CREATE TABLE IF NOT EXISTS capture_alarms (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    invariant_id TEXT    NOT NULL,
    raised_at    INTEGER NOT NULL,
    details_json TEXT    NOT NULL,
    acked_at     INTEGER,
    ack_note     TEXT
);

-- Partial index on un-acked alarms keyed by invariant — this is the hot
-- path for the rate-limit query (check for recent un-acked alarm).
CREATE INDEX IF NOT EXISTS idx_capture_alarms_invariant_active
    ON capture_alarms (invariant_id, acked_at)
    WHERE acked_at IS NULL;

-- Watcher offset tracking: resume-after-restart for the FS watcher (T-5).
CREATE TABLE IF NOT EXISTS claude_session_offsets (
    path              TEXT    PRIMARY KEY,
    session_id        TEXT,
    byte_offset       INTEGER NOT NULL DEFAULT 0,
    inode             INTEGER,
    device            INTEGER,
    size_at_last_read INTEGER NOT NULL DEFAULT 0,
    updated_at        INTEGER NOT NULL
) STRICT;

-- Hourly parity snapshots comparing tailer vs. watcher counts (M3 gate).
CREATE TABLE IF NOT EXISTS claude_session_parity (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    path           TEXT    NOT NULL,
    tailer_count   INTEGER NOT NULL DEFAULT 0,
    watcher_count  INTEGER NOT NULL DEFAULT 0,
    mismatch_count INTEGER NOT NULL DEFAULT 0,
    window_start   INTEGER NOT NULL,
    window_end     INTEGER NOT NULL
) STRICT;

CREATE INDEX IF NOT EXISTS idx_claude_session_parity_path_window
    ON claude_session_parity (path, window_start);

PRAGMA user_version = 10;
