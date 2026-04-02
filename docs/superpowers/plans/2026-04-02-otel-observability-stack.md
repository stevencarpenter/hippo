# OpenTelemetry Observability Stack Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an optional, self-contained Docker Compose observability stack that collects logs, metrics, and traces from all Hippo services (daemon, brain, MCP server), with Grafana dashboards for diagnosing performance and errors.

**Architecture:** An OTel Collector receives OTLP data from instrumented services and fans out to Grafana Tempo (traces), Grafana Loki (logs), and Prometheus (metrics). Grafana provides unified visualization. All instrumentation is feature-gated / config-gated so there is zero overhead when the stack is off. The `otel/` directory is designed to be extractable as a standalone repo.

**Tech Stack:**
- Docker Compose (OTel Collector contrib, Grafana, Tempo, Loki, Prometheus)
- Rust: `opentelemetry`, `opentelemetry-otlp`, `opentelemetry-appender-tracing`, `tracing-opentelemetry` (behind cargo feature `otel`)
- Python: `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http`, `opentelemetry-api` (behind `HIPPO_OTEL_ENABLED=1` env var)

---

## File Structure

### New files

```
otel/                                    # Self-contained, extractable OTel stack
├── docker-compose.yml                   # All 5 services: collector, grafana, tempo, loki, prometheus
├── otelcol-config.yml                   # OTel Collector pipeline config
├── prometheus.yml                       # Prometheus scrape config
├── grafana/
│   ├── datasources.yml                  # Auto-provision Tempo, Loki, Prometheus datasources
│   └── dashboards/
│       ├── dashboards.yml               # Dashboard provisioner config
│       └── hippo-overview.json          # Pre-built Grafana dashboard
├── tempo-config.yml                     # Tempo local storage config
├── loki-config.yml                      # Loki local storage config
└── README.md                            # Standalone usage docs
```

### Modified files

```
Cargo.toml                               # Add workspace otel deps behind feature flag
crates/hippo-core/Cargo.toml             # No changes (no OTel in core)
crates/hippo-daemon/Cargo.toml           # Add otel feature + deps
crates/hippo-daemon/src/telemetry.rs     # NEW — OTel init/shutdown module
crates/hippo-daemon/src/main.rs          # Wire telemetry init, conditional on feature
crates/hippo-daemon/src/daemon.rs        # Add span instrumentation to key functions
crates/hippo-daemon/src/lib.rs           # Export telemetry module

brain/pyproject.toml                     # Add optional otel dependency group
brain/src/hippo_brain/telemetry.py       # NEW — OTel init module for Python
brain/src/hippo_brain/__init__.py        # Wire telemetry init on startup
brain/src/hippo_brain/server.py          # Add span instrumentation to enrichment loop
brain/src/hippo_brain/mcp.py             # Add span instrumentation to MCP tools
brain/src/hippo_brain/mcp_logging.py     # Bridge to OTel when enabled

config/config.default.toml               # Add [telemetry] section
crates/hippo-core/src/config.rs          # Parse [telemetry] config section
mise.toml                                # Add otel:up, otel:down, otel:logs tasks
```

### Test files

```
crates/hippo-daemon/tests/telemetry_test.rs   # Integration test: OTel init/shutdown
brain/tests/test_telemetry.py                  # Unit test: OTel setup, env gating
```

---

## Task 1: Docker Compose OTel Stack

**Files:**
- Create: `otel/docker-compose.yml`
- Create: `otel/otelcol-config.yml`
- Create: `otel/prometheus.yml`
- Create: `otel/tempo-config.yml`
- Create: `otel/loki-config.yml`
- Create: `otel/grafana/datasources.yml`
- Create: `otel/grafana/dashboards/dashboards.yml`

This task creates the entire containerized stack. No application code changes yet.

- [ ] **Step 1: Create the OTel Collector config**

```yaml
# otel/otelcol-config.yml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317
      http:
        endpoint: 0.0.0.0:4318

processors:
  batch:
    timeout: 5s
    send_batch_size: 512

  resource:
    attributes:
      - key: host.name
        from_attribute: host.name
        action: upsert

exporters:
  otlphttp/tempo:
    endpoint: http://tempo:3200

  loki:
    endpoint: http://loki:3100/loki/api/v1/push

  prometheus:
    endpoint: 0.0.0.0:8889
    namespace: hippo

extensions:
  health_check:
    endpoint: 0.0.0.0:13133

service:
  extensions: [health_check]
  pipelines:
    traces:
      receivers: [otlp]
      processors: [batch, resource]
      exporters: [otlphttp/tempo]
    logs:
      receivers: [otlp]
      processors: [batch, resource]
      exporters: [loki]
    metrics:
      receivers: [otlp]
      processors: [batch, resource]
      exporters: [prometheus]
```

- [ ] **Step 2: Create Tempo config**

```yaml
# otel/tempo-config.yml
server:
  http_listen_port: 3200

distributor:
  receivers:
    otlp:
      protocols:
        http:
          endpoint: 0.0.0.0:3200

storage:
  trace:
    backend: local
    local:
      path: /var/tempo/traces
    wal:
      path: /var/tempo/wal
```

- [ ] **Step 3: Create Loki config**

```yaml
# otel/loki-config.yml
auth_enabled: false

server:
  http_listen_port: 3100

common:
  path_prefix: /loki
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory

schema_config:
  configs:
    - from: "2024-01-01"
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

limits_config:
  allow_structured_metadata: true
  volume_enabled: true
```

- [ ] **Step 4: Create Prometheus config**

```yaml
# otel/prometheus.yml
global:
  scrape_interval: 15s
  evaluation_interval: 15s

scrape_configs:
  - job_name: "otel-collector"
    static_configs:
      - targets: ["otel-collector:8889"]
```

- [ ] **Step 5: Create Grafana datasource provisioning**

