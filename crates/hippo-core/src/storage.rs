use anyhow::Result;
use chrono::Utc;
use rusqlite::{Connection, OptionalExtension};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap};
use std::path::Path;

use crate::events::{BrowserEvent, ShellEvent};

const SCHEMA: &str = include_str!("schema.sql");

/// Schema version the daemon expects a healthy DB to be at. Exposed so
/// startup code (e.g. the brain handshake) can cross-check without
/// re-declaring the value. Keep in sync with
/// `brain/src/hippo_brain/schema_version.py::EXPECTED_SCHEMA_VERSION`.
pub const EXPECTED_VERSION: i64 = 13;

/// Idempotent `ALTER TABLE … ADD COLUMN`. Pre-checks `PRAGMA table_info`
/// for the column name; if absent, runs the supplied DDL. Used by
/// migrations so a partial-success crash (column added but `user_version`
/// not yet bumped) is safe to re-run without depending on SQLite's
/// "duplicate column name" error string, which is locale/version-sensitive.
fn add_column_if_missing(
    conn: &Connection,
    table: &str,
    column: &str,
    add_column_ddl: &str,
) -> Result<()> {
    let exists: bool = conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM pragma_table_info(?1) WHERE name = ?2)",
        rusqlite::params![table, column],
        |row| row.get(0),
    )?;
    if !exists {
        conn.execute_batch(add_column_ddl)?;
    }
    Ok(())
}

pub fn open_db(path: &Path) -> Result<Connection> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let conn = Connection::open(path)?;
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
         PRAGMA foreign_keys=ON;
         PRAGMA busy_timeout=5000;",
    )?;
    let version: i64 = conn.query_row("PRAGMA user_version", [], |row| row.get(0))?;

    // Migrate from v1 → v2: add envelope_id column for dedup
    if version == 1 {
        conn.execute_batch(
            "ALTER TABLE events ADD COLUMN envelope_id TEXT;
             CREATE UNIQUE INDEX IF NOT EXISTS idx_events_envelope_id
                 ON events (envelope_id) WHERE envelope_id IS NOT NULL;
             PRAGMA user_version = 2;",
        )?;
    }

    // Migrate from v2 → v3: add Claude session tables
    if version == 1 || version == 2 {
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS claude_sessions (
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
                created_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
                UNIQUE (session_id, segment_index)
             );
             CREATE TABLE IF NOT EXISTS knowledge_node_claude_sessions (
                knowledge_node_id INTEGER NOT NULL REFERENCES knowledge_nodes (id),
                claude_session_id INTEGER NOT NULL REFERENCES claude_sessions (id),
                PRIMARY KEY (knowledge_node_id, claude_session_id)
             );
             CREATE TABLE IF NOT EXISTS claude_enrichment_queue (
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
             CREATE INDEX IF NOT EXISTS idx_claude_sessions_cwd ON claude_sessions (cwd);
             CREATE INDEX IF NOT EXISTS idx_claude_sessions_session ON claude_sessions (session_id);
             CREATE INDEX IF NOT EXISTS idx_claude_queue_pending ON claude_enrichment_queue (status, priority)
                 WHERE status = 'pending';
             PRAGMA user_version = 3;",
        )?;
    }

    // Migrate from v3 → v4: add browser event tables
    if (1..=3).contains(&version) {
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS browser_events (
                id              INTEGER PRIMARY KEY,
                timestamp       INTEGER NOT NULL,
                url             TEXT NOT NULL,
                title           TEXT,
                domain          TEXT NOT NULL,
                dwell_ms        INTEGER NOT NULL,
                scroll_depth    REAL,
                extracted_text  TEXT,
                search_query    TEXT,
                referrer        TEXT,
                content_hash    TEXT,
                envelope_id     TEXT,
                enriched        INTEGER NOT NULL DEFAULT 0,
                created_at      INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
             );
             CREATE TABLE IF NOT EXISTS browser_enrichment_queue (
                id                  INTEGER PRIMARY KEY,
                browser_event_id    INTEGER NOT NULL UNIQUE REFERENCES browser_events(id),
                status              TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'processing', 'done', 'failed', 'skipped')),
                priority            INTEGER NOT NULL DEFAULT 5,
                retry_count         INTEGER NOT NULL DEFAULT 0,
                max_retries         INTEGER NOT NULL DEFAULT 5,
                error_message       TEXT,
                locked_at           INTEGER,
                locked_by           TEXT,
                created_at          INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
                updated_at          INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
             );
             CREATE TABLE IF NOT EXISTS knowledge_node_browser_events (
                knowledge_node_id   INTEGER NOT NULL REFERENCES knowledge_nodes(id),
                browser_event_id    INTEGER NOT NULL REFERENCES browser_events(id),
                PRIMARY KEY (knowledge_node_id, browser_event_id)
             );
             CREATE INDEX IF NOT EXISTS idx_browser_events_timestamp ON browser_events(timestamp DESC);
             CREATE INDEX IF NOT EXISTS idx_browser_events_domain ON browser_events(domain);
             CREATE UNIQUE INDEX IF NOT EXISTS idx_browser_events_envelope_id ON browser_events(envelope_id)
                 WHERE envelope_id IS NOT NULL;
             CREATE INDEX IF NOT EXISTS idx_browser_events_enriched ON browser_events(enriched)
                 WHERE enriched = 0;
             CREATE INDEX IF NOT EXISTS idx_browser_queue_pending ON browser_enrichment_queue(status, priority)
                 WHERE status = 'pending';
             CREATE INDEX IF NOT EXISTS idx_browser_events_ts_domain ON browser_events(timestamp, domain);
             PRAGMA user_version = 4;",
        )?;
    }

    // Migrate from v4 → v5: GitHub Actions tables
    if (1..=4).contains(&version) {
        // Keep in sync with the v5 block in schema.sql.
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS workflow_runs (
                id              INTEGER PRIMARY KEY,
                repo            TEXT NOT NULL,
                head_sha        TEXT NOT NULL,
                head_branch     TEXT,
                event           TEXT NOT NULL,
                status          TEXT NOT NULL,
                conclusion      TEXT,
                started_at      INTEGER,
                completed_at    INTEGER,
                html_url        TEXT NOT NULL,
                actor           TEXT,
                raw_json        TEXT NOT NULL,
                first_seen_at   INTEGER NOT NULL,
                last_seen_at    INTEGER NOT NULL,
                enriched        INTEGER NOT NULL DEFAULT 0
             );
             CREATE INDEX IF NOT EXISTS idx_workflow_runs_sha ON workflow_runs(head_sha);
             CREATE INDEX IF NOT EXISTS idx_workflow_runs_repo_started ON workflow_runs(repo, started_at);
             CREATE TABLE IF NOT EXISTS workflow_jobs (
                id              INTEGER PRIMARY KEY,
                run_id          INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
                name            TEXT NOT NULL,
                status          TEXT NOT NULL,
                conclusion      TEXT,
                started_at      INTEGER,
                completed_at    INTEGER,
                runner_name     TEXT,
                raw_json        TEXT NOT NULL
             );
             CREATE INDEX IF NOT EXISTS idx_workflow_jobs_run ON workflow_jobs(run_id);
             CREATE TABLE IF NOT EXISTS workflow_annotations (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id          INTEGER NOT NULL REFERENCES workflow_jobs(id) ON DELETE CASCADE,
                level           TEXT NOT NULL,
                tool            TEXT,
                rule_id         TEXT,
                path            TEXT,
                start_line      INTEGER,
                message         TEXT NOT NULL
             );
             CREATE INDEX IF NOT EXISTS idx_workflow_annotations_job ON workflow_annotations(job_id);
             CREATE INDEX IF NOT EXISTS idx_workflow_annotations_tool_rule ON workflow_annotations(tool, rule_id);
             CREATE TABLE IF NOT EXISTS workflow_log_excerpts (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id          INTEGER NOT NULL REFERENCES workflow_jobs(id) ON DELETE CASCADE,
                step_name       TEXT,
                excerpt         TEXT NOT NULL,
                truncated       INTEGER NOT NULL DEFAULT 0
             );
             CREATE INDEX IF NOT EXISTS idx_workflow_log_excerpts_job ON workflow_log_excerpts(job_id);
             CREATE TABLE IF NOT EXISTS sha_watchlist (
                sha             TEXT NOT NULL,
                repo            TEXT NOT NULL,
                created_at      INTEGER NOT NULL,
                expires_at      INTEGER NOT NULL,
                terminal_status TEXT,
                notified        INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (sha, repo)
             );
             CREATE INDEX IF NOT EXISTS idx_sha_watchlist_expires ON sha_watchlist(expires_at);
             CREATE TABLE IF NOT EXISTS workflow_enrichment_queue (
                run_id          INTEGER PRIMARY KEY REFERENCES workflow_runs(id) ON DELETE CASCADE,
                status          TEXT NOT NULL DEFAULT 'pending'
                                    CHECK (status IN ('pending','processing','done','failed','skipped')),
                priority        INTEGER NOT NULL DEFAULT 5,
                retry_count     INTEGER NOT NULL DEFAULT 0,
                max_retries     INTEGER NOT NULL DEFAULT 5,
                error_message   TEXT,
                locked_at       INTEGER,
                locked_by       TEXT,
                enqueued_at     INTEGER NOT NULL,
                updated_at      INTEGER NOT NULL
             );
             CREATE INDEX IF NOT EXISTS idx_workflow_queue_pending ON workflow_enrichment_queue(status, priority);
             CREATE TABLE IF NOT EXISTS lessons (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                repo            TEXT NOT NULL,
                tool            TEXT NOT NULL DEFAULT '',
                rule_id         TEXT NOT NULL DEFAULT '',
                path_prefix     TEXT NOT NULL DEFAULT '',
                summary         TEXT NOT NULL,
                fix_hint        TEXT,
                occurrences     INTEGER NOT NULL DEFAULT 1,
                first_seen_at   INTEGER NOT NULL,
                last_seen_at    INTEGER NOT NULL,
                UNIQUE(repo, tool, rule_id, path_prefix)
             );
             CREATE INDEX IF NOT EXISTS idx_lessons_repo ON lessons(repo);
             CREATE TABLE IF NOT EXISTS lesson_pending (
                repo            TEXT NOT NULL,
                tool            TEXT NOT NULL DEFAULT '',
                rule_id         TEXT NOT NULL DEFAULT '',
                path_prefix     TEXT NOT NULL DEFAULT '',
                count           INTEGER NOT NULL DEFAULT 1,
                first_seen_at   INTEGER NOT NULL,
                UNIQUE(repo, tool, rule_id, path_prefix)
             );
             CREATE TABLE IF NOT EXISTS knowledge_node_workflow_runs (
                knowledge_node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id),
                run_id            INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
                PRIMARY KEY (knowledge_node_id, run_id)
             );
             CREATE TABLE IF NOT EXISTS knowledge_node_lessons (
                knowledge_node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id),
                lesson_id         INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
                PRIMARY KEY (knowledge_node_id, lesson_id)
             );
             PRAGMA user_version = 5;",
        )?;
    }

    // Migrate from v5 → v6: FTS5 index on knowledge_nodes + sync triggers.
    // vec0 `knowledge_vectors` is created by the Python brain (which loads the
    // sqlite-vec extension); the Rust daemon does not load vec0.
    if (1..=5).contains(&version) {
        // FTS index + triggers require the knowledge_nodes table. Very old
        // databases (v1) predate it; create it now so the migration chain
        // completes cleanly. Fresh databases (v0) get it from schema.sql.
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS knowledge_nodes (
                id                   INTEGER PRIMARY KEY,
                uuid                 TEXT NOT NULL UNIQUE,
                content              TEXT NOT NULL,
                embed_text           TEXT NOT NULL,
                node_type            TEXT NOT NULL DEFAULT 'observation',
                outcome              TEXT,
                tags                 TEXT,
                enrichment_model     TEXT,
                enrichment_version   INTEGER NOT NULL DEFAULT 1,
                created_at           INTEGER NOT NULL DEFAULT (unixepoch('now','subsec') * 1000),
                updated_at           INTEGER NOT NULL DEFAULT (unixepoch('now','subsec') * 1000)
             );",
        )?;
        conn.execute_batch(
            "CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
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
             END;",
        )?;
        // Backfill FTS only if knowledge_nodes exists (it may not on very old
        // DBs whose migration chain hasn't reached v2 yet — the base schema
        // creates it at version 0).
        let has_knowledge_nodes: i64 = conn.query_row(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='knowledge_nodes'",
            [],
            |r| r.get(0),
        )?;
        if has_knowledge_nodes > 0 {
            conn.execute_batch(
                "INSERT INTO knowledge_fts (rowid, summary, embed_text, content)
                 SELECT id,
                        COALESCE(CASE WHEN json_valid(content) THEN json_extract(content, '$.summary') END, ''),
                        embed_text,
                        content
                 FROM knowledge_nodes
                 WHERE id NOT IN (SELECT rowid FROM knowledge_fts);",
            )?;
        }
        conn.execute_batch("PRAGMA user_version = 6;")?;
    }

    // Migrate from v6 → v7: add source_kind + tool_name to events for the
    // Claude tool enrichment policy. source_kind groups events by origin
    // ('shell' vs 'claude-tool' vs future sources) so queries can filter
    // synthesized rows; tool_name records the exact upstream tool name
    // (e.g. 'Bash', 'Agent', 'mcp__github__create_pull_request') for rows
    // that originated from a Claude Code session. Keep in sync with
    // schema.sql.
    if (1..=6).contains(&version) {
        conn.execute_batch(
            "ALTER TABLE events ADD COLUMN source_kind TEXT NOT NULL DEFAULT 'shell';
             ALTER TABLE events ADD COLUMN tool_name TEXT;
             CREATE INDEX IF NOT EXISTS idx_events_source_kind
                 ON events (source_kind) WHERE source_kind != 'shell';
             PRAGMA user_version = 7;",
        )?;
    }

    // Migrate from v7 → v8: add source_health table for capture reliability
    // monitoring, and probe_tag columns on the three event tables so probes can
    // stamp the rows they inject without touching real-capture data.
    // Keep in sync with schema.sql.
    if (1..=7).contains(&version) {
        // CREATE TABLE IF NOT EXISTS and INSERT OR IGNORE are idempotent — safe to batch.
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS source_health (
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
             INSERT OR IGNORE INTO source_health (source, last_event_ts, updated_at) VALUES
                 ('shell',         (SELECT MAX(timestamp)  FROM events          WHERE source_kind = 'shell'),   unixepoch('now') * 1000),
                 ('claude-tool',   (SELECT MAX(timestamp)  FROM events          WHERE source_kind = 'claude-tool'), unixepoch('now') * 1000),
                 ('claude-session',(SELECT MAX(start_time) FROM claude_sessions),                               unixepoch('now') * 1000),
                 ('browser',       (SELECT MAX(timestamp)  FROM browser_events),                                unixepoch('now') * 1000);",
        )?;
        // ALTER TABLE doesn't support IF NOT EXISTS in SQLite. A crash
        // between the CREATE TABLE above and the PRAGMA user_version = 8
        // below would leave the DB at v7, causing a retry that errors on
        // the already-added column. `add_column_if_missing` pre-checks
        // table_info so the migration is idempotent on re-run.
        for (table, column, ddl) in [
            (
                "events",
                "probe_tag",
                "ALTER TABLE events ADD COLUMN probe_tag TEXT",
            ),
            (
                "claude_sessions",
                "probe_tag",
                "ALTER TABLE claude_sessions ADD COLUMN probe_tag TEXT",
            ),
            (
                "browser_events",
                "probe_tag",
                "ALTER TABLE browser_events ADD COLUMN probe_tag TEXT",
            ),
        ] {
            add_column_if_missing(&conn, table, column, ddl)?;
        }
        conn.execute_batch("PRAGMA user_version = 8;")?;
    }

    // Migrate from v8 → v9: add capture_alarms table for watchdog invariant violations.
    // Written by `hippo watchdog run`; acked via `hippo alarms ack` (T-2).
    if (1..=8).contains(&version) {
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS capture_alarms (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                invariant_id TEXT    NOT NULL,
                raised_at    INTEGER NOT NULL,
                details_json TEXT    NOT NULL,
                acked_at     INTEGER,
                ack_note     TEXT
             );
             CREATE INDEX IF NOT EXISTS idx_capture_alarms_invariant_active
                 ON capture_alarms (invariant_id, acked_at)
                 WHERE acked_at IS NULL;
             CREATE INDEX IF NOT EXISTS idx_claude_sessions_start_time
                 ON claude_sessions (start_time DESC);
             PRAGMA user_version = 9;",
        )?;
    }

    // Migrate from v9 → v10: add watcher offset tracking and parity tables (T-5).
    if (1..=9).contains(&version) {
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS claude_session_offsets (
                path              TEXT    PRIMARY KEY,
                session_id        TEXT,
                byte_offset       INTEGER NOT NULL DEFAULT 0,
                inode             INTEGER,
                device            INTEGER,
                size_at_last_read INTEGER NOT NULL DEFAULT 0,
                updated_at        INTEGER NOT NULL
             ) STRICT;
             -- claude_session_parity: unused since T-8 (PR #89). Kept here so
             -- v9→v10 migrations on existing DBs land at the same schema as
             -- fresh installs (see schema.sql for the matching note).
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
             CREATE INDEX IF NOT EXISTS idx_claude_sessions_start_time
                 ON claude_sessions (start_time DESC);
             PRAGMA user_version = 10;",
        )?;
    }

    // Migrate from v10 → v11: auto-resolve fields on capture_alarms.
    //
    // `resolved_at` is set by the watchdog when an alarm's underlying
    // invariant has stayed clean for 2 consecutive ticks. `clean_ticks`
    // counts consecutive clean evaluations between watchdog runs (single-
    // shot process, so per-row DB state is the only persistence).
    //
    // Resolved rows stop suppressing new alarms (rate-limit ignores them)
    // and stop counting toward the doctor exit code. They survive in the
    // table until acked or pruned via `hippo alarms prune`.
    if (1..=10).contains(&version) {
        // Both ALTERs are idempotent via `add_column_if_missing`, so a
        // partial-success crash (one column added, then crash before the
        // second or before the user_version bump) is safe to re-run.
        // `clean_ticks` carries a CHECK constraint so a stray UPDATE can't
        // leave it negative.
        add_column_if_missing(
            &conn,
            "capture_alarms",
            "resolved_at",
            "ALTER TABLE capture_alarms ADD COLUMN resolved_at INTEGER",
        )?;
        add_column_if_missing(
            &conn,
            "capture_alarms",
            "clean_ticks",
            "ALTER TABLE capture_alarms ADD COLUMN clean_ticks INTEGER NOT NULL DEFAULT 0
                 CHECK (clean_ticks >= 0)",
        )?;
        // The "active alarm" predicate now includes resolved_at — old index
        // would let resolved rows still suppress new alarms via rate-limit.
        conn.execute_batch(
            "DROP INDEX IF EXISTS idx_capture_alarms_invariant_active;
             CREATE INDEX IF NOT EXISTS idx_capture_alarms_invariant_active
                 ON capture_alarms (invariant_id, acked_at)
                 WHERE acked_at IS NULL AND resolved_at IS NULL;
             PRAGMA user_version = 11;",
        )?;
    }

    // Migrate from v11 → v12: add content_hash and last_enriched_content_hash
    // to claude_sessions in support of the Phase 1 data-loss fix.
    //
    // Background: the FS watcher used INSERT OR IGNORE on (session_id,
    // segment_index), so segments that grew after first capture were silently
    // truncated — the stale row was never updated. The fix switches to an
    // upsert (INSERT … ON CONFLICT DO UPDATE) keyed on content_hash so the
    // daemon can detect when a segment has changed and overwrite the stale row.
    //
    // `content_hash` — set by the daemon watcher on every upsert; NULL on
    //     rows that pre-date v12 (legacy rows are re-hashed on next watcher
    //     pass via T-A.7 backfill).
    // `last_enriched_content_hash` — set by the brain enrichment worker when
    //     it completes enrichment of a segment. The brain compares this against
    //     content_hash to detect stale knowledge nodes and re-enrich when they
    //     diverge. NULL until first enrichment.
    //
    // See docs/capture-reliability/11-watcher-data-loss-fix.md, task T-A.1.
    if (1..=11).contains(&version) {
        // Guard: claude_sessions may not exist in minimal test DBs that only
        // seed the tables relevant to an earlier migration. In production the
        // table always exists (created in v3); skip the ALTERs if it is absent
        // so older migration tests keep working.
        let table_exists: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='claude_sessions')",
            [],
            |row| row.get(0),
        )?;
        if table_exists {
            // Both ALTERs are idempotent via `add_column_if_missing`, so a
            // partial-success crash (one column added, then crash before the
            // second or before the user_version bump) is safe to re-run.
            add_column_if_missing(
                &conn,
                "claude_sessions",
                "content_hash",
                "ALTER TABLE claude_sessions ADD COLUMN content_hash TEXT",
            )?;
            add_column_if_missing(
                &conn,
                "claude_sessions",
                "last_enriched_content_hash",
                "ALTER TABLE claude_sessions ADD COLUMN last_enriched_content_hash TEXT",
            )?;
        }
        conn.execute_batch("PRAGMA user_version = 12;")?;
    }

    // Migrate from v12 → v13: extend the entities.type CHECK list with
    // 'env_var' so the enrichment pipeline can bucket environment variable
    // names as a first-class identifier type. Surfaced on the RAG
    // `Entities:` line via `IDENTIFIER_ENTITY_TYPES` (issue #108 follow-up).
    //
    // SQLite does not support ALTER TABLE … ADD CHECK, so we follow the
    // documented 12-step recipe: create entities_new with the expanded
    // CHECK, copy rows, drop the old table, rename the new one, and
    // recreate the indexes. The ALTER TABLE RENAME preserves foreign-key
    // references from `relationships`, `event_entities`, and
    // `knowledge_node_entities` (their REFERENCES clauses are textual and
    // resolve by name), but the DROP + RENAME sequence still requires
    // `foreign_keys=OFF` to avoid a transient FK violation.
    //
    // Idempotency: `DROP TABLE IF EXISTS entities_new` lets a partial-
    // success crash (e.g. crash between INSERT and DROP) be safely retried
    // — the next run drops the half-populated entities_new and starts over.
    if (1..=12).contains(&version) {
        let entities_exists: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name='entities')",
            [],
            |row| row.get(0),
        )?;
        if entities_exists {
            conn.execute_batch(
                "PRAGMA foreign_keys = OFF;
                 BEGIN;
                 DROP TABLE IF EXISTS entities_new;
                 CREATE TABLE entities_new (
                     id INTEGER PRIMARY KEY,
                     type TEXT NOT NULL CHECK (type IN (
                         'project', 'file', 'tool', 'service', 'repo', 'host', 'person',
                         'concept', 'domain', 'env_var'
                     )),
                     name TEXT NOT NULL,
                     canonical TEXT,
                     metadata TEXT,
                     first_seen INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
                     last_seen INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
                     created_at INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
                     UNIQUE (type, canonical)
                 );
                 INSERT INTO entities_new
                     (id, type, name, canonical, metadata, first_seen, last_seen, created_at)
                     SELECT id, type, name, canonical, metadata, first_seen, last_seen, created_at
                     FROM entities;
                 DROP TABLE entities;
                 ALTER TABLE entities_new RENAME TO entities;
                 CREATE INDEX IF NOT EXISTS idx_entities_type_name ON entities (type, name);
                 CREATE INDEX IF NOT EXISTS idx_entities_canonical ON entities (canonical)
                     WHERE canonical IS NOT NULL;
                 COMMIT;
                 PRAGMA foreign_keys = ON;",
            )?;
        }
        conn.execute_batch("PRAGMA user_version = 13;")?;
    } else if version != 0 && version != EXPECTED_VERSION {
        anyhow::bail!(
            "DB schema version mismatch: expected {}, found {}. \
             Please run migrations or delete the database.",
            EXPECTED_VERSION,
            version
        );
    }

    if version == 0 {
        conn.execute_batch(SCHEMA)?;
    }
    Ok(conn)
}

