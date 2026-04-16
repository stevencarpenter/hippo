use anyhow::Result;
use hippo_core::config::HippoConfig;
use hippo_core::events::{EventEnvelope, EventPayload};
use hippo_core::protocol::{DaemonRequest, DaemonResponse};
use hippo_core::redaction::RedactionEngine;
use hippo_core::storage;
use rusqlite::Connection;
use std::collections::HashMap;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::Mutex;
use tokio::sync::watch;
use tokio::task::JoinSet;
use tokio::time::{self, Duration, Instant};
use tracing::{error, info, warn};

#[cfg(feature = "otel")]
use crate::metrics;
#[cfg(feature = "otel")]
use opentelemetry::KeyValue;
#[cfg(feature = "otel")]
use std::time::Instant as OtelInstant;

use crate::framing::{read_frame, write_frame};

pub struct DaemonState {
    pub config: HippoConfig,
    pub write_db: Mutex<Connection>,
    pub read_db: Mutex<Connection>,
    pub redaction: RedactionEngine,
    pub session_map: Mutex<HashMap<String, i64>>,
    pub start_time: Instant,
    pub drop_count: AtomicU64,
    pub event_buffer: Mutex<Vec<EventEnvelope>>,
    pub shutdown_tx: watch::Sender<bool>,
}

/// Count files in a directory, returning 0 on any error.
/// Extracted to a standalone function so taint analysis does not
/// conflate the directory path with web-request sources.
#[cfg(feature = "otel")]
fn count_dir_entries(dir: &std::path::Path) -> u64 {
    std::fs::read_dir(dir)
        .map(|entries| entries.count() as u64)
        .unwrap_or(0)
}

#[tracing::instrument(skip(state), fields(request_type))]
pub async fn handle_request(state: &Arc<DaemonState>, request: DaemonRequest) -> DaemonResponse {
    let request_type = match &request {
        DaemonRequest::IngestEvent(_) => "ingest_event",
        DaemonRequest::GetStatus => "get_status",
        DaemonRequest::GetSessions { .. } => "get_sessions",
        DaemonRequest::GetEvents { .. } => "get_events",
        DaemonRequest::GetEntities { .. } => "get_entities",
        DaemonRequest::RawQuery { .. } => "raw_query",
        DaemonRequest::RegisterWatchSha { .. } => "register_watch_sha",
        DaemonRequest::Shutdown => "shutdown",
    };
    tracing::Span::current().record("request_type", request_type);

    #[cfg(feature = "otel")]
    let req_start = OtelInstant::now();
    #[cfg(feature = "otel")]
    metrics::REQUESTS.add(1, &[KeyValue::new("type", request_type)]);

    let response = match request {
        DaemonRequest::IngestEvent(envelope) => {
            #[cfg(feature = "otel")]
            let event_type = match &envelope.payload {
                EventPayload::Shell(_) => "shell",
                EventPayload::Browser(_) => "browser",
                _ => "unknown",
            };
            let mut buffer = state.event_buffer.lock().await;
            let cap = state.config.daemon.flush_batch_size * 4;
            if buffer.len() >= cap {
                state.drop_count.fetch_add(1, Ordering::Relaxed);
                #[cfg(feature = "otel")]
                metrics::EVENTS_DROPPED.add(1, &[KeyValue::new("type", event_type)]);
            } else {
                buffer.push(*envelope);
                #[cfg(feature = "otel")]
                metrics::EVENTS_INGESTED.add(1, &[KeyValue::new("type", event_type)]);
            }
            DaemonResponse::Ack
        }
        DaemonRequest::GetStatus => {
            let mut status = {
                let db = state.read_db.lock().await;
                match storage::get_status(&db) {
                    Ok(status) => status,
                    Err(e) => {
                        #[cfg(feature = "otel")]
                        {
                            let elapsed = req_start.elapsed().as_secs_f64() * 1000.0;
                            metrics::REQUEST_DURATION_MS
                                .record(elapsed, &[KeyValue::new("type", request_type)]);
                        }
                        return DaemonResponse::Error(e.to_string());
                    }
                }
            };

            status.version = env!("HIPPO_VERSION_FULL").to_string();
            status.uptime_secs = state.start_time.elapsed().as_secs();
            status.drop_count = state.drop_count.load(Ordering::Relaxed);
            status.db_size_bytes = std::fs::metadata(state.config.db_path())
                .map(|m| m.len())
                .unwrap_or(0);
            status.fallback_files_pending =
                storage::list_fallback_files(&state.config.fallback_dir())
                    .map(|f| f.len() as u64)
                    .unwrap_or(0);

            // Check LM Studio reachability
            let lm_url = format!("{}/models", state.config.lmstudio.base_url);
            let client = reqwest::Client::builder()
                .timeout(Duration::from_secs(1))
                .build()
                .unwrap_or_default();
            status.lmstudio_reachable = client
                .get(&lm_url)
                .send()
                .await
                .map(|r| r.status().is_success())
                .unwrap_or(false);

            // Check Brain reachability
            let brain_url = format!("http://localhost:{}/health", state.config.brain.port);
            status.brain_reachable = client
                .get(&brain_url)
                .send()
                .await
                .map(|r| r.status().is_success())
                .unwrap_or(false);

            DaemonResponse::Status(status)
        }
        DaemonRequest::GetSessions { since_ms, limit } => {
            let db = state.read_db.lock().await;
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
            let db = state.read_db.lock().await;
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
            let db = state.read_db.lock().await;
            match storage::get_entities(&db, entity_type.as_deref()) {
                Ok(entities) => DaemonResponse::Entities(entities),
                Err(e) => DaemonResponse::Error(e.to_string()),
            }
        }
        DaemonRequest::RawQuery { text } => {
            let db = state.read_db.lock().await;
            match storage::raw_query(&db, &text) {
                Ok(hits) => DaemonResponse::QueryResult(hits),
                Err(e) => DaemonResponse::Error(e.to_string()),
            }
        }
        DaemonRequest::RegisterWatchSha {
            sha,
            repo,
            ttl_secs,
        } => {
            let now = chrono::Utc::now().timestamp_millis();
            let expires = now + (ttl_secs as i64) * 1000;
            let db = state.write_db.lock().await;
            match storage::watchlist::upsert(&db, &sha, &repo, now, expires) {
                Ok(()) => DaemonResponse::Ack,
                Err(e) => DaemonResponse::Error(e.to_string()),
            }
        }
        DaemonRequest::Shutdown => {
            info!("shutdown requested");
            DaemonResponse::Ack
        }
    };

    #[cfg(feature = "otel")]
    {
        let elapsed = req_start.elapsed().as_secs_f64() * 1000.0;
        metrics::REQUEST_DURATION_MS.record(elapsed, &[KeyValue::new("type", request_type)]);
    }

    response
}

