use anyhow::Result;
use hippo_core::config::{ENV_ALLOWLIST, HippoConfig};
use hippo_core::events::{EventEnvelope, EventPayload};
use hippo_core::protocol::{DaemonRequest, DaemonResponse};
use hippo_core::redaction::RedactionEngine;
use hippo_core::storage;
use rusqlite::Connection;
use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::Mutex;
use tokio::time::{self, Duration, Instant};
use tracing::{error, info, warn};

pub struct DaemonState {
    pub config: HippoConfig,
    pub db: Mutex<Connection>,
    pub redaction: RedactionEngine,
    pub session_map: Mutex<HashMap<String, i64>>,
    pub start_time: Instant,
    pub drop_count: AtomicU64,
    pub event_buffer: Mutex<Vec<EventEnvelope>>,
}

async fn read_frame(stream: &mut UnixStream) -> Result<Option<Vec<u8>>> {
    let mut len_buf = [0u8; 4];
    match stream.read_exact(&mut len_buf).await {
        Ok(_) => {}
        Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(e.into()),
    }
    let len = u32::from_be_bytes(len_buf) as usize;
    if len > 10 * 1024 * 1024 {
        anyhow::bail!("frame too large: {} bytes", len);
    }
    let mut buf = vec![0u8; len];
    stream.read_exact(&mut buf).await?;
    Ok(Some(buf))
}

async fn write_frame(stream: &mut UnixStream, data: &[u8]) -> Result<()> {
    let len = (data.len() as u32).to_be_bytes();
    stream.write_all(&len).await?;
    stream.write_all(data).await?;
    stream.flush().await?;
    Ok(())
}

pub async fn handle_request(state: &Arc<DaemonState>, request: DaemonRequest) -> DaemonResponse {
    match request {
        DaemonRequest::IngestEvent(envelope) => {
            let mut buffer = state.event_buffer.lock().await;
            buffer.push(*envelope);
            DaemonResponse::Ack
        }
        DaemonRequest::GetStatus => {
            let db = state.db.lock().await;
            match storage::get_status(&db) {
                Ok(mut status) => {
                    status.uptime_secs = state.start_time.elapsed().as_secs();
                    status.drop_count = state.drop_count.load(Ordering::Relaxed);
                    status.db_size_bytes = std::fs::metadata(state.config.db_path())
                        .map(|m| m.len())
                        .unwrap_or(0);
                    status.fallback_files_pending =
                        storage::list_fallback_files(&state.config.fallback_dir())
                            .map(|f| f.len() as u64)
                            .unwrap_or(0);
                    DaemonResponse::Status(status)
                }
                Err(e) => DaemonResponse::Error(e.to_string()),
            }
        }
        DaemonRequest::GetSessions { since_ms, limit } => {
            let db = state.db.lock().await;
            match storage::get_sessions(&db, since_ms, limit.unwrap_or(50)) {
                Ok(sessions) => DaemonResponse::Sessions(sessions),
                Err(e) => DaemonResponse::Error(e.to_string()),
            }
        }
        DaemonRequest::GetEvents {
            session_id,
            since_ms,
            project,
            limit,
        } => {
            let db = state.db.lock().await;
            match storage::get_events(
                &db,
                session_id,
                since_ms,
                project.as_deref(),
                limit.unwrap_or(50),
            ) {
                Ok(events) => DaemonResponse::Events(events),
                Err(e) => DaemonResponse::Error(e.to_string()),
            }
        }
        DaemonRequest::GetEntities { entity_type } => {
            let db = state.db.lock().await;
            match storage::get_entities(&db, entity_type.as_deref()) {
                Ok(entities) => DaemonResponse::Entities(entities),
                Err(e) => DaemonResponse::Error(e.to_string()),
            }
        }
        DaemonRequest::RawQuery { text } => {
            let db = state.db.lock().await;
            match storage::raw_query(&db, &text) {
                Ok(hits) => DaemonResponse::QueryResult(hits),
                Err(e) => DaemonResponse::Error(e.to_string()),
            }
        }
        DaemonRequest::Shutdown => {
            info!("shutdown requested");
            DaemonResponse::Ack
        }
    }
}

