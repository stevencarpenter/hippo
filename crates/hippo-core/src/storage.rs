use anyhow::Result;
use chrono::Utc;
use rusqlite::{Connection, OptionalExtension};
use sha2::{Digest, Sha256};
use std::collections::{BTreeMap, HashMap};
use std::path::Path;

use crate::events::ShellEvent;

const SCHEMA: &str = include_str!("schema.sql");

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
    const EXPECTED_VERSION: i64 = 2;

    // Migrate from v1 → v2: add envelope_id column for dedup
    if version == 1 {
        conn.execute_batch(
            "ALTER TABLE events ADD COLUMN envelope_id TEXT;
             CREATE UNIQUE INDEX IF NOT EXISTS idx_events_envelope_id
                 ON events (envelope_id) WHERE envelope_id IS NOT NULL;
             PRAGMA user_version = 2;",
        )?;
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

    let existing: Option<i64> = conn
        .query_row(
            "SELECT id FROM env_snapshots WHERE content_hash = ?1",
            [&content_hash],
            |row| row.get(0),
        )
        .optional()?;

    if let Some(id) = existing {
        return Ok(Some(id));
    }

    conn.execute(
        "INSERT INTO env_snapshots (content_hash, env_json) VALUES (?1, ?2)",
        rusqlite::params![content_hash, env_json],
    )?;
    Ok(Some(conn.last_insert_rowid()))
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
    )
}

pub fn insert_event_at(
    conn: &Connection,
    session_id: i64,
    event: &ShellEvent,
    timestamp: i64,
    redaction_count: u32,
    env_snapshot_id: Option<i64>,
    envelope_id: Option<&str>,
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

    let tx = conn.unchecked_transaction()?;

    let rows = tx.execute(
        "INSERT OR IGNORE INTO events (session_id, timestamp, command, stdout, stderr, stdout_truncated, stderr_truncated,
         exit_code, duration_ms, cwd, hostname, shell, git_repo, git_branch, git_commit, git_dirty,
         env_snapshot_id, redaction_count, envelope_id)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18, ?19)",
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
        ],
    )?;
    if rows == 0 {
        // Duplicate envelope_id — skip enrichment queue too
        tx.commit()?;
        return Ok(-1);
    }
    let event_id = tx.last_insert_rowid();

    // Auto-queue for enrichment (atomic with event insert — rolls back on failure)
    tx.execute(
        "INSERT INTO enrichment_queue (event_id) VALUES (?1)",
        [event_id],
    )?;

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
                (SELECT COUNT(*) FROM events e WHERE e.session_id = s.id) as event_count,
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
         FROM events WHERE 1=1",
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
        "SELECT id, command, cwd, timestamp FROM events WHERE command LIKE ?1
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
        "SELECT COUNT(*) FROM events WHERE timestamp >= ?1",
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
        let content = std::fs::read_to_string(file_path)?;
        let mut file_errors = 0usize;
        for line in content.lines() {
            if line.trim().is_empty() {
                continue;
            }
            match serde_json::from_str::<crate::events::EventEnvelope>(line) {
                Ok(envelope) => {
                    if let crate::events::EventPayload::Shell(ref shell_event) = envelope.payload {
                        let username =
                            std::env::var("USER").unwrap_or_else(|_| "unknown".to_string());
                        let session_id = get_or_create_session(
                            conn,
                            &shell_event.session_id.to_string(),
                            &shell_event.hostname,
                            &format!("{:?}", shell_event.shell),
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
                        ) {
                            Ok(_) => recovered += 1,
                            Err(_) => file_errors += 1,
                        }
                    }
                }
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
        assert_eq!(v, 2);
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
        // open_db should migrate to v2
        let conn = open_db(&db_path).unwrap();
        let v: i64 = conn
            .query_row("PRAGMA user_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(v, 2);
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
}