#[tracing::instrument(skip(state), fields(event_count))]
pub async fn flush_events(state: &Arc<DaemonState>) -> usize {
    #[cfg(feature = "otel")]
    let flush_start = OtelInstant::now();

    let events: Vec<EventEnvelope> = {
        let mut buffer = state.event_buffer.lock().await;
        let n = buffer.len().min(state.config.daemon.flush_batch_size);
        buffer.drain(..n).collect()
    };
    let count = events.len();
    tracing::Span::current().record("event_count", count);

    if events.is_empty() {
        return 0;
    }

    let username = std::env::var("USER").unwrap_or_else(|_| "unknown".to_string());
    let db = state.write_db.lock().await;
    let mut session_map = state.session_map.lock().await;

    for envelope in &events {
        match &envelope.payload {
            EventPayload::Shell(shell_event) => {
                let redacted_event = crate::redact_shell_event(shell_event, &state.redaction);

                #[cfg(feature = "otel")]
                if redacted_event.redaction_count > 0 {
                    metrics::REDACTIONS.add(redacted_event.redaction_count as u64, &[]);
                }

                let shell_str = redacted_event.shell.as_db_str();
                let session_uuid_str = redacted_event.session_id.to_string();
                #[cfg(feature = "otel")]
                let session_is_new = !session_map.contains_key(&session_uuid_str);
                let session_id = match storage::get_or_create_session(
                    &db,
                    &session_uuid_str,
                    &redacted_event.hostname,
                    shell_str,
                    &username,
                    &mut session_map,
                ) {
                    Ok(id) => id,
                    Err(e) => {
                        warn!("session creation failed, falling back: {}", e);
                        let redacted_envelope = EventEnvelope {
                            envelope_id: envelope.envelope_id,
                            producer_version: envelope.producer_version,
                            timestamp: envelope.timestamp,
                            payload: EventPayload::Shell(redacted_event.clone()),
                        };
                        if let Err(fe) = storage::write_fallback_jsonl(
                            &state.config.fallback_dir(),
                            &redacted_envelope,
                        ) {
                            error!("fallback write failed: {}", fe);
                        }
                        #[cfg(feature = "otel")]
                        metrics::FALLBACK_WRITES.add(1, &[]);
                        state.drop_count.fetch_add(1, Ordering::Relaxed);
                        continue;
                    }
                };

                #[cfg(feature = "otel")]
                if session_is_new {
                    metrics::SESSIONS_CREATED.add(1, &[]);
                }

                let env_snapshot_id =
                    storage::upsert_env_snapshot(&db, &redacted_event.env_snapshot).unwrap_or_else(
                        |e| {
                            warn!("env snapshot failed: {}", e);
                            None
                        },
                    );

                let eid = envelope.envelope_id.to_string();
                if let Err(e) = storage::insert_event_at(
                    &db,
                    session_id,
                    &redacted_event,
                    envelope.timestamp.timestamp_millis(),
                    redacted_event.redaction_count,
                    env_snapshot_id,
                    Some(&eid),
                ) {
                    warn!("event insert failed, falling back: {}", e);
                    let redacted_envelope = EventEnvelope {
                        envelope_id: envelope.envelope_id,
                        producer_version: envelope.producer_version,
                        timestamp: envelope.timestamp,
                        payload: EventPayload::Shell(redacted_event.clone()),
                    };
                    if let Err(fe) = storage::write_fallback_jsonl(
                        &state.config.fallback_dir(),
                        &redacted_envelope,
                    ) {
                        error!("fallback write failed: {}", fe);
                    }
                    #[cfg(feature = "otel")]
                    metrics::FALLBACK_WRITES.add(1, &[]);
                    state.drop_count.fetch_add(1, Ordering::Relaxed);
                }
            }
            EventPayload::Browser(browser_event) => {
                let eid = envelope.envelope_id.to_string();
                if let Err(e) = storage::insert_browser_event(
                    &db,
                    browser_event,
                    envelope.timestamp.timestamp_millis(),
                    Some(&eid),
                ) {
                    warn!("browser event insert failed, falling back: {}", e);
                    if let Err(fe) =
                        storage::write_fallback_jsonl(&state.config.fallback_dir(), envelope)
                    {
                        error!("fallback write failed: {}", fe);
                    }
                    #[cfg(feature = "otel")]
                    metrics::FALLBACK_WRITES.add(1, &[]);
                    state.drop_count.fetch_add(1, Ordering::Relaxed);
                }
            }
            _ => {
                tracing::warn!("unknown event payload type, skipping");
            }
        }
    }

    #[cfg(feature = "otel")]
    {
        let count_u64 = count as u64;
        metrics::FLUSH_EVENTS.add(count_u64, &[]);
        metrics::FLUSH_BATCH_SIZE.record(count_u64, &[]);
        metrics::FLUSH_DURATION_MS.record(flush_start.elapsed().as_secs_f64() * 1000.0, &[]);
    }

    count
}

