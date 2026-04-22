# Synthetic Probes

**TL;DR:** Synthetic probes round-trip real events through each capture pipeline and verify them as SQLite rows, providing end-to-end liveness verification that socket pings cannot. All probe-generated rows are tagged via a `probe_tag` column and excluded from every user-facing query.

## Philosophy

A probe is not a ping. Pinging the daemon socket (`commands::probe_socket` in `crates/hippo-daemon/src/commands.rs:54`) confirms the socket accepts connections but says nothing about buffer/flush/insert paths. A probe must exercise the real pipeline — event accepted over socket, buffered, flushed, inserted into SQLite — and confirm the row is queryable before declaring success.

Probe rows are permanent citizens of the DB. Tagged, excluded from user queries, enrichment, and lesson graduation. Never enriched, never embedded, never returned by `hippo ask`, `hippo query`, `mcp__hippo__*`, or brain RAG. They exist solely to give the watchdog (`04-watchdog.md`) and `source_health` observable liveness signals.

## Probe Tag

New `probe_tag TEXT` column (default NULL) added to three tables:

```sql
-- schema v7 → v8 (the source_health migration)
ALTER TABLE events ADD COLUMN probe_tag TEXT;
ALTER TABLE claude_sessions ADD COLUMN probe_tag TEXT;
ALTER TABLE browser_events ADD COLUMN probe_tag TEXT;
```

`probe_tag` holds the UUID submitted with the synthetic event (lowercase hyphenated). For real events: NULL.

### Migration

Non-destructive `ALTER TABLE ADD COLUMN` is safe on live DB (no table rebuild). Bumps `EXPECTED_VERSION` to `8` in three places: `crates/hippo-core/src/storage.rs:16`, `crates/hippo-core/src/schema.sql:438` (`PRAGMA user_version`), `brain/src/hippo_brain/schema_version.py::EXPECTED_SCHEMA_VERSION`.

### Grep List: Places That Need `WHERE probe_tag IS NULL`

Exhaustive:

| File | Context |
|---|---|
| `crates/hippo-daemon/src/commands.rs` | `GetStatus` — `events_today` count |
| `crates/hippo-daemon/src/commands.rs` | `handle_query_raw` — keyword search |
| `crates/hippo-daemon/src/commands.rs` | `handle_sessions` — session listing |
| `crates/hippo-daemon/src/commands.rs` | `handle_events` — event listing |
| `brain/src/hippo_brain/server.py` | `/query` endpoint |
| `brain/src/hippo_brain/server.py` | `/ask` endpoint |
| `brain/src/hippo_brain/enrichment.py` | `claim_pending_events_by_session` |
| `brain/src/hippo_brain/claude_sessions.py` | `claim_pending_claude_segments` |
| `brain/src/hippo_brain/browser_enrichment.py` | `claim_pending_browser_events` |
| `brain/src/hippo_brain/mcp_queries.py` | `search_events_impl`, `search_knowledge_lexical`, `get_entities_impl`, `get_lessons_impl` |
| `brain/src/hippo_brain/rag.py` | `ask()` — RAG context retrieval |
| `brain/src/hippo_brain/retrieval.py` | `search()` — hybrid FTS5/vec0 |

**Upstream filter:** Daemon flush path checks `probe_tag IS NOT NULL` before enqueuing into any enrichment queue. If probes never enter queues, no downstream knowledge node is ever sourced from a probe row — single upstream filter is load-bearing for all downstream cleanliness.

## Probe Orchestrator

New CLI: `hippo probe [--source <name>]`. Without `--source`, runs all four in sequence.

Launchd plist `launchd/com.hippo.probe.plist`:

```xml
<key>StartInterval</key><integer>300</integer>   <!-- every 5 min -->
<key>RunAtLoad</key><false/>                     <!-- avoid install-time storm -->
```

For each probe, orchestrator writes to `source_health`:

```sql
UPDATE source_health SET
    probe_ok          = :ok,         -- 1 or 0
    probe_lag_ms      = :lag_ms,     -- NULL if probe failed before measurement
    probe_last_run_ts = :now_ms,
    updated_at        = :now_ms
WHERE source = :source;
```

