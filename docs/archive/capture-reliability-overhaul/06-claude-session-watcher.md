# Claude Session Watcher

> **Status (2026-04-25): shipped.** T-5 (PR #86) implemented the watcher and the dual-run mode. T-7 made `watcher` the default. T-8 deleted the tmux tailer code path entirely. The "Why tmux-per-session Must Go" and "Phased rollout" sections below remain as the historical decision record; the actual rollout matched the plan modulo the M3 gate (replaced with a one-shot reconciliation against ground truth — see `m3-decision.md`).

**TL;DR:** Replace the per-session tmux tailer model with one long-lived `hippo watch-claude-sessions` process under launchd that subscribes to FSEvents on `~/.claude/projects/` and ingests every JSONL as it grows. The SessionStart hook becomes a no-op marker write; all PID-chain, tmux-targeting, and duplicate-tailer failure modes are structurally eliminated.

## Why tmux-per-session Must Go

The current model (SessionStart hook → tmux new-window → `hippo ingest claude-session --inline`) has regressed four times in six weeks. Each regression maps to a structural deficiency, not an implementation bug.

| # | Failure mode | Where it recurred | Root nature |
|---|---|---|---|
| 1 | Tmux targeting fragility | `-t` flag (current sev1, 1330113); prior base-index mismatches | Three independent guess-axes: session name, target flag, index semantics |
| 2 | Daemon-down silent loss | Reproduced on `hippo daemon restart` mid-session | Tailer's fire-and-forget writes during daemon outage go to fallback JSONL but tailer never retries |
| 3 | Hook silent exit-0 | Reproduced when `python3` absent from launchd PATH | `2>/dev/null` + empty fallback yields empty `TRANSCRIPT_PATH`, hook exits 0 |
| 4 | Duplicate tailers on resume | Reproduced on re-launching Claude in same project | SessionStart fires again; second tailer spawns on same JSONL from offset 0 |
| 5 | PID-chain walk breaks under wrappers | Reproduced with `claade` wrapper | `$PPID` assumption fails whenever a wrapper is added |
| 6 | File-not-yet-exists at hook time | Every session start | Claude fires hook BEFORE creating JSONL; tailer polls via `--wait-for-file 30`; batch fallback silently drops if tmux absent |

The correct fix is architectural: remove the entire mechanism. None of these failure modes exist in a long-lived FS-watcher.

## Architecture Overview

```
~/.claude/projects/**/*.jsonl  (APFS/HFS+)
        │
        │  FSEvents via notify crate
        ▼
┌──────────────────────────────────────────────────────────────┐
│         hippo watch-claude-sessions  (launchd, KeepAlive)    │
│                                                              │
│  Startup: walk ~/.claude/projects → upsert offset rows       │
│  Runtime: notify → modified → read from offset               │
│           parse JSONL → EventEnvelope                        │
│           advance byte_offset in same SQLite tx              │
│                                                              │
│  State: claude_session_offsets (hippo.db)                    │
└──────────────────────┬───────────────────────────────────────┘
                       │  Unix socket (fire-and-forget,
                       │  same protocol as shell hook)
                       ▼
              hippo-daemon  (launchd, KeepAlive)
                       │
                       ▼
           claude_sessions table  (hippo.db)
```

Single Rust binary sub-command (`hippo watch-claude-sessions`) registered as launchd agent. Never spawns subprocesses. Reads directly from FS, writes to daemon socket via the identical `send_event_fire_and_forget` path the shell hook uses (`crates/hippo-daemon/src/commands.rs:104`).

## State Schema

```sql
-- Schema v8 migration
CREATE TABLE IF NOT EXISTS claude_session_offsets (
  path              TEXT PRIMARY KEY,
  session_id        TEXT,                 -- UUID from first JSONL line; NULL until parsed
  byte_offset       INTEGER NOT NULL DEFAULT 0,
  last_read_ts      INTEGER NOT NULL,
  last_segment_ts   INTEGER,              -- distinct: read can yield 0 parseable lines
  inode             INTEGER,              -- detect file recreation
  device            INTEGER,              -- paired with inode for identity
  size_at_last_read INTEGER,              -- detect truncation
  created_at        INTEGER NOT NULL DEFAULT (unixepoch('now','subsec')*1000)
);

CREATE INDEX IF NOT EXISTS idx_offsets_session
  ON claude_session_offsets(session_id);
```

**Column rationale:**

- `path` (PK) canonical absolute path; updates on Rename
- `session_id` extracted from first parseable `sessionId` field; NULL allows row creation before content
- `byte_offset` resume cursor; advanced only inside same tx as segment upsert
- `last_read_ts` heartbeat for staleness detection
- `last_segment_ts` distinct — read-without-content vs abandoned mid-session
- `inode` + `device` file identity fingerprint; inode change = recreation → reset offset
- `size_at_last_read` truncation guard; `stat.size < size_at_last_read` → reset offset
- `created_at` auditing only

## Discovery

**Startup scan:** Recursive walk of `~/.claude/projects/`. For each `*.jsonl`:

```rust
let meta = std::fs::metadata(&path)?;
conn.execute(
    "INSERT OR IGNORE INTO claude_session_offsets
     (path, byte_offset, last_read_ts, inode, device, size_at_last_read)
     VALUES (?1, 0, ?2, ?3, ?4, ?5)",
    params![path_str, now_ms, meta.ino(), meta.dev(), meta.len()],
)?;
```

`INSERT OR IGNORE` preserves existing offsets across restarts.

**Continuous FS subscription:** notify crate, FSEvents on macOS:

| Event | Action |
|---|---|
| Create | Upsert offset row with offset=0; schedule immediate read |
| Modify (data) | Lookup offset; read from stored position |
| Remove | Mark for deferred deletion after 60s grace |
| Rename | `UPDATE offsets SET path=? WHERE path=?`; keep offset |
| Modify (metadata only) | No-op |

**FSEvents coalescing:** macOS batches events by 10ms–1s, up to 30s under load. Target: all events processed within 5s. Read loop tolerates Modify on unchanged file (reads 0 bytes).

**NFS/iCloud known-unknown:** `~/.claude/projects` on networked mount may not fire FSEvents reliably. Fallback config:

```toml
[capture]
# claude_session_poll_interval_secs = 2   # only if ~/.claude/projects is NFS/iCloud
```

Detection: startup warning if not on local APFS/HFS+ (`statfs` `f_fstypename`).

## Reading and Parsing

### Per-file Read Loop

1. Open file
2. `stat` → `(inode, device, size)`
3. Compare against stored values:
   - `inode` or `device` changed → reset `byte_offset=0`
   - `size < size_at_last_read` → reset (truncation)
4. Seek to `byte_offset`
5. Read to EOF
6. Split on `\n`; last chunk may be partial

**Partial-line safety:** If buffer doesn't end in `\n`, last "line" is incomplete. Do not parse. Do not advance offset past it.

```rust
let mut last_complete_offset = byte_offset;
for line in chunks {
    if line.ends_with('\n') {
        last_complete_offset += line.len() as u64 + 1;  // +1 for '\n'
    }
    // partial: don't advance
}
// Only advance to last_complete_offset, not to EOF
```

This generalizes the existing tail logic in `crates/hippo-daemon/src/claude_session.rs:483–519`.

### Parsing

Reuse existing `process_line` from `crates/hippo-daemon/src/claude_session.rs:194`. No new parsing. Watcher is a consumer, not a reimplementation.

Maintains per-file `HashMap<PathBuf, (HashMap<String, PendingToolUse>, HashMap<String, Option<String>>)>` matching in-memory state of current `ingest_tail` at `claude_session.rs:425–427`.

### Transactional Offset Advancement

After a batch of lines for one file is parsed and envelopes are sent:

```sql
-- single transaction:
UPDATE claude_session_offsets
SET byte_offset = ?new_offset,
    last_read_ts = ?now,
    last_segment_ts = ?segment_ts,
    size_at_last_read = ?current_size
WHERE path = ?path;
```

Committed only after socket sends complete (or fallback written). Transaction failure → offset not advanced → next cycle re-reads (idempotent via `idx_events_envelope_id` and `UNIQUE (session_id, segment_index)`).

### Malformed-line Handling

`process_line` returns `Err` → do not crash:
1. `tracing::warn!(path=%path, byte_offset, "malformed JSONL, skipping")`
2. Advance offset past bad line
3. Bump `source_health.claude-session.consecutive_failures`
4. Continue

Matches existing pattern at `claude_session.rs:488–496`.

### Per-file Read Timeout

```rust
let result = tokio::time::timeout(
    Duration::from_secs(30),
    read_and_process_file(&path, &mut state),
).await;
if result.is_err() {
    tracing::warn!(path=%path, "read timeout, backoff 60s");
    backoff_set.insert(path.clone(), now + Duration::from_secs(60));
}
```

## File Lifecycle Edge Cases

| Scenario | Detection | Action |
|---|---|---|
| Session resumed (append) | `inode` unchanged; `size > size_at_last_read` | Normal: seek, read new content. Unique index absorbs any dedup. |
| Truncated/rewritten | `size < size_at_last_read` OR `inode` changed | Reset `byte_offset=0`. Re-read entire file. `UNIQUE` constraint absorbs dupes. |
| Moved/archived | Rename `from→to` | `UPDATE offsets SET path=new`. Offset preserved. |
| Project dir deleted | Remove cascade | Defer offset deletion 60s (APFS atomic rename-replace protection). |
| File created → immediately written | Create + Modify | Offset row inserted on Create; Modify triggers first read. |
| Watcher down when session started | Startup scan on restart | Finds JSONL, inserts offset row, reads entire file. Same as `--batch` import. |

## SessionStart Hook in New Model

Reduced to 8 lines:

```bash
#!/usr/bin/env bash
set -euo pipefail

INPUT=$(cat)
SESSION_ID=$(echo "$INPUT" | python3 -c \
    "import sys,json; print(json.load(sys.stdin).get('session_id',''))" \
    2>/dev/null || true)

[ -z "$SESSION_ID" ] && exit 0

MARKER_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/hippo/claude-seen"
mkdir -p "$MARKER_DIR"
touch "${MARKER_DIR}/${SESSION_ID}"
```

Marker is belt-and-braces for "watcher was down when session started." Startup FS scan alone is sufficient.

### Feature Flag Transition

```toml
[capture]
# "watcher" (default after Phase B) or "tmux-tailer" (Phase A default)
claude_session_mode = "tmux-tailer"
```

Hook branches on flag. Retire tmux-spawn branch in Phase C.

## Launchd Plist

New file: `launchd/com.hippo.claude-session-watcher.plist` with `KeepAlive=true`, `RunAtLoad=true`. No hard daemon dependency (launchd user agents don't support it without SMJobBless). Watcher implements soft startup retry — exponential backoff up to 30 s on first socket connect failure.

Installed via `hippo daemon install` (extend `crates/hippo-daemon/src/main.rs:244`).

## Failure and Recovery Modes

**Watcher crashes:** `KeepAlive=true` restarts. Startup scan + `INSERT OR IGNORE` preserves offsets. Zero loss, zero re-ingestion. Worst case: lines written between last offset update and crash (atomic with segment upsert, so window ≤ one segment boundary).

**Daemon down:** `send_event_fire_and_forget` fails → fallback JSONL write at `config.fallback_dir()` (`commands.rs:336`). On daemon recovery, `recover_fallback_files` drains (`storage.rs:813`). **Critical difference from tmux model:** watcher does NOT advance `byte_offset` if fallback also fails. Retries next Modify event. Closes silent-loss failure mode #2.

**Offset table corruption:** Bootstrap mode rebuilds from `claude_sessions.MAX(end_time)` per session. Triggered by `hippo watch-claude-sessions --rebuild-offsets`. Fallback: set offset=0, re-ingest idempotently (unique index).

**Stalled on large JSONL:** 30 s timeout + 60 s backoff (see §6).

## Migration Plan

**Phase A — Dual-run (one release):** Watcher ships alongside hook. Flag default `tmux-tailer`. Both run simultaneously. Dedup absorbs overlap. Observability: compare row counts per path over 48 h. Exit criterion: watcher ≥ tailer, no misses.

**Phase B — Watcher default (next release):** Flip default. Hook reduces to marker-write path. Tmux windows no longer spawned.

**Phase C — Hook simplification (following release):** Delete tmux branch entirely. Script becomes 8-line marker writer. Remove feature flag.

## Interaction with source_health

Two rows:

| `source` | Semantics |
|---|---|
| `'claude-session'` | Data-path health: last ingest, consecutive failures |
| `'claude-session-watcher'` | Process health: 30 s heartbeat regardless of file activity |

**Rationale:** `'claude-session'` silent could mean no sessions (expected) OR watcher broken (critical). `'claude-session-watcher'` silent unambiguously means dead process. Separate rows allow doctor to distinguish.

**Heartbeat (every 30 s):**
```sql
INSERT INTO source_health (source, updated_at) VALUES ('claude-session-watcher', ?now)
ON CONFLICT(source) DO UPDATE SET updated_at = excluded.updated_at;
```

**Successful segment (same tx as upsert):**
```sql
INSERT INTO source_health (source, last_event_ts, consecutive_failures, updated_at)
VALUES ('claude-session', ?now, 0, ?now)
ON CONFLICT(source) DO UPDATE SET
    last_event_ts = excluded.last_event_ts,
    consecutive_failures = 0,
    updated_at = excluded.updated_at;
```

**Failure:**
```sql
INSERT INTO source_health (source, consecutive_failures, updated_at)
VALUES ('claude-session', 1, ?now)
ON CONFLICT(source) DO UPDATE SET
    consecutive_failures = source_health.consecutive_failures + 1,
    updated_at = excluded.updated_at;
```

## Testability

**Unit tests** (in `crates/hippo-daemon/src/watch_claude_sessions.rs`):
- Offset math / partial-line handling
- Truncation detection
- Inode change detection
- Malformed line skip

**Integration test** (`crates/hippo-daemon/tests/claude_session_watcher_integration.rs`):
- Spawn watcher with temp `HIPPO_DATA_HOME`
- Write Claude-shaped JSONL lines
- Assert daemon socket receives `IngestEvent` frames
- Assert offset advances to final `\n`

**End-to-end:** Real daemon + watcher → synthetic JSONL stream → assert `claude_sessions` rows with correct `session_id` and `segment_index`.

**Property test** (proptest/quickcheck):
- Random JSONL stream + random truncation/rename/append
- Assert: no loss (total events = line count), no double-counting (envelope_ids unique), offset ≤ stat().size, post-mutation `ingest_batch` produces 0 new rows

## Known-Unknowns

- **APFS snapshots (Time Machine):** Read-only; FSEvents doesn't fire for snapshot writes. Live file still fires — expected benign. Verify during active TM backup.
- **Claude subagent sessions:** `claude_sessions` has `is_subagent` and `parent_session_id` columns. Unclear whether subagents get distinct JSONLs or share parent's. Verify empirically before shipping. If distinct: watcher handles automatically. If shared: `process_line` already tracks by `tool_use_id` (globally unique).
- **`claade` wrapper:** Under new model, irrelevant — watcher detects via FS, not PID chain. Confirm by reading the wrapper script for any transcript-path redirection.
- **Multiple Claude instances on same project:** Both append to same JSONL. `process_line` tracks by `tool_use_id`. Exercise in property test.

## Cross-References

- **D1:** Two `source_health` rows (`claude-session` + `claude-session-watcher`) require `consecutive_failures` and `last_event_ts` columns — both present in D1 schema.
- **D2 (I-2):** Watcher is new implementation target for I-2.
- **D3:** Doctor live-JSONL-vs-DB reconciliation uses `claude_session_offsets` — for each JSONL with `last_read_ts > 5min ago`, assert `byte_offset == stat(path).size`. Catches stalled watcher.
- **D3 (probes):** Claude-session assertion probe verifies watcher SLA.