pub fn upsert_session(
    conn: &Connection,
    session_uuid: &str,
    hostname: &str,
    shell: &str,
    username: &str,
) -> Result<i64> {
    if let Some(existing) = conn
        .query_row(
            "SELECT id FROM sessions WHERE terminal = ?1 ORDER BY start_time DESC LIMIT 1",
            [session_uuid],
            |row| row.get(0),
        )
        .optional()?
    {
        return Ok(existing);
    }

    let now = Utc::now().timestamp_millis();
    conn.execute(
        "INSERT INTO sessions (start_time, terminal, shell, hostname, username)
         VALUES (?1, ?2, ?3, ?4, ?5)",
        rusqlite::params![now, session_uuid, shell, hostname, username],
    )?;
    Ok(conn.last_insert_rowid())
}

pub fn get_or_create_session(
    conn: &Connection,
    session_uuid: &str,
    hostname: &str,
    shell: &str,
    username: &str,
    session_map: &mut HashMap<String, i64>,
) -> Result<i64> {
    if let Some(&id) = session_map.get(session_uuid) {
        return Ok(id);
    }
    let id = upsert_session(conn, session_uuid, hostname, shell, username)?;
    session_map.insert(session_uuid.to_string(), id);
    Ok(id)
}

fn stable_env_json(env: &HashMap<String, String>) -> Result<String> {
    let ordered: BTreeMap<&str, &str> = env.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();
    Ok(serde_json::to_string(&ordered)?)
}

pub fn upsert_env_snapshot(
    conn: &Connection,
    env: &HashMap<String, String>,
) -> Result<Option<i64>> {
    if env.is_empty() {
        return Ok(None);
    }
    let env_json = stable_env_json(env)?;
    let mut hasher = Sha256::new();
    hasher.update(env_json.as_bytes());
    let content_hash: String = hasher
        .finalize()
        .iter()
        .map(|b| format!("{:02x}", b))
        .collect();

    conn.execute(
        "INSERT OR IGNORE INTO env_snapshots (content_hash, env_json) VALUES (?1, ?2)",
        rusqlite::params![content_hash, env_json],
    )?;
    let id: i64 = conn.query_row(
        "SELECT id FROM env_snapshots WHERE content_hash = ?1",
        [&content_hash],
        |row| row.get(0),
    )?;
    Ok(Some(id))
}

