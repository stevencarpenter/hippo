# Capture Architecture

Reference for hippo's capture-reliability stack: how events land, what the system promises, and what fires when something breaks. For per-source detail (what each source captures and where it lands), see [`sources.md`](sources.md). For first-aid recipes when something goes wrong, see [`operator-runbook.md`](operator-runbook.md). For review-blocker rules every contributor needs to internalize, see [`anti-patterns.md`](anti-patterns.md).

## TL;DR

Every capture path writes two things in the same SQLite transaction: the event row and a `source_health` row. A background watchdog reads `source_health` once a minute, asserts ten named invariants, and writes alarms to `capture_alarms` on violations. A separate probe job sends synthetic events through each path every five minutes and records round-trip latency. Operators see all of this through `hippo doctor` and `hippo alarms`.

## The four layers

```
+--------------------+       +-------------------+      +-------------+
|  capture path      |       |  source_health    |      | capture_    |
|  (per source)      | ----> |  (one row/source) | <----| alarms      |
|  writes event +    |       |                   |      |             |
|  health in same Tx |       |                   |      +-------------+
+--------------------+       +-------------------+              ^
                                       ^                        |
                                       |                        |
                              +-----------------+      +-----------------+
                              |  watchdog       |      |  doctor / CLI   |
                              |  every 60s,     |----> |  reads alarms,  |
                              |  asserts I-1..  |      |  shows status   |
                              |  I-10           |      +-----------------+
                              +-----------------+
                                       ^
                                       |
                              +-----------------+
                              |  probe          |
                              |  every 5m,      |
                              |  synthetic      |
                              |  round-trip     |
                              +-----------------+
```

1. **Capture path** — the per-source code that writes events. Shell hook → daemon socket. FSEvents watcher → daemon. Native messaging → daemon. Each path writes to its source's events table AND to `source_health` in the same SQLite transaction. (See [`anti-patterns.md`](anti-patterns.md) AP-1: writing health from inside the user's interactive prompt is forbidden — health writes happen in the daemon's `flush_events`, never in `shell/hippo.zsh`.)
2. **`source_health` table** — one row per source, holds the latest "did the event land?" signal: `last_event_ts`, `consecutive_failures`, `events_last_1h`, `probe_ok`, `probe_last_run_ts`, `probe_lag_ms`. Single SQL ground truth.
3. **Watchdog** (`com.hippo.watchdog`, every 60 s) — asserts ten invariants against `source_health`, writes `capture_alarms` rows on violations. Rate-limited per invariant (one alarm per invariant per hour). Implemented in `crates/hippo-daemon/src/watchdog.rs`.
4. **Probe** (`com.hippo.probe`, every 5 minutes) — sends synthetic events through each capture path, measures end-to-end latency, records `probe_lag_ms` in `source_health`. Probe rows carry `probe_tag IS NOT NULL` and are filtered out of every user-facing query (RAG, MCP tools, `hippo events`). See `crates/hippo-daemon/src/probe.rs`. (See [`anti-patterns.md`](anti-patterns.md) AP-6: probe rows must never appear in user-facing queries.)