```yaml
# otel/grafana/datasources.yml
apiVersion: 1
datasources:
  - name: Tempo
    type: tempo
    access: proxy
    url: http://tempo:3200
    isDefault: false
    jsonData:
      tracesToLogsV2:
        datasourceUid: loki
        filterByTraceID: true
      tracesToMetrics:
        datasourceUid: prometheus

  - name: Loki
    type: loki
    access: proxy
    url: http://loki:3100
    uid: loki
    isDefault: true
    jsonData:
      derivedFields:
        - datasourceUid: tempo
          matcherRegex: "trace_id=(\\w+)"
          name: TraceID
          url: "$${__value.raw}"

  - name: Prometheus
    type: prometheus
    access: proxy
    url: http://prometheus:9090
    uid: prometheus
    isDefault: false
```

- [ ] **Step 6: Create dashboard provisioner config**

```yaml
# otel/grafana/dashboards/dashboards.yml
apiVersion: 1
providers:
  - name: "hippo"
    orgId: 1
    folder: "Hippo"
    type: file
    disableDeletion: false
    editable: true
    options:
      path: /etc/grafana/provisioning/dashboards
      foldersFromFilesStructure: false
```

- [ ] **Step 7: Create Docker Compose file**

```yaml
# otel/docker-compose.yml
name: hippo-otel

services:
  otel-collector:
    image: otel/opentelemetry-collector-contrib:0.120.0
    command: ["--config", "/etc/otelcol/config.yml"]
    volumes:
      - ./otelcol-config.yml:/etc/otelcol/config.yml:ro
    ports:
      - "4317:4317"   # OTLP gRPC
      - "4318:4318"   # OTLP HTTP
      - "13133:13133" # Health check
    depends_on:
      - tempo
      - loki

  tempo:
    image: grafana/tempo:2.7.2
    command: ["-config.file=/etc/tempo/config.yml"]
    volumes:
      - ./tempo-config.yml:/etc/tempo/config.yml:ro
      - tempo-data:/var/tempo

  loki:
    image: grafana/loki:3.4.2
    command: ["-config.file=/etc/loki/config.yml"]
    volumes:
      - ./loki-config.yml:/etc/loki/config.yml:ro
      - loki-data:/loki

  prometheus:
    image: prom/prometheus:v3.2.1
    command:
      - "--config.file=/etc/prometheus/prometheus.yml"
      - "--storage.tsdb.path=/prometheus"
      - "--storage.tsdb.retention.time=7d"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
      - prometheus-data:/prometheus

  grafana:
    image: grafana/grafana:11.5.2
    environment:
      - GF_SECURITY_ADMIN_PASSWORD=hippo
      - GF_AUTH_ANONYMOUS_ENABLED=true
      - GF_AUTH_ANONYMOUS_ORG_ROLE=Admin
    volumes:
      - ./grafana/datasources.yml:/etc/grafana/provisioning/datasources/datasources.yml:ro
      - ./grafana/dashboards:/etc/grafana/provisioning/dashboards:ro
      - grafana-data:/var/lib/grafana
    ports:
      - "3000:3000"
    depends_on:
      - tempo
      - loki
      - prometheus

volumes:
  tempo-data:
  loki-data:
  prometheus-data:
  grafana-data:
```

- [ ] **Step 8: Test the stack boots**

Run:
```bash
cd otel && docker compose up -d && sleep 5 && docker compose ps
```

Expected: All 5 services show "running" or "healthy". Grafana reachable at http://localhost:3000.

Run:
```bash
curl -s http://localhost:13133/ | head -5
```

Expected: OTel Collector health check returns 200.

- [ ] **Step 9: Commit**

```bash
git add otel/
git commit -m "feat(otel): add Docker Compose observability stack

Grafana LGTM stack: OTel Collector, Tempo (traces), Loki (logs),
Prometheus (metrics), Grafana (dashboards). Self-contained in otel/
for portability."
```

---

## Task 2: Hippo Config — Telemetry Section

**Files:**
- Modify: `config/config.default.toml`
- Modify: `crates/hippo-core/src/config.rs`

Adds a `[telemetry]` section to config so OTel endpoint/enablement is runtime-configurable.

- [ ] **Step 1: Read the existing config module**

Read: `crates/hippo-core/src/config.rs`

Understand the existing `HippoConfig` struct and how sections are parsed.

- [ ] **Step 2: Write a failing test for telemetry config parsing**

Add to the bottom of `crates/hippo-core/src/config.rs` (inside the existing `#[cfg(test)]` block, or create one):

```rust
#[cfg(test)]
mod telemetry_config_tests {
    use super::*;

    #[test]
    fn test_telemetry_defaults() {
        let config = HippoConfig::default();
        assert!(!config.telemetry.enabled);
        assert_eq!(config.telemetry.endpoint, "http://localhost:4317");
    }

    #[test]
    fn test_telemetry_from_toml() {
        let toml_str = r#"
[telemetry]
enabled = true
endpoint = "http://collector:4317"
"#;
        let config: HippoConfig = toml::from_str(toml_str).unwrap();
        assert!(config.telemetry.enabled);
        assert_eq!(config.telemetry.endpoint, "http://collector:4317");
    }
}
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cargo test -p hippo-core telemetry_config`
Expected: Compilation error — `telemetry` field doesn't exist on `HippoConfig`.

- [ ] **Step 4: Add TelemetryConfig struct and wire it into HippoConfig**

The config uses serde `Deserialize` directly — no separate raw/cooked types. Follow the existing pattern.

In `crates/hippo-core/src/config.rs`, add a new struct:

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TelemetryConfig {
    #[serde(default)]
    pub enabled: bool,
    #[serde(default = "default_telemetry_endpoint")]
    pub endpoint: String,
}

fn default_telemetry_endpoint() -> String {
    "http://localhost:4317".to_string()
}