pub fn insert_event(
    conn: &Connection,
    session_id: i64,
    event: &ShellEvent,
    redaction_count: u32,
    env_snapshot_id: Option<i64>,
) -> Result<i64> {
    insert_event_at(
        conn,
        session_id,
        event,
        Utc::now().timestamp_millis(),
        redaction_count,
        env_snapshot_id,
        None,
        None, // probe_tag: real events never have a probe_tag
    )
}

/// Derive the `source_kind` string for a shell event — same logic used by `insert_event_at`.
/// Exposed so callers that need the kind for bookkeeping don't duplicate the derivation.
pub fn source_kind_of(event: &ShellEvent) -> &'static str {
    if event.tool_name.is_some() {
        "claude-tool"
    } else {
        "shell"
    }
}

#[allow(clippy::too_many_arguments)]
pub fn insert_event_at(
    conn: &Connection,
    session_id: i64,
    event: &ShellEvent,
    timestamp: i64,
    redaction_count: u32,
    env_snapshot_id: Option<i64>,
    envelope_id: Option<&str>,
    probe_tag: Option<&str>,
) -> Result<i64> {
    let shell_str = event.shell.as_db_str();
    let (git_repo, git_branch, git_commit, git_dirty) = match &event.git_state {
        Some(gs) => (
            gs.repo.as_deref(),
            gs.branch.as_deref(),
            gs.commit.as_deref(),
            Some(gs.is_dirty as i32),
        ),
        None => (None, None, None, None),
    };
    let (stdout, stdout_truncated) = match &event.stdout {
        Some(o) => (Some(o.content.as_str()), Some(o.truncated as i32)),
        None => (None, None),
    };
    let (stderr, stderr_truncated) = match &event.stderr {
        Some(o) => (Some(o.content.as_str()), Some(o.truncated as i32)),
        None => (None, None),
    };

    let source_kind = source_kind_of(event);

    let tx = conn.unchecked_transaction()?;

    let rows = tx.execute(
        "INSERT OR IGNORE INTO events (session_id, timestamp, command, stdout, stderr, stdout_truncated, stderr_truncated,
         exit_code, duration_ms, cwd, hostname, shell, git_repo, git_branch, git_commit, git_dirty,
         env_snapshot_id, redaction_count, envelope_id, source_kind, tool_name, probe_tag)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19, ?20, ?21, ?22)",
        rusqlite::params![
            session_id,
            timestamp,
            event.command,
            stdout,
            stderr,
            stdout_truncated,
            stderr_truncated,
            event.exit_code,
            event.duration_ms as i64,
            event.cwd.to_string_lossy(),
            event.hostname,
            shell_str,
            git_repo,
            git_branch,
            git_commit,
            git_dirty,
            env_snapshot_id,
            redaction_count,
            envelope_id,
            source_kind,
            event.tool_name.as_deref(),
            probe_tag,
        ],
    )?;
    if rows == 0 {
        // Duplicate envelope_id — skip enrichment queue too
        tx.commit()?;
        return Ok(-1);
    }
    let event_id = tx.last_insert_rowid();

    // Probe events are excluded from enrichment: their purpose is liveness
    // verification, not knowledge extraction. Upstream filter is load-bearing
    // — downstream queue joins also filter but this is the definitive gate.
    if probe_tag.is_none() {
        tx.execute(
            "INSERT INTO enrichment_queue (event_id) VALUES (?1)",
            [event_id],
        )?;
    }

    tx.commit()?;
    Ok(event_id)
}

pub fn insert_browser_event(
    conn: &Connection,
    event: &BrowserEvent,
    timestamp_ms: i64,
    envelope_id: Option<&str>,
    probe_tag: Option<&str>,
) -> Result<i64> {
    // Compute content_hash from extracted_text if present
    let content_hash = event.extracted_text.as_ref().map(|text| {
        let mut hasher = Sha256::new();
        hasher.update(text.as_bytes());
        hasher
            .finalize()
            .iter()
            .map(|b| format!("{:02x}", b))
            .collect::<String>()
    });

    let tx = conn.unchecked_transaction()?;

    let rows = tx.execute(
        "INSERT OR IGNORE INTO browser_events
         (timestamp, url, title, domain, dwell_ms, scroll_depth,
          extracted_text, search_query, referrer, content_hash, envelope_id, probe_tag)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
        rusqlite::params![
            timestamp_ms,
            event.url,
            event.title,
            event.domain,
            event.dwell_ms as i64,
            event.scroll_depth as f64,
            event.extracted_text,
            event.search_query,
            event.referrer,
            content_hash,
            envelope_id,
            probe_tag,
        ],
    )?;

    if rows == 0 {
        // Duplicate envelope_id — skip enrichment queue too
        tx.commit()?;
        return Ok(-1);
    }

    let event_id = tx.last_insert_rowid();

    // Probe events are excluded from enrichment (upstream filter — see AP-6).
    if probe_tag.is_none() {
        tx.execute(
            "INSERT INTO browser_enrichment_queue (browser_event_id) VALUES (?1)",
            [event_id],
        )?;
    }

    tx.commit()?;
    Ok(event_id)
}

pub fn get_sessions(
    conn: &Connection,
    since_ms: Option<i64>,
    limit: usize,
) -> Result<Vec<crate::protocol::SessionInfo>> {
    let mut sql = String::from(
        "SELECT s.id, s.start_time, s.end_time, s.hostname, s.shell,
                (SELECT COUNT(*) FROM events e WHERE e.session_id = s.id AND e.probe_tag IS NULL) as event_count,
                s.summary
         FROM sessions s",
    );
    let mut params: Vec<Box<dyn rusqlite::types::ToSql>> = Vec::new();
    if let Some(since) = since_ms {
        sql.push_str(" WHERE s.start_time >= ?1");
        params.push(Box::new(since));
    }
    sql.push_str(" ORDER BY s.start_time DESC");
    sql.push_str(&format!(" LIMIT {}", limit));

    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(rusqlite::params_from_iter(params.iter()), |row| {
        Ok(crate::protocol::SessionInfo {
            id: row.get(0)?,
            start_time: row.get(1)?,
            end_time: row.get(2)?,
            hostname: row.get(3)?,
            shell: row.get(4)?,
            event_count: row.get::<_, i64>(5)? as u64,
            summary: row.get(6)?,
        })
    })?;
    Ok(rows.collect::<Result<Vec<_>, _>>()?)
}

pub fn get_events(
    conn: &Connection,
    session_id: Option<i64>,
    since_ms: Option<i64>,
    project: Option<&str>,
    limit: usize,
) -> Result<Vec<crate::protocol::EventInfo>> {
    let mut sql = String::from(
        "SELECT id, session_id, timestamp, command, exit_code, duration_ms, cwd, git_branch, enriched
         FROM events WHERE probe_tag IS NULL",
    );
    let mut params: Vec<Box<dyn rusqlite::types::ToSql>> = Vec::new();
    let mut idx = 1;

    if let Some(sid) = session_id {
        sql.push_str(&format!(" AND session_id = ?{}", idx));
        params.push(Box::new(sid));
        idx += 1;
    }
    if let Some(since) = since_ms {
        sql.push_str(&format!(" AND timestamp >= ?{}", idx));
        params.push(Box::new(since));
        idx += 1;
    }
    if let Some(proj) = project {
        sql.push_str(&format!(" AND cwd LIKE ?{}", idx));
        params.push(Box::new(format!("%{}%", proj)));
    }
    sql.push_str(" ORDER BY timestamp DESC");
    sql.push_str(&format!(" LIMIT {}", limit));

    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(rusqlite::params_from_iter(params.iter()), |row| {
        Ok(crate::protocol::EventInfo {
            id: row.get(0)?,
            session_id: row.get(1)?,
            timestamp: row.get(2)?,
            command: row.get(3)?,
            exit_code: row.get(4)?,
            duration_ms: row.get::<_, i64>(5)? as u64,
            cwd: row.get(6)?,
            git_branch: row.get(7)?,
            enriched: row.get::<_, i32>(8)? != 0,
        })
    })?;
    Ok(rows.collect::<Result<Vec<_>, _>>()?)
}

pub fn get_entities(
    conn: &Connection,
    entity_type: Option<&str>,
) -> Result<Vec<crate::protocol::EntityInfo>> {
    let mut sql =
        String::from("SELECT id, type, name, canonical, first_seen, last_seen FROM entities");
    let mut params: Vec<Box<dyn rusqlite::types::ToSql>> = Vec::new();
    if let Some(et) = entity_type {
        sql.push_str(" WHERE type = ?1");
        params.push(Box::new(et.to_string()));
    }
    sql.push_str(" ORDER BY last_seen DESC");

    let mut stmt = conn.prepare(&sql)?;
    let rows = stmt.query_map(rusqlite::params_from_iter(params.iter()), |row| {
        Ok(crate::protocol::EntityInfo {
            id: row.get(0)?,
            entity_type: row.get(1)?,
            name: row.get(2)?,
            canonical: row.get(3)?,
            first_seen: row.get(4)?,
            last_seen: row.get(5)?,
        })
    })?;
    Ok(rows.collect::<Result<Vec<_>, _>>()?)
}

pub fn raw_query(conn: &Connection, text: &str) -> Result<Vec<crate::protocol::QueryHit>> {
    let pattern = format!("%{}%", text);
    let mut stmt = conn.prepare(
        "SELECT id, command, cwd, timestamp FROM events WHERE command LIKE ?1 AND probe_tag IS NULL
         ORDER BY timestamp DESC LIMIT 20",
    )?;
    let rows = stmt.query_map([&pattern], |row| {
        Ok(crate::protocol::QueryHit {
            event_id: row.get(0)?,
            command: row.get(1)?,
            cwd: row.get(2)?,
            timestamp: row.get(3)?,
            relevance: "keyword".to_string(),
        })
    })?;
    Ok(rows.collect::<Result<Vec<_>, _>>()?)
}

pub fn get_status(conn: &Connection) -> Result<crate::protocol::StatusInfo> {
    let today_start = {
        let now = Utc::now();
        now.date_naive()
            .and_hms_opt(0, 0, 0)
            .unwrap()
            .and_utc()
            .timestamp_millis()
    };

    let events_today: u64 = conn.query_row(
        "SELECT COUNT(*) FROM events WHERE timestamp >= ?1 AND probe_tag IS NULL",
        [today_start],
        |row| row.get::<_, i64>(0).map(|v| v as u64),
    )?;

    let sessions_today: u64 = conn.query_row(
        "SELECT COUNT(*) FROM sessions WHERE start_time >= ?1",
        [today_start],
        |row| row.get::<_, i64>(0).map(|v| v as u64),
    )?;

    let queue_depth: u64 = conn.query_row(
        "SELECT COUNT(*) FROM enrichment_queue WHERE status = 'pending'",
        [],
        |row| row.get::<_, i64>(0).map(|v| v as u64),
    )?;

    let queue_failed: u64 = conn.query_row(
        "SELECT COUNT(*) FROM enrichment_queue WHERE status = 'failed'",
        [],
        |row| row.get::<_, i64>(0).map(|v| v as u64),
    )?;

    Ok(crate::protocol::StatusInfo {
        version: String::new(),
        uptime_secs: 0,
        events_today,
        sessions_today,
        queue_depth,
        queue_failed,
        drop_count: 0,
        lmstudio_reachable: false,
        brain_reachable: false,
        db_size_bytes: 0,
        fallback_files_pending: 0,
    })
}

pub fn write_fallback_jsonl(
    fallback_dir: &Path,
    envelope: &crate::events::EventEnvelope,
) -> Result<()> {
    use std::io::Write;
    std::fs::create_dir_all(fallback_dir)?;
    let date = Utc::now().format("%Y-%m-%d");
    let path = fallback_dir.join(format!("{}.jsonl", date));
    let mut file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)?;
    let json = serde_json::to_string(envelope)?;
    writeln!(file, "{}", json)?;
    Ok(())
}

pub fn list_fallback_files(fallback_dir: &Path) -> Result<Vec<std::path::PathBuf>> {
    if !fallback_dir.exists() {
        return Ok(Vec::new());
    }
    // fallback_dir is hippo's own XDG data dir, not user input.
    // nosemgrep: rust.actix.path-traversal.tainted-path.tainted-path
    let mut files: Vec<std::path::PathBuf> = std::fs::read_dir(fallback_dir)?
        .filter_map(|e| e.ok())
        .map(|e| e.path())
        .filter(|p| p.extension().and_then(|e| e.to_str()) == Some("jsonl"))
        .collect();
    files.sort();
    Ok(files)
}

