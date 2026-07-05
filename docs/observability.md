# Observability

How to run Hippo's optional OpenTelemetry stack, which dashboards to open, and which provisioned alerts fire when capture paths degrade. Companion to [`otel/README.md`](../otel/README.md) (stack setup and commands) and [`capture/operator-runbook.md`](capture/operator-runbook.md) (first-aid when alarms fire).

Telemetry is **off by default**. Nothing is emitted until you build with OTel support, enable `[telemetry]` in config, and start the Docker stack.

## Quick start

```bash
mise run otel:up          # Grafana + Prometheus + collector on localhost
mise run build:otel       # daemon with OTel feature
hippo config edit         # [telemetry] enabled = true
export HIPPO_OTEL_ENABLED=1   # brain + MCP
mise run restart
open http://localhost:3030
```

Default Grafana login: `admin` / `hippo` (anonymous Admin is also enabled for local use).

## Architecture

```
hippo-daemon ──┐
               ├── OTLP ──→ OTel Collector ──→ Tempo (traces)
hippo-brain  ──┤                            ──→ Loki (logs)
hippo-mcp   ──┘                            ──→ Prometheus (metrics)
                                               Grafana (dashboards + alerts)
```

| Service | Port | Purpose |
|---------|------|---------|
| Grafana | **3030** | Dashboards, Explore, provisioned alert rules |
| Prometheus | 9090 | Metrics storage (30d / 10GB default retention) |
| OTel Collector | 4317 (gRPC), 4318 (HTTP) | OTLP ingest |
| Tempo | 3200 | Trace storage |
| Loki | 3100 | Log aggregation |

Persistent data: `~/.local/share/hippo/otel/`. Stack restarts do not wipe state.

## Dashboards

All dashboards provision automatically from `otel/grafana/dashboards/` into the **Hippo** folder. No manual import.

| Dashboard | UID | URL | What it shows |
|-----------|-----|-----|---------------|
| **Hippo Overview** | `hippo-overview` | http://localhost:3030/d/hippo-overview | Health grade, capture lag, probe success/lag, invariant violations, alarm firings, daemon drops |
| **Hippo Daemon** | `hippo-daemon` | http://localhost:3030/d/hippo-daemon | Event ingest/drop rates, flush latency, redactions, fallback writes, watcher throughput |
| **Hippo Enrichment** | `hippo-enrichment` | http://localhost:3030/d/hippo-enrichment | Brain queue depth, LLM latency, enrichment throughput, MCP tool metrics |
| **Hippo Processes** | `hippo-processes` | http://localhost:3030/d/hippo-processes | `process.*` CPU/memory for daemon and brain |

Metric names in PromQL use Prometheus exporter suffixes (`_total`, `_milliseconds`, etc.). The canonical allow-list and drift tests live in `brain/tests/test_otel_dashboards.py`.

## Provisioned alert rules

Alert rules provision from `otel/grafana/alerting/hippo-capture-alerts.yml` on stack start. They appear under **Alerting → Alert rules** in the **Hippo** folder.

| Alert | Fires when | `for` | Severity |
|-------|------------|-------|----------|
| Daemon events dropped | `rate(hippo_daemon_events_dropped_total[5m]) > 0` | 5m | warning |
| FS watcher events dropped | `rate(hippo_watcher_events_dropped_total[5m]) > 0` | 5m | warning |
| Watchdog not running | `rate(hippo_watchdog_run_total[5m]) < 0.001` | 5m | critical |
| Probe failure rate high | `ok=false` probe runs > 10% over 15m | 15m | warning |
| Capture invariant violation | `rate(hippo_watchdog_invariant_violation_total[15m]) > 0` | 15m | critical |

All rules use `noDataState: OK` so a stack with telemetry disabled does not page. When OTel is enabled but a rule has no series, treat that as "instrument not emitting" rather than healthy silence.

**Notification routing:** this repo provisions rules only. Wire contact points and notification policies in Grafana UI (or add provisioning YAML) when you want Slack/PagerDuty delivery.

## Enabling telemetry

### Daemon (Rust)

```toml
# ~/.config/hippo/config.toml
[telemetry]
enabled = true
endpoint = "http://localhost:4317"
```

Build: `mise run build:otel` or `cargo build --features otel`.

### Brain / MCP (Python)

```bash
export HIPPO_OTEL_ENABLED=1
# optional: export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
```

## Commands

```bash
mise run otel:up       # start stack
mise run otel:down     # stop stack
mise run otel:status   # container health
mise run otel:logs     # tail compose logs
mise run otel:backup   # snapshot persisted data
```

See [`otel/README.md`](../otel/README.md) for retention overrides, reset workflow, and process-metric details.

## When alerts fire

1. `hippo doctor --explain` — isolated checks with CAUSE/FIX per failure
2. `hippo alarms list` — unacknowledged capture alarms from SQLite ground truth
3. [`capture/operator-runbook.md`](capture/operator-runbook.md) — version mismatch, enrichment wedge, probe failures

SQLite `source_health` remains the correctness source during OTel outages; dashboards and alerts are time-series views on the same invariants.
