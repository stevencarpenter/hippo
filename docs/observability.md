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
| **Hippo — Knowledge Health** | `hippo-knowledge-health` | http://localhost:3030/d/hippo-knowledge-health | Recall probe (golden-question `/ask` round-trips), capture alarms/staleness, corpus size, project graveyard and dead-project contamination, identity fragmentation, redaction canary. Fed by the knowledge-health exporter, not OTel. |

Metric names in PromQL use Prometheus exporter suffixes (`_total`, `_milliseconds`, etc.). The canonical allow-list and drift tests live in `brain/tests/test_otel_dashboards.py`.

Dashboards draw on two metric sources: OTel instruments in the daemon and brain (`hippo_daemon_*`, `hippo_brain_*`), and the knowledge-health exporter (`hippo_kb_*`, see below). Both are covered by the same drift tests.

`_total` is reserved for cumulative counters. A point-in-time reading is a gauge with a bare name — `hippo_kb_events`, not `hippo_kb_events_total` — because `increase()`/`rate()` over a non-monotonic `_total` series is silently always zero.

## Provisioned alert rules

Alert rules provision from every file in `otel/grafana/alerting/` on stack start — `hippo-capture-alerts.yml` (capture reliability) and `hippo-knowledge-alerts.yml` (knowledge health). They appear under **Alerting → Alert rules** in the **Hippo** folder.

| Alert | Fires when | `for` | Severity |
|-------|------------|-------|----------|
| Daemon events dropped | `rate(hippo_daemon_events_dropped_total[5m]) > 0` | 5m | warning |
| FS watcher events dropped | `rate(hippo_watcher_events_dropped_total[5m]) > 0` | 5m | warning |
| Watchdog not running | `rate(hippo_watchdog_run_total[5m]) < 0.001` | 5m | critical |
| Probe failure rate high | `ok=false` probe runs > 10% over 15m | 15m | warning |
| Capture invariant violation | `rate(hippo_watchdog_invariant_violation_total[15m]) > 0` | 15m | critical |

### Knowledge health (`hippo-knowledge-alerts.yml`)

| Alert | Fires when | `for` | Severity |
|-------|------------|-------|----------|
| Recall path down | `min_over_time(hippo_kb_recall_up[5m]) < 1` while the exporter scrape target is up | 5m | critical |
| Exporter not scraping | `up{job="hippo-knowledge-health"} < 1` for 10m | 10m | warning |
| Recall path degraded | `avg_over_time(hippo_kb_recall_latency_milliseconds[15m]) > 15000` (successful probes only) | 15m | warning |
| Recall probe failure burst | `sum(increase(hippo_kb_recall_failures_total[1h])) > 3` | 5m | warning |
| Capture alarm backlog | `hippo_kb_capture_alarms_active` above threshold | 4h | warning |
| Capture stale | `max(hippo_kb_capture_source_last_event_age_milliseconds)` above threshold | 10m | warning |
| Graveyard contamination | `hippo_kb_dead_project_node_ratio` above threshold | 1h | warning |
| Stranded-hours jump | `delta(hippo_kb_stranded_hours{window="30d"}[7d])` above threshold | 1h | info |
| Secretish env keys present | `hippo_kb_env_secretish_keys > 0` | 5m | warning |
| Canary leak | `max(hippo_kb_canary_found) > 0` | 0m | critical |
| Collector errors | `sum(increase(hippo_kb_collector_errors_total[15m])) > 0` | 5m | warning |

Two further rules (`hippo_kb_epitaph_unconfirmed`, `hippo_kb_push_useful_floor`) ship `isPaused: true` — they target snowball metrics whose backing tables do not exist yet. Unpause them when the feature lands.

All rules use `noDataState: OK`. Dead-exporter detection moved out of `hippo_kb_recall_down` (which previously used `noDataState: Alerting` for that) into the dedicated `hippo_kb_exporter_down` rule on `up{job="hippo-knowledge-health"}` — that keeps the OTel-stack-without-exporter deployment (`[telemetry] enabled = false`) from paging critical, while still surfacing the dead-exporter case as a warning.

All capture rules use `noDataState: OK` so a stack with telemetry disabled does not page. When OTel is enabled but a rule has no series, treat that as "instrument not emitting" rather than healthy silence.

**Notification routing:** this repo provisions rules only. Wire contact points and notification policies in Grafana UI (or add provisioning YAML) when you want Slack/PagerDuty delivery.

## Knowledge-health exporter

`scripts/hippo-metrics-exporter.py` is a stdlib-only Python bridge that reads `hippo.db` read-only (`mode=ro` + `PRAGMA query_only=ON`; it can never write or lock capture) and serves Prometheus text on `127.0.0.1:9835`. It covers what the OTel capture instruments cannot: what is *in* the knowledge base and whether it can still be recalled.

- **Installed by** `hippo daemon install` as `com.hippo.metrics-exporter`, but only when `[telemetry] enabled = true` — the recall probe spends local LLM inference on a timer, so it is not imposed on users who have not opted into observability. With telemetry disabled the plist is removed. `hippo doctor` reports `[--] not installed (optional)` in that case, and `[!!]` if the plist is present but nothing answers on :9835.
- **Scraped by** the `hippo-knowledge-health` job in `otel/prometheus.yml` (`host.docker.internal:9835`).
- **Recall probe:** three golden questions are POSTed to brain `/ask` on a TTL (default 120s) in a background thread, so a scrape never blocks on the LLM. Failures are classified `brain_down` / `llm_timeout` / `http_error_*` / `empty_answer`.
- **Snowball metrics** (`hippo_kb_epitaphs`, `hippo_kb_bets`, …) are emitted only when their backing table exists, so an unshipped feature shows No data rather than a fake zero. They live in a collapsed dashboard row.
- **Run in the foreground** for debugging: `mise run metrics:exporter`. Reload Grafana/Prometheus provisioning after editing `otel/`: `mise run otel:restart`.

Endpoints: `/metrics` (Prometheus), `/metrics.json` (same samples as JSON), `/healthz`.

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