impl Default for TelemetryConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            endpoint: default_telemetry_endpoint(),
        }
    }
}
```

Add a `telemetry` field to the existing `HippoConfig` struct:

```rust
#[serde(default)]
pub telemetry: TelemetryConfig,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cargo test -p hippo-core telemetry_config`
Expected: Both tests pass.

- [ ] **Step 6: Add telemetry section to default config**

Append to `config/config.default.toml`:

```toml
[telemetry]
# Set enabled = true and run `mise run otel:up` to start the observability stack.
# endpoint is the OTel Collector gRPC address.
enabled = false
endpoint = "http://localhost:4317"
```

- [ ] **Step 7: Run full core tests**

Run: `cargo test -p hippo-core`
Expected: All tests pass.

- [ ] **Step 8: Commit**

```bash
git add config/config.default.toml crates/hippo-core/src/config.rs
git commit -m "feat(config): add [telemetry] section for OTel configuration"
```

---

## Task 3: Rust OTel Instrumentation — Feature-Gated

**Files:**
- Modify: `Cargo.toml` (workspace deps)
- Modify: `crates/hippo-daemon/Cargo.toml` (feature flag + deps)
- Create: `crates/hippo-daemon/src/telemetry.rs`
- Modify: `crates/hippo-daemon/src/lib.rs`
- Modify: `crates/hippo-daemon/src/main.rs`

All OTel code lives behind `--features otel` so the default build has zero new dependencies.

- [ ] **Step 1: Add workspace OTel dependencies**

In the root `Cargo.toml` `[workspace.dependencies]` section, add:

```toml
opentelemetry = "0.29"
opentelemetry_sdk = { version = "0.29", features = ["rt-tokio"] }
opentelemetry-otlp = { version = "0.29", features = ["grpc-tonic"] }
opentelemetry-appender-tracing = "0.29"
tracing-opentelemetry = "0.30"
```

- [ ] **Step 2: Add otel feature to hippo-daemon**

In `crates/hippo-daemon/Cargo.toml`, add:

```toml
[features]
default = []
otel = [
    "dep:opentelemetry",
    "dep:opentelemetry_sdk",
    "dep:opentelemetry-otlp",
    "dep:opentelemetry-appender-tracing",
    "dep:tracing-opentelemetry",
]

[dependencies]
opentelemetry = { workspace = true, optional = true }
opentelemetry_sdk = { workspace = true, optional = true }
opentelemetry-otlp = { workspace = true, optional = true }
opentelemetry-appender-tracing = { workspace = true, optional = true }
tracing-opentelemetry = { workspace = true, optional = true }
```

- [ ] **Step 3: Verify default build still compiles**

Run: `cargo build -p hippo-daemon`
Expected: Compiles with no new dependencies pulled in.

- [ ] **Step 4: Create the telemetry module**

Create `crates/hippo-daemon/src/telemetry.rs`:

```rust
//! OpenTelemetry initialization — only compiled with `--features otel`.

use anyhow::Result;
use opentelemetry::global;
use opentelemetry_appender_tracing::layer::OpenTelemetryTracingBridge;
use opentelemetry_otlp::{LogExporter, SpanExporter, MetricExporter};
use opentelemetry_sdk::logs::SdkLoggerProvider;
use opentelemetry_sdk::metrics::SdkMeterProvider;
use opentelemetry_sdk::trace::SdkTracerProvider;
use opentelemetry_sdk::Resource;
use tracing::info;
use tracing_opentelemetry::OpenTelemetryLayer;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;
use tracing_subscriber::EnvFilter;

fn resource(service_name: &str) -> Resource {
    Resource::builder()
        .with_service_name(service_name.to_string())
        .build()
}

pub struct TelemetryGuard {
    tracer_provider: SdkTracerProvider,
    meter_provider: SdkMeterProvider,
    logger_provider: SdkLoggerProvider,
}

impl TelemetryGuard {
    pub fn shutdown(self) {
        if let Err(e) = self.tracer_provider.shutdown() {
            eprintln!("tracer shutdown error: {e}");
        }
        if let Err(e) = self.meter_provider.shutdown() {
            eprintln!("meter shutdown error: {e}");
        }
        if let Err(e) = self.logger_provider.shutdown() {
            eprintln!("logger shutdown error: {e}");
        }
    }
}

