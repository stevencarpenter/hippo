use anyhow::Result;
use chrono::Utc;
use rusqlite::Connection;
use sha2::{Digest, Sha256};
use std::collections::HashMap;
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
    conn.execute_batch(SCHEMA)?;
    Ok(conn)
}

pub fn upsert_session(
    conn: &Connection,
    session_uuid: &str,
    hostname: &str,
    shell: &str,
    username: &str,
) -> Result<i64> {
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

pub fn upsert_env_snapshot(
    conn: &Connection,
    env: &HashMap<String, String>,
) -> Result<Option<i64>> {
    if env.is_empty() {
        return Ok(None);
    }
    let env_json = serde_json::to_string(env)?;
    let mut hasher = Sha256::new();
    hasher.update(env_json.as_bytes());
    let content_hash = format!("{:x}", hasher.finalize());

    // Try to find existing
    let existing: Option<i64> = conn
        .query_row(
            "SELECT id FROM env_snapshots WHERE content_hash = ?1",
            [&content_hash],
            |row| row.get(0),
        )
        .ok();

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
    let timestamp = Utc::now().timestamp_millis();
    let shell_str = format!("{:?}", event.shell);
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

    conn.execute(
        "INSERT INTO events (session_id, timestamp, command, stdout, stderr, stdout_truncated, stderr_truncated,
         exit_code, duration_ms, cwd, hostname, shell, git_repo, git_branch, git_commit, git_dirty,
         env_snapshot_id, redaction_count)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17, ?18)",
        rusqlite::params![
            session_id,
            timestamp,
            event.command,
            stdout,
            stderr,
            stdout_truncated,
            stderr_truncated,
            event.exit_code,
            event.duration_ms,
            event.cwd.to_string_lossy(),
            event.hostname,
            shell_str,
            git_repo,
            git_branch,
            git_commit,
            git_dirty,
            env_snapshot_id,
            redaction_count,
        ],
    )?;
    let event_id = conn.last_insert_rowid();

    // Auto-queue for enrichment
    conn.execute(
        "INSERT INTO enrichment_queue (event_id) VALUES (?1)",
        [event_id],
    )?;

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
            event_count: row.get(5)?,
            summary: row.get(6)?,
        })
    })?;
    Ok(rows.filter_map(|r| r.ok()).collect())
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
            duration_ms: row.get(5)?,
            cwd: row.get(6)?,
            git_branch: row.get(7)?,
            enriched: row.get::<_, i32>(8)? != 0,
        })
    })?;
    Ok(rows.filter_map(|r| r.ok()).collect())
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
    Ok(rows.filter_map(|r| r.ok()).collect())
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
    Ok(rows.filter_map(|r| r.ok()).collect())
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
        |row| row.get(0),
    )?;

    let sessions_today: u64 = conn.query_row(
        "SELECT COUNT(*) FROM sessions WHERE start_time >= ?1",
        [today_start],
        |row| row.get(0),
    )?;

    let queue_depth: u64 = conn.query_row(
        "SELECT COUNT(*) FROM enrichment_queue WHERE status = 'pending'",
        [],
        |row| row.get(0),
    )?;

    let queue_failed: u64 = conn.query_row(
        "SELECT COUNT(*) FROM enrichment_queue WHERE status = 'failed'",
        [],
        |row| row.get(0),
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
                        match insert_event(
                            conn,
                            session_id,
                            shell_event,
                            shell_event.redaction_count,
                            None,
                        ) {
                            Ok(_) => recovered += 1,
                            Err(_) => errors += 1,
                        }
                    }
                }
                Err(_) => errors += 1,
            }
        }
        // Rename to .done
        let done_path = file_path.with_extension("jsonl.done");
        std::fs::rename(file_path, done_path)?;
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
    use crate::events::{GitState, ShellEvent, ShellKind};
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
}