pub fn recover_fallback_files(
    conn: &Connection,
    fallback_dir: &Path,
    session_map: &mut HashMap<String, i64>,
) -> Result<(usize, usize)> {
    let files = list_fallback_files(fallback_dir)?;
    let mut recovered = 0usize;
    let mut errors = 0usize;

    for file_path in &files {
        // file_path comes from list_fallback_files, which reads hippo's own XDG data dir.
        // nosemgrep: rust.actix.path-traversal.tainted-path.tainted-path
        let content = std::fs::read_to_string(file_path)?;
        let mut file_errors = 0usize;
        for line in content.lines() {
            if line.trim().is_empty() {
                continue;
            }
            match serde_json::from_str::<crate::events::EventEnvelope>(line) {
                Ok(envelope) => match &envelope.payload {
                    crate::events::EventPayload::Shell(shell_event) => {
                        let username =
                            std::env::var("USER").unwrap_or_else(|_| "unknown".to_string());
                        let session_id = get_or_create_session(
                            conn,
                            &shell_event.session_id.to_string(),
                            &shell_event.hostname,
                            shell_event.shell.as_db_str(),
                            &username,
                            session_map,
                        )?;
                        let eid = envelope.envelope_id.to_string();
                        match insert_event_at(
                            conn,
                            session_id,
                            shell_event,
                            envelope.timestamp.timestamp_millis(),
                            shell_event.redaction_count,
                            None,
                            Some(&eid),
                            envelope.probe_tag.as_deref(),
                        ) {
                            Ok(_) => recovered += 1,
                            Err(_) => file_errors += 1,
                        }
                    }
                    crate::events::EventPayload::Browser(browser_event) => {
                        let eid = envelope.envelope_id.to_string();
                        match insert_browser_event(
                            conn,
                            browser_event,
                            envelope.timestamp.timestamp_millis(),
                            Some(&eid),
                            envelope.probe_tag.as_deref(),
                        ) {
                            Ok(_) => recovered += 1,
                            Err(_) => file_errors += 1,
                        }
                    }
                    _ => {
                        // Other payload types not yet recoverable
                        file_errors += 1;
                    }
                },
                Err(_) => file_errors += 1,
            }
        }
        errors += file_errors;

        if file_errors == 0 {
            // All lines succeeded — mark as done
            let done_path = file_path.with_extension("jsonl.done");
            std::fs::rename(file_path, done_path)?;
        } else {
            // Some lines failed — preserve for operator inspection
            let partial_path = file_path.with_extension("jsonl.partial");
            std::fs::rename(file_path, partial_path)?;
        }
    }

    Ok((recovered, errors))
}