/// Initialize OTel tracing subscriber with OTLP exporters.
/// Replaces the default `tracing_subscriber::fmt` when OTel is enabled.
/// Returns a guard that must be held for the lifetime of the program.
pub fn init(service_name: &str, endpoint: &str) -> Result<TelemetryGuard> {
    let res = resource(service_name);

    // Traces
    let span_exporter = SpanExporter::builder()
        .with_tonic()
        .with_endpoint(endpoint)
        .build()?;
    let tracer_provider = SdkTracerProvider::builder()
        .with_resource(res.clone())
        .with_batch_exporter(span_exporter)
        .build();

    // Metrics
    let metric_exporter = MetricExporter::builder()
        .with_tonic()
        .with_endpoint(endpoint)
        .build()?;
    let meter_provider = SdkMeterProvider::builder()
        .with_resource(res.clone())
        .with_periodic_exporter(metric_exporter)
        .build();

    // Logs
    let log_exporter = LogExporter::builder()
        .with_tonic()
        .with_endpoint(endpoint)
        .build()?;
    let logger_provider = SdkLoggerProvider::builder()
        .with_resource(res)
        .with_batch_exporter(log_exporter)
        .build();

    // Set global providers
    global::set_tracer_provider(tracer_provider.clone());
    global::set_meter_provider(meter_provider.clone());

    // Build the tracing subscriber with OTel layers
    let env_filter =
        EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info"));

    let fmt_layer = tracing_subscriber::fmt::layer().with_writer(std::io::stderr);

    let otel_trace_layer = OpenTelemetryLayer::new(tracer_provider.clone());

    let otel_log_layer = OpenTelemetryTracingBridge::new(&logger_provider);

    tracing_subscriber::registry()
        .with(env_filter)
        .with(fmt_layer)
        .with(otel_trace_layer)
        .with(otel_log_layer)
        .init();

    info!("OpenTelemetry initialized: endpoint={endpoint}, service={service_name}");

    Ok(TelemetryGuard {
        tracer_provider,
        meter_provider,
        logger_provider,
    })
}
```

- [ ] **Step 5: Export the telemetry module from lib.rs**

In `crates/hippo-daemon/src/lib.rs`, add:

```rust
#[cfg(feature = "otel")]
pub mod telemetry;
```

- [ ] **Step 6: Wire telemetry init into main.rs**

In `crates/hippo-daemon/src/main.rs`, the tracing subscriber init must happen before CLI parsing. Move the config load earlier and use it for both telemetry and the CLI.

Replace the top of `main()` (the `tracing_subscriber::fmt()` block + config load) with:

```rust
#[tokio::main]
async fn main() -> Result<()> {
    // Load config early — needed for telemetry init before CLI parsing
    let config = match HippoConfig::load_default() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("Warning: failed to load config: {e:#}. Using defaults.");
            HippoConfig::default()
        }
    };

    // Initialize telemetry — OTel if feature-enabled and config says so, else plain fmt
    #[cfg(feature = "otel")]
    let _otel_guard = if config.telemetry.enabled {
        match hippo_daemon::telemetry::init("hippo-daemon", &config.telemetry.endpoint) {
            Ok(guard) => Some(guard),
            Err(e) => {
                tracing_subscriber::fmt()
                    .with_writer(std::io::stderr)
                    .with_env_filter(
                        EnvFilter::try_from_default_env()
                            .unwrap_or_else(|_| EnvFilter::new("info")),
                    )
                    .init();
                tracing::warn!("OTel init failed, using plain logging: {e}");
                None
            }
        }
    } else {
        tracing_subscriber::fmt()
            .with_writer(std::io::stderr)
            .with_env_filter(
                EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
            )
            .init();
        None
    };

    #[cfg(not(feature = "otel"))]
    tracing_subscriber::fmt()
        .with_writer(std::io::stderr)
        .with_env_filter(
            EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")),
        )
        .init();

    let cli = Cli::parse();
    // Remove the duplicate config load that was here — reuse `config` from above

    match cli.command {
        // ... all existing match arms unchanged
    }

    #[cfg(feature = "otel")]
    if let Some(guard) = _otel_guard {
        guard.shutdown();
    }

    Ok(())
}
```

This eliminates the double config load. The existing `let config = match HippoConfig::load_default()` block after `Cli::parse()` should be deleted since we now load config at the top.

- [ ] **Step 7: Verify it compiles with the otel feature**

Run: `cargo build -p hippo-daemon --features otel`
Expected: Compiles successfully.

- [ ] **Step 8: Verify default build still works**

Run: `cargo build -p hippo-daemon`
Expected: Compiles with no OTel code included.

- [ ] **Step 9: Commit**

```bash
git add Cargo.toml Cargo.lock crates/hippo-daemon/
git commit -m "feat(daemon): add OpenTelemetry instrumentation behind --features otel

Traces, metrics, and logs exported via OTLP gRPC when [telemetry]
enabled = true in config. Zero overhead when feature is off."
```

---

## Task 4: Rust Span Instrumentation on Hot Paths

**Files:**
- Modify: `crates/hippo-daemon/src/daemon.rs`

Add `#[tracing::instrument]` to key functions so traces show meaningful spans.

- [ ] **Step 1: Instrument handle_request**

In `crates/hippo-daemon/src/daemon.rs`, add the `#[tracing::instrument]` attribute to `handle_request`:

```rust
#[tracing::instrument(skip(state), fields(request_type))]
pub async fn handle_request(state: &Arc<DaemonState>, request: DaemonRequest) -> DaemonResponse {
    // Record the request type as a span field
    let request_type = match &request {
        DaemonRequest::IngestEvent(_) => "ingest_event",
        DaemonRequest::GetStatus => "get_status",
        DaemonRequest::GetSessions { .. } => "get_sessions",
        DaemonRequest::GetEvents { .. } => "get_events",
        DaemonRequest::GetEntities { .. } => "get_entities",
        DaemonRequest::RawQuery { .. } => "raw_query",
        DaemonRequest::Shutdown => "shutdown",
    };
    tracing::Span::current().record("request_type", request_type);

    match request {
        // ... existing match arms unchanged
```

- [ ] **Step 2: Instrument flush_events**

Add to `flush_events`:

```rust
#[tracing::instrument(skip(state), fields(event_count))]
pub async fn flush_events(state: &Arc<DaemonState>) {
    let events: Vec<EventEnvelope> = {
        // ... existing drain logic
    };

    tracing::Span::current().record("event_count", events.len());

    if events.is_empty() {
        return;
    }
    // ... rest unchanged
```

- [ ] **Step 3: Instrument handle_connection**

Add to `handle_connection`:

```rust
#[tracing::instrument(skip_all)]
async fn handle_connection(state: Arc<DaemonState>, mut stream: UnixStream) -> Result<()> {
    // ... existing code unchanged
```

- [ ] **Step 4: Run all daemon tests**

