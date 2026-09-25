# Hippo OTel stack — knowledge-health monitoring

Grafana (3030) · Prometheus (9090) · Loki · Tempo, plus the knowledge-health
exporter (host, :9835) that bridges SQLite-derived metrics and the /ask
recall probe into Prometheus.

Set `HIPPO_OTEL_GRAFANA_ADMIN_PASSWORD` before `mise run otel:up`. The
Compose stack accepts connections only on `127.0.0.1` and requires Grafana
authentication. Keep the variable available for subsequent Compose commands.

Dashboard inventory, the full alert-rule tables, enabling telemetry, and
on-call pointers: [`docs/observability.md`](../docs/observability.md).

## Components

| Piece | What it does |
|---|---|
| `scripts/hippo-metrics-exporter.py` | Read-only bridge: hippo.db → Prometheus text + JSON on :9835. Runs the **recall probe** (3 golden questions round-tripped through brain `/ask`, cached, async). Never writes to the DB. Under launchd as `com.hippo.metrics-exporter`. |
| `otel/prometheus.yml` | Scrapes `host.docker.internal:9835` (`hippo-knowledge-health` job) + the OTel collector. |
| `otel/grafana/dashboards/hippo-knowledge-health.json` | "Hippo — Knowledge Health": recall, capture, corpus, graveyard, identity/hygiene, snowball rows. |
| `otel/grafana/alerting/hippo-knowledge-alerts.yml` | 10 active rules + 2 pre-wired **paused** snowball rules. |
| `launchd/com.hippo.metrics-exporter.plist` | LaunchAgent template, installed by `hippo daemon install` when `[telemetry] enabled = true` (removed when disabled); `hippo doctor` verifies the port answers. |
| Runtime metric drift checks | See the [metric contract and contribution guidance](../docs/observability.md#dashboards). |

## Tasks

- `mise run metrics:exporter` — run the exporter in the foreground
- `mise run metrics:install` — alias for `hippo daemon install --force` (which installs the exporter agent)
- `mise run otel:restart` — recreate Prometheus + Grafana to reload `otel/` provisioning

## Snowball map — what lights up which metric

Metrics in the "Snowball" dashboard row emit **only when their backing table
exists**, so panels show No data (never fake zeros) until the feature ships:

| Dashboard metric | Unlock | Alert that arms on unlock |
|---|---|---|
| `hippo_kb_canary_found` | Build #0: a canary leak drill writes `~/.local/share/hippo/canary_drill.json` — `{"timestamp": <unix>, "stores": {"<store>": <bool found>}}` (the drill job itself is not written yet; run it BEFORE shipping agent write paths) | `hippo_kb_canary_leak` (already active; fires when the file exists and a canary is found) |
| `hippo_kb_retrieval_events` | Build #1: `retrieval_events` table (the feedback loop — critical path for every other claim) | — |
| `hippo_kb_epitaphs{confirmed}` | Build #3: dormancy detector + one-question exit probe + `epitaphs` table | `hippo_kb_epitaph_unconfirmed` (paused — unpause at ship) |
| `hippo_kb_push_fires{tapped}` | Build #8: preflight injection + useful/noise taps + `push_trials` | `hippo_kb_push_useful_floor` (paused — unpause at ship) |
| `hippo_kb_nodes_by_status`, `hippo_kb_contradictions_open` | Build #7: node status machine + write API (provisional-only) | — |
| `hippo_kb_bets` | Kill ledger: `bets` table + behavioral resolver | — |

## Why recall has a probe

2026-08-29: all 8 `/ask` synthesis queries timed out while every capture probe
was green — capture is industrialized, recall had no watchdog. The exporter's
golden-question probe (death-reason, decision-recall, freshness) treats the
retrieval path as a production dependency with an SLO:

- `hippo_kb_recall_up == 0` for 5m → **critical** (noDataState: Alerting, so a
  dead exporter also fires it)
- latency >15s for 15m → warning (`hippo_kb_recall_slow`)
- failure bursts classified by reason: `brain_down` / `llm_timeout` /
  `http_error_*` / `empty_answer`

## Notes

- Exporter opens the DB `mode=ro` + `PRAGMA query_only=ON` — it can never
  write, never lock, and never wedge capture.
- Metric families compute under per-family try/except; failures increment
  `hippo_kb_collector_errors_total{name}` instead of vanishing. That counter,
  like `hippo_kb_recall_failures_total`, is cumulative for the life of the
  process — a per-scrape value would make `increase()` identically zero and the
  alerts built on it unfireable.
- `_total` is reserved for those cumulative counters. Point-in-time readings are
  gauges with bare names (`hippo_kb_events`, `hippo_kb_knowledge_nodes`).
- Every scrape builds its own sample registry, so concurrent `/metrics` and
  `/metrics.json` requests cannot interleave into duplicate series.
- Database families are cached for `HIPPO_DB_TTL` (default 15s): the full scans
  over `events` are the expensive part and grow with the corpus.
- The recall probe is disabled by default (`HIPPO_PROBE_TTL=0`). Set a positive
  cooldown such as `HIPPO_PROBE_TTL=3600` to opt into synthetic inference.
  The cooldown starts after each batch completes; scrapes serve cached state.
  `hippo_kb_recall_probe_enabled` reports the setting, and recall result series
  are absent while disabled.
- Contamination definition: nodes linked (via `knowledge_node_agentic_sessions`)
  exclusively to projects dead ≥30d, where a project is a git repo or an
  agentic-session project dir (baseline ~2%, Aug 2026).
- Stranded hours: all-time shell hours on dead-30d projects (baseline ~1,800h).