#[cfg(test)]
pub fn open_memory() -> Result<Connection> {
    let conn = Connection::open_in_memory()?;
    conn.execute_batch(
        "PRAGMA journal_mode=WAL;
         PRAGMA foreign_keys=ON;
         PRAGMA busy_timeout=5000;",
    )?;
    conn.execute_batch(SCHEMA)?;
    Ok(conn)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::events::{EventEnvelope, GitState, ShellEvent, ShellKind};
    use std::path::PathBuf;

    fn sample_shell_event() -> ShellEvent {
        ShellEvent {
            session_id: uuid::Uuid::new_v4(),
            command: "cargo build".to_string(),
            exit_code: 0,
            duration_ms: 1234,
            cwd: PathBuf::from("/home/user/project"),
            hostname: "laptop".to_string(),
            shell: ShellKind::Zsh,
            stdout: None,
            stderr: None,
            env_snapshot: HashMap::new(),
            git_state: Some(GitState {
                repo: Some("myrepo".to_string()),
                branch: Some("main".to_string()),
                commit: Some("abc1234".to_string()),
                is_dirty: false,
            }),
            redaction_count: 0,
            tool_name: None,
        }
    }

    #[test]
    fn test_open_memory_creates_tables() {
        let conn = open_memory().unwrap();
        let expected_tables = [
            "sessions",
            "env_snapshots",
            "events",
            "entities",
            "relationships",
            "event_entities",
            "knowledge_nodes",
            "knowledge_node_entities",
            "knowledge_node_events",
            "enrichment_queue",
        ];
        for table in &expected_tables {
            let exists: bool = conn
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
                    [table],
                    |row| row.get(0),
                )
                .unwrap();
            assert!(exists, "table '{}' should exist", table);
        }
    }

    #[test]
    fn test_open_file_db() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("test.db");
        let conn = open_db(&db_path).unwrap();
        let mode: String = conn
            .query_row("PRAGMA journal_mode", [], |row| row.get(0))
            .unwrap();
        assert_eq!(mode, "wal");
    }

    #[test]
    fn test_insert_event_and_queue() {
        let conn = open_memory().unwrap();
        let session_id = upsert_session(&conn, "sess-1", "laptop", "zsh", "user").unwrap();
        let event = sample_shell_event();
        let event_id = insert_event(&conn, session_id, &event, 0, None).unwrap();
        assert!(event_id > 0);

        // Verify event exists
        let cmd: String = conn
            .query_row(
                "SELECT command FROM events WHERE id = ?1",
                [event_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(cmd, "cargo build");

        // Verify enrichment queue entry
        let queue_event_id: i64 = conn
            .query_row(
                "SELECT event_id FROM enrichment_queue WHERE event_id = ?1",
                [event_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(queue_event_id, event_id);
    }

    #[test]
    fn test_env_snapshot_dedup() {
        let conn = open_memory().unwrap();
        let env: HashMap<String, String> =
            HashMap::from([("HOME".to_string(), "/home/user".to_string())]);

        let id1 = upsert_env_snapshot(&conn, &env).unwrap().unwrap();
        let id2 = upsert_env_snapshot(&conn, &env).unwrap().unwrap();
        assert_eq!(id1, id2);

        // Empty env returns None
        let empty: HashMap<String, String> = HashMap::new();
        assert!(upsert_env_snapshot(&conn, &empty).unwrap().is_none());
    }

    #[test]
    fn test_env_snapshot_dedup_with_different_insertion_order() {
        let conn = open_memory().unwrap();

        let env_a: HashMap<String, String> = HashMap::from([
            ("HOME".to_string(), "/home/user".to_string()),
            ("PATH".to_string(), "/usr/bin".to_string()),
        ]);
        let env_b: HashMap<String, String> = HashMap::from([
            ("PATH".to_string(), "/usr/bin".to_string()),
            ("HOME".to_string(), "/home/user".to_string()),
        ]);

        let id_a = upsert_env_snapshot(&conn, &env_a).unwrap().unwrap();
        let id_b = upsert_env_snapshot(&conn, &env_b).unwrap().unwrap();

        assert_eq!(id_a, id_b);
    }

    #[test]
    fn test_session_map() {
        let conn = open_memory().unwrap();
        let mut map = HashMap::new();

        let id1 =
            get_or_create_session(&conn, "uuid-a", "laptop", "zsh", "user", &mut map).unwrap();
        let id1b =
            get_or_create_session(&conn, "uuid-a", "laptop", "zsh", "user", &mut map).unwrap();
        assert_eq!(id1, id1b);

        let id2 =
            get_or_create_session(&conn, "uuid-b", "laptop", "zsh", "user", &mut map).unwrap();
        assert_ne!(id1, id2);
    }

    #[test]
    fn test_upsert_session_reuses_existing_terminal_session() {
        let conn = open_memory().unwrap();

        let first = upsert_session(&conn, "sess-1", "laptop", "zsh", "user").unwrap();
        let second = upsert_session(&conn, "sess-1", "laptop", "zsh", "user").unwrap();

        assert_eq!(first, second);

        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sessions WHERE terminal = 'sess-1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn test_get_sessions() {
        let conn = open_memory().unwrap();
        upsert_session(&conn, "s1", "laptop", "zsh", "user").unwrap();
        upsert_session(&conn, "s2", "laptop", "bash", "user").unwrap();

        let sessions = get_sessions(&conn, None, 100).unwrap();
        assert_eq!(sessions.len(), 2);
    }

    #[test]
    fn test_get_events_with_filter() {
        let conn = open_memory().unwrap();
        let sid = upsert_session(&conn, "s1", "laptop", "zsh", "user").unwrap();
        let event = sample_shell_event();
        insert_event(&conn, sid, &event, 0, None).unwrap();

        let mut event2 = sample_shell_event();
        event2.command = "npm test".to_string();
        event2.cwd = PathBuf::from("/home/user/other");
        insert_event(&conn, sid, &event2, 0, None).unwrap();

        // All events
        let all = get_events(&conn, None, None, None, 100).unwrap();
        assert_eq!(all.len(), 2);

        // Filter by project
        let filtered = get_events(&conn, None, None, Some("project"), 100).unwrap();
        assert_eq!(filtered.len(), 1);
        assert_eq!(filtered[0].command, "cargo build");
    }

    #[test]
    fn test_raw_query() {
        let conn = open_memory().unwrap();
        let sid = upsert_session(&conn, "s1", "laptop", "zsh", "user").unwrap();
        let event = sample_shell_event();
        insert_event(&conn, sid, &event, 0, None).unwrap();

        let hits = raw_query(&conn, "cargo").unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].command, "cargo build");

        let empty = raw_query(&conn, "nonexistent").unwrap();
        assert!(empty.is_empty());
    }

    #[test]
    fn test_get_status() {
        let conn = open_memory().unwrap();
        let sid = upsert_session(&conn, "s1", "laptop", "zsh", "user").unwrap();
        let event = sample_shell_event();
        insert_event(&conn, sid, &event, 0, None).unwrap();

        let status = get_status(&conn).unwrap();
        assert_eq!(status.events_today, 1);
        assert_eq!(status.sessions_today, 1);
        assert_eq!(status.queue_depth, 1);
        assert_eq!(status.queue_failed, 0);
    }

    #[test]
    fn test_write_and_recover_fallback() {
        use crate::events::EventEnvelope;

        let dir = tempfile::tempdir().unwrap();
        let fallback_dir = dir.path().join("fallback");

        // Write 2 events to JSONL
        let event1 = EventEnvelope::shell(sample_shell_event());
        let mut event2_shell = sample_shell_event();
        event2_shell.command = "npm test".to_string();
        let event2 = EventEnvelope::shell(event2_shell);

        write_fallback_jsonl(&fallback_dir, &event1).unwrap();
        write_fallback_jsonl(&fallback_dir, &event2).unwrap();

        // Verify JSONL file exists
        let files = list_fallback_files(&fallback_dir).unwrap();
        assert_eq!(files.len(), 1);

        // Recover into SQLite
        let conn = open_memory().unwrap();
        let mut session_map = HashMap::new();
        let (recovered, errors) =
            recover_fallback_files(&conn, &fallback_dir, &mut session_map).unwrap();
        assert_eq!(recovered, 2);
        assert_eq!(errors, 0);

        // Verify events exist in DB
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM events", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 2);

        // Verify original file renamed to .done
        let remaining = list_fallback_files(&fallback_dir).unwrap();
        assert!(remaining.is_empty());

        let done_files: Vec<_> = std::fs::read_dir(&fallback_dir)
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| e.path().to_string_lossy().ends_with(".done"))
            .collect();
        assert_eq!(done_files.len(), 1);
    }

    #[test]
    fn test_recover_fallback_preserves_envelope_timestamp() {
        let dir = tempfile::tempdir().unwrap();
        let fallback_dir = dir.path().join("fallback");
        let conn = open_memory().unwrap();
        let mut session_map = HashMap::new();

        let mut envelope = EventEnvelope::shell(sample_shell_event());
        envelope.timestamp = chrono::DateTime::from_timestamp_millis(1_700_000_000_123).unwrap();
        write_fallback_jsonl(&fallback_dir, &envelope).unwrap();

        let (recovered, errors) =
            recover_fallback_files(&conn, &fallback_dir, &mut session_map).unwrap();
        assert_eq!((recovered, errors), (1, 0));

        let stored_timestamp: i64 = conn
            .query_row("SELECT timestamp FROM events LIMIT 1", [], |row| row.get(0))
            .unwrap();
        assert_eq!(stored_timestamp, envelope.timestamp.timestamp_millis());
    }

    #[test]
    fn test_insert_event_no_git_no_output() {
        // Exercises the None branches for git_state, stdout, stderr
        let conn = open_memory().unwrap();
        let sid = upsert_session(&conn, "sess-no-git", "laptop", "zsh", "user").unwrap();
        let event = ShellEvent {
            session_id: uuid::Uuid::new_v4(),
            command: "echo hello".to_string(),
            exit_code: 0,
            duration_ms: 10,
            cwd: PathBuf::from("/tmp"),
            hostname: "laptop".to_string(),
            shell: ShellKind::Bash,
            stdout: None,
            stderr: None,
            env_snapshot: HashMap::new(),
            git_state: None,
            redaction_count: 0,
            tool_name: None,
        };
        let eid = insert_event(&conn, sid, &event, 0, None).unwrap();
        assert!(eid > 0);

        // Verify NULLs stored correctly
        let (git_repo, stdout): (Option<String>, Option<String>) = conn
            .query_row(
                "SELECT git_repo, stdout FROM events WHERE id = ?1",
                [eid],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert!(git_repo.is_none());
        assert!(stdout.is_none());
    }

    #[test]
    fn test_insert_event_with_output() {
        // Exercises Some branches for stdout/stderr
        use crate::events::CapturedOutput;
        let conn = open_memory().unwrap();
        let sid = upsert_session(&conn, "sess-output", "laptop", "zsh", "user").unwrap();
        let event = ShellEvent {
            session_id: uuid::Uuid::new_v4(),
            command: "ls -la".to_string(),
            exit_code: 0,
            duration_ms: 5,
            cwd: PathBuf::from("/tmp"),
            hostname: "laptop".to_string(),
            shell: ShellKind::Zsh,
            stdout: Some(CapturedOutput {
                content: "file1\nfile2".to_string(),
                truncated: false,
                original_bytes: 11,
            }),
            stderr: Some(CapturedOutput {
                content: "warning: something".to_string(),
                truncated: true,
                original_bytes: 500,
            }),
            env_snapshot: HashMap::new(),
            git_state: Some(GitState {
                repo: Some("myrepo".to_string()),
                branch: Some("main".to_string()),
                commit: Some("abc1234".to_string()),
                is_dirty: true,
            }),
            redaction_count: 2,
            tool_name: None,
        };
        let eid = insert_event(&conn, sid, &event, 2, None).unwrap();
        let (stdout_val, stderr_val, stdout_trunc, stderr_trunc, redact): (
            String,
            String,
            i32,
            i32,
            u32,
        ) = conn
            .query_row(
                "SELECT stdout, stderr, stdout_truncated, stderr_truncated, redaction_count FROM events WHERE id = ?1",
                [eid],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?)),
            )
            .unwrap();
        assert_eq!(stdout_val, "file1\nfile2");
        assert_eq!(stderr_val, "warning: something");
        assert_eq!(stdout_trunc, 0);
        assert_eq!(stderr_trunc, 1);
        assert_eq!(redact, 2);
    }

    #[test]
    fn test_get_sessions_with_since_filter() {
        let conn = open_memory().unwrap();
        upsert_session(&conn, "s1", "laptop", "zsh", "user").unwrap();

        // A very large since_ms should return no sessions
        let future_ms = Utc::now().timestamp_millis() + 100_000;
        let sessions = get_sessions(&conn, Some(future_ms), 100).unwrap();
        assert!(sessions.is_empty());

        // since_ms of 0 should return all sessions
        let all = get_sessions(&conn, Some(0), 100).unwrap();
        assert_eq!(all.len(), 1);
    }

    #[test]
    fn test_get_events_with_session_filter() {
        let conn = open_memory().unwrap();
        let sid1 = upsert_session(&conn, "s1", "laptop", "zsh", "user").unwrap();
        let sid2 = upsert_session(&conn, "s2", "laptop", "bash", "user").unwrap();

        let event1 = sample_shell_event();
        insert_event(&conn, sid1, &event1, 0, None).unwrap();

        let mut event2 = sample_shell_event();
        event2.command = "npm test".to_string();
        insert_event(&conn, sid2, &event2, 0, None).unwrap();

        // Filter by session_id
        let filtered = get_events(&conn, Some(sid1), None, None, 100).unwrap();
        assert_eq!(filtered.len(), 1);
        assert_eq!(filtered[0].command, "cargo build");

        let filtered2 = get_events(&conn, Some(sid2), None, None, 100).unwrap();
        assert_eq!(filtered2.len(), 1);
        assert_eq!(filtered2[0].command, "npm test");
    }

    #[test]
    fn test_get_events_with_since_filter() {
        let conn = open_memory().unwrap();
        let sid = upsert_session(&conn, "s1", "laptop", "zsh", "user").unwrap();
        let event = sample_shell_event();
        insert_event(&conn, sid, &event, 0, None).unwrap();

        // Future since should return nothing
        let future_ms = Utc::now().timestamp_millis() + 100_000;
        let empty = get_events(&conn, None, Some(future_ms), None, 100).unwrap();
        assert!(empty.is_empty());

        // Past since should return the event
        let past = get_events(&conn, None, Some(0), None, 100).unwrap();
        assert_eq!(past.len(), 1);
    }

    #[test]
    fn test_get_events_combined_filters() {
        let conn = open_memory().unwrap();
        let sid = upsert_session(&conn, "s1", "laptop", "zsh", "user").unwrap();
        let event = sample_shell_event();
        insert_event(&conn, sid, &event, 0, None).unwrap();

        // session_id + since + project all combined
        let result = get_events(&conn, Some(sid), Some(0), Some("project"), 100).unwrap();
        assert_eq!(result.len(), 1);

        // Wrong session_id
        let result = get_events(&conn, Some(sid + 999), Some(0), Some("project"), 100).unwrap();
        assert!(result.is_empty());
    }

    #[test]
    fn test_get_entities_no_filter() {
        let conn = open_memory().unwrap();
        // Insert entities directly
        conn.execute(
            "INSERT INTO entities (type, name, canonical, first_seen, last_seen) VALUES (?1, ?2, ?3, ?4, ?5)",
            rusqlite::params!["tool", "cargo", "cargo", 1000, 2000],
        )
            .unwrap();
        conn.execute(
            "INSERT INTO entities (type, name, canonical, first_seen, last_seen) VALUES (?1, ?2, ?3, ?4, ?5)",
            rusqlite::params!["project", "hippo", "hippo", 1000, 2000],
        )
            .unwrap();

        let all = get_entities(&conn, None).unwrap();
        assert_eq!(all.len(), 2);
    }

    #[test]
    fn test_get_entities_with_type_filter() {
        let conn = open_memory().unwrap();
        conn.execute(
            "INSERT INTO entities (type, name, canonical, first_seen, last_seen) VALUES (?1, ?2, ?3, ?4, ?5)",
            rusqlite::params!["tool", "cargo", "cargo", 1000, 2000],
        )
            .unwrap();
        conn.execute(
            "INSERT INTO entities (type, name, canonical, first_seen, last_seen) VALUES (?1, ?2, ?3, ?4, ?5)",
            rusqlite::params!["project", "hippo", "hippo", 1000, 2000],
        )
            .unwrap();

        let tools = get_entities(&conn, Some("tool")).unwrap();
        assert_eq!(tools.len(), 1);
        assert_eq!(tools[0].name, "cargo");
        assert_eq!(tools[0].entity_type, "tool");

        let projects = get_entities(&conn, Some("project")).unwrap();
        assert_eq!(projects.len(), 1);
        assert_eq!(projects[0].name, "hippo");

        let empty = get_entities(&conn, Some("nonexistent")).unwrap();
        assert!(empty.is_empty());
    }

    #[test]
    fn test_list_fallback_files_nonexistent_dir() {
        let dir = tempfile::tempdir().unwrap();
        let nonexistent = dir.path().join("does_not_exist");
        let files = list_fallback_files(&nonexistent).unwrap();
        assert!(files.is_empty());
    }

    #[test]
    fn test_recover_fallback_with_malformed_json() {
        use std::io::Write;

        let dir = tempfile::tempdir().unwrap();
        let fallback_dir = dir.path().join("fallback");
        std::fs::create_dir_all(&fallback_dir).unwrap();

        // Write a file with some valid and some invalid lines
        let date = Utc::now().format("%Y-%m-%d");
        let file_path = fallback_dir.join(format!("{}.jsonl", date));
        let mut file = std::fs::File::create(&file_path).unwrap();

        // Valid event line
        let event = EventEnvelope::shell(sample_shell_event());
        let valid_json = serde_json::to_string(&event).unwrap();
        writeln!(file, "{}", valid_json).unwrap();

        // Invalid JSON line
        writeln!(file, "{{this is not valid json}}").unwrap();

        // Empty line (should be skipped, not counted as error)
        writeln!(file).unwrap();

        // Another invalid line
        writeln!(file, "also bad").unwrap();

        drop(file);

        let conn = open_memory().unwrap();
        let mut session_map = HashMap::new();
        let (recovered, errors) =
            recover_fallback_files(&conn, &fallback_dir, &mut session_map).unwrap();
        assert_eq!(recovered, 1);
        assert_eq!(errors, 2);
    }

    #[test]
    fn test_insert_event_with_env_snapshot() {
        let conn = open_memory().unwrap();
        let sid = upsert_session(&conn, "sess-env", "laptop", "zsh", "user").unwrap();

        // Create env snapshot
        let env: HashMap<String, String> =
            HashMap::from([("HOME".to_string(), "/home/test".to_string())]);
        let env_id = upsert_env_snapshot(&conn, &env).unwrap();

        let event = sample_shell_event();
        let eid = insert_event(&conn, sid, &event, 0, env_id).unwrap();

        // Verify env_snapshot_id stored
        let stored_env_id: Option<i64> = conn
            .query_row(
                "SELECT env_snapshot_id FROM events WHERE id = ?1",
                [eid],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(stored_env_id, env_id);
    }

    #[test]
    fn test_insert_event_at_is_atomic_under_queue_failure() {
        let conn = open_memory().unwrap();
        conn.execute_batch(
            "CREATE TRIGGER fail_queue_insert BEFORE INSERT ON enrichment_queue
             BEGIN SELECT RAISE(ABORT, 'injected failure'); END;",
        )
        .unwrap();

        let sid = upsert_session(&conn, "s1", "host", "zsh", "user").unwrap();
        let result = insert_event(&conn, sid, &sample_shell_event(), 0, None);

        assert!(result.is_err());
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
            .unwrap();
        assert_eq!(
            count, 0,
            "event row must not survive when queue insert fails"
        );
    }

    #[test]
    fn test_partial_fallback_recovery_preserves_failed_lines() {
        use std::io::Write;

        let dir = tempfile::tempdir().unwrap();
        let fallback_dir = dir.path().join("fallback");
        std::fs::create_dir_all(&fallback_dir).unwrap();

        let date = Utc::now().format("%Y-%m-%d");
        let file_path = fallback_dir.join(format!("{}.jsonl", date));
        let mut file = std::fs::File::create(&file_path).unwrap();

        // Valid event line 1
        let event1 = EventEnvelope::shell(sample_shell_event());
        let valid1 = serde_json::to_string(&event1).unwrap();
        writeln!(file, "{}", valid1).unwrap();

        // Malformed line in the middle
        writeln!(file, "NOT VALID JSON").unwrap();

        // Valid event line 2
        let mut shell2 = sample_shell_event();
        shell2.command = "ls -la".to_string();
        let event2 = EventEnvelope::shell(shell2);
        let valid2 = serde_json::to_string(&event2).unwrap();
        writeln!(file, "{}", valid2).unwrap();

        drop(file);

        let conn = open_memory().unwrap();
        let mut session_map = HashMap::new();
        let (recovered, errors) =
            recover_fallback_files(&conn, &fallback_dir, &mut session_map).unwrap();

        // Two valid events stored, one malformed line counted as error
        assert_eq!(recovered, 2);
        assert_eq!(errors, 1);

        // Valid events are in the database
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 2);

        // Original .jsonl file is gone
        assert!(!file_path.exists(), ".jsonl file should have been renamed");

        // .partial file exists (not .done) because of the failed line
        let partial_path = file_path.with_extension("jsonl.partial");
        assert!(
            partial_path.exists(),
            "file should be renamed to .partial when some lines fail"
        );

        // .partial files are NOT picked up by list_fallback_files
        let pending = list_fallback_files(&fallback_dir).unwrap();
        assert!(
            pending.is_empty(),
            ".partial files must not be collected for re-recovery"
        );
    }

    #[test]
    fn test_open_db_version_matches_schema() {
        let dir = tempfile::tempdir().unwrap();
        let conn = open_db(&dir.path().join("test.db")).unwrap();
        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, EXPECTED_VERSION);
    }

    /// v10→v11 migration: add `resolved_at` and `clean_ticks` columns,
    /// preserve existing rows, replace the partial index predicate so
    /// resolved alarms no longer suppress new ones via rate-limit.
    #[test]
    fn test_migrate_v10_to_v11_adds_auto_resolve_columns() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("test.db");

        // Build a v10-shaped DB: v9-shaped capture_alarms + user_version=10.
        {
            let conn = rusqlite::Connection::open(&db_path).unwrap();
            conn.execute_batch(
                "CREATE TABLE capture_alarms (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    invariant_id TEXT    NOT NULL,
                    raised_at    INTEGER NOT NULL,
                    details_json TEXT    NOT NULL,
                    acked_at     INTEGER,
                    ack_note     TEXT
                 );
                 CREATE INDEX idx_capture_alarms_invariant_active
                     ON capture_alarms (invariant_id, acked_at)
                     WHERE acked_at IS NULL;
                 INSERT INTO capture_alarms (invariant_id, raised_at, details_json)
                     VALUES ('I-1', 1700000000000, '{\"source\":\"shell\"}');
                 PRAGMA user_version = 10;",
            )
            .unwrap();
        }

        // Run migrations.
        let conn = open_db(&db_path).unwrap();

        // Schema is at EXPECTED_VERSION (v11→v12 also runs since the range
        // covers all versions <= 11).
        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, EXPECTED_VERSION);

        // Pre-existing row preserved.
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM capture_alarms", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);

        // New columns exist with expected defaults on the migrated row.
        let (resolved_at, clean_ticks): (Option<i64>, i64) = conn
            .query_row(
                "SELECT resolved_at, clean_ticks FROM capture_alarms",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert!(resolved_at.is_none());
        assert_eq!(clean_ticks, 0);

        // Partial index predicate must include resolved_at IS NULL — verify
        // by introspecting sqlite_master for the index DDL.
        let ddl: String = conn
            .query_row(
                "SELECT sql FROM sqlite_master
                 WHERE type='index' AND name='idx_capture_alarms_invariant_active'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert!(
            ddl.contains("resolved_at IS NULL"),
            "v11 index must filter resolved alarms; got: {ddl}"
        );
    }

    /// A previous v10→v11 attempt may have crashed after adding `resolved_at`
    /// but before adding `clean_ticks` (or before bumping user_version).
    /// `add_column_if_missing` must complete the migration on re-run without
    /// erroring on the column that already exists.
    #[test]
    fn test_migrate_v10_to_v11_recovers_from_partial_success() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("test.db");

        // v10-shaped table with `resolved_at` already added (simulating
        // a crash mid-migration). user_version still 10 so open_db re-runs
        // the v10→v11 block.
        {
            let conn = rusqlite::Connection::open(&db_path).unwrap();
            conn.execute_batch(
                "CREATE TABLE capture_alarms (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    invariant_id TEXT    NOT NULL,
                    raised_at    INTEGER NOT NULL,
                    details_json TEXT    NOT NULL,
                    acked_at     INTEGER,
                    ack_note     TEXT,
                    resolved_at  INTEGER
                 );
                 PRAGMA user_version = 10;",
            )
            .unwrap();
        }

        // Re-run open_db — must complete the migration without erroring.
        let conn = open_db(&db_path).unwrap();

        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, EXPECTED_VERSION);

        // Both columns must now exist.
        let cols: Vec<String> = conn
            .prepare("SELECT name FROM pragma_table_info('capture_alarms')")
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert!(cols.contains(&"resolved_at".to_string()));
        assert!(cols.contains(&"clean_ticks".to_string()));

        // CHECK constraint enforces clean_ticks >= 0.
        let result = conn.execute(
            "INSERT INTO capture_alarms (invariant_id, raised_at, details_json, clean_ticks)
             VALUES ('I-1', 0, '{}', -1)",
            [],
        );
        assert!(
            result.is_err(),
            "CHECK (clean_ticks >= 0) must reject negative values"
        );
    }

    /// v11→v12 migration: add `content_hash` and `last_enriched_content_hash`
    /// columns to `claude_sessions`. Pre-existing rows must survive with NULL
    /// values in both new columns, and the schema must land at v12.
    #[test]
    fn test_migrate_v11_to_v12_adds_content_hash_columns() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("test.db");

        // Build a v11-shaped DB: full claude_sessions column list as it
        // exists today (no content_hash / last_enriched_content_hash), plus
        // the capture_alarms and capture_alarms index that v11 introduced.
        // user_version = 11 so open_db runs only the v11→v12 block.
        {
            let conn = rusqlite::Connection::open(&db_path).unwrap();
            conn.execute_batch(
                "CREATE TABLE claude_sessions (
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
                    probe_tag TEXT,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch('now','subsec') * 1000),
                    UNIQUE (session_id, segment_index)
                 );
                 INSERT INTO claude_sessions
                     (session_id, project_dir, cwd, segment_index, start_time,
                      end_time, summary_text, message_count, source_file)
                 VALUES
                     ('sess-1', '/proj', '/proj', 0, 1700000000000,
                      1700000001000, 'hello world', 4, '/path/to/file.jsonl');
                 PRAGMA user_version = 11;",
            )
            .unwrap();
        }

        // Run migrations.
        let conn = open_db(&db_path).unwrap();

        // Schema lands at EXPECTED_VERSION (the v12→v13 block also runs
        // since the range covers v11..=v12).
        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, EXPECTED_VERSION);

        // Pre-existing row is preserved.
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM claude_sessions", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);

        // Both new columns exist and are NULL on the migrated row.
        let (content_hash, last_enriched): (Option<String>, Option<String>) = conn
            .query_row(
                "SELECT content_hash, last_enriched_content_hash FROM claude_sessions",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        assert!(
            content_hash.is_none(),
            "content_hash must be NULL on legacy row"
        );
        assert!(
            last_enriched.is_none(),
            "last_enriched_content_hash must be NULL on legacy row"
        );
    }

    /// A previous v11→v12 attempt may have crashed after adding `content_hash`
    /// but before adding `last_enriched_content_hash` (or before bumping
    /// user_version). `add_column_if_missing` must complete the migration on
    /// re-run without erroring on the column that already exists.
    #[test]
    fn test_migrate_v11_to_v12_recovers_from_partial_success() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("test.db");

        // v11-shaped table with `content_hash` already added (simulating a
        // crash mid-migration). user_version still 11 so open_db re-runs the
        // v11→v12 block.
        {
            let conn = rusqlite::Connection::open(&db_path).unwrap();
            conn.execute_batch(
                "CREATE TABLE claude_sessions (
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
                    probe_tag TEXT,
                    content_hash TEXT,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch('now','subsec') * 1000),
                    UNIQUE (session_id, segment_index)
                 );
                 PRAGMA user_version = 11;",
            )
            .unwrap();
        }

        // Re-run open_db — must complete the migration without erroring.
        let conn = open_db(&db_path).unwrap();

        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, EXPECTED_VERSION);

        // Both columns must now exist.
        let cols: Vec<String> = conn
            .prepare("SELECT name FROM pragma_table_info('claude_sessions')")
            .unwrap()
            .query_map([], |r| r.get(0))
            .unwrap()
            .collect::<rusqlite::Result<Vec<_>>>()
            .unwrap();
        assert!(cols.contains(&"content_hash".to_string()));
        assert!(cols.contains(&"last_enriched_content_hash".to_string()));
    }

    /// v12→v13 migration: extend the entities.type CHECK list with
    /// 'env_var'. The migration recreates the table because SQLite cannot
    /// alter a CHECK constraint in place. Existing rows must survive the
    /// recreation, and new env_var inserts must succeed afterwards.
    #[test]
    fn test_migrate_v12_to_v13_extends_entities_check_with_env_var() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("test.db");

        // Build a v12-shaped entities table (the v8 CHECK list, no env_var)
        // and seed one row of every existing type so we can prove the
        // recreation preserved them all.
        {
            let conn = rusqlite::Connection::open(&db_path).unwrap();
            conn.execute_batch(
                "CREATE TABLE entities (
                    id INTEGER PRIMARY KEY,
                    type TEXT NOT NULL CHECK (type IN (
                        'project', 'file', 'tool', 'service', 'repo', 'host', 'person',
                        'concept', 'domain'
                    )),
                    name TEXT NOT NULL,
                    canonical TEXT,
                    metadata TEXT,
                    first_seen INTEGER NOT NULL DEFAULT 1700000000000,
                    last_seen INTEGER NOT NULL DEFAULT 1700000000000,
                    created_at INTEGER NOT NULL DEFAULT 1700000000000,
                    UNIQUE (type, canonical)
                 );
                 INSERT INTO entities (id, type, name, canonical) VALUES
                    (1, 'project', 'hippo', 'hippo'),
                    (2, 'file', '/tmp/foo.py', '/tmp/foo.py'),
                    (3, 'tool', 'pytest', 'pytest'),
                    (4, 'service', 'sqlite-vec', 'sqlite-vec'),
                    (5, 'concept', 'database is locked', 'database is locked'),
                    (6, 'domain', 'docs.rs', 'docs.rs');
                 PRAGMA user_version = 12;",
            )
            .unwrap();
        }

        let conn = open_db(&db_path).unwrap();

        // Schema lands at v13.
        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, EXPECTED_VERSION);

        // All six legacy rows survived the table recreation.
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM entities", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 6, "every legacy entity row must survive migration");

        // env_var inserts now succeed (would fail with CHECK constraint
        // error pre-migration).
        conn.execute(
            "INSERT INTO entities (type, name, canonical) \
             VALUES ('env_var', 'HIPPO_PROJECT_ROOTS', 'HIPPO_PROJECT_ROOTS')",
            [],
        )
        .expect("post-migration env_var insert must succeed");

        // The two indexes are recreated on the renamed table.
        let idx_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master \
                 WHERE type='index' AND tbl_name='entities' \
                 AND name IN ('idx_entities_type_name', 'idx_entities_canonical')",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(idx_count, 2, "both entity indexes must be recreated");
    }

    /// A previous v12→v13 attempt may have crashed mid-migration, leaving
    /// `entities_new` populated but `entities` not yet dropped. The migration
    /// must drop the half-built `entities_new` and start over cleanly.
    #[test]
    fn test_migrate_v12_to_v13_recovers_from_partial_success() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("test.db");

        // v12-shaped entities table plus a half-populated entities_new from a
        // crashed prior attempt. user_version is still 12 so open_db re-runs
        // the migration block.
        {
            let conn = rusqlite::Connection::open(&db_path).unwrap();
            conn.execute_batch(
                "CREATE TABLE entities (
                    id INTEGER PRIMARY KEY,
                    type TEXT NOT NULL CHECK (type IN (
                        'project', 'file', 'tool', 'service', 'repo', 'host', 'person',
                        'concept', 'domain'
                    )),
                    name TEXT NOT NULL,
                    canonical TEXT,
                    metadata TEXT,
                    first_seen INTEGER NOT NULL DEFAULT 1700000000000,
                    last_seen INTEGER NOT NULL DEFAULT 1700000000000,
                    created_at INTEGER NOT NULL DEFAULT 1700000000000,
                    UNIQUE (type, canonical)
                 );
                 INSERT INTO entities (id, type, name, canonical) VALUES
                    (1, 'tool', 'pytest', 'pytest');
                 -- Half-populated entities_new from a crashed prior attempt.
                 CREATE TABLE entities_new (
                    id INTEGER PRIMARY KEY,
                    type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    canonical TEXT,
                    metadata TEXT,
                    first_seen INTEGER NOT NULL DEFAULT 0,
                    last_seen INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL DEFAULT 0,
                    UNIQUE (type, canonical)
                 );
                 INSERT INTO entities_new (id, type, name, canonical)
                     VALUES (1, 'tool', 'pytest', 'pytest');
                 PRAGMA user_version = 12;",
            )
            .unwrap();
        }

        // Migration must complete cleanly despite the half-baked entities_new.
        let conn = open_db(&db_path).unwrap();

        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, EXPECTED_VERSION);

        // Single legacy row survived; entities_new is gone (renamed).
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM entities", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);

        // entities_new no longer exists at the end of a successful run.
        let leftover: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='entities_new'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(leftover, 0, "entities_new must not survive the migration");
    }

    #[test]
    fn test_open_db_rejects_wrong_version() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("test.db");
        {
            let conn = rusqlite::Connection::open(&db_path).unwrap();
            conn.execute_batch("PRAGMA user_version = 99").unwrap();
        }
        assert!(open_db(&db_path).is_err());
    }

    #[test]
    fn test_open_db_migrates_v1_to_v2() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("test.db");
        // Create a v1 database — minimal schema WITHOUT envelope_id
        {
            let conn = rusqlite::Connection::open(&db_path).unwrap();
            conn.execute_batch(
                "CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY,
                    start_time INTEGER NOT NULL,
                    end_time INTEGER,
                    terminal TEXT,
                    shell TEXT NOT NULL,
                    hostname TEXT NOT NULL,
                    username TEXT NOT NULL,
                    summary TEXT,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch('now','subsec') * 1000)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    session_id INTEGER NOT NULL REFERENCES sessions(id),
                    timestamp INTEGER NOT NULL,
                    command TEXT NOT NULL,
                    stdout TEXT, stderr TEXT,
                    stdout_truncated INTEGER DEFAULT 0, stderr_truncated INTEGER DEFAULT 0,
                    exit_code INTEGER,
                    duration_ms INTEGER NOT NULL,
                    cwd TEXT NOT NULL, hostname TEXT NOT NULL, shell TEXT NOT NULL,
                    git_repo TEXT, git_branch TEXT, git_commit TEXT, git_dirty INTEGER,
                    env_snapshot_id INTEGER,
                    enriched INTEGER NOT NULL DEFAULT 0,
                    redaction_count INTEGER NOT NULL DEFAULT 0,
                    archived_at INTEGER,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch('now','subsec') * 1000)
                );
                CREATE TABLE IF NOT EXISTS enrichment_queue (
                    id INTEGER PRIMARY KEY,
                    event_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    priority INTEGER NOT NULL DEFAULT 0,
                    retries INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    last_error TEXT,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch('now','subsec') * 1000),
                    updated_at INTEGER NOT NULL DEFAULT (unixepoch('now','subsec') * 1000)
                );
                PRAGMA user_version = 1;",
            )
            .unwrap();
        }
        // open_db should migrate through every step to the latest version
        let conn = open_db(&db_path).unwrap();
        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, EXPECTED_VERSION);
        // Verify envelope_id column exists by inserting with it
        let sid = upsert_session(&conn, "mig-test", "host", "zsh", "user").unwrap();
        let eid = insert_event_at(
            &conn,
            sid,
            &sample_shell_event(),
            0,
            0,
            None,
            Some("test-envelope-id"),
            None,
        )
        .unwrap();
        assert!(eid > 0);
    }

    #[test]
    fn test_duplicate_envelope_id_is_ignored() {
        let conn = open_memory().unwrap();
        let sid = upsert_session(&conn, "dedup-test", "host", "zsh", "user").unwrap();

        let eid1 = insert_event_at(
            &conn,
            sid,
            &sample_shell_event(),
            1000,
            0,
            None,
            Some("same-envelope"),
            None,
        )
        .unwrap();
        assert!(eid1 > 0);

        // Second insert with same envelope_id should be silently ignored
        let eid2 = insert_event_at(
            &conn,
            sid,
            &sample_shell_event(),
            2000,
            0,
            None,
            Some("same-envelope"),
            None,
        )
        .unwrap();
        assert_eq!(eid2, -1, "duplicate should return -1");

        // Only one event in the table
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);

        // Only one enrichment queue entry
        let q_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM enrichment_queue", [], |r| r.get(0))
            .unwrap();
        assert_eq!(q_count, 1);
    }

    #[test]
    fn test_browser_events_table_exists_after_open() {
        let dir = tempfile::tempdir().unwrap();
        let conn = open_db(&dir.path().join("test.db")).unwrap();

        let browser_tables = [
            "browser_events",
            "browser_enrichment_queue",
            "knowledge_node_browser_events",
        ];
        for table in &browser_tables {
            let exists: bool = conn
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
                    [table],
                    |row| row.get(0),
                )
                .unwrap();
            assert!(exists, "table '{}' should exist after fresh open_db", table);
        }

        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, EXPECTED_VERSION);
    }

    #[test]
    fn test_migration_v3_to_v4() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("test.db");

        // Create a v3 database with the minimum schema needed
        {
            let conn = rusqlite::Connection::open(&db_path).unwrap();
            conn.execute_batch(
                "CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY,
                    start_time INTEGER NOT NULL,
                    end_time INTEGER,
                    terminal TEXT,
                    shell TEXT NOT NULL,
                    hostname TEXT NOT NULL,
                    username TEXT NOT NULL,
                    summary TEXT,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch('now','subsec') * 1000)
                );
                CREATE TABLE IF NOT EXISTS env_snapshots (
                    id INTEGER PRIMARY KEY,
                    content_hash TEXT NOT NULL UNIQUE,
                    env_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch('now','subsec') * 1000)
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY,
                    session_id INTEGER NOT NULL REFERENCES sessions(id),
                    timestamp INTEGER NOT NULL,
                    command TEXT NOT NULL,
                    stdout TEXT, stderr TEXT,
                    stdout_truncated INTEGER DEFAULT 0, stderr_truncated INTEGER DEFAULT 0,
                    exit_code INTEGER,
                    duration_ms INTEGER NOT NULL,
                    cwd TEXT NOT NULL, hostname TEXT NOT NULL, shell TEXT NOT NULL,
                    git_repo TEXT, git_branch TEXT, git_commit TEXT, git_dirty INTEGER,
                    env_snapshot_id INTEGER REFERENCES env_snapshots(id),
                    envelope_id TEXT,
                    enriched INTEGER NOT NULL DEFAULT 0,
                    redaction_count INTEGER NOT NULL DEFAULT 0,
                    archived_at INTEGER,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch('now','subsec') * 1000)
                );
                CREATE TABLE IF NOT EXISTS entities (
                    id INTEGER PRIMARY KEY,
                    type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    canonical TEXT,
                    metadata TEXT,
                    first_seen INTEGER NOT NULL DEFAULT (unixepoch('now','subsec') * 1000),
                    last_seen INTEGER NOT NULL DEFAULT (unixepoch('now','subsec') * 1000),
                    created_at INTEGER NOT NULL DEFAULT (unixepoch('now','subsec') * 1000),
                    UNIQUE (type, canonical)
                );
                CREATE TABLE IF NOT EXISTS knowledge_nodes (
                    id INTEGER PRIMARY KEY,
                    uuid TEXT NOT NULL UNIQUE,
                    content TEXT NOT NULL,
                    embed_text TEXT NOT NULL,
                    node_type TEXT NOT NULL DEFAULT 'observation',
                    outcome TEXT,
                    tags TEXT,
                    enrichment_model TEXT,
                    enrichment_version INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch('now','subsec') * 1000),
                    updated_at INTEGER NOT NULL DEFAULT (unixepoch('now','subsec') * 1000)
                );
                CREATE TABLE IF NOT EXISTS claude_sessions (
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
                    created_at INTEGER NOT NULL DEFAULT (unixepoch('now','subsec') * 1000),
                    UNIQUE (session_id, segment_index)
                );
                PRAGMA user_version = 3;",
            )
            .unwrap();
        }

        // open_db should migrate v3 through to the latest version
        let conn = open_db(&db_path).unwrap();
        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, EXPECTED_VERSION);

        // Verify browser tables exist
        let browser_tables = [
            "browser_events",
            "browser_enrichment_queue",
            "knowledge_node_browser_events",
        ];
        for table in &browser_tables {
            let exists: bool = conn
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
                    [table],
                    |row| row.get(0),
                )
                .unwrap();
            assert!(
                exists,
                "table '{}' should exist after v3→v4 migration",
                table
            );
        }

        // Close and re-open — should remain at current version without error
        drop(conn);
        let conn2 = open_db(&db_path).unwrap();
        let v2: i64 = conn2
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v2, EXPECTED_VERSION);
    }

    fn sample_browser_event() -> BrowserEvent {
        BrowserEvent {
            url: "https://docs.rs/serde/latest/serde/".to_string(),
            title: "serde - Rust".to_string(),
            domain: "docs.rs".to_string(),
            dwell_ms: 45000,
            scroll_depth: 0.75,
            extracted_text: Some("Serde is a framework for serializing...".to_string()),
            search_query: Some("rust serde tutorial".to_string()),
            referrer: Some("https://www.google.com/".to_string()),
            content_hash: None, // will be computed by insert_browser_event
        }
    }

    #[test]
    fn test_insert_browser_event() {
        let dir = tempfile::tempdir().unwrap();
        let conn = open_db(&dir.path().join("test.db")).unwrap();

        let event = sample_browser_event();
        let timestamp_ms = chrono::Utc::now().timestamp_millis();
        let event_id =
            insert_browser_event(&conn, &event, timestamp_ms, Some("browser-env-1"), None).unwrap();
        assert!(event_id > 0);

        // Verify it's stored in browser_events with correct fields
        let mut stmt = conn
            .prepare(
                "SELECT url, title, domain, dwell_ms, scroll_depth, extracted_text,
                        search_query, referrer, content_hash, envelope_id
                 FROM browser_events WHERE id = ?1",
            )
            .unwrap();
        let mut rows = stmt.query([event_id]).unwrap();
        let row = rows.next().unwrap().expect("should have one row");

        assert_eq!(
            row.get::<_, String>(0).unwrap(),
            "https://docs.rs/serde/latest/serde/"
        );
        assert_eq!(row.get::<_, String>(1).unwrap(), "serde - Rust");
        assert_eq!(row.get::<_, String>(2).unwrap(), "docs.rs");
        assert_eq!(row.get::<_, i64>(3).unwrap(), 45000);
        assert!((row.get::<_, f64>(4).unwrap() - 0.75).abs() < f64::EPSILON);
        assert_eq!(
            row.get::<_, Option<String>>(5).unwrap().as_deref(),
            Some("Serde is a framework for serializing...")
        );
        assert_eq!(
            row.get::<_, Option<String>>(6).unwrap().as_deref(),
            Some("rust serde tutorial")
        );
        assert_eq!(
            row.get::<_, Option<String>>(7).unwrap().as_deref(),
            Some("https://www.google.com/")
        );
        assert_eq!(
            row.get::<_, Option<String>>(9).unwrap().as_deref(),
            Some("browser-env-1")
        );

        // Verify content_hash was computed from extracted_text via SHA256
        let expected_hash = {
            let mut hasher = Sha256::new();
            hasher.update(b"Serde is a framework for serializing...");
            hasher
                .finalize()
                .iter()
                .map(|b| format!("{:02x}", b))
                .collect::<String>()
        };
        let content_hash = row.get::<_, Option<String>>(8).unwrap();
        assert_eq!(content_hash.as_deref(), Some(expected_hash.as_str()));

        // Verify browser_enrichment_queue entry created with status 'pending'
        let (queue_event_id, status): (i64, String) = conn
            .query_row(
                "SELECT browser_event_id, status FROM browser_enrichment_queue
                 WHERE browser_event_id = ?1",
                [event_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(queue_event_id, event_id);
        assert_eq!(status, "pending");
    }

    #[test]
    fn test_insert_browser_event_no_extracted_text() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("test.db");
        let conn = open_db(&db_path).unwrap();

        let event = BrowserEvent {
            url: "https://github.com/test".to_string(),
            title: "Test".to_string(),
            domain: "github.com".to_string(),
            dwell_ms: 5000,
            scroll_depth: 0.5,
            extracted_text: None,
            search_query: None,
            referrer: None,
            content_hash: None,
        };

        let id =
            insert_browser_event(&conn, &event, 1711900000000, Some("no-text-1"), None).unwrap();
        assert!(id > 0);

        let hash: Option<String> = conn
            .query_row(
                "SELECT content_hash FROM browser_events WHERE id = ?",
                [id],
                |row| row.get(0),
            )
            .unwrap();
        assert!(
            hash.is_none(),
            "content_hash should be None when extracted_text is None"
        );
    }

    #[test]
    fn test_insert_browser_event_no_envelope_id() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("test.db");
        let conn = open_db(&db_path).unwrap();

        let event = BrowserEvent {
            url: "https://docs.rs/test".to_string(),
            title: "Test".to_string(),
            domain: "docs.rs".to_string(),
            dwell_ms: 8000,
            scroll_depth: 0.3,
            extracted_text: None,
            search_query: None,
            referrer: None,
            content_hash: None,
        };

        let id = insert_browser_event(&conn, &event, 1711900000000, None, None).unwrap();
        assert!(id > 0);
    }

    #[test]
    fn test_insert_browser_event_dedup() {
        let dir = tempfile::tempdir().unwrap();
        let conn = open_db(&dir.path().join("test.db")).unwrap();

        let event = sample_browser_event();
        let timestamp_ms = chrono::Utc::now().timestamp_millis();

        let eid1 = insert_browser_event(&conn, &event, timestamp_ms, Some("dup-browser-env"), None)
            .unwrap();
        assert!(eid1 > 0);

        // Second insert with same envelope_id should return -1
        let eid2 = insert_browser_event(
            &conn,
            &event,
            timestamp_ms + 1000,
            Some("dup-browser-env"),
            None,
        )
        .unwrap();
        assert_eq!(eid2, -1, "duplicate envelope_id should return -1");

        // Only one row in browser_events
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM browser_events", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);

        // Only one enrichment queue entry
        let q_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM browser_enrichment_queue", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(q_count, 1);
    }

    #[test]
    fn test_source_health_table_exists() {
        let dir = tempfile::tempdir().unwrap();
        let db_path = dir.path().join("test.db");
        let conn = open_db(&db_path).unwrap();

        // Each expected source must have a pre-seeded row; use per-source
        // assertions so adding a new source doesn't silently break this test.
        for expected_source in &["shell", "claude-tool", "claude-session", "browser"] {
            let exists: i64 = conn
                .query_row(
                    "SELECT COUNT(*) FROM source_health WHERE source = ?1",
                    [expected_source],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(
                exists, 1,
                "source_health missing pre-seeded row for '{expected_source}'"
            );
        }

        // Verify key columns exist via PRAGMA table_info
        let mut stmt = conn
            .prepare("SELECT name FROM pragma_table_info('source_health')")
            .unwrap();
        let col_names: Vec<String> = stmt
            .query_map([], |row| row.get(0))
            .unwrap()
            .filter_map(|r| r.ok())
            .collect();

        for expected_col in &[
            "source",
            "last_event_ts",
            "consecutive_failures",
            "updated_at",
        ] {
            assert!(
                col_names.iter().any(|c| c == expected_col),
                "column '{}' should exist in source_health; found: {:?}",
                expected_col,
                col_names
            );
        }
    }
}