Run: `cargo test -p hippo-daemon`
Expected: All tests pass (instrument attributes don't change behavior).

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-daemon/src/daemon.rs
git commit -m "feat(daemon): add tracing spans to hot paths

Instrument handle_request, flush_events, handle_connection with
tracing::instrument for OTel trace visibility."
```

---

## Task 5: Python OTel Instrumentation — Env-Gated

**Files:**
- Modify: `brain/pyproject.toml`
- Create: `brain/src/hippo_brain/telemetry.py`
- Create: `brain/tests/test_telemetry.py`

All OTel code is gated behind `HIPPO_OTEL_ENABLED=1` environment variable. When unset, the module is a no-op.

- [ ] **Step 1: Add OTel dependencies as optional group**

In `brain/pyproject.toml`, add an `otel` dependency group:

```toml
[dependency-groups]
dev = [
    "pytest>=9.0.2",
    "pytest-asyncio>=1.3",
    "pytest-cov>=7.1.0",
    "ruff>=0.15",
]
otel = [
    "opentelemetry-api>=1.33",
    "opentelemetry-sdk>=1.33",
    "opentelemetry-exporter-otlp-proto-http>=1.33",
]
```

- [ ] **Step 2: Write the failing test**

Create `brain/tests/test_telemetry.py`:

```python
import os
from unittest.mock import patch


def test_telemetry_disabled_by_default():
    """When HIPPO_OTEL_ENABLED is not set, init_telemetry is a no-op."""
    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("HIPPO_OTEL_ENABLED", None)
        from hippo_brain.telemetry import init_telemetry

        result = init_telemetry("test-service")
        assert result is None


def test_telemetry_enabled_returns_providers():
    """When HIPPO_OTEL_ENABLED=1 and otel packages are available, returns providers."""
    with patch.dict(os.environ, {"HIPPO_OTEL_ENABLED": "1"}):
        try:
            from hippo_brain.telemetry import init_telemetry

            result = init_telemetry("test-service", endpoint="http://localhost:4318")
            # If otel packages aren't installed, should still return None gracefully
            # If they are installed, should return a shutdown callable
            assert result is None or callable(result)
        except ImportError:
            pass  # otel deps not installed, that's fine


def test_telemetry_missing_packages_returns_none():
    """When HIPPO_OTEL_ENABLED=1 but otel packages missing, returns None gracefully."""
    import importlib
    import sys

    with patch.dict(os.environ, {"HIPPO_OTEL_ENABLED": "1"}):
        # Temporarily hide otel packages
        hidden = {}
        for mod_name in list(sys.modules.keys()):
            if "opentelemetry" in mod_name:
                hidden[mod_name] = sys.modules.pop(mod_name)

        try:
            # Force reimport
            if "hippo_brain.telemetry" in sys.modules:
                del sys.modules["hippo_brain.telemetry"]

            with patch.dict(sys.modules, {"opentelemetry": None}):
                from hippo_brain.telemetry import init_telemetry

                result = init_telemetry("test-service")
                assert result is None
        finally:
            sys.modules.update(hidden)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run --project brain pytest brain/tests/test_telemetry.py -v`
Expected: ImportError — `hippo_brain.telemetry` doesn't exist yet.

- [ ] **Step 4: Create the telemetry module**

Create `brain/src/hippo_brain/telemetry.py`:

```python
"""OpenTelemetry initialization for Hippo Brain services.

Gated behind HIPPO_OTEL_ENABLED=1 environment variable.
When disabled or when OTel packages are not installed, all functions are no-ops.
"""

import logging
import os

logger = logging.getLogger("hippo_brain.telemetry")

OTEL_ENABLED = os.environ.get("HIPPO_OTEL_ENABLED", "").strip() == "1"
DEFAULT_ENDPOINT = "http://localhost:4318"


def init_telemetry(
    service_name: str,
    endpoint: str = "",
) -> "callable | None":
    """Initialize OpenTelemetry providers for traces, metrics, and logs.

    Returns a shutdown callable, or None if OTel is disabled/unavailable.
    """
    if not OTEL_ENABLED:
        return None

    if not endpoint:
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_ENDPOINT)

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning("OpenTelemetry packages not installed — telemetry disabled")
        return None

    resource = Resource.create({"service.name": service_name})

    # Traces
    tracer_provider = TracerProvider(resource=resource)
    span_exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # Logs — bridge stdlib logging to OTel
    logger_provider = LoggerProvider(resource=resource)
    log_exporter = OTLPLogExporter(endpoint=f"{endpoint}/v1/logs")
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    handler = LoggingHandler(logger_provider=logger_provider)
    logging.getLogger().addHandler(handler)

    logger.info(
        "OpenTelemetry initialized: endpoint=%s, service=%s",
        endpoint,
        service_name,
    )

    def shutdown():
        tracer_provider.shutdown()
        logger_provider.shutdown()

    return shutdown
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run --project brain pytest brain/tests/test_telemetry.py -v`
Expected: All 3 tests pass.

- [ ] **Step 6: Commit**

```bash
git add brain/pyproject.toml brain/src/hippo_brain/telemetry.py brain/tests/test_telemetry.py
git commit -m "feat(brain): add OpenTelemetry instrumentation gated by HIPPO_OTEL_ENABLED

Traces and logs exported via OTLP HTTP when env var is set.
Gracefully degrades when otel packages not installed."
```

---

## Task 6: Wire Python OTel into Brain Server and MCP

**Files:**
- Modify: `brain/src/hippo_brain/__init__.py`
- Modify: `brain/src/hippo_brain/server.py`
- Modify: `brain/src/hippo_brain/mcp.py`

- [ ] **Step 1: Wire telemetry init into brain startup**

In `brain/src/hippo_brain/__init__.py`, in the `main()` function, after loading settings and before starting the server, add:

```python
if command == "serve":
    import uvicorn
    from hippo_brain.server import create_app
    from hippo_brain.telemetry import init_telemetry

    settings = _load_runtime_settings()

    # Initialize OTel if enabled (reads HIPPO_OTEL_ENABLED env var)
    otel_endpoint = settings.get("telemetry_endpoint", "")
    _otel_shutdown = init_telemetry("hippo-brain", endpoint=otel_endpoint)

    app = create_app(
        # ... existing args
    )
    uvicorn.run(app, host="127.0.0.1", port=settings["port"])