## Per-Source Probe Specifications

### Shell Probe

**Send:** `hippo send-event shell --cmd "__hippo_probe__" --cwd "$HOME" --duration-ms 0 --exit 0 --probe-tag <uuid>`

`--probe-tag` is a new CLI arg on `Commands::SendEvent`. Passed in `EventEnvelope` so daemon flush writes `events.probe_tag`.

**Exercises:** zsh-hook-equivalent → Unix socket → daemon buffer → flush → `events` INSERT.

**Verify:** Poll (10 s deadline):

```sql
SELECT id, created_at FROM events
WHERE envelope_id = :uuid AND probe_tag = :uuid
LIMIT 1;
```

`probe_lag_ms = created_at - probe_start_ms`.

**Catches:** daemon running, socket reachable, flush working, INSERT succeeding.

**Does NOT catch:** Real zsh hook (`shell/hippo.zsh`) not invoked — probe bypasses shell. Broken hook (wrong HIPPO_BIN path, precmd not registered) undetected. Separate integration test must trigger real shell.

---

### Claude-Tool Probe

**Send:** `hippo send-event shell --cmd "__hippo_probe_claude_tool__" --source-kind claude-tool --tool-name Bash --probe-tag <uuid>`

New flags (`--source-kind`, `--tool-name`) produce `events` row with `source_kind = 'claude-tool'`.

**Exercises:** Same socket/buffer/flush as shell, with distinct source_kind — verifies daemon correctly handles the claude-tool code path (which brain enrichment skips differently).

**Verify:** Same pattern, filter `AND source_kind = 'claude-tool'`.

---

### Claude-Session Probe (Assertion-based)

JSONL is written by Claude Code, not hippo — cannot inject synthetic JSONL. Instead, assertion on live state:

For every `~/.claude/projects/*/*.jsonl` with `mtime` within last 5 min, assert `claude_sessions` row exists where `source_file = <path>` and `end_time >= (mtime_ms - 300_000)` (5-min tolerance):

```sql
SELECT COUNT(*) FROM claude_sessions
WHERE source_file = :path
  AND probe_tag IS NULL
  AND end_time >= :mtime_ms - 300000;
```

If no JSONL modified in last 5 min: trivially pass (no Claude active). If JSONL exists but no matching row: `probe_ok = 0`. The live JSONL IS the canary.

`probe_lag_ms = now_ms - MAX(end_time)` for most recent ingest; NULL if never ingested.

**Catches:** Tailer/watcher running and inserting segments.

**Does NOT catch:** Whether `claude-session-hook.sh` fired at all. If hook never fires, no JSONL → probe vacuously passes.

---

### Browser Probe — Daemon Side

**Send:** `hippo probe browser --native` invokes NM host stdio directly, bypassing Firefox:

```json
{
  "url": "https://probe.hippo.local/synthetic",
  "title": "Hippo Probe",
  "domain": "probe.hippo.local",
  "dwell_ms": 1, "scroll_depth": 1.0,
  "timestamp": <now_ms>
}
```

`probe.hippo.local` always allowlisted via `[browser] probe_domain = "probe.hippo.local"` injected into allowlist check at `native_messaging.rs:182–188`. Never visited by real browsers.

Writes via NM stdio with 4-byte length-prefix framing (`native_messaging.rs:71–82`). Polls DB:

```sql
SELECT id FROM browser_events
WHERE envelope_id = :deterministic_uuid AND probe_tag = :probe_uuid
LIMIT 1;
```

`envelope_id` deterministic for `probe.hippo.local` (`make_envelope_id` at `native_messaging.rs:116–123`).

**Catches:** NM binary compiled and reachable, allowlist logic works, daemon-side browser pipeline functional.

**Does NOT catch:** Whether Firefox invokes the NM host. See Extension Heartbeat.

---

### Browser Probe — Extension Side (Heartbeat)

Only way to verify Firefox has the extension loaded and running.

