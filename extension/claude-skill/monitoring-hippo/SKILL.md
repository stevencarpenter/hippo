---
name: monitoring-hippo
description: Use when user asks if hippo is working, running, healthy, or needs debugging. Provides commands to check daemon, brain, LM Studio, logs, and enrichment status.
---

# Monitoring Hippo

Use this skill when the user asks if Hippo is working, running, healthy, or needs debugging.

## Quick Health Check

Run these commands to verify everything is operational:

### 1. Check running processes
```bash
ps aux | grep -E "(hippo|lmstudio)" | grep -v grep
```

### 2. Check if daemon socket exists and is responsive
```bash
ls -la ~/.local/share/hippo/daemon.sock
```

### 3. Check brain HTTP server
```bash
curl -s http://localhost:9175/health
```

### 4. Check LM Studio API
```bash
curl -s http://localhost:1234/v1/models
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

### Check enrichment queue
```bash
sqlite3 ~/.local/share/hippo/hippo.db "SELECT status, COUNT(*) FROM claude_segments GROUP BY status;"
```

### Check knowledge nodes count
```bash
sqlite3 ~/.local/share/hippo/hippo.db "SELECT COUNT(*) FROM knowledge_nodes;"
```

### Check pending segments
```bash
sqlite3 ~/.local/share/hippo/hippo.db "SELECT id, source_type, LENGTH(content) as len FROM claude_segments WHERE status = 'pending' ORDER BY id DESC LIMIT 10;"
```

## Common Issues

| Symptom | Check | Fix |
|---------|-------|-----|
| "brain not reachable" in daemon logs | Brain running on port 9175? | `mise run run:brain` |
| No enrichment happening | Check `brain.stderr.log` for errors | Restart brain |
| LM Studio not responding | Check `http://localhost:1234/v1/models` | Start LM Studio, load a model |
| Socket not found | Daemon running? | `mise run run:daemon` |

## OTEL Stack Monitoring

The OTEL (OpenTelemetry) stack provides Grafana dashboards for Hippo metrics.

### Start/Stop OTEL Stack
```bash
mise run otel:up    # Start OTEL stack (Grafana, Loki, Tempo, Prometheus, Collector)
mise run otel:down # Stop OTEL stack
```

### Check OTEL Health
```bash
# Collector health endpoint
curl -s http://localhost:13133/

# Should return: {"status":"Server available","upSince":"...","uptime":"..."}
```

### Check Grafana Dashboards
- Hippo Overview: http://localhost:3000/d/hippo-overview
- Hippo Daemon: http://localhost:3000/d/hippo-daemon
- Hippo Enrichment Pipeline: http://localhost:3000/d/hippo-enrichment

Login: admin/admin

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

## Verification Commands

Run these to confirm hippo is fully operational:

1. Daemon: `cargo run --bin hippo -- status`
2. Brain health: `curl -s http://localhost:9175/health`
3. LM Studio: `curl -s http://localhost:1234/v1/models | jq '.data[].id'`
4. Recent enrichment: `tail -10 ~/.local/share/hippo/brain.stderr.log | grep enriched`
5. OTEL collector: `curl -s http://localhost:13133/`
6. Grafana: `curl -s -u admin:admin 'http://localhost:3000/api/search' | jq '.[] | .title'`