```

Also add `telemetry_endpoint` to the settings dict in `_load_runtime_settings()`:

```python
telemetry = config.get("telemetry", {})
# ... in the return dict:
"telemetry_endpoint": telemetry.get("endpoint", "http://localhost:4318"),
```

- [ ] **Step 2: Add trace spans to enrichment loop**

In `brain/src/hippo_brain/server.py`, add a helper at the top of the file:

```python
def _get_tracer():
    """Get OTel tracer if available, else return None."""
    try:
        if not os.environ.get("HIPPO_OTEL_ENABLED", "").strip() == "1":
            return None
        from opentelemetry import trace
        return trace.get_tracer("hippo-brain")
    except ImportError:
        return None
```

Add `import os` to the imports if not already present.

Then wrap key operations in the `_enrichment_loop` method with spans:

```python
# Inside the shell enrichment section, around the LLM call:
tracer = _get_tracer()
if tracer:
    with tracer.start_as_current_span(
        "enrichment.shell",
        attributes={
            "hippo.event_count": len(event_ids),
            "hippo.model": self.enrichment_model,
        },
    ):
        raw = await self.client.chat(messages=messages, model=self.enrichment_model)
else:
    raw = await self.client.chat(messages=messages, model=self.enrichment_model)
```

Apply the same pattern for Claude enrichment (span name `enrichment.claude`) and browser enrichment (span name `enrichment.browser`).

- [ ] **Step 3: Add trace spans to MCP tool calls**

In `brain/src/hippo_brain/mcp.py`, add the same `_get_tracer` helper, then wrap each tool function body:

```python
@mcp.tool()
async def search_knowledge(query: str, mode: str = "semantic", limit: int = 10) -> list[dict]:
    # ... existing docstring and setup ...
    tracer = _get_tracer()
    span_ctx = tracer.start_as_current_span(
        "mcp.search_knowledge",
        attributes={"hippo.query": query, "hippo.mode": mode},
    ) if tracer else nullcontext()
    with span_ctx:
        # ... existing implementation unchanged, just indented under `with`
```

Add `from contextlib import nullcontext` to imports.

- [ ] **Step 4: Run all Python tests**

Run: `uv run --project brain pytest brain/tests -v`
Expected: All tests pass (OTel code is no-op when env var unset).

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/__init__.py brain/src/hippo_brain/server.py brain/src/hippo_brain/mcp.py
git commit -m "feat(brain): wire OTel spans into enrichment loop and MCP tools

Trace spans on shell/claude/browser enrichment and all MCP tool calls.
No-op when HIPPO_OTEL_ENABLED is unset."
```

---

## Task 7: Mise Tasks for OTel Stack Management

**Files:**
- Modify: `mise.toml`

Add convenience tasks so `mise run otel:up` / `otel:down` / `otel:logs` work.

- [ ] **Step 1: Add otel tasks to mise.toml**

Append to `mise.toml`:

```toml
# ── Observability (OTel) ────────────────────────────────────────────

[tasks."otel:up"]
description = "Start the OTel observability stack (Grafana, Tempo, Loki, Prometheus, Collector)"
run = """
#!/usr/bin/env bash
set -euo pipefail
cd otel
docker compose up -d
echo ""
echo "=== OTel stack running ==="
echo "  Grafana:        http://localhost:3000 (admin/hippo)"
echo "  OTel Collector: localhost:4317 (gRPC) / localhost:4318 (HTTP)"
echo "  Collector health: http://localhost:13133"
echo ""
echo "To send telemetry from hippo services:"
echo "  Daemon: cargo build --features otel && set [telemetry] enabled = true"
echo "  Brain:  export HIPPO_OTEL_ENABLED=1"
"""

[tasks."otel:down"]
description = "Stop the OTel observability stack"
run = "cd otel && docker compose down"

[tasks."otel:logs"]
description = "Tail OTel stack logs"
run = "cd otel && docker compose logs -f --tail 50"

[tasks."otel:reset"]
description = "Stop the OTel stack and delete all stored data"
run = """
#!/usr/bin/env bash
set -euo pipefail
cd otel
docker compose down -v
echo "OTel stack stopped and all data volumes removed."
"""

[tasks."otel:status"]
description = "Show OTel stack container status"
run = "cd otel && docker compose ps"

[tasks."build:otel"]
description = "Build daemon with OTel instrumentation"
run = "cargo build -p hippo-daemon --features otel"

[tasks."build:otel:release"]
description = "Build daemon with OTel instrumentation (release)"
run = "cargo build -p hippo-daemon --features otel --release"
```

- [ ] **Step 2: Verify the tasks register**

Run: `mise tasks | grep otel`
Expected: Shows all 6 otel-related tasks.

- [ ] **Step 3: Commit**

```bash
git add mise.toml
git commit -m "feat(mise): add otel:up/down/logs/reset/status tasks

Convenience commands for managing the Docker Compose observability stack."
```

---

## Task 8: Grafana Dashboard

**Files:**
- Create: `otel/grafana/dashboards/hippo-overview.json`

A pre-built dashboard with panels for logs, traces, and metrics.

- [ ] **Step 1: Create the dashboard JSON**

Create `otel/grafana/dashboards/hippo-overview.json` with a dashboard containing these panels:

1. **Log Volume** — Loki log rate by service (`{service_name=~"hippo.*"}`)
2. **Error Logs** — Loki filtered to level=ERROR
3. **Recent Traces** — Tempo trace list for `hippo-daemon` and `hippo-brain`
4. **Request Rate** — Prometheus `hippo_` prefixed metrics (from OTel Collector prometheus exporter)
5. **Enrichment Latency** — Trace duration for `enrichment.*` spans

The dashboard JSON should be a valid Grafana dashboard model. Here is a minimal but functional version:

