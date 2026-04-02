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