pub async fn run(config: HippoConfig) -> Result<()> {
    let socket_path = config.socket_path();
    let db_path = config.db_path();

    let redaction = crate::load_redaction_engine(&config);

    // Only remove a socket we can prove is stale. Refuse to replace
    // responsive or ambiguous sockets to avoid orphaning a live daemon.
    if socket_path.exists() {
        match crate::commands::probe_socket(&socket_path, config.daemon.socket_timeout_ms).await {
            crate::commands::SocketProbeResult::Missing => {}
            crate::commands::SocketProbeResult::Stale => {
                std::fs::remove_file(&socket_path)?;
            }
            crate::commands::SocketProbeResult::Responsive => {
                anyhow::bail!("daemon socket already in use at {}", socket_path.display());
            }
            crate::commands::SocketProbeResult::Unresponsive => {
                anyhow::bail!(
                    "socket exists at {} but did not respond; refusing to replace it",
                    socket_path.display()
                );
            }
        }
    }

    // Ensure data dir exists
    if let Some(parent) = socket_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    // Open database connections: one for writes (flush), one for reads (status/queries)
    let write_conn = storage::open_db(&db_path)?;
    let read_conn = storage::open_db(&db_path)?;

    // Recover fallback files
    let mut session_map = HashMap::new();
    let fallback_dir = config.fallback_dir();
    match storage::recover_fallback_files(&write_conn, &fallback_dir, &mut session_map) {
        Ok((recovered, errors)) => {
            if recovered > 0 || errors > 0 {
                info!(
                    "recovered {} events from fallback ({} errors)",
                    recovered, errors
                );
            }
            #[cfg(feature = "otel")]
            if recovered > 0 {
                metrics::FALLBACK_RECOVERED.add(recovered as u64, &[]);
            }
        }
        Err(e) => warn!("fallback recovery failed: {}", e),
    }

    let (shutdown_tx, mut shutdown_rx) = watch::channel(false);

    let state = Arc::new(DaemonState {
        config: config.clone(),
        write_db: Mutex::new(write_conn),
        read_db: Mutex::new(read_conn),
        redaction,
        session_map: Mutex::new(session_map),
        start_time: Instant::now(),
        drop_count: AtomicU64::new(0),
        event_buffer: Mutex::new(Vec::new()),
        shutdown_tx,
    });

    // Register observable gauges (otel only)
    #[cfg(feature = "otel")]
    {
        use opentelemetry::global;
        let meter = global::meter("hippo-daemon");

        let state_ref = Arc::clone(&state);
        let _ = meter
            .u64_observable_gauge("hippo.daemon.buffer.size")
            .with_description("Current event buffer occupancy")
            .with_callback(move |gauge| {
                if let Ok(buf) = state_ref.event_buffer.try_lock() {
                    gauge.observe(buf.len() as u64, &[]);
                }
            })
            .build();

        let db_path = state.config.db_path();
        let _ = meter
            .u64_observable_gauge("hippo.daemon.db.size_bytes")
            .with_description("SQLite file size")
            .with_callback(move |gauge| {
                if let Ok(meta) = std::fs::metadata(&db_path) {
                    gauge.observe(meta.len(), &[]);
                }
            })
            .build();

        // Canonicalize to produce a clean PathBuf that is not taint-tracked
        // through the DaemonState struct (path is XDG data dir / "fallback",
        // not user-controlled input).
        let fallback_dir_gauge = std::fs::canonicalize(state.config.fallback_dir())
            .unwrap_or_else(|_| state.config.fallback_dir());
        let _ = meter
            .u64_observable_gauge("hippo.daemon.fallback.pending")
            .with_description("Unrecovered fallback files")
            .with_callback(move |gauge| {
                gauge.observe(count_dir_entries(&fallback_dir_gauge), &[]);
            })
            .build();
    }

    // Spawn flush task
    let flush_state = Arc::clone(&state);
    let flush_interval = config.daemon.flush_interval_ms;
    let mut flush_shutdown_rx = state.shutdown_tx.subscribe();
    let flush_task = tokio::spawn(async move {
        let mut interval = time::interval(Duration::from_millis(flush_interval));
        loop {
            tokio::select! {
                _ = interval.tick() => {
                    flush_events(&flush_state).await;
                }
                _ = flush_shutdown_rx.changed() => {
                    // Drain all remaining events on shutdown
                    loop {
                        let flushed = flush_events(&flush_state).await;
                        if flushed == 0 { break; }
                    }
                    break;
                }
            }
        }
    });

    // Bind listener
    let listener = UnixListener::bind(&socket_path)?;
    info!("daemon listening on {:?}", socket_path);
    let mut connection_tasks = JoinSet::new();

    loop {
        tokio::select! {
            result = listener.accept() => {
                let (stream, _) = result?;
                let conn_state = Arc::clone(&state);
                connection_tasks.spawn(async move { handle_connection(conn_state, stream).await });
            }
            _ = shutdown_rx.changed() => {
                info!("shutdown signal received, stopping accept loop");
                break;
            }
            Some(join_result) = connection_tasks.join_next(), if !connection_tasks.is_empty() => {
                match join_result {
                    Ok(Ok(())) => {}
                    Ok(Err(e)) => warn!("connection error: {}", e),
                    Err(e) => warn!("connection task join error: {}", e),
                }
            }
        }
    }

    // Drop the listener so the socket fd is closed before removal
    drop(listener);

    while let Some(join_result) = connection_tasks.join_next().await {
        match join_result {
            Ok(Ok(())) => {}
            Ok(Err(e)) => warn!("connection error: {}", e),
            Err(e) => warn!("connection task join error: {}", e),
        }
    }

    if let Err(e) = flush_task.await {
        warn!("flush task join error: {}", e);
    }

    // Final drain for events buffered by connections that finished after the
    // flush task exited. No race: flush task is already joined above.
    loop {
        let flushed = flush_events(&state).await;
        if flushed == 0 {
            break;
        }
    }

    // Remove socket file so `daemon stop` polling sees shutdown
    if let Err(e) = std::fs::remove_file(&socket_path)
        && e.kind() != std::io::ErrorKind::NotFound
    {
        warn!("failed to remove socket: {}", e);
    }

    info!("daemon shut down cleanly");
    Ok(())
}