```json
{
  "annotations": { "list": [] },
  "editable": true,
  "fiscalYearStartMonth": 0,
  "graphTooltip": 1,
  "id": null,
  "links": [],
  "panels": [
    {
      "title": "Log Volume by Service",
      "type": "timeseries",
      "datasource": { "type": "loki", "uid": "loki" },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 0 },
      "targets": [
        {
          "expr": "sum by(service_name) (count_over_time({service_name=~\"hippo.*\"} [1m]))",
          "refId": "A"
        }
      ],
      "fieldConfig": {
        "defaults": { "custom": { "drawStyle": "bars", "fillOpacity": 30 } },
        "overrides": []
      }
    },
    {
      "title": "Error Logs",
      "type": "logs",
      "datasource": { "type": "loki", "uid": "loki" },
      "gridPos": { "h": 8, "w": 12, "x": 12, "y": 0 },
      "targets": [
        {
          "expr": "{service_name=~\"hippo.*\"} |= \"ERROR\" or {service_name=~\"hippo.*\"} | severity >= \"ERROR\"",
          "refId": "A"
        }
      ]
    },
    {
      "title": "Recent Traces",
      "type": "table",
      "datasource": { "type": "tempo", "uid": "tempo" },
      "gridPos": { "h": 10, "w": 24, "x": 0, "y": 8 },
      "targets": [
        {
          "queryType": "traceqlSearch",
          "filters": [
            { "id": "service-name", "tag": "service.name", "operator": "=~", "value": ["hippo-daemon", "hippo-brain", "hippo-mcp"] }
          ],
          "limit": 20,
          "refId": "A"
        }
      ]
    },
    {
      "title": "Enrichment Span Duration (p95)",
      "type": "timeseries",
      "datasource": { "type": "prometheus", "uid": "prometheus" },
      "gridPos": { "h": 8, "w": 12, "x": 0, "y": 18 },
      "targets": [
        {
          "expr": "histogram_quantile(0.95, sum by(le) (rate(hippo_span_duration_bucket{span_name=~\"enrichment.*\"}[5m])))",
          "legendFormat": "p95 enrichment",
          "refId": "A"
        }
      ]
    },
    {
      "title": "Service Logs (Live)",
      "type": "logs",
      "datasource": { "type": "loki", "uid": "loki" },
      "gridPos": { "h": 10, "w": 24, "x": 0, "y": 26 },
      "targets": [
        {
          "expr": "{service_name=~\"hippo.*\"}",
          "refId": "A"
        }
      ]
    }
  ],
  "schemaVersion": 39,
  "tags": ["hippo", "observability"],
  "templating": { "list": [] },
  "time": { "from": "now-1h", "to": "now" },
  "timepicker": {},
  "timezone": "browser",
  "title": "Hippo Overview",
  "uid": "hippo-overview",
  "version": 1
}
```

- [ ] **Step 2: Restart Grafana to pick up the dashboard**

Run:
```bash
cd otel && docker compose restart grafana && sleep 3
curl -s http://localhost:3000/api/dashboards/uid/hippo-overview | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('dashboard',{}).get('title','NOT FOUND'))"
```

Expected: Prints `Hippo Overview`.

- [ ] **Step 3: Commit**

```bash
git add otel/grafana/dashboards/hippo-overview.json
git commit -m "feat(otel): add pre-built Grafana dashboard for Hippo services

Panels: log volume, error logs, recent traces, enrichment latency,
live service logs."
```

---

## Task 9: LaunchAgent OTel Environment Wiring

**Files:**
- Modify: `launchd/com.hippo.daemon.plist`
- Modify: `launchd/com.hippo.brain.plist`
- Modify: `crates/hippo-daemon/src/install.rs`

When the OTel stack is running, the launchd plist needs `HIPPO_OTEL_ENABLED=1` and `OTEL_EXPORTER_OTLP_ENDPOINT` for the brain. The daemon reads from config.toml so it doesn't need env vars, but the brain does.

- [ ] **Step 1: Read the install module**

Read: `crates/hippo-daemon/src/install.rs`

Understand how plist templates are processed and what variables are substituted.

- [ ] **Step 2: Add OTEL env vars as optional plist placeholders**

In `launchd/com.hippo.brain.plist`, add optional OTel env vars to the EnvironmentVariables dict:

```xml
<key>HIPPO_OTEL_ENABLED</key>
<string>__HIPPO_OTEL_ENABLED__</string>
<key>OTEL_EXPORTER_OTLP_ENDPOINT</key>
<string>__OTEL_ENDPOINT__</string>
```

- [ ] **Step 3: Wire the substitution in install.rs**

In `install.rs`, add the OTel variable substitution. Read `config.telemetry.enabled` and `config.telemetry.endpoint` to fill in the placeholders:
- `__HIPPO_OTEL_ENABLED__` → `"1"` if enabled, `"0"` if not
- `__OTEL_ENDPOINT__` → the endpoint from config (default `http://localhost:4318`)

- [ ] **Step 4: Test a fresh install**

Run: `cargo build --release --features otel`

Then verify the generated plist has the env vars:
```bash
cargo run --release --features otel --bin hippo -- daemon install --force 2>&1 | grep -i otel
```

- [ ] **Step 5: Commit**

```bash
git add launchd/ crates/hippo-daemon/src/install.rs
git commit -m "feat(install): wire OTel env vars into LaunchAgent plists

Brain plist gets HIPPO_OTEL_ENABLED and OTEL_EXPORTER_OTLP_ENDPOINT
from config.toml [telemetry] section during install."
```

---

## Task 10: OTel README and Integration Test

**Files:**
- Create: `otel/README.md`
- Create: `crates/hippo-daemon/tests/telemetry_test.rs`

- [ ] **Step 1: Create the OTel README**

Create `otel/README.md`:

```markdown
# Hippo OTel Observability Stack

Optional Docker Compose stack for monitoring Hippo services with OpenTelemetry.

## Quick Start

```bash
# Start the stack
mise run otel:up

# Build daemon with OTel support
mise run build:otel

# Enable telemetry in config
hippo config edit
# Set: [telemetry] enabled = true

# For brain: set env var before starting
export HIPPO_OTEL_ENABLED=1

# Restart services
mise run restart

# Open Grafana
open http://localhost:3000
```

## Architecture

```
hippo-daemon ──┐
               ├── OTLP ──→ OTel Collector ──→ Tempo (traces)
