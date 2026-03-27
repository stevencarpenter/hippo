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
            .query_row("SELECT command FROM events WHERE id = ?1", [event_id], |row| {
                row.get(0)
            })
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
}
