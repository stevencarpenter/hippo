# CLAUDE.md

Read [AGENTS.md](AGENTS.md) for project structure, canonical mise commands,
architecture, storage paths, and coding conventions.

## MCP Server

Start the stdio server from the repository root:

```bash
uv run --project brain hippo-mcp
```

See [MCP setup](README.md#mcp-server) for the client configuration and
[the tool reference](docs/mcp-reference.md) for arguments and return values.
MCP reads SQLite directly and calls the configured inference server for retrieval
and synthesis. It does not require the brain HTTP server. Logs go to stderr.

To enable MCP telemetry, add this field to the `hippo` entry in `mcpServers`:

```json
{"env": {"HIPPO_OTEL_ENABLED": "1"}}
```

The MCP process needs this spawn environment even when brain telemetry is enabled.
See [observability](docs/observability.md) for collector and dashboard setup.

## Capture References

- [Source contracts](docs/capture/sources.md): ingestion paths, storage, probes,
  and manual recovery. All agentic sources share `agentic_sessions`.
- [Runtime defaults](config/config.default.toml): source roots, poll intervals,
  idle thresholds, and `enabled` controls for Codex, Cursor, and other sources.
- [Schema reference](docs/schema.md): version constants and migration contracts.
  Change the Rust and Python schema versions together.
- [Operator runbook](docs/capture/operator-runbook.md): doctor diagnostics and
  alarm recovery. `hippo doctor --explain` prints CAUSE/FIX/DOC for failures.
- [Capture anti-patterns](docs/capture/anti-patterns.md): review blockers for
  capture, enrichment, and probe filtering.

The `SessionStart` hook in `shell/claude-session-hook.sh` only logs invocation;
`check_session_hook_log` uses that log to verify hook activity. It does not spawn
a tailer. `hippo daemon install` configures the hook in `~/.claude/settings.json`.

## Observability / OTel

### Metric naming (OTel → Prometheus)

The OTel → Prometheus exporter appends a unit suffix to every instrument name:

| OTel unit | Prometheus suffix appended |
|---|---|
| `ms` | `_milliseconds` |
| `By` | `_bytes` |
| counter (any unit) | `_total` |
| `1` | `_ratio` (avoid) |

**Do not use `unit="1"` for scores or raw counts.** Unit `"1"` produces a misleading `_ratio` suffix on the Prometheus side (e.g., a 0–100 health score or an alarm count would become `hippo_daemon_health_grade_ratio`, not `hippo_daemon_health_grade`). Use an explicit descriptive unit or omit the unit entirely.

Dashboard PromQL queries must use the suffixed Prometheus name. `brain/tests/test_otel_dashboards.py` enforces dashboard ↔ emitter name agreement. Add new metrics there when adding new instruments.

### Bench results

Use `hippo-bench export-dashboard` for leaderboard, per-run, and per-model results.
It generates a self-contained HTML dashboard from the SQLite results datastore.
See [the bench reference](brain/src/hippo_brain/bench/README.md).