hippo-brain  ──┤                            ──→ Loki (logs)
               │                            ──→ Prometheus (metrics)
hippo-mcp   ──┘
                                               Grafana (visualization)
```

## Services

| Service | Port | Purpose |
|---------|------|---------|
| OTel Collector | 4317 (gRPC), 4318 (HTTP) | Receives OTLP telemetry |
| Grafana | 3000 | Dashboards and exploration |
| Tempo | 3200 | Trace storage |
| Loki | 3100 | Log aggregation |
| Prometheus | 9090 | Metrics storage |

## Enabling Telemetry

### Daemon (Rust)

1. Build with OTel feature: `cargo build --features otel`
2. Set in `~/.config/hippo/config.toml`:

```toml
[telemetry]
enabled = true
endpoint = "http://localhost:4317"
```

### Brain / MCP (Python)

Set the environment variable:

```bash
export HIPPO_OTEL_ENABLED=1
# Optional: override endpoint (default: http://localhost:4318)
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

## Commands

```bash
mise run otel:up       # Start stack
mise run otel:down     # Stop stack
mise run otel:logs     # Tail logs
mise run otel:reset    # Stop + delete all data
mise run otel:status   # Show container status
```

## Reuse

This `otel/` directory is self-contained. To use it in another project:

1. Copy the `otel/` directory
2. Edit `otel/grafana/dashboards/` to add your dashboards
3. Run `docker compose up -d` from the `otel/` directory
4. Point your services at `localhost:4317` (gRPC) or `localhost:4318` (HTTP)
```

- [ ] **Step 2: Create Rust telemetry integration test**

Create `crates/hippo-daemon/tests/telemetry_test.rs`:

```rust
//! Integration test: verify OTel telemetry module initializes and shuts down cleanly.
//! Only compiled with --features otel.

#[cfg(feature = "otel")]
mod otel_tests {
    use hippo_daemon::telemetry;

    /// Test that init succeeds when pointing at a non-existent collector.
    /// The batch exporter buffers spans — it won't fail until export time.
    /// We just verify the init/shutdown cycle doesn't panic.
    #[test]
    fn test_telemetry_init_shutdown() {
        // Use a port nothing listens on
        let guard = telemetry::init("test-service", "http://localhost:19999")
            .expect("telemetry init should succeed even without a collector");
        guard.shutdown();
    }
}
```

- [ ] **Step 3: Run the integration test**

Run: `cargo test -p hippo-daemon --features otel --test telemetry_test`
Expected: Test passes.

- [ ] **Step 4: Run all tests to check for regressions**

Run: `cargo test` (without otel feature — default)
Expected: All existing tests pass. The telemetry test is skipped (cfg-gated).

Run: `uv run --project brain pytest brain/tests -v`
Expected: All Python tests pass.

- [ ] **Step 5: Commit**

```bash
git add otel/README.md crates/hippo-daemon/tests/telemetry_test.rs
git commit -m "docs(otel): add README and integration test

README covers quick start, architecture, reuse instructions.
Integration test verifies OTel init/shutdown cycle."
```

---

## Task 11: End-to-End Smoke Test

**Files:** None created — this is a verification task.

- [ ] **Step 1: Start the OTel stack**

Run: `mise run otel:up`
Expected: All 5 containers running.

- [ ] **Step 2: Build and run daemon with OTel**

Set `[telemetry] enabled = true` in `~/.config/hippo/config.toml`.

Run: `cargo run --features otel --bin hippo -- daemon run`

Expected: Log line `OpenTelemetry initialized: endpoint=http://localhost:4317, service=hippo-daemon` appears in stderr.

- [ ] **Step 3: Send a test event**

In another terminal:

```bash
hippo send-event shell --cmd "echo otel-test" --exit 0 --cwd /tmp --duration-ms 42
```

- [ ] **Step 4: Verify traces appear in Grafana**

Open http://localhost:3000, navigate to Explore → Tempo.

Search for traces with `service.name = hippo-daemon`.

Expected: At least one trace with spans for `handle_request` and `flush_events`.

- [ ] **Step 5: Verify logs appear in Grafana**

In Explore → Loki, query `{service_name="hippo-daemon"}`.

Expected: Log lines from the daemon visible.

- [ ] **Step 6: Stop daemon and OTel stack**

```bash
# Stop daemon (Ctrl+C)
mise run otel:down
```

- [ ] **Step 7: Restore config**

Set `[telemetry] enabled = false` in `~/.config/hippo/config.toml` (or leave it — it's your call).

---

## Summary of Architecture Decisions

| Decision | Rationale |
|----------|-----------|
| **Grafana LGTM stack** over SigNoz/Uptrace | Each component is best-in-class, independently configurable, widely adopted. Easy to swap one piece without replacing everything. |
| **OTel Collector as intermediary** | Decouples services from backends. Switch from Tempo to Jaeger by changing one config line. Adds batching, retrying, and health checking. |
| **Cargo feature flag (`otel`)** | Zero-cost when disabled. No new deps in the default build. CI can test both paths. |
| **Env var gating (`HIPPO_OTEL_ENABLED`)** | Python has no equivalent of Cargo features. Env var is the standard OTel pattern and works with launchd. |
| **OTLP gRPC for Rust, HTTP for Python** | Rust has mature tonic/gRPC support. Python's HTTP exporter is simpler and avoids grpcio build issues. Both are OTLP — the collector handles both protocols. |
| **Self-contained `otel/` directory** | Extractable to other projects. No dependency on Hippo code. Only needs Docker Compose. |
| **`tracing::instrument` over manual spans** | Rust daemon already uses `tracing` extensively. `#[instrument]` is zero-effort and captures function args automatically. |
| **Prometheus exporter on collector** (not direct Prometheus scrape) | Services don't need to expose HTTP metrics endpoints. The collector translates OTel metrics to Prometheus format. |