**TypeScript (`extension/firefox/src/background.ts`):**

```typescript
interface HippoHeartbeat {
  type: "heartbeat";
  extension_version: string;
  enabled_state: boolean;
  sent_at_ms: number;
}

async function sendHeartbeat(): Promise<void> {
  const manifest = browser.runtime.getManifest();
  const msg: HippoHeartbeat = {
    type: "heartbeat",
    extension_version: manifest.version,
    enabled_state: true,
    sent_at_ms: Date.now(),
  };
  try {
    await browser.runtime.sendNativeMessage("hippo_daemon", msg);
  } catch (e) {
    console.warn("[hippo] heartbeat failed:", e);
  }
}

sendHeartbeat();  // on startup
setInterval(sendHeartbeat, 5 * 60 * 1000);  // every 5 min
```

**Rust (`crates/hippo-daemon/src/native_messaging.rs`):**

```rust
#[derive(Debug, Clone, Deserialize)]
#[serde(tag = "type")]
pub enum NmMessage {
    #[serde(rename = "visit")]
    Visit(BrowserVisit),
    #[serde(rename = "heartbeat")]
    Heartbeat(ExtensionHeartbeat),
}

#[derive(Debug, Clone, Deserialize)]
pub struct ExtensionHeartbeat {
    pub extension_version: String,
    pub enabled_state: bool,
    pub sent_at_ms: i64,
}
```

On heartbeat: NM host sends `DaemonRequest::UpdateSourceHealthHeartbeat { source: "browser", ts }` to daemon socket. Daemon handler:

```sql
UPDATE source_health
SET last_heartbeat_ts = :ts,
    updated_at        = :now_ms
WHERE source = 'browser';
```

`last_heartbeat_ts` — new column on `source_health` (see D1 cross-ref).

## Exclusion from User Data — Implementation

For enrichment queues, join against event tables with `probe_tag IS NULL`:

```sql
SELECT eq.* FROM enrichment_queue eq
JOIN events e ON eq.event_id = e.id
WHERE eq.status = 'pending' AND e.probe_tag IS NULL ...;

SELECT ceq.* FROM claude_enrichment_queue ceq
JOIN claude_sessions cs ON ceq.claude_session_id = cs.id
WHERE ceq.status = 'pending' AND cs.probe_tag IS NULL ...;

SELECT beq.* FROM browser_enrichment_queue beq
JOIN browser_events be ON beq.browser_event_id = be.id
WHERE beq.status = 'pending' AND be.probe_tag IS NULL ...;
```

**Upstream filter wins:** daemon never enqueues probe events in the first place. These downstream filters are belt-and-braces.

## Probe State in source_health

Four columns (all defined in `01-source-health.md`):

- `probe_ok INTEGER` — 1 = last probe succeeded, 0 = failed, NULL = never run
- `probe_lag_ms INTEGER` — measured round-trip lag in ms; NULL if no round-trip
- `probe_last_run_ts INTEGER` — epoch ms of last run; NULL if never
- `last_heartbeat_ts INTEGER` — epoch ms of last extension heartbeat (`browser` only)

## Failure Visibility

Probe results feed **Invariant I-8 (Probe Freshness)** — see `02-invariants.md`. Evaluated by watchdog in step 3.

## Boundary

Probes verify each pipeline is alive, not correct:
- Events flow from submission to DB row ✓
- Daemon, socket, buffer, flush functional ✓

Do not verify:
- Enrichment quality / model output
- zsh hook registered in user's shell
- Firefox extension installed (partial — heartbeat helps)
- Claude Code firing SessionStart hook
- Redaction working (dedicated test: `hippo redact test`)

Boundary is explicit. Correctness belongs to `cargo test -p hippo-daemon` and `uv run --project brain pytest brain/tests`.

## Cross-References

- `source_health` columns `probe_ok`, `probe_lag_ms`, `probe_last_run_ts`, `last_heartbeat_ts`: `01-source-health.md`.
- Invariant I-8 (probe freshness): `02-invariants.md`.
- Watchdog evaluation: `04-watchdog.md` step 3.