pub async fn flush_events(state: &Arc<DaemonState>) {
    let events: Vec<EventEnvelope> = {
        let mut buffer = state.event_buffer.lock().await;
        buffer.drain(..).collect()
    };

    if events.is_empty() {
        return;
    }

    let db = state.db.lock().await;
    let mut session_map = state.session_map.lock().await;

    for envelope in &events {
        if let EventPayload::Shell(ref shell_event) = envelope.payload {
            // Redact command
            let redact_result = state.redaction.redact(&shell_event.command);
            let mut redacted_event = shell_event.clone();
            redacted_event.command = redact_result.text;
            redacted_event.redaction_count = redact_result.count;

            // Filter env to allowlist and redact values
            let filtered_env: HashMap<String, String> = redacted_event
                .env_snapshot
                .iter()
                .filter(|(k, _)| ENV_ALLOWLIST.contains(&k.as_str()))
                .map(|(k, v)| {
                    let rv = state.redaction.redact(v);
                    (k.clone(), rv.text)
                })
                .collect();

            let shell_str = format!("{:?}", redacted_event.shell);
            let session_id = match storage::get_or_create_session(
                &db,
                &redacted_event.session_id.to_string(),
                &redacted_event.hostname,
                &shell_str,
                "unknown",
                &mut session_map,
            ) {
                Ok(id) => id,
                Err(e) => {
                    warn!("session creation failed, falling back: {}", e);
                    if let Err(fe) =
                        storage::write_fallback_jsonl(&state.config.fallback_dir(), envelope)
                    {
                        error!("fallback write failed: {}", fe);
                    }
                    state.drop_count.fetch_add(1, Ordering::Relaxed);
                    continue;
                }
            };

            let env_snapshot_id = match storage::upsert_env_snapshot(&db, &filtered_env) {
                Ok(id) => id,
                Err(e) => {
                    warn!("env snapshot failed: {}", e);
                    None
                }
            };

            if let Err(e) = storage::insert_event(
                &db,
                session_id,
                &redacted_event,
                redacted_event.redaction_count,
                env_snapshot_id,
            ) {
                warn!("event insert failed, falling back: {}", e);
                if let Err(fe) =
                    storage::write_fallback_jsonl(&state.config.fallback_dir(), envelope)
                {
                    error!("fallback write failed: {}", fe);
                }
                state.drop_count.fetch_add(1, Ordering::Relaxed);
            }
        }
    }
}

pub async fn run(config: HippoConfig) -> Result<()> {
    let socket_path = config.socket_path();
    let db_path = config.db_path();

    // Remove stale socket
    if socket_path.exists() {
        std::fs::remove_file(&socket_path)?;
    }

    // Ensure data dir exists
    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    // Open database
    let conn = storage::open_db(&db_path)?;

    // Recover fallback files
    let mut session_map = HashMap::new();
    let fallback_dir = config.fallback_dir();
    match storage::recover_fallback_files(&conn, &fallback_dir, &mut session_map) {
        Ok((recovered, errors)) => {
            if recovered > 0 || errors > 0 {
                info!(
                    "recovered {} events from fallback ({} errors)",
                    recovered, errors
                );
            }
        }
        Err(e) => warn!("fallback recovery failed: {}", e),
    }

    let state = Arc::new(DaemonState {
        config: config.clone(),
        db: Mutex::new(conn),
        redaction: RedactionEngine::builtin(),
        session_map: Mutex::new(session_map),
        start_time: Instant::now(),
        drop_count: AtomicU64::new(0),
        event_buffer: Mutex::new(Vec::new()),
    });

    // Spawn flush task
    let flush_state = Arc::clone(&state);
    let flush_interval = config.daemon.flush_interval_ms;
    tokio::spawn(async move {
        let mut interval = time::interval(Duration::from_millis(flush_interval));
        loop {
            interval.tick().await;
            flush_events(&flush_state).await;
        }
    });

    // Bind listener
    let listener = UnixListener::bind(&socket_path)?;
    info!("daemon listening on {:?}", socket_path);

    loop {
        let (stream, _) = listener.accept().await?;
        let conn_state = Arc::clone(&state);
        tokio::spawn(async move {
            if let Err(e) = handle_connection(conn_state, stream).await {
                warn!("connection error: {}", e);
            }
        });
    }
}

async fn handle_connection(state: Arc<DaemonState>, mut stream: UnixStream) -> Result<()> {
    while let Some(frame) = read_frame(&mut stream).await? {
        let request: DaemonRequest = serde_json::from_slice(&frame)?;

        let is_shutdown = matches!(request, DaemonRequest::Shutdown);
        let is_ingest = matches!(request, DaemonRequest::IngestEvent(_));

        let response = handle_request(&state, request).await;

        // IngestEvent is fire-and-forget — no response
        if !is_ingest {
            let response_json = serde_json::to_vec(&response)?;
            write_frame(&mut stream, &response_json).await?;
        }

        if is_shutdown {
            // Flush remaining events before exit
            flush_events(&state).await;
            std::process::exit(0);
        }
    }
    Ok(())
}