pub mod watchlist {
    use anyhow::Result;
    use rusqlite::{Connection, params};

    #[derive(Debug, Clone, PartialEq, Eq)]
    pub struct WatchEntry {
        pub sha: String,
        pub repo: String,
        pub created_at: i64,
        pub expires_at: i64,
        pub terminal_status: Option<String>,
        pub notified: bool,
    }

    fn from_row(r: &rusqlite::Row<'_>) -> rusqlite::Result<WatchEntry> {
        Ok(WatchEntry {
            sha: r.get(0)?,
            repo: r.get(1)?,
            created_at: r.get(2)?,
            expires_at: r.get(3)?,
            terminal_status: r.get(4)?,
            notified: r.get::<_, i32>(5)? != 0,
        })
    }

    pub fn upsert(
        conn: &Connection,
        sha: &str,
        repo: &str,
        created_at: i64,
        expires_at: i64,
    ) -> Result<()> {
        conn.execute(
            "INSERT INTO sha_watchlist (sha, repo, created_at, expires_at)
             VALUES (?1, ?2, ?3, ?4)
             ON CONFLICT(sha, repo) DO UPDATE SET expires_at = excluded.expires_at",
            params![sha, repo, created_at, expires_at],
        )?;
        Ok(())
    }

    pub fn list_active(conn: &Connection, now_ms: i64) -> Result<Vec<WatchEntry>> {
        let mut stmt = conn.prepare(
            "SELECT sha, repo, created_at, expires_at, terminal_status, notified
             FROM sha_watchlist
             WHERE expires_at > ?1 AND terminal_status IS NULL
             ORDER BY created_at DESC",
        )?;
        let rows = stmt
            .query_map([now_ms], from_row)?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    pub fn mark_terminal(conn: &Connection, sha: &str, repo: &str, status: &str) -> Result<bool> {
        let n = conn.execute(
            "UPDATE sha_watchlist SET terminal_status = ?1
             WHERE sha = ?2 AND repo = ?3",
            params![status, sha, repo],
        )?;
        Ok(n > 0)
    }

    pub fn pending_notifications(conn: &Connection, now_ms: i64) -> Result<Vec<WatchEntry>> {
        let mut stmt = conn.prepare(
            "SELECT sha, repo, created_at, expires_at, terminal_status, notified
             FROM sha_watchlist
             WHERE terminal_status IN ('failure', 'cancelled')
               AND notified = 0
               AND expires_at > ?1",
        )?;
        let rows = stmt
            .query_map([now_ms], from_row)?
            .collect::<Result<Vec<_>, _>>()?;
        Ok(rows)
    }

    pub fn mark_notified(conn: &Connection, sha: &str, repo: &str) -> Result<()> {
        conn.execute(
            "UPDATE sha_watchlist SET notified = 1 WHERE sha = ?1 AND repo = ?2",
            params![sha, repo],
        )?;
        Ok(())
    }

    /// Delete watchlist rows that are expired, terminal, and already notified.
    pub fn cleanup_expired(conn: &Connection, now_ms: i64) -> Result<usize> {
        let n = conn.execute(
            "DELETE FROM sha_watchlist
             WHERE expires_at < ?1 AND terminal_status IS NOT NULL AND notified = 1",
            [now_ms],
        )?;
        Ok(n)
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        fn open_test_db() -> rusqlite::Connection {
            super::super::open_memory().unwrap()
        }

        #[test]
        fn upsert_creates_row() {
            let conn = open_test_db();
            let now = 1_000_000i64;
            upsert(&conn, "abc123", "me/repo", now, now + 1_200_000).unwrap();
            let count: i64 = conn
                .query_row(
                    "SELECT count(*) FROM sha_watchlist WHERE sha='abc123' AND repo='me/repo'",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(count, 1);
        }

        #[test]
        fn upsert_updates_expires_at_on_conflict() {
            let conn = open_test_db();
            let now = 1_000_000i64;
            upsert(&conn, "abc123", "me/repo", now, now + 600_000).unwrap();
            upsert(&conn, "abc123", "me/repo", now, now + 1_200_000).unwrap();
            let expires: i64 = conn
                .query_row(
                    "SELECT expires_at FROM sha_watchlist WHERE sha='abc123'",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(
                expires,
                now + 1_200_000,
                "expires_at should be updated on conflict"
            );
            let count: i64 = conn
                .query_row("SELECT count(*) FROM sha_watchlist", [], |r| r.get(0))
                .unwrap();
            assert_eq!(count, 1, "should remain one row");
        }

        #[test]
        fn list_active_returns_non_expired_non_terminal_entries() {
            let conn = open_test_db();
            let now = 1_000_000i64;
            let future = now + 3_600_000;
            let past = now - 1;
            upsert(&conn, "sha_live", "me/repo", now, future).unwrap();
            upsert(&conn, "sha_expired", "me/repo", now, past).unwrap();
            let active = list_active(&conn, now).unwrap();
            assert_eq!(active.len(), 1);
            assert_eq!(active[0].sha, "sha_live");
            assert_eq!(active[0].repo, "me/repo");
            assert_eq!(active[0].terminal_status, None);
            assert!(!active[0].notified);
        }

        #[test]
        fn list_active_excludes_terminal_entries() {
            let conn = open_test_db();
            let now = 1_000_000i64;
            let future = now + 3_600_000;
            upsert(&conn, "sha_pending", "me/repo", now, future).unwrap();
            upsert(&conn, "sha_done", "me/repo", now, future).unwrap();
            mark_terminal(&conn, "sha_done", "me/repo", "success").unwrap();
            let active = list_active(&conn, now).unwrap();
            assert_eq!(active.len(), 1);
            assert_eq!(active[0].sha, "sha_pending");
        }

        #[test]
        fn mark_terminal_returns_true_when_row_exists() {
            let conn = open_test_db();
            let now = 1_000_000i64;
            upsert(&conn, "sha_x", "me/repo", now, now + 9999).unwrap();
            let updated = mark_terminal(&conn, "sha_x", "me/repo", "success").unwrap();
            assert!(updated, "should return true when a row is updated");
            let status: Option<String> = conn
                .query_row(
                    "SELECT terminal_status FROM sha_watchlist WHERE sha='sha_x'",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(status.as_deref(), Some("success"));
        }

        #[test]
        fn mark_terminal_returns_false_when_no_matching_row() {
            let conn = open_test_db();
            let updated = mark_terminal(&conn, "nonexistent", "me/repo", "failure").unwrap();
            assert!(!updated, "should return false when no row was updated");
        }

        #[test]
        fn pending_notifications_returns_unnotified_failures_and_cancellations() {
            let conn = open_test_db();
            let now = 1_000_000i64;
            let ttl = now + 9999;
            upsert(&conn, "sha_fail", "me/repo", now, ttl).unwrap();
            upsert(&conn, "sha_cancel", "me/repo", now, ttl).unwrap();
            upsert(&conn, "sha_success", "me/repo", now, ttl).unwrap();
            upsert(&conn, "sha_pending", "me/repo", now, ttl).unwrap();
            mark_terminal(&conn, "sha_fail", "me/repo", "failure").unwrap();
            mark_terminal(&conn, "sha_cancel", "me/repo", "cancelled").unwrap();
            mark_terminal(&conn, "sha_success", "me/repo", "success").unwrap();
            let pending = pending_notifications(&conn, now + 1).unwrap();
            assert_eq!(pending.len(), 2);
            let shas: Vec<&str> = pending.iter().map(|e| e.sha.as_str()).collect();
            assert!(shas.contains(&"sha_fail"));
            assert!(shas.contains(&"sha_cancel"));
        }

        #[test]
        fn pending_notifications_excludes_expired() {
            let conn = open_test_db();
            let now = 1_000_000i64;
            let ttl = now + 9999;
            upsert(&conn, "sha_fail", "me/repo", now, ttl).unwrap();
            mark_terminal(&conn, "sha_fail", "me/repo", "failure").unwrap();
            // Before expiry: visible
            let pending = pending_notifications(&conn, now + 1).unwrap();
            assert_eq!(pending.len(), 1);
            // After expiry: excluded
            let pending = pending_notifications(&conn, ttl + 1).unwrap();
            assert!(pending.is_empty());
        }

        #[test]
        fn pending_notifications_excludes_already_notified() {
            let conn = open_test_db();
            let now = 1_000_000i64;
            let ttl = now + 9999;
            upsert(&conn, "sha_a", "me/repo", now, ttl).unwrap();
            upsert(&conn, "sha_b", "me/repo", now, ttl).unwrap();
            mark_terminal(&conn, "sha_a", "me/repo", "failure").unwrap();
            mark_terminal(&conn, "sha_b", "me/repo", "failure").unwrap();
            mark_notified(&conn, "sha_a", "me/repo").unwrap();
            let pending = pending_notifications(&conn, now + 1).unwrap();
            assert_eq!(pending.len(), 1);
            assert_eq!(pending[0].sha, "sha_b");
        }

        #[test]
        fn mark_notified_sets_notified_flag() {
            let conn = open_test_db();
            let now = 1_000_000i64;
            upsert(&conn, "sha_n", "me/repo", now, now + 9999).unwrap();
            mark_terminal(&conn, "sha_n", "me/repo", "failure").unwrap();
            mark_notified(&conn, "sha_n", "me/repo").unwrap();
            let notified: i32 = conn
                .query_row(
                    "SELECT notified FROM sha_watchlist WHERE sha='sha_n'",
                    [],
                    |r| r.get(0),
                )
                .unwrap();
            assert_eq!(notified, 1);
        }

        #[test]
        fn pending_notifications_empty_when_none_qualify() {
            let conn = open_test_db();
            let pending = pending_notifications(&conn, 1_000_000).unwrap();
            assert!(pending.is_empty());
        }
    }
}

pub mod workflow_store {
    use anyhow::Result;
    use rusqlite::{Connection, params};

    use crate::gh_annotations::parse as parse_annotation;

    /// Storage-layer projection of a workflow_run for upsert.
    /// Distinct from `hippo_daemon::gh_api::WorkflowRun` (the API response type).
    pub struct RunRow<'a> {
        pub id: i64,
        pub repo: &'a str,
        pub head_sha: &'a str,
        pub head_branch: Option<&'a str>,
        pub event: &'a str,
        pub status: &'a str,
        pub conclusion: Option<&'a str>,
        pub started_at: Option<i64>,
        pub completed_at: Option<i64>,
        pub html_url: &'a str,
        pub actor: Option<&'a str>,
        pub raw_json: &'a str,
    }

    pub fn upsert_run(conn: &Connection, run: &RunRow, now_ms: i64) -> Result<()> {
        conn.execute(
            "INSERT INTO workflow_runs
                (id, repo, head_sha, head_branch, event, status, conclusion,
                 started_at, completed_at, html_url, actor, raw_json,
                 first_seen_at, last_seen_at)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?13)
             ON CONFLICT(id) DO UPDATE SET
                status=excluded.status, conclusion=excluded.conclusion,
                completed_at=excluded.completed_at, last_seen_at=excluded.last_seen_at,
                raw_json=excluded.raw_json",
            // `?13` is bound once for both first_seen_at (initial insert) and
            // last_seen_at (update). If you reorder params, keep them paired.
            params![
                run.id,
                run.repo,
                run.head_sha,
                run.head_branch,
                run.event,
                run.status,
                run.conclusion,
                run.started_at,
                run.completed_at,
                run.html_url,
                run.actor,
                run.raw_json,
                now_ms,
            ],
        )?;
        Ok(())
    }

    /// Storage-layer projection of a workflow_job for upsert.
    /// Distinct from `hippo_daemon::gh_api::Job` (the API response type).
    pub struct JobRow<'a> {
        pub id: i64,
        pub run_id: i64,
        pub name: &'a str,
        pub status: &'a str,
        pub conclusion: Option<&'a str>,
        pub started_at: Option<i64>,
        pub completed_at: Option<i64>,
        pub runner_name: Option<&'a str>,
        pub raw_json: &'a str,
    }

    pub fn upsert_job(conn: &Connection, job: &JobRow) -> Result<()> {
        conn.execute(
            "INSERT INTO workflow_jobs
                (id, run_id, name, status, conclusion, started_at, completed_at,
                 runner_name, raw_json)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9)
             ON CONFLICT(id) DO UPDATE SET
                status=excluded.status, conclusion=excluded.conclusion,
                completed_at=excluded.completed_at, raw_json=excluded.raw_json",
            params![
                job.id,
                job.run_id,
                job.name,
                job.status,
                job.conclusion,
                job.started_at,
                job.completed_at,
                job.runner_name,
                job.raw_json,
            ],
        )?;
        Ok(())
    }

