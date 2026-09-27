# Observability

How to run Hippo's optional OpenTelemetry stack, which dashboards to open, and which provisioned alerts fire when capture paths degrade. Companion to [`otel/README.md`](../otel/README.md) (stack setup and commands) and [`capture/operator-runbook.md`](capture/operator-runbook.md) (first-aid when alarms fire).

Telemetry is **off by default**. Nothing is emitted until you build with OTel support, enable `[telemetry]` in config, and start the Docker stack.

## Quick start

```bash
export HIPPO_OTEL_GRAFANA_ADMIN_PASSWORD='<private password>'
mise run otel:up          # Grafana + Prometheus + collector on localhost
mise run build:otel       # daemon with OTel feature
hippo config edit         # [telemetry] enabled = true
export HIPPO_OTEL_ENABLED=1   # brain + MCP
mise run restart
open http://localhost:3030
```

Grafana requires the configured password for the `admin` account. The stack
publishes its ports on `127.0.0.1`, and anonymous access is disabled. Grafana
uses `GF_SECURITY_ADMIN_PASSWORD` only when the database is first created. If
you already ran the stack with the former `admin` / `hippo` credentials, change
the existing account password in Grafana's user preferences. [Grafana's
configuration reference](https://grafana.com/docs/grafana/latest/setup-grafana/configure-grafana/)
documents the first-run behavior.

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
| **Hippo Enrichment** | `hippo-enrichment` | http://localhost:3030/d/hippo-enrichment | Brain queue depth, LLM latency, enrichment throughput, MCP tool metrics, Jev/rules/local decision outcomes, stage latency, Jev tokens, error ratio, and classification backlog (see [Jev decisions](jev-decisions.md#observability-and-rollback)) |
| **Hippo Processes** | `hippo-processes` | http://localhost:3030/d/hippo-processes | `process.*` CPU/memory for daemon and brain |
| **Hippo — Knowledge Health** | `hippo-knowledge-health` | http://localhost:3030/d/hippo-knowledge-health | Recall probe (golden-question `/ask` round-trips), capture alarms/staleness, corpus size, project graveyard and dead-project contamination, identity fragmentation, redaction canary. Fed by the knowledge-health exporter, not OTel. |

Metric names in PromQL use Prometheus exporter suffixes (`_total`, `_milliseconds`, etc.). The shared OTel name/type contract lives in `tests/fixtures/otel-metric-names.json`. When adding an instrument, update that contract and exercise its runtime emission in `brain/tests/test_otel_dashboards.py` (Python) or `crates/hippo-daemon/tests/dashboard_metrics.rs` (Rust). These tests collect SDK measurements and check names, types, and units; Python validates parsed dashboard and alert references against the contract and collected Python samples. The standalone knowledge-health exporter owns its registry, which the Python tests verify against synthetic-database scrapes. Dashboard selector checks reject the retired `service_namespace` label, not harmless mentions in descriptions or label values.

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

- **Installed by** `hippo daemon install` as `com.hippo.metrics-exporter`, but only when `[telemetry] enabled = true`. With telemetry disabled the plist is removed. `hippo doctor` reports `[--] not installed (optional)` in that case, and `[!!]` if the plist is present but nothing answers on :9835.
- **Scraped by** the `hippo-knowledge-health` job in `otel/prometheus.yml` (`host.docker.internal:9835`).
- **Recall probe:** disabled by default (`HIPPO_PROBE_TTL=0`) so monitoring does not keep the inference server busy. To opt in, run `HIPPO_PROBE_TTL=3600 mise run metrics:exporter`, or set that environment variable in the exporter's LaunchAgent. Three golden questions are POSTed to brain `/ask` in a background thread; the TTL is a cooldown after the batch completes. Probe requests cap synthesis at 2,048 output tokens; interactive `/ask` callers are uncapped. The 45-second client timeout does not cancel backend generation. Failures are classified `brain_down` / `llm_timeout` / `http_error_*` / `empty_answer` / `degraded_answer`. `hippo_kb_recall_probe_enabled` reports whether probes are enabled. Recall result series are absent when disabled; recall panels show No data, and recall alerts use their existing `noDataState: OK` policy.
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
