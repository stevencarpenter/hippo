---
name: monitoring-hippo
description: Use when user asks if hippo is working, running, healthy, or needs debugging. Provides commands to check daemon, brain, the inference server (oMLX or LM Studio), logs, and enrichment status.
---

# Monitoring Hippo

Use this skill when the user asks if Hippo is working, running, healthy, or needs debugging.

## Which commands you can run

Hippo installs a `hippo` binary on PATH (`~/.local/bin/hippo`) and runs as
launchd services (`com.hippo.*`). Everything up to and including
[Common Issues](#common-issues) works from **any directory** on **any install**
— `hippo`, `launchctl`, `curl`, and `sqlite3` only need the installed binary
and the runtime data directory.

The [From a source checkout](#from-a-source-checkout) section is different:
`mise run …` and the OTEL stack need a clone of the hippo repository. A release
install (`curl | bash`) has no checkout, so **check whether one exists before
suggesting those commands**:

```bash
# Substitute the user's checkout path throughout; do not assume a location.
git -C "$HIPPO_CHECKOUT" rev-parse --show-toplevel 2>/dev/null
```

If there is no checkout, everything in that section is unavailable. Use the
launchctl equivalents noted inline instead.

## Start Here: `hippo doctor`

The canonical health check. Runs diagnostic checks across the daemon, brain,
inference server, and per-source data freshness in one shot:

```bash
hippo doctor      # full diagnostics — start here
hippo status      # daemon status only (faster)
```

If `doctor` is green, you're done. Use the deeper checks below only to chase a
specific failure it reports.

## Quick Health Check

Run these commands to verify everything is operational:

### 1. Check running processes
```bash
ps aux | grep -E "(hippo|lmstudio|omlx)" | grep -v grep
```

### 2. Check if daemon socket exists and is responsive
```bash
ls -la ~/.local/share/hippo/daemon.sock
```

### 3. Check brain HTTP server
```bash
curl -s http://localhost:9175/health
```

### 4. Check inference server API
```bash
# omlx default (LM Studio uses port 1234)
curl -s http://localhost:42069/v1/models
```

## Log Monitoring

### Brain stderr (enrichment activity)
```bash
tail -50 ~/.local/share/hippo/brain.stderr.log
```
Look for: `enriched X segments -> node N` and `embedded node N into vector store`

### Brain stdout (HTTP requests)
```bash
tail -20 ~/.local/share/hippo/brain.stdout.log
```

### Daemon logs
```bash
tail -20 ~/.local/share/hippo/daemon.stderr.log
```

## Database Status

There is one enrichment queue **per source**, each with a `status` column
(`pending` / `processing` / `done` / `failed` / `skipped`):
`enrichment_queue` (shell), `claude_enrichment_queue`,
`browser_enrichment_queue`, `workflow_enrichment_queue`,
`agentic_enrichment_queue` (Codex / opencode).

### Check enrichment queue depth (all sources)
```bash
for q in enrichment_queue claude_enrichment_queue browser_enrichment_queue \
         workflow_enrichment_queue agentic_enrichment_queue; do
  echo "== $q =="
  sqlite3 ~/.local/share/hippo/hippo.db "SELECT status, COUNT(*) FROM $q GROUP BY status;"
done
```

### Check knowledge nodes count
```bash
sqlite3 ~/.local/share/hippo/hippo.db "SELECT COUNT(*) FROM knowledge_nodes;"
```

### Inspect failed enrichment (with error messages)
```bash
sqlite3 ~/.local/share/hippo/hippo.db "SELECT id, retry_count, error_message FROM claude_enrichment_queue WHERE status = 'failed' ORDER BY updated_at DESC LIMIT 10;"
```

## Service Management (launchd)

Hippo runs as launchd agents under `gui/$(id -u)`: `com.hippo.daemon`,
`com.hippo.brain`, `com.hippo.omlx`, plus `watchdog`, `probe`,
`claude-session-watcher`, `gh-poll`, `opencode-poll`, `codex-session`.

These work from anywhere and are the right tool on a release install:

```bash
launchctl list | grep com.hippo                       # which services are loaded
launchctl kickstart -k "gui/$(id -u)/com.hippo.brain" # restart one service
```

To restart everything without a checkout, kickstart each loaded agent:

```bash
launchctl list | grep -o 'com\.hippo\.[a-z-]*' | while read -r svc; do
  launchctl kickstart -k "gui/$(id -u)/${svc}"
done
```

## Common Issues

| Symptom | Check | Fix |
|---------|-------|-----|
| "brain not reachable" in daemon logs | Brain on port 9175? `curl localhost:9175/health` | `launchctl kickstart -k "gui/$(id -u)/com.hippo.brain"` |
| No enrichment happening | `brain.stderr.log` for errors; queue depth growing? | Restart brain (kickstart), then `hippo doctor` |
| Inference server not responding | `curl http://localhost:42069/v1/models` (`:1234` for LM Studio) | `launchctl kickstart -k "gui/$(id -u)/com.hippo.omlx"`, ensure a model is loaded |
| Socket not found | Daemon loaded? `launchctl list \| grep daemon` | `launchctl kickstart -k "gui/$(id -u)/com.hippo.daemon"` |
| Multiple things wedged | — | Kickstart every agent (above); from a checkout, `mise run restart` |

## Verification Commands

Run these to confirm hippo is fully operational:

1. Everything at once: `hippo doctor`
2. Daemon: `hippo status`
3. Brain health: `curl -s http://localhost:9175/health`
4. Inference server: `curl -s http://localhost:42069/v1/models | jq '.data[].id'`  (`:1234` for LM Studio)
5. Recent enrichment: `tail -10 ~/.local/share/hippo/brain.stderr.log | grep enriched`

---

# From a source checkout

**Everything below requires a clone of the hippo repository.** Confirm one
exists and `cd` to it first; these commands fail from anywhere else, and a
release install has no checkout at all. Substitute the user's own path.

## Service management via mise

```bash
mise run start     # bootstrap all services
mise run stop      # bootout all services
mise run restart   # stop + start all services
mise run monitor   # live enrichment pipeline view (refreshes every 5s)
mise run nuke      # SIGKILL everything + remove socket (hard reset; data preserved)
```

## OTEL Stack Monitoring

The OTEL (OpenTelemetry) stack provides Grafana dashboards for Hippo metrics.
It runs from `otel/docker-compose.yml` in the checkout and requires Docker.

### Start/Stop OTEL Stack
```bash
mise run otel:up    # Start OTEL stack (Grafana, Loki, Tempo, Prometheus, Collector)
mise run otel:down  # Stop OTEL stack
```

### Check OTEL Health
```bash
# Collector health endpoint
curl -s http://localhost:13133/

# Should return: {"status":"Server available","upSince":"...","uptime":"..."}
```

### Check Grafana Dashboards
- Hippo Overview: http://localhost:3030/d/hippo-overview
- Hippo Daemon: http://localhost:3030/d/hippo-daemon
- Hippo Enrichment Pipeline: http://localhost:3030/d/hippo-enrichment

Login: user `admin`; the password is whatever `GF_SECURITY_ADMIN_PASSWORD` is
set to in `otel/docker-compose.yml`. This is a localhost-only development
stack with anonymous access enabled.

### Check OTEL Logs
```bash
mise run otel:logs
```

### OTEL Containers Status
```bash
mise run otel:status
cd otel && docker compose ps
```

### Check Prometheus Metrics (Query via API)
```bash
# List all hippo metrics
curl -s 'http://localhost:9090/api/v1/label/__name__/values' | jq -r '.values[] | select(. | startswith("hippo"))'

# Check specific metrics
curl -s 'http://localhost:9090/api/v1/query?query=hippo_daemon_buffer_size' | jq .
curl -s 'http://localhost:9090/api/v1/query?query=hippo_brain_enrichment_queue_depth' | jq .

# Check queue depth (should match daemon status)
curl -s 'http://localhost:9090/api/v1/query?query=hippo_brain_enrichment_queue_depth' | jq -r '.data.result[]?.value[1]'
```

## OTEL Troubleshooting

**Dashboards showing no data?**

1. Check if daemon is running with OTEL:
```bash
tail ~/.local/share/hippo/daemon.stderr.log | grep telemetry
```
Should see: `OpenTelemetry initialized: endpoint=http://localhost:4317`

2. Check Prometheus port is mapped:
```bash
docker port hippo-otel-prometheus-1
```
Should show: `9090/tcp -> 0.0.0.0:9090`

If port is missing, fix docker-compose.yml:
```bash
cd otel && docker compose up -d prometheus
```

3. Check daemon -> collector connection:
```bash
lsof -i :4317 | grep -v LISTEN
```
Should show hippo connections to localhost:4317

4. If dashboards still empty or Loki timestamps drift:
```bash
mise run otel:reset  # Warning: clears all OTEL data
mise run otel:up
```

### OTEL verification
- OTEL collector: `curl -s http://localhost:13133/`
- Grafana: `curl -s http://localhost:3030/api/health` (unauthenticated liveness;
  anonymous `/api/search` 404s on this stack, so list dashboards from the UI)