Operator interface: [`hippo doctor`](operator-runbook.md#doctor) for a snapshot, [`hippo alarms`](operator-runbook.md#alarms) for unacknowledged violations, [`hippo probe`](operator-runbook.md#probes) to run a one-off synthetic check.

## Tables

### `source_health`

One row per source. Updated in the same transaction as event writes; the watchdog reads it; the probe job updates `probe_*` columns.

| Column | Type | Meaning |
|---|---|---|
| `source` | TEXT PK | Source name: `shell`, `claude-tool`, `claude-session`, `claude-session-watcher`, `browser`, `workflow`, `watchdog`, `probe`. |
| `last_event_ts` | INTEGER | Epoch ms of the most recent successful event write for this source. |
| `consecutive_failures` | INTEGER | Bumped on each failure; reset on success. Backstop for I-1, I-4 freshness alarms. |
| `events_last_1h` / `_24h` | INTEGER | Rolling counts. Maintained by the daemon (incremented in `crates/hippo-daemon/src/daemon.rs::flush_events`, periodically rolled forward); the watchdog reads them, it does not compute them. |
| `probe_ok` | INTEGER | Last probe-job result: 1 = healthy, 0 = unhealthy. Source-specific definition (see "Probes" below). |
| `probe_last_run_ts` | INTEGER | When the probe last completed for this source. |
| `probe_lag_ms` | INTEGER | End-to-end latency of the most recent successful probe. |
| `updated_at` | INTEGER | Always bumped on any column update. |

### `capture_alarms`

Append-only ledger of invariant violations. The watchdog writes; `hippo alarms ack` flips the acknowledgment flag.

| Column | Meaning |
|---|---|
| `id` | PK |
| `invariant` | One of `I-1` … `I-10` |
| `source` | Affected source (or `watchdog` for I-7) |
| `fired_at` | First detection time |
| `last_seen_at` | Most recent confirmation; updated when the watchdog re-asserts the same violation |
| `last_notified_at` | Last macOS notification; rate-limit gate |
| `acknowledged_at` | NULL until `hippo alarms ack <id>` |
| `note` | Operator notes from `--note "..."` |

## Invariants (I-1..I-10)

Asserted by the watchdog every 60 s. Each has a formal predicate in `crates/hippo-daemon/src/watchdog.rs`. Violations create or refresh a `capture_alarms` row; the doctor surfaces them with `[!!]` severity.

| ID | Assertion | Threshold | Suppressed when | Backstop |
|---|---|---|---|---|
| **I-1** Shell liveness | If user has an active zsh and `hippo.zsh` is sourced, shell events must land within 60 s. | 60 s | No zsh process; HID idle > 5 min; night-hours window with no recent command. | Watchdog alarm + doctor `[!!] shell events`. |
| **I-2** Claude-session end-to-end | For every Claude JSONL with `mtime < 5 min`, a matching `claude_sessions` row must exist. | 5 min | No live JSONL. | Watchdog alarm naming each missing `session_id`. |
| **I-3** Claude-tool concurrency | If a live JSONL has received a `tool_use` line within 5 min, at least one matching `events.source_kind='claude-tool'` row must exist in that window. | 5 min | No live JSONL with recent `tool_use`. | Structured log only by default; opt-in alarm via `[watchdog] claude_tool_alarm = true`. |
| **I-4** Browser round-trip | If Firefox is up AND extension heartbeat is recent, `browser_events` rows must land within 2 min. | 2 min | Firefox not running; extension heartbeat absent or stale. | Watchdog alarm + doctor `[!!] browser events`. |
| **I-5** Drop visibility | Every event dropped (socket accept + crash, buffer overflow) increments a persistent counter. Zero tolerance for invisible drops. | every drop | — | OTel counter `hippo.daemon.events.dropped` (paired with `hippo.daemon.events.ingested`); see `crates/hippo-daemon/src/metrics.rs`. |
| **I-6** Buffer non-saturation | Sustained drop rate over any 5 min sliding window ≤ 0.1% of total event traffic. | 0.1% / 5 min | — | Watchdog alarm + doctor `[!!] drop-rate`. |
| **I-7** Watchdog liveness | The watchdog itself writes to `source_health WHERE source='watchdog'` at least every 60 s. | 180 s stale | — | Doctor only (a dead watchdog can't alarm about itself). |
| **I-8** Probe freshness | For each source with `probe_last_run_ts IS NOT NULL`: `probe_ok = 1` OR `probe_last_run_ts > now − 15 min`. | 15 min | — | Watchdog alarm + doctor `[!!] <source> probe`. |
| **I-9** Fallback file age | If any JSONL fallback file under `~/.local/share/hippo/` is > 24 h old AND the daemon socket is responsive, recovery is broken. | 24 h | Daemon down (fallback drain happens at startup). | Doctor `[!!] fallback files`. |
| **I-10** Capture/enrichment decoupling | Brain being down (HTTP 5xx/timeout) MUST NOT prevent `source_health` updates for capture sources. Architectural — verified via canary in CI, not at runtime. | — | — | Architectural enforcement; if violated, every other invariant becomes unreliable. |

## Probes

Synthetic round-trip verification, every 5 minutes per source.

- **Mechanism.** A `hippo probe --source <name>` invocation generates a synthetic event tagged with a per-run UUID in `probe_tag`, then waits for it to appear in the source's events table. End-to-end latency is recorded in `source_health.probe_lag_ms`.
- **Where they live.** Probe rows have `probe_tag IS NOT NULL`. Every user-facing query (RAG retrieval, MCP `search_events` / `search_knowledge` / `get_entities`, `hippo events`, `hippo ask`) filters them out at the daemon-side query path. A Semgrep rule blocks new query call-sites that omit the filter. (See [`anti-patterns.md`](anti-patterns.md) AP-6.)
- **Per-source `probe_ok` definition.** For shell: `pgrep -x zsh` non-empty AND `hippo.zsh` sourced AND HID idle < 5 min. For browser: Firefox running AND extension heartbeat fresh. For claude-session: at least one JSONL under `~/.claude/projects` with recent `mtime`. The watchdog computes these on every cycle.
- **Manual probe.** `hippo probe --source <name>` runs one cycle on demand. Useful when bringing a source back up after a configuration change.

## Backstops

The system promises observability, not correctness. If something breaks, the goal is for the user to see it within minutes, not 21 days (the duration of an actual past silent browser-capture outage that motivated this architecture).

- **`hippo doctor`** — interactive, < 2 s wall-clock, ten checks, exit code = fail count. `--explain` adds CAUSE/FIX/DOC per failure.
- **`hippo alarms list`** — unacknowledged alarms; exits 1 if any.
- **macOS notification** — opt-in via `[watchdog] notify_macos = true`. Rate-limited to one per invariant per hour.
- **OTel** — every counter is a Prometheus metric when the `otel` feature is built (default-on). See [`otel/README.md`](../../otel/README.md) for the local Grafana stack.

## See also

- [`sources.md`](sources.md) — what each source captures, where it lands, what fires.
- [`anti-patterns.md`](anti-patterns.md) — AP-1..AP-12: review blockers.
- [`operator-runbook.md`](operator-runbook.md) — doctor recipes, alarm responses, recovery flows.
- [`test-matrix.md`](test-matrix.md) — failure-mode-to-test mapping and the contract for adding new tests.
- [`docs/archive/capture-reliability-overhaul/`](../archive/capture-reliability-overhaul/) — historical design records (P0–P3 overhaul, post-mortems).