    pub fn insert_annotation(
        conn: &Connection,
        job_id: i64,
        job_name: &str,
        level: &str,
        message: &str,
        path: Option<&str>,
        start_line: Option<i64>,
    ) -> Result<()> {
        let parsed = parse_annotation(job_name, message);
        conn.execute(
            "INSERT INTO workflow_annotations
                (job_id, level, tool, rule_id, path, start_line, message)
             VALUES (?1,?2,?3,?4,?5,?6,?7)",
            params![
                job_id,
                level,
                parsed.tool,
                parsed.rule_id,
                path,
                start_line,
                message,
            ],
        )?;
        Ok(())
    }

    pub fn insert_log_excerpt(
        conn: &Connection,
        job_id: i64,
        step_name: Option<&str>,
        excerpt: &str,
        truncated: bool,
    ) -> Result<()> {
        conn.execute(
            "INSERT INTO workflow_log_excerpts (job_id, step_name, excerpt, truncated)
             VALUES (?1,?2,?3,?4)",
            params![job_id, step_name, excerpt, truncated as i32],
        )?;
        Ok(())
    }

    pub fn enqueue_enrichment(conn: &Connection, run_id: i64, now_ms: i64) -> Result<()> {
        conn.execute(
            "INSERT INTO workflow_enrichment_queue (run_id, enqueued_at, updated_at)
             VALUES (?1, ?2, ?2)
             ON CONFLICT(run_id) DO NOTHING",
            params![run_id, now_ms],
        )?;
        Ok(())
    }
}