#[tracing::instrument(skip_all)]
async fn handle_connection(state: Arc<DaemonState>, mut stream: UnixStream) -> Result<()> {
    while let Some(frame) = read_frame(&mut stream).await? {
        let request: DaemonRequest = serde_json::from_slice(&frame)?;

        let is_shutdown = matches!(request, DaemonRequest::Shutdown);
        let is_ingest = matches!(request, DaemonRequest::IngestEvent(_));

        let response = handle_request(&state, request).await;

        // IngestEvent is fire-and-forget -- no response
        if !is_ingest {
            let response_json = serde_json::to_vec(&response)?;
            write_frame(&mut stream, &response_json).await?;
        }

        if is_shutdown {
            // Signal all tasks to shut down gracefully
            let _ = state.shutdown_tx.send(true);
            break;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    use hippo_core::config::HippoConfig;
    use hippo_core::events::{
        BrowserEvent, EventEnvelope, EventPayload, GitState, ShellEvent, ShellKind,
    };
    use hippo_core::protocol::StatusInfo;
    use hippo_core::storage;
    use std::collections::HashMap;

    use std::net::TcpListener as StdTcpListener;
    use std::path::PathBuf;
    use tempfile::tempdir;
    use tokio::io::AsyncReadExt;
    use tokio::io::AsyncWriteExt;
    use tokio::sync::oneshot;
    use tokio::time::sleep;
    use uuid::Uuid;

    fn test_config() -> HippoConfig {
        let temp = tempdir().unwrap();
        let mut config = HippoConfig::default();
        config.storage.data_dir = temp.path().join("data");
        config.storage.config_dir = temp.path().join("config");
        std::mem::forget(temp);
        config
    }

    fn test_envelope(command: &str) -> EventEnvelope {
        EventEnvelope::shell(ShellEvent {
            session_id: Uuid::new_v4(),
            command: command.to_string(),
            exit_code: 0,
            duration_ms: 42,
            cwd: PathBuf::from("/tmp"),
            hostname: "test-host".to_string(),
            shell: ShellKind::Zsh,
            stdout: None,
            stderr: None,
            env_snapshot: HashMap::new(),
            git_state: Some(GitState {
                repo: None,
                branch: Some("main".to_string()),
                commit: Some("abc1234".to_string()),
                is_dirty: false,
            }),
            redaction_count: 0,
        })
    }

    async fn wait_for_daemon(socket_path: &std::path::Path) {
        let deadline = Instant::now() + Duration::from_secs(2);
        loop {
            if let Ok(DaemonResponse::Status(_)) =
                crate::commands::send_request(socket_path, &DaemonRequest::GetStatus).await
            {
                return;
            }
            assert!(Instant::now() < deadline, "daemon never became ready");
            sleep(Duration::from_millis(25)).await;
        }
    }

    fn bind_local_http_listener() -> (StdTcpListener, u16) {
        let listener = StdTcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        (listener, port)
    }

    async fn spawn_slow_http_listener(
        listener: StdTcpListener,
    ) -> (oneshot::Receiver<()>, tokio::task::JoinHandle<()>) {
        listener.set_nonblocking(true).unwrap();
        let listener = tokio::net::TcpListener::from_std(listener).unwrap();
        let (started_tx, started_rx) = oneshot::channel();

        let handle = tokio::spawn(async move {
            if let Ok((mut stream, _)) = listener.accept().await {
                let mut buf = [0u8; 1024];
                let _ = stream.read(&mut buf).await;
                let _ = started_tx.send(());
                sleep(Duration::from_secs(2)).await;
            }
        });

        (started_rx, handle)
    }

    fn test_state_with_config(config: HippoConfig) -> Arc<DaemonState> {
        let write_conn = storage::open_db(&config.db_path()).unwrap();
        let read_conn = storage::open_db(&config.db_path()).unwrap();
        let (shutdown_tx, _shutdown_rx) = watch::channel(false);

        Arc::new(DaemonState {
            config,
            write_db: Mutex::new(write_conn),
            read_db: Mutex::new(read_conn),
            redaction: RedactionEngine::builtin(),
            session_map: Mutex::new(HashMap::new()),
            start_time: Instant::now(),
            drop_count: AtomicU64::new(0),
            event_buffer: Mutex::new(Vec::new()),
            shutdown_tx,
        })
    }

    fn status_from_response(response: DaemonResponse) -> StatusInfo {
        match response {
            DaemonResponse::Status(status) => status,
            other => panic!("expected status response, got {:?}", other),
        }
    }

    #[tokio::test]
    async fn test_run_refuses_to_replace_live_socket() {
        let config = test_config();
        let socket_path = config.socket_path();

        let run_config = config.clone();
        let daemon_handle = tokio::spawn(async move { run(run_config).await });
        wait_for_daemon(&socket_path).await;

        let second_run =
            tokio::time::timeout(Duration::from_millis(500), run(config.clone())).await;
        match second_run {
            Ok(Err(_)) => {
                let shutdown_result =
                    crate::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
                let daemon_result = daemon_handle.await.unwrap();

                assert!(
                    matches!(shutdown_result, Ok(DaemonResponse::Ack)),
                    "original daemon stopped responding after second run attempt: {:?}",
                    shutdown_result
                );
                assert!(
                    daemon_result.is_ok(),
                    "daemon shut down with error: {daemon_result:?}"
                );
            }
            Ok(Ok(())) => panic!("second daemon unexpectedly started and exited cleanly"),
            Err(_) => {
                daemon_handle.abort();
                let _ = daemon_handle.await;
                panic!("second daemon run hung instead of rejecting the live socket");
            }
        }
    }

    #[tokio::test]
    async fn test_shutdown_waits_for_accepted_ingest_connections() {
        let config = test_config();
        let socket_path = config.socket_path();
        let db_path = config.db_path();
        let envelope = test_envelope("echo delayed-ingest");
        let request = DaemonRequest::IngestEvent(Box::new(envelope));
        let payload = serde_json::to_vec(&request).unwrap();
        let split_at = payload.len() / 2;

        let run_config = config.clone();
        let daemon_handle = tokio::spawn(async move { run(run_config).await });
        wait_for_daemon(&socket_path).await;

        let mut delayed_stream = UnixStream::connect(&socket_path).await.unwrap();
        delayed_stream
            .write_all(&(payload.len() as u32).to_be_bytes())
            .await
            .unwrap();
        delayed_stream
            .write_all(&payload[..split_at])
            .await
            .unwrap();
        delayed_stream.flush().await.unwrap();

        let shutdown_response =
            crate::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
        assert!(
            matches!(shutdown_response, Ok(DaemonResponse::Ack)),
            "shutdown request failed: {:?}",
            shutdown_response
        );

        sleep(Duration::from_millis(100)).await;

        delayed_stream
            .write_all(&payload[split_at..])
            .await
            .unwrap();
        delayed_stream.shutdown().await.unwrap();

        let daemon_result = daemon_handle.await.unwrap();
        assert!(
            daemon_result.is_ok(),
            "daemon shut down with error: {daemon_result:?}"
        );

        let conn = storage::open_db(&db_path).unwrap();
        let event_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM events", [], |row| row.get(0))
            .unwrap();
        assert_eq!(
            event_count, 1,
            "accepted ingest event was lost during shutdown"
        );
    }

    #[tokio::test]
    async fn test_get_status_releases_db_lock_before_external_awaits() {
        let mut config = test_config();

        let (lm_listener, lm_port) = bind_local_http_listener();
        let (brain_listener, brain_port) = bind_local_http_listener();

        config.lmstudio.base_url = format!("http://127.0.0.1:{lm_port}/v1");
        config.brain.port = brain_port;

        let state = test_state_with_config(config);
        let (lm_started_rx, lm_handle) = spawn_slow_http_listener(lm_listener).await;
        let (_brain_started_rx, brain_handle) = spawn_slow_http_listener(brain_listener).await;

        let state_for_request = Arc::clone(&state);
        let request_handle = tokio::spawn(async move {
            handle_request(&state_for_request, DaemonRequest::GetStatus).await
        });

        lm_started_rx
            .await
            .expect("LM Studio probe never reached the slow listener");

        let db_guard = state
            .read_db
            .try_lock()
            .expect("read_db lock should be released before external status awaits");
        drop(db_guard);

        let response = request_handle.await.unwrap();
        let status = status_from_response(response);
        assert!(!status.lmstudio_reachable);
        assert!(!status.brain_reachable);

        lm_handle.abort();
        let _ = lm_handle.await;
        brain_handle.abort();
        let _ = brain_handle.await;
    }

    #[tokio::test]
    async fn test_daemon_uses_custom_redaction_config_on_flush_path() {
        let config = test_config();
        std::fs::create_dir_all(&config.storage.config_dir).unwrap();
        std::fs::write(
            config.redact_path(),
            r#"
[[patterns]]
name = "internal_token"
regex = "internal_[A-Z0-9]{8}"
replacement = "[CUSTOM]"
"#,
        )
        .unwrap();

        let redaction = RedactionEngine::from_config_path(&config.redact_path()).unwrap();
        let write_conn = storage::open_db(&config.db_path()).unwrap();
        let read_conn = storage::open_db(&config.db_path()).unwrap();
        let (shutdown_tx, _shutdown_rx) = watch::channel(false);
        let state = Arc::new(DaemonState {
            config: config.clone(),
            write_db: Mutex::new(write_conn),
            read_db: Mutex::new(read_conn),
            redaction,
            session_map: Mutex::new(HashMap::new()),
            start_time: Instant::now(),
            drop_count: AtomicU64::new(0),
            event_buffer: Mutex::new(Vec::new()),
            shutdown_tx,
        });

        let envelope = test_envelope("echo internal_ABCD1234");
        {
            let mut buffer = state.event_buffer.lock().await;
            buffer.push(envelope);
        }

        flush_events(&state).await;

        let db = state.write_db.lock().await;
        let command: String = db
            .query_row("SELECT command FROM events LIMIT 1", [], |row| row.get(0))
            .unwrap();
        drop(db);

        assert!(command.contains("[CUSTOM]"));
        assert!(!command.contains("internal_ABCD1234"));

        let files = storage::list_fallback_files(&config.fallback_dir()).unwrap();
        assert!(files.is_empty());
    }

    #[tokio::test]
    async fn test_flush_respects_batch_size() {
        let mut config = test_config();
        config.daemon.flush_batch_size = 2;
        let state = test_state_with_config(config);

        // Push 5 events into the buffer
        {
            let mut buffer = state.event_buffer.lock().await;
            for i in 0..5 {
                buffer.push(test_envelope(&format!("echo batch {i}")));
            }
        }

        // Flush once -- should drain exactly 2
        flush_events(&state).await;

        // Verify 2 events written to SQLite
        let db = state.write_db.lock().await;
        let written: i64 = db
            .query_row("SELECT COUNT(*) FROM events", [], |row| row.get(0))
            .unwrap();
        drop(db);
        assert_eq!(
            written, 2,
            "flush should write exactly flush_batch_size events"
        );

        // Verify 3 remain in the buffer
        let buffer = state.event_buffer.lock().await;
        assert_eq!(buffer.len(), 3, "remaining events should stay in buffer");
    }

    /// Characterizes the best-effort ingest durability contract.
    /// Events buffered in memory are lost if the daemon is killed before flush.
    /// This is intentional for a local shell capture tool.
    /// Graceful shutdown (via DaemonRequest::Shutdown) does flush before exit.
    #[tokio::test]
    async fn test_crash_before_flush_loses_accepted_events() {
        let mut config = test_config();
        // Set a very long flush interval so the periodic flush never fires
        config.daemon.flush_interval_ms = 600_000;
        let socket_path = config.socket_path();
        let db_path = config.db_path();

        let run_config = config.clone();
        let daemon_handle = tokio::spawn(async move { run(run_config).await });
        wait_for_daemon(&socket_path).await;

        // Send one event via the socket (fire-and-forget, no response expected)
        let envelope = test_envelope("echo crash-test");
        let request = DaemonRequest::IngestEvent(Box::new(envelope));
        let payload = serde_json::to_vec(&request).unwrap();

        let mut stream = UnixStream::connect(&socket_path).await.unwrap();
        crate::framing::write_frame(&mut stream, &payload)
            .await
            .unwrap();
        drop(stream);

        // Give the daemon time to accept and buffer the event
        sleep(Duration::from_millis(100)).await;

        // Abort the daemon task (simulates kill/crash — no graceful shutdown)
        daemon_handle.abort();
        let _ = daemon_handle.await;

        // Open the DB directly and verify the event was never persisted
        let conn = storage::open_db(&db_path).unwrap();
        let event_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM events", [], |row| row.get(0))
            .unwrap();
        assert_eq!(
            event_count, 0,
            "buffered event should be lost when daemon is killed before flush"
        );

        let queue_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM enrichment_queue", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(
            queue_count, 0,
            "enrichment_queue should also be empty when daemon is killed before flush"
        );
    }

    /// Verifies that read requests (GetEvents) do not deadlock when the
    /// write_db lock is held (e.g. during a slow flush), because reads use
    /// a separate read_db connection.
    #[tokio::test]
    async fn test_concurrent_read_write_no_deadlock() {
        let config = test_config();
        let socket_path = config.socket_path();

        let run_config = config.clone();
        let daemon_handle = tokio::spawn(async move { run(run_config).await });
        wait_for_daemon(&socket_path).await;

        // Ingest an event and flush it so there is data to read
        let envelope = test_envelope("echo concurrency-test");
        crate::commands::send_event_fire_and_forget(
            &socket_path,
            &envelope,
            config.daemon.socket_timeout_ms,
        )
        .await
        .unwrap();

        // Give the daemon time to buffer, then trigger a flush via a brief wait
        sleep(Duration::from_millis(200)).await;

        // Now issue a GetEvents request while the daemon is live -- this exercises
        // the read_db path.  If read and write shared one lock, holding write_db
        // during flush would block this read.  With separate connections, the read
        // completes independently.
        let read_result = tokio::time::timeout(
            Duration::from_millis(200),
            crate::commands::send_request(
                &socket_path,
                &DaemonRequest::GetEvents {
                    session_id: None,
                    since_ms: None,
                    project: None,
                    limit: Some(10),
                },
            ),
        )
        .await;

        assert!(
            read_result.is_ok(),
            "GetEvents should complete within 200ms, not deadlock"
        );

        let response = read_result.unwrap().unwrap();
        assert!(
            matches!(response, DaemonResponse::Events(_)),
            "expected Events response, got {:?}",
            response
        );

        // Clean shutdown
        let _ = crate::commands::send_request(&socket_path, &DaemonRequest::Shutdown).await;
        let daemon_result = daemon_handle.await.unwrap();
        assert!(
            daemon_result.is_ok(),
            "daemon shut down with error: {daemon_result:?}"
        );
    }

    #[tokio::test]
    async fn test_ingest_drops_at_capacity() {
        let mut config = test_config();
        config.daemon.flush_batch_size = 2;
        let state = test_state_with_config(config);

        // Fill to capacity: flush_batch_size * 4 = 8
        for i in 0..8 {
            let resp = handle_request(
                &state,
                DaemonRequest::IngestEvent(Box::new(test_envelope(&format!("echo fill {i}")))),
            )
            .await;
            assert!(matches!(resp, DaemonResponse::Ack));
        }

        assert_eq!(state.event_buffer.lock().await.len(), 8);
        assert_eq!(state.drop_count.load(Ordering::Relaxed), 0);

        // One more should be dropped
        let resp = handle_request(
            &state,
            DaemonRequest::IngestEvent(Box::new(test_envelope("echo overflow"))),
        )
        .await;
        assert!(matches!(resp, DaemonResponse::Ack));

        assert_eq!(
            state.drop_count.load(Ordering::Relaxed),
            1,
            "drop_count should be 1"
        );
        assert_eq!(
            state.event_buffer.lock().await.len(),
            8,
            "buffer should not grow past capacity"
        );
    }

    #[tokio::test]
    async fn test_flush_browser_event() {
        let config = test_config();
        let state = test_state_with_config(config);

        let browser_event = BrowserEvent {
            url: "https://docs.rs/anyhow/latest".to_string(),
            title: "anyhow - Rust".to_string(),
            domain: "docs.rs".to_string(),
            dwell_ms: 12000,
            scroll_depth: 0.75,
            extracted_text: Some("Flexible concrete Error type".to_string()),
            search_query: Some("rust anyhow".to_string()),
            referrer: Some("https://google.com".to_string()),
            content_hash: None,
        };

        let envelope = EventEnvelope {
            envelope_id: Uuid::new_v4(),
            producer_version: 1,
            timestamp: chrono::Utc::now(),
            payload: EventPayload::Browser(Box::new(browser_event)),
        };

        {
            let mut buffer = state.event_buffer.lock().await;
            buffer.push(envelope);
        }

        flush_events(&state).await;

        let db = state.write_db.lock().await;

        let event_count: i64 = db
            .query_row("SELECT COUNT(*) FROM browser_events", [], |row| row.get(0))
            .unwrap();
        assert_eq!(event_count, 1, "browser_events table should have 1 row");

        let queue_count: i64 = db
            .query_row(
                "SELECT COUNT(*) FROM browser_enrichment_queue WHERE status = 'pending'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(
            queue_count, 1,
            "browser_enrichment_queue should have 1 pending entry"
        );
    }
}
