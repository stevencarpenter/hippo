# Hippo — Agent Team Execution Plan

## What this document is

This is the execution-ready companion to `hippo/docs/architecture-review-tracker.md`.

It is structured specifically for **Claude Code with `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=true`**.
Each task block is self-contained: an agent can pick one up, read the context files listed,
make the described changes, run the verification commands, and mark it complete — without reading
the rest of this document or the tracker.

The tracker remains the ground truth for finding status. When a task is done, the executing agent
must update the relevant `ARCH-Rxx` finding in the tracker to `done` or `in-progress` and append a
verification note.

---

## How to use this document (orchestrator agent)

1. Read the **Phase overview** table to understand sequencing and parallelism.
2. Spawn sub-agents in parallel for tasks in the same parallel lane.
3. Never spawn two agents with overlapping write scopes at the same time.
4. After each phase completes, run the **phase gate** commands before proceeding.
5. If any task fails verification, re-run that task before moving on.
6. After all phases complete, update `hippo/docs/architecture-review-tracker.md` Pass 3 checkbox.

---

## Project context (all agents must read this first)

**Hippo** is a local knowledge capture daemon for macOS.

Two processes share a SQLite database at `~/.local/share/hippo/hippo.db`:

- `hippo-daemon` (Rust) — captures shell events via Unix socket, redacts secrets,
  writes to SQLite, serves CLI queries.
- `hippo-brain` (Python) — polls enrichment queue from SQLite, calls LM Studio API,
  writes knowledge nodes and embeddings to LanceDB, serves HTTP query API on port 9175.

**Repository root:** `hippo/`

**Style rules:**
- Rust: edition 2024, clippy clean (`cargo clippy --all-targets -- -D warnings`), `thiserror` for
  lib errors, `anyhow` for bin errors.
- Python: 3.13+, `ruff` for lint and format, `uv` for package management.
- All timestamps: Unix epoch milliseconds (`i64` / `INTEGER`).
- SQLite: WAL mode, `PRAGMA foreign_keys=ON`, `PRAGMA busy_timeout=5000` on every connection.

**Baseline commands (run before any phase to verify a clean starting state):**

```
cargo test -p hippo-core -p hippo-daemon
uv run --project brain pytest brain/tests -q
cargo clippy --all-targets -- -D warnings
```

All three must pass before starting any task.

---

## Phase overview

| Phase | Tasks (parallel lanes) | Sequencing constraint | Tracker findings closed |
|---|---|---|---|
| Phase 0 | P0-A, P0-B, P0-C | A and B parallel; C after A+B | R02 ✅ R07 ✅ R13 partial R14 partial |
| Phase 1 | P1-A and P1-B parallel; then P1-C; then P1-D | A‖B → C → D | R05, R03, R04, R01 |
| Phase 2 | P2-A → P2-B → P2-C (must be sequential) | serial | R09, R10, R11 |
| Phase 3 | P3-A and P3-B parallel; then P3-C; then P3-D | A‖B → C → D | R06, R08, R12, R14 |

**Phase 0 is already complete.** Start from Phase 1.

---

## Phase 0 — COMPLETE (reference only)

### What was done

- `ARCH-R02`: `GetStatus` no longer holds the DB lock during external reachability awaits.
  The SQLite snapshot is taken under the lock; the lock is released before any `reqwest` calls.
  Evidence: `hippo/crates/hippo-daemon/src/daemon.rs` `handle_request` / `GetStatus` branch.

- `ARCH-R07`: Custom redaction config is now runtime-wired.
  `RedactionEngine::from_config_path()` and `RedactConfig::load()` added to `hippo-core`.
  Daemon and fallback paths load from `config.redact_path()` and fall back to builtins on failure.
  Evidence: `hippo/crates/hippo-core/src/redaction.rs`, `hippo/crates/hippo-daemon/src/daemon.rs`.

- `ARCH-R13` (partial): Brain `/health` now exposes `queue_depth`, `queue_failed`, `db_reachable`,
  `last_success_at_ms`, `last_error`, `last_error_at_ms`. Doctor parses and prints richer details.
  Evidence: `hippo/brain/src/hippo_brain/server.py`, `hippo/crates/hippo-daemon/src/commands.rs`.

- `ARCH-R14` (partial): Phase 0 seam tests added. See Rust and Python test files.

### Phase 0 gate (already passing — do not re-run unless reverting)

```
cargo test -p hippo-core -p hippo-daemon
uv run --project brain pytest brain/tests/test_server.py brain/tests/test_init.py -q
```

---

## Phase 1 — Daemon ingest reliability and durability

### Phase 1 goal

Make daemon ingest behavior explicit and safe under load and failure. The two tasks in lane A and B
can run in parallel because they have completely disjoint write scopes.

---

### Task P1-A — Atomic event and queue writes (ARCH-R05)

**Tracker finding:** `ARCH-R05`
**Status to set on completion:** `done`
**Parallel lane:** A (can run at the same time as P1-B)
**Depends on:** Phase 0 complete
**Must not run in parallel with:** P1-D (both write `storage.rs`)

#### Write scope (only these files)

- `hippo/crates/hippo-core/src/storage.rs`

#### Context files to read first

- `hippo/crates/hippo-core/src/storage.rs` (full file — pay attention to `insert_event_at` and
  `recover_fallback_files`)
- `hippo/crates/hippo-core/src/schema.sql` (understand `events` and `enrichment_queue` tables)

#### Problem

`insert_event_at` executes `INSERT INTO events` and then `INSERT INTO enrichment_queue` as two
separate statements with no explicit transaction. If the process fails between them, an event row
exists with no queue row — it will never be enriched and will not appear in queue metrics.

`recover_fallback_files` renames the source file to `.jsonl.done` unconditionally even when some
lines fail. Partially recovered input is permanently discarded.

Evidence:
- `hippo/crates/hippo-core/src/storage.rs` around line 157–189 (two bare `conn.execute` calls)
- `hippo/crates/hippo-core/src/storage.rs` around line 399–447 (unconditional rename to `.done`)

#### Changes to make

**Change 1 — Wrap `insert_event_at` in a transaction**

Inside `insert_event_at`, wrap the two SQL statements in an explicit transaction:

```rust
conn.execute_batch("BEGIN")?;
// existing INSERT INTO events ...
// existing INSERT INTO enrichment_queue ...
conn.execute_batch("COMMIT")?;
```

Use a guard or `defer`-style pattern to `ROLLBACK` on early return via `?`. Because `rusqlite`
uses autocommit by default, this is safe to add.

**Change 2 — Preserve partially failed fallback files**

In `recover_fallback_files`, rename to `.done` only after all lines succeed. If any line fails,
rename the file to `.jsonl.partial` (not `.done`) so the operator can inspect it and a future
run can retry. Return the error count clearly.

Update the renamed-extension logic so `.jsonl.partial` is not picked up by `list_fallback_files`
on the next run (add `.jsonl.partial` to the exclusion filter, or only collect `.jsonl` files).

#### Tests to add (in the existing test module inside `storage.rs`)

**Test 1 — event and queue insert are atomic under injected failure**

Use a SQLite trigger to force the `enrichment_queue` INSERT to fail:

```rust
#[test]
fn test_insert_event_at_is_atomic_under_queue_failure() {
    let conn = open_memory().unwrap();
    conn.execute_batch(
        "CREATE TRIGGER fail_queue_insert BEFORE INSERT ON enrichment_queue
         BEGIN SELECT RAISE(ABORT, 'injected failure'); END;"
    ).unwrap();

    let sid = upsert_session(&conn, "s1", "host", "zsh", "user").unwrap();
    let result = insert_event(&conn, sid, &sample_shell_event(), 0, None);

    assert!(result.is_err());
    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM events", [], |r| r.get(0))
        .unwrap();
    assert_eq!(count, 0, "event row must not survive when queue insert fails");
}
```

**Test 2 — partial fallback recovery preserves failed lines**

Write a fallback file with two valid lines and one malformed line in the middle. After recovery,
verify the `.partial` file exists and contains the malformed line, and the valid events are stored.

#### Verification

```
cargo test -p hippo-core test_insert_event_at_is_atomic_under_queue_failure
cargo test -p hippo-core test_partial_fallback_recovery_preserves_failed_lines
cargo clippy -p hippo-core -- -D warnings
```

All must pass. No regressions in existing storage tests:

```
cargo test -p hippo-core
```

#### Tracker update

Set `ARCH-R05` status to `done`. Add verification note with test names.

---

### Task P1-B — Bounded ingest buffer and honor `flush_batch_size` (ARCH-R03)

**Tracker finding:** `ARCH-R03`
**Status to set on completion:** `done`
**Parallel lane:** B (can run at the same time as P1-A)
**Depends on:** Phase 0 complete
**Must not run in parallel with:** P1-D (both write `daemon.rs`)

#### Write scope (only these files)

- `hippo/crates/hippo-daemon/src/daemon.rs`

#### Context files to read first

- `hippo/crates/hippo-daemon/src/daemon.rs` (full file — focus on `DaemonState`,
  `handle_request` IngestEvent arm, and `flush_events`)
- `hippo/crates/hippo-core/src/config.rs` (look at `DaemonConfig.flush_batch_size`)

#### Problem

The event buffer is an unbounded `Vec<EventEnvelope>`. Every `IngestEvent` pushes unconditionally.
`flush_events` drains the entire buffer at once regardless of `flush_batch_size`.
`flush_batch_size` is in config but has no effect at runtime.

Under burst ingest the buffer can grow without limit, and the next flush will hold the DB mutex
for the entire backlog in one pass.

Evidence:
- `hippo/crates/hippo-daemon/src/daemon.rs` line ~20–28 (`event_buffer: Mutex<Vec<EventEnvelope>>`)
- `hippo/crates/hippo-daemon/src/daemon.rs` line ~33–36 (unconditional push on IngestEvent)
- `hippo/crates/hippo-daemon/src/daemon.rs` line ~126–129 (`buffer.drain(..).collect()`)

#### Changes to make

**Change 1 — Flush in batches up to `flush_batch_size`**

In `flush_events`, drain at most `state.config.daemon.flush_batch_size` events per call instead
of draining everything:

```rust
let events: Vec<EventEnvelope> = {
    let mut buffer = state.event_buffer.lock().await;
    let n = buffer.len().min(state.config.daemon.flush_batch_size);
    buffer.drain(..n).collect()
};
```

The flush task already calls `flush_events` on an interval. With bounded draining, large backlogs
are processed incrementally across intervals rather than in one long-held critical section.

**Change 2 — Drop and count when buffer exceeds capacity**

On `IngestEvent`, if the buffer already contains `flush_batch_size * 4` events (a simple high-
water mark), drop the new event and increment `drop_count` instead of pushing. This prevents
unbounded memory growth under sustained overload:

```rust
DaemonRequest::IngestEvent(envelope) => {
    let mut buffer = state.event_buffer.lock().await;
    let cap = state.config.daemon.flush_batch_size * 4;
    if buffer.len() >= cap {
        state.drop_count.fetch_add(1, Ordering::Relaxed);
    } else {
        buffer.push(*envelope);
    }
    DaemonResponse::Ack
}
```

The `flush_batch_size` default is 50, so the cap defaults to 200 in-flight events. This is
adjustable via config.

#### Tests to add (in the existing `#[cfg(test)]` module inside `daemon.rs`)

**Test 1 — flush respects batch size**

Set `config.daemon.flush_batch_size = 2`, push 5 events into the buffer, call `flush_events` once,
then assert exactly 2 events were written to SQLite and 3 remain in the buffer.

**Test 2 — ingest drops events when buffer is at capacity**

Set `flush_batch_size = 2`, push `2 * 4 = 8` events to reach capacity, then push one more and
assert `drop_count` is 1 and buffer length is still 8.

#### Verification

```
cargo test -p hippo-daemon test_flush_respects_batch_size
cargo test -p hippo-daemon test_ingest_drops_at_capacity
cargo clippy -p hippo-daemon -- -D warnings
```

No regressions:

```
cargo test -p hippo-daemon
```

#### Tracker update

Set `ARCH-R03` status to `done`. Add verification note with test names.

---

### Phase 1 lane A+B gate

Run after both P1-A and P1-B complete before starting P1-C:

```
cargo test -p hippo-core -p hippo-daemon
cargo clippy --all-targets -- -D warnings
```

---

### Task P1-C — Make ingest durability contract explicit (ARCH-R04)

**Tracker finding:** `ARCH-R04`
**Status to set on completion:** `done`
**Parallel lane:** C (serial after P1-A and P1-B)
**Depends on:** P1-A complete, P1-B complete
**Must not run in parallel with:** P1-D

#### Write scope (only these files)

- `hippo/crates/hippo-daemon/src/daemon.rs` (tests only — no production code changes)
- `hippo/crates/hippo-daemon/src/commands.rs` (tests only — no production code changes)

#### Context files to read first

- `hippo/crates/hippo-daemon/src/daemon.rs` (flush_events, handle_request IngestEvent arm)
- `hippo/crates/hippo-daemon/src/commands.rs` (send_event_fire_and_forget,
  handle_send_event_shell)

#### Problem

The shell side sends an event and the daemon accepts it into memory. If the daemon is killed after
accept but before the next flush, the event is lost. The sender has no way to know. The fallback
path only triggers on socket connect or write failure — not on daemon crash after accept.

This is an accepted best-effort contract for a local shell capture tool, but it is not documented
anywhere in code, and there is no test that characterizes this behavior.

#### Changes to make

**Change 1 — Characterize the durability loss window with a test**

Add a test in `daemon.rs` that:
1. starts a daemon with a very long `flush_interval_ms` (e.g. 600_000)
2. sends one event and waits for it to be buffered (check buffer length)
3. aborts the daemon task without calling shutdown
4. opens the DB directly
5. asserts the event is absent from both `events` and `enrichment_queue`

This test proves the contract: accepted-but-unbuffered events are lost on abrupt daemon failure.
Name it `test_crash_before_flush_loses_accepted_events` and add a doc comment:

```rust
/// Characterizes the best-effort ingest durability contract.
/// Events buffered in memory are lost if the daemon is killed before flush.
/// This is intentional for a local shell capture tool.
/// Graceful shutdown (via DaemonRequest::Shutdown) does flush before exit.
#[tokio::test]
async fn test_crash_before_flush_loses_accepted_events() { ... }
```

**Change 2 — Add a doc comment to `send_event_fire_and_forget`**

In `commands.rs`, above `send_event_fire_and_forget`, add:

```rust
/// Fire-and-forget event send. Returns Ok(()) once the frame is written to the socket.
///
/// Durability contract: success means the event was accepted by the daemon socket.
/// It does NOT mean the event has been written to SQLite. If the daemon crashes
/// after accept but before the next periodic flush, the event may be lost.
///
/// The fallback JSONL path is triggered only when the socket is unreachable — not
/// when the daemon crashes after accepting the event.
```

#### Verification

```
cargo test -p hippo-daemon test_crash_before_flush_loses_accepted_events
cargo clippy -p hippo-daemon -- -D warnings
```

No regressions:

```
cargo test -p hippo-daemon
```

#### Tracker update

Set `ARCH-R04` status to `done`. Note: remediation is explicit documentation of the best-effort
contract, not a change to add durable acknowledgment semantics.

---

### Task P1-D — Separate DB connections for ingest and read paths (ARCH-R01)

**Tracker finding:** `ARCH-R01`
**Status to set on completion:** `done`
**Parallel lane:** D (serial, after P1-A, P1-B, P1-C)
**Depends on:** P1-A, P1-B, P1-C all complete
**Must not run in parallel with:** anything (this is the largest single change)

#### Write scope (only these files)

- `hippo/crates/hippo-daemon/src/daemon.rs`
- `hippo/crates/hippo-core/src/storage.rs` (minor: open_db exposed for read-only use)

#### Context files to read first

- `hippo/crates/hippo-daemon/src/daemon.rs` (full file — understand DaemonState, handle_request,
  flush_events)
- `hippo/crates/hippo-core/src/storage.rs` (`open_db` function)

#### Problem

All daemon DB work serializes behind one `Mutex<Connection>`. Ingest flush, status, sessions,
events, entities, and raw queries all contend for the same lock. A long flush or slow query blocks
everything else.

Evidence: `hippo/crates/hippo-daemon/src/daemon.rs` line ~20–28 (`db: Mutex<Connection>`)

#### Changes to make

**Change 1 — Add a second read-only connection for query/status paths**

In `DaemonState`, add a second connection field for read-only operations:

```rust
pub struct DaemonState {
    pub config: HippoConfig,
    pub write_db: Mutex<Connection>,   // was: db
    pub read_db: Mutex<Connection>,    // new: for status/events/sessions/entities/rawquery
    pub redaction: RedactionEngine,
    ...
}
```

Open both connections in `run()`:
- `write_db`: opened as before with WAL pragmas
- `read_db`: a second `open_db()` call to the same path (WAL mode allows concurrent readers)

Update `run()` to construct the new state with both connections.

**Change 2 — Route read-only requests to `read_db`**

In `handle_request`, route these arms to use `state.read_db.lock().await`:
- `GetStatus`
- `GetSessions`
- `GetEvents`
- `GetEntities`
- `RawQuery`

Keep `flush_events` using `state.write_db.lock().await`.

**Change 3 — Update field references**

Search `daemon.rs` for `state.db` and rename each to `state.write_db` or `state.read_db`
per the routing in Change 2. Also update `test_state_with_config` and similar helpers in the
test module.

**Change 4 — Update `storage.rs` if needed**

If `open_db` needs a mode flag for read-only connections, add an optional `readonly: bool`
parameter or a new `open_db_readonly` function using `rusqlite::OpenFlags`. WAL mode already
supports concurrent readers, so this may not require extra flags, but verify.

#### Tests to add (in `daemon.rs` test module)

**Test — concurrent read and write do not deadlock**

Start a daemon, acquire `write_db` in a spawned task and hold it for 50ms (simulating a slow
flush), then concurrently issue a `GetEvents` request via the socket, and assert the response
arrives within 200ms. This proves read requests are no longer blocked by write-path locks.

#### Verification

```
cargo test -p hippo-core -p hippo-daemon
cargo clippy --all-targets -- -D warnings
```

The new concurrent test must pass. All prior daemon tests must still pass.

#### Tracker update

Set `ARCH-R01` status to `done`. Add verification note including test name.

---

### Phase 1 final gate

```
cargo test -p hippo-core -p hippo-daemon
cargo clippy --all-targets -- -D warnings
```

All tests must pass before Phase 2 starts.

---

## Phase 2 — Brain write-path correctness and batching

### Phase 2 goal

Prevent malformed model output or mid-write failures from producing inconsistent enrichment state.
These three tasks share `enrichment.py` and must run sequentially.

---

### Task P2-A — Enforce enrichment output contract at parse time (ARCH-R09)

**Tracker finding:** `ARCH-R09`
**Status to set on completion:** `done`
**Parallel lane:** A (first in the sequential chain)
**Depends on:** Phase 1 complete

#### Write scope (only these files)

- `hippo/brain/src/hippo_brain/enrichment.py`
- `hippo/brain/src/hippo_brain/models.py`

#### Context files to read first

- `hippo/brain/src/hippo_brain/models.py` (read `EnrichmentResult` dataclass and `ENRICHMENT_SCHEMA`)
- `hippo/brain/src/hippo_brain/enrichment.py` (read `parse_enrichment_response` and `write_knowledge_node`)

#### Problem

`parse_enrichment_response` only strips code fences and calls `json.loads`. It fills
`EnrichmentResult` using `.get()` defaults. Invalid types flow into `write_knowledge_node`, where
they fail late inside `name.lower().strip()` on non-string entity values. `ENRICHMENT_SCHEMA` is
defined but never used.

Evidence:
- `hippo/brain/src/hippo_brain/models.py` line ~23–54 (`ENRICHMENT_SCHEMA` defined but unused)
- `hippo/brain/src/hippo_brain/enrichment.py` line ~48–65 (`parse_enrichment_response`)
- `hippo/brain/src/hippo_brain/enrichment.py` line ~186–199 (name.lower() can fail on non-string)

#### Changes to make

**Change 1 — Add a validate function in `models.py`**

Add a `validate_enrichment_data(data: dict) -> EnrichmentResult` function that:
- checks required top-level string fields: `summary`, `intent`, `embed_text`
- checks `outcome` is one of `"success"`, `"partial"`, `"failure"`, `"unknown"`
- checks `entities` is a `dict` (default to `{}` if missing)
- checks each entity list (projects, tools, files, services, errors) is a `list` of `str`
  (skip any non-string items rather than crashing)
- checks `relationships` is a `list` (default to `[]` if missing or wrong type)
- checks `tags` is a `list` of `str` (skip non-string items)
- raises `ValueError` with a descriptive message on any structural violation
- returns a valid `EnrichmentResult`

**Change 2 — Wire `validate_enrichment_data` into `parse_enrichment_response`**

Replace the manual `.get()` hydration in `parse_enrichment_response` with a call to
`validate_enrichment_data(data)`. Let `ValueError` propagate — the caller already has error
handling.

#### Tests to add in `hippo/brain/tests/test_enrichment.py`

Add a test class or group of tests:
- `test_parse_rejects_missing_required_field` — omit `summary`, assert `ValueError`
- `test_parse_rejects_invalid_outcome` — set `outcome: "succeeded"`, assert `ValueError`
- `test_parse_skips_non_string_entity_items` — set `entities.tools: ["cargo", 123]`,
  assert parse succeeds and tools list contains only `"cargo"`
- `test_parse_rejects_entities_not_dict` — set `entities: ["not", "a", "dict"]`,
  assert `ValueError`
- `test_parse_rejects_invalid_json` — pass `"not json"`, assert `json.JSONDecodeError`

#### Verification

```
uv run --project brain pytest brain/tests/test_enrichment.py -q
uv run --project brain ruff check brain/
```

All new tests must pass. All prior enrichment tests must still pass.

#### Tracker update

Set `ARCH-R09` status to `done`. Note that `ENRICHMENT_SCHEMA` may now be cleaned up or removed
since runtime validation replaces it, or it may be kept as documentation.

---

### Task P2-B — Atomic knowledge node writes with rollback (ARCH-R10)

**Tracker finding:** `ARCH-R10`
**Status to set on completion:** `done`
**Parallel lane:** B (serial after P2-A)
**Depends on:** P2-A complete

#### Write scope (only these files)

- `hippo/brain/src/hippo_brain/enrichment.py`
- `hippo/brain/src/hippo_brain/server.py`

#### Context files to read first

- `hippo/brain/src/hippo_brain/enrichment.py` (`write_knowledge_node`, `mark_queue_failed`)
- `hippo/brain/src/hippo_brain/server.py` (`_enrichment_loop` exception handling)
- `hippo/crates/hippo-core/src/schema.sql` (`knowledge_node_events`, `knowledge_node_entities`)

#### Problem

`write_knowledge_node` runs multiple inserts and a commit. If it raises mid-write (which is now
more likely to happen at a known point after P2-A because validation is pushed earlier — but the
write path still has other failure modes), the caller catches the exception and calls
`mark_queue_failed` on the same connection, which commits. Any SQL that already ran but hadn't
committed may persist as a partial fragment.

Evidence:
- `hippo/brain/src/hippo_brain/enrichment.py` line ~132–226 (multi-step, single commit at end)
- `hippo/brain/src/hippo_brain/server.py` line ~102–134 (exception catch → `mark_queue_failed`
  on the same `conn`)
- `hippo/crates/hippo-core/src/schema.sql` line ~323–345: `knowledge_node_events` keyed on
  `(knowledge_node_id, event_id)` — same event can link to multiple nodes on retry

#### Changes to make

**Change 1 — Wrap `write_knowledge_node` in an explicit transaction**

At the start of `write_knowledge_node`, begin an explicit transaction:

```python
conn.execute("BEGIN")
```

Wrap the body in a try/except. On any exception: rollback, then re-raise:

```python
try:
    # all the existing inserts and updates
    conn.commit()
    return node_id
except Exception:
    conn.rollback()
    raise
```

Remove the `conn.commit()` that currently sits at the end of the function body and place it only
in the success path above.

**Change 2 — Separate connections for write and retry bookkeeping in `_enrichment_loop`**

In `_enrichment_loop`, after a write failure, get a fresh connection for `mark_queue_failed`
rather than reusing the connection that may have a failed transaction in progress:

```python
except Exception as e:
    logger.error("enrichment failed: %s", e)
    retry_conn = self._get_conn()
    try:
        mark_queue_failed(retry_conn, event_ids, str(e))
    finally:
        retry_conn.close()
```

Close the original `conn` on the failure path too.

#### Tests to add in `hippo/brain/tests/test_enrichment.py`

**Test — mid-write failure leaves no partial state**

Use Python's `unittest.mock.patch` to make the `knowledge_node_events` INSERT raise an exception.
After the failed call to `write_knowledge_node`, verify:
- no row in `knowledge_nodes`
- no row in `knowledge_node_events`
- no row in `knowledge_node_entities`
- `events.enriched` is still `0` for all affected events

**Test — retry after rollback writes a clean node**

Run `write_knowledge_node` once with injected failure, then run it again with a valid result.
Verify exactly one node exists and it is correctly linked.

#### Verification

```
uv run --project brain pytest brain/tests/test_enrichment.py brain/tests/test_server.py -q
uv run --project brain ruff check brain/
```

All new tests must pass. All prior enrichment and server tests must still pass.

#### Tracker update

Set `ARCH-R10` status to `done`. Note the connection-separation pattern in the verification note.

---

### Task P2-C — Deterministic batch ordering and grouping (ARCH-R11)

**Tracker finding:** `ARCH-R11`
**Status to set on completion:** `done`
**Parallel lane:** C (serial after P2-B)
**Depends on:** P2-B complete

#### Write scope (only these files)

- `hippo/brain/src/hippo_brain/enrichment.py`

#### Context files to read first

- `hippo/brain/src/hippo_brain/enrichment.py` (`claim_pending_events`, the event fetch query)
- `hippo/crates/hippo-core/src/schema.sql` (`enrichment_queue` table definition)

#### Problem

The event fetch after `claim_pending_events` uses `WHERE id IN (...)` with no `ORDER BY`. Row
order from SQLite without an `ORDER BY` is unspecified — the order of events sent to the LLM
depends on internal page layout. One problematic event causes the entire claimed batch to retry.

Evidence:
- `hippo/brain/src/hippo_brain/enrichment.py` line ~101–125 (no ORDER BY on event fetch)

#### Changes to make

**Change 1 — Add `ORDER BY timestamp ASC` to the event fetch**

In `claim_pending_events`, update the event fetch query:

```python
cursor = conn.execute(
    f"""
    SELECT id, session_id, timestamp, command, exit_code, duration_ms,
           cwd, hostname, shell, git_repo, git_branch, git_commit, git_dirty
    FROM events
    WHERE id IN ({placeholders})
    ORDER BY timestamp ASC
    """,
    event_ids,
)
```

This ensures events are always presented to the LLM in chronological order within a batch.

**Change 2 — Add a doc comment explaining the batching contract**

Above `claim_pending_events`, add:

```python
# Batching contract:
# Events are claimed from the queue in priority/creation order and fetched
# in timestamp order. Batches are not coherence-grouped by session or repo;
# the batch size is configured via enrichment_batch_size. One failed batch
# retries all claimed events together up to max_retries.
```

#### Tests to add in `hippo/brain/tests/test_enrichment.py`

**Test — events within a batch are returned in timestamp order**

Insert 3 events with known timestamps (out of insertion order), claim them, and assert the
returned list is sorted by `timestamp` ascending.

**Test — events from different sessions can be claimed in the same batch**

Insert events from two different sessions. Claim them all in one batch. Assert all event IDs
are returned. This pins the documented behavior — batches are not session-scoped.

#### Verification

```
uv run --project brain pytest brain/tests/test_enrichment.py -q
uv run --project brain ruff check brain/
```

All new tests must pass. No regressions.

#### Tracker update

Set `ARCH-R11` status to `done`.

---

### Phase 2 final gate

```
uv run --project brain pytest brain/tests -q
uv run --project brain ruff check brain/
```

All 63+ tests must pass before Phase 3 starts.

---

## Phase 3 — Shared contract evolution and product-truth alignment

### Phase 3 goal

Make shared DB evolution safe, align the query implementation with what Hippo claims to do, and
close the architecture-level test matrix. Tasks P3-A and P3-B can run in parallel.

---

### Task P3-A — Schema versioning and migration strategy (ARCH-R06)

**Tracker finding:** `ARCH-R06`
**Status to set on completion:** `done`
**Parallel lane:** A (can run at the same time as P3-B)
**Depends on:** Phase 2 complete

#### Write scope (only these files)

- `hippo/crates/hippo-core/src/schema.sql`
- `hippo/crates/hippo-core/src/storage.rs`
- `hippo/brain/src/hippo_brain/server.py`
- `hippo/docs/schema-migration-strategy.md` (new file)

#### Context files to read first

- `hippo/crates/hippo-core/src/schema.sql` (full file)
- `hippo/crates/hippo-core/src/storage.rs` (`open_db` function)
- `hippo/brain/src/hippo_brain/server.py` (`_get_conn`)
- `hippo/README.md` (architecture section on shared SQLite)

#### Problem

Rust and Python share one SQLite database but there is no schema version tracking. `open_db`
executes `CREATE TABLE IF NOT EXISTS` idempotently on every startup, which works only for additive
changes. Any column addition, removal, or constraint change will silently diverge across installs.

Evidence:
- `hippo/crates/hippo-core/src/storage.rs` line ~12–22 (`execute_batch(SCHEMA)` on every open)
- No `PRAGMA user_version` or migrations directory anywhere in the project

#### Changes to make

**Change 1 — Set `PRAGMA user_version = 1` in `schema.sql`**

At the top of `hippo/crates/hippo-core/src/schema.sql`, after the existing `CREATE TABLE` blocks,
add:

```sql
PRAGMA user_version = 1;
```

This marks the current schema as version 1 without changing any behavior.

**Change 2 — Add a version check to `open_db`**

After `execute_batch(SCHEMA)` in `open_db`, add a check:

```rust
let version: i64 = conn.query_row(
    "PRAGMA user_version",
    [],
    |row| row.get(0),
)?;
const EXPECTED_VERSION: i64 = 1;
if version != EXPECTED_VERSION {
    anyhow::bail!(
        "DB schema version mismatch: expected {}, found {}. \
         Please run migrations or delete the database.",
        EXPECTED_VERSION,
        version
    );
}
```

**Change 3 — Add a version check to `BrainServer._get_conn`**

In `server.py`, after opening the connection and setting pragmas, check the version:

```python
version = conn.execute("PRAGMA user_version").fetchone()[0]
EXPECTED_VERSION = 1
if version != EXPECTED_VERSION:
    conn.close()
    raise RuntimeError(
        f"DB schema version mismatch: expected {EXPECTED_VERSION}, found {version}. "
        "Please run migrations or delete the database."
    )
```

**Change 4 — Write migration documentation**

Create `hippo/docs/schema-migration-strategy.md` that explains:
- Current version is 1
- How to bump the version (update `user_version` in `schema.sql` and both version checks)
- The convention for adding new tables: use `CREATE TABLE IF NOT EXISTS`, increment version
- The convention for column changes: write an explicit `ALTER TABLE`, increment version
- How to test migrations: create a v(N-1) fixture, open with the current code, verify outcome

#### Tests to add

In `storage.rs` test module:

```rust
#[test]
fn test_open_db_version_matches_schema() {
    let dir = tempfile::tempdir().unwrap();
    let conn = open_db(&dir.path().join("test.db")).unwrap();
    let v: i64 = conn.query_row("PRAGMA user_version", [], |r| r.get(0)).unwrap();
    assert_eq!(v, 1);
}

#[test]
fn test_open_db_rejects_wrong_version() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("test.db");
    {
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute_batch("PRAGMA user_version = 99").unwrap();
    }
    assert!(open_db(&db_path).is_err());
}
```

In `brain/tests/test_server.py`, add a test that opens a connection with the wrong `user_version`
and asserts `_get_conn` raises `RuntimeError`.

#### Verification

```
cargo test -p hippo-core test_open_db_version_matches_schema
cargo test -p hippo-core test_open_db_rejects_wrong_version
uv run --project brain pytest brain/tests/test_server.py -q
cargo clippy --all-targets -- -D warnings
```

#### Tracker update

Set `ARCH-R06` status to `done`. Note the version number (1) and the migration doc path.

---

### Task P3-B — Align query implementation with documented behavior (ARCH-R08)

**Tracker finding:** `ARCH-R08`
**Status to set on completion:** `done`
**Parallel lane:** B (can run at the same time as P3-A)
**Depends on:** Phase 2 complete

#### Write scope (only these files)

- `hippo/brain/src/hippo_brain/server.py`
- `hippo/brain/README.md`

#### Context files to read first

- `hippo/brain/src/hippo_brain/server.py` (`query` endpoint)
- `hippo/brain/src/hippo_brain/embeddings.py` (`open_vector_db`, `get_or_create_table`,
  `embed_knowledge_node`, `search_similar`)
- `hippo/brain/README.md`
- `hippo/README.md` (Data Storage table, Usage examples)

#### Problem

The `/query` endpoint uses SQL `LIKE` with a single `%text%` pattern. The docs describe the
query as semantic search. The embeddings module and LanceDB support exist but are not wired into
the production query path.

**Decision required:** this task has two valid approaches. Choose one:

**Option A — Implement semantic retrieval (recommended if LM Studio is configured)**

Wire vector retrieval into `/query`:
1. When `/query` is called, generate an embedding for `text` via `self.client.embed()`
2. Open the LanceDB vector store via `open_vector_db` and `get_or_create_table`
3. Call `search_similar(table, query_vec)` to retrieve relevant nodes
4. Merge those results with the existing lexical SQL results, deduplicating by node ID
5. Return a unified results list sorted by relevance (vector hits first, then lexical)
6. If LanceDB is unavailable or the table is empty, fall back to lexical only

Note: you will also need to wire `embed_knowledge_node` into `write_knowledge_node` or
`_enrichment_loop` so nodes actually get embedded. Currently `write_knowledge_node` only writes to
SQLite — it does not write to LanceDB. This is a larger change.

**Option B — Document the current implementation as lexical search**

Update `hippo/brain/README.md` so it does not claim "semantic search". Change:
- `"serves semantic search queries over HTTP"` → `"serves knowledge queries over HTTP"`
- `"POST /query — Semantic search over enriched knowledge nodes"` →
  `"POST /query — Full-text search over events and enriched knowledge nodes"`

Add a comment above the `query` method:
```python
# Current implementation: lexical substring search over events.command and
# knowledge_nodes.content/embed_text. Semantic (vector) retrieval is available
# via the embeddings module but is not yet wired into this endpoint.
```

Add a failing integration test that will pass when semantic retrieval is wired:
```python
@pytest.mark.xfail(reason="semantic retrieval not yet wired into /query")
def test_query_returns_semantically_related_result(tmp_db): ...
```

**Recommendation:** implement Option B now to fix the docs/runtime mismatch immediately, and
leave the `xfail` test as a marker for Option A work.

#### Verification

If Option B:

```
uv run --project brain pytest brain/tests/test_server.py -q
uv run --project brain ruff check brain/
```

If Option A:

```
uv run --project brain pytest brain/tests/test_server.py brain/tests/test_embeddings.py -q
uv run --project brain ruff check brain/
```

#### Tracker update

Set `ARCH-R08` status to `done`. Note which option was implemented.

---

### Phase 3 lane A+B gate

```
cargo test -p hippo-core -p hippo-daemon
uv run --project brain pytest brain/tests -q
cargo clippy --all-targets -- -D warnings
```

---

### Task P3-C — Clarify graph schema coverage (ARCH-R12)

**Tracker finding:** `ARCH-R12`
**Status to set on completion:** `done`
**Parallel lane:** C (serial after P3-A and P3-B)
**Depends on:** P3-A complete (schema version locked), P3-B complete

#### Write scope (only these files)

- `hippo/brain/src/hippo_brain/enrichment.py`
- `hippo/crates/hippo-core/src/schema.sql`

#### Context files to read first

- `hippo/crates/hippo-core/src/schema.sql` (`relationships` and `event_entities` tables)
- `hippo/brain/src/hippo_brain/enrichment.py` (`write_knowledge_node`)
- `hippo/brain/src/hippo_brain/models.py` (`EnrichmentResult.relationships`)

#### Problem

The schema has `relationships` and `event_entities` tables but `write_knowledge_node` stores
relationships only inside `knowledge_nodes.content` JSON. The dedicated tables are never populated.
This is both unused schema and an incomplete data model.

**Decision required:**

**Option A — Populate `relationships` table**

After the entity upsert loop in `write_knowledge_node`, iterate `result.relationships` and insert
into `relationships`:

```python
for rel in result.relationships:
    from_canonical = rel.get("from", "").lower().strip()
    to_canonical = rel.get("to", "").lower().strip()
    relationship = rel.get("relationship", "")
    if not (from_canonical and to_canonical and relationship):
        continue
    from_id = conn.execute(
        "SELECT id FROM entities WHERE canonical = ?", (from_canonical,)
    ).fetchone()
    to_id = conn.execute(
        "SELECT id FROM entities WHERE canonical = ?", (to_canonical,)
    ).fetchone()
    if from_id and to_id:
        conn.execute(
            """INSERT INTO relationships (from_entity_id, to_entity_id, relationship)
               VALUES (?, ?, ?)
               ON CONFLICT (from_entity_id, to_entity_id, relationship)
               DO UPDATE SET evidence_count = evidence_count + 1, last_seen = ?""",
            (from_id[0], to_id[0], relationship, int(time.time() * 1000)),
        )
```

**Option B — Remove or clearly defer unused tables**

If Option A is deferred, add SQL comments to `schema.sql` above the `relationships` and
`event_entities` table definitions:

```sql
-- NOTE: relationships and event_entities are not yet populated by the enrichment pipeline.
-- They are reserved for future graph-query features.
-- See ARCH-R12 in docs/architecture-review-tracker.md.
```

**Recommendation:** implement Option A for `relationships`. Leave `event_entities` for a future
pass since populating it correctly requires per-event attribution at enrichment time, which the
current batch-level enrichment contract does not support.

#### Tests to add

**If Option A:**

In `brain/tests/test_enrichment.py`, add a test that calls `write_knowledge_node` with a
result containing non-empty relationships, then asserts `relationships` table has the expected
rows and they correctly reference entity IDs.

**If Option B:**

Add a regression test that calls `write_knowledge_node` and asserts `relationships` is empty,
pinning the intentional behavior.

#### Verification

```
uv run --project brain pytest brain/tests/test_enrichment.py -q
uv run --project brain ruff check brain/
```

#### Tracker update

Set `ARCH-R12` status to `done`. Note which option was implemented.

---

### Task P3-D — Close the architecture seam test matrix (ARCH-R14)

**Tracker finding:** `ARCH-R14`
**Status to set on completion:** `done`
**Parallel lane:** D (final, after all P3 tasks complete)
**Depends on:** P3-A, P3-B, P3-C all complete

#### Write scope (only these files)

- `hippo/crates/hippo-daemon/src/daemon.rs` (test module only)
- `hippo/crates/hippo-core/src/storage.rs` (test module only)
- `hippo/brain/tests/test_enrichment.py`
- `hippo/brain/tests/test_server.py`

#### Context files to read first

- `hippo/docs/architecture-review-tracker.md` (ARCH-R14 section, Phase 1D audit notes)

#### Goal

ARCH-R14 is a cross-cutting test gate. The individual seam tests were added in tasks P1 through
P3-C. This task's job is to verify all of them exist and pass, then close the finding.

#### Checklist — verify each of these seam tests exists and passes

Rust seam tests (check `hippo-core` and `hippo-daemon` test output):
- [ ] `test_insert_event_at_is_atomic_under_queue_failure` (P1-A)
- [ ] `test_partial_fallback_recovery_preserves_failed_lines` (P1-A)
- [ ] `test_flush_respects_batch_size` (P1-B)
- [ ] `test_ingest_drops_at_capacity` (P1-B)
- [ ] `test_crash_before_flush_loses_accepted_events` (P1-C)
- [ ] `test_open_db_version_matches_schema` (P3-A)
- [ ] `test_open_db_rejects_wrong_version` (P3-A)
- [ ] `test_get_status_releases_db_lock_before_external_awaits` (Phase 0, already passing)
- [ ] `test_daemon_uses_custom_redaction_config_on_flush_path` (Phase 0, already passing)

Python seam tests (check brain test output):
- [ ] `test_parse_rejects_missing_required_field` (P2-A)
- [ ] `test_parse_rejects_invalid_outcome` (P2-A)
- [ ] `test_parse_skips_non_string_entity_items` (P2-A)
- [ ] `test_parse_rejects_entities_not_dict` (P2-A)
- [ ] `test_write_knowledge_node_failure_leaves_no_partial_state` (P2-B)
- [ ] `test_batch_events_returned_in_timestamp_order` (P2-C)
- [ ] `test_batch_can_span_multiple_sessions` (P2-C)
- [ ] `test_brain_server_rejects_wrong_schema_version` (P3-A)

#### If any test is missing

Add the missing test now. Use the context from the task that was supposed to add it.
If a task was skipped or its test was omitted, fill in the test here.

#### Run the full suite

```
cargo test -p hippo-core -p hippo-daemon
uv run --project brain pytest brain/tests -q
cargo clippy --all-targets -- -D warnings
uv run --project brain ruff check brain/
```

All of the above must pass clean.

#### Tracker update

Set `ARCH-R14` status to `done`. List the final test count for both Rust and Python.

---

## Phase 3 final gate and project close-out

Run the full suite one final time:

```
cargo test -p hippo-core -p hippo-daemon
cargo test -p hippo-daemon --test shell_hook
uv run --project brain pytest brain/tests -q
cargo clippy --all-targets -- -D warnings
uv run --project brain ruff check brain/
uv run --project brain ruff format --check brain/
cargo fmt --check
```

All commands must exit clean.

Then update `hippo/docs/architecture-review-tracker.md`:
- Check off `Pass 3: implementation tracking against accepted remediation items`
- Verify every finding from `ARCH-R01` to `ARCH-R14` shows `done` or `wont-fix`
- Add a final changelog entry with the date and a summary of all closed findings

---

## Quick reference: write scope matrix

Use this table to prevent parallel write conflicts. Never assign two agents to the same row.

| File | Tasks that write it |
|---|---|
| `hippo/crates/hippo-core/src/storage.rs` | P1-A, P1-D, P3-A |
| `hippo/crates/hippo-core/src/schema.sql` | P3-A, P3-C |
| `hippo/crates/hippo-core/src/config.rs` | (done in Phase 0 — no writes in Phases 1-3) |
| `hippo/crates/hippo-core/src/redaction.rs` | (done in Phase 0 — no writes in Phases 1-3) |
| `hippo/crates/hippo-daemon/src/daemon.rs` | P1-B, P1-C (tests only), P1-D |
| `hippo/crates/hippo-daemon/src/commands.rs` | (done in Phase 0 — no writes in Phases 1-3) |
| `hippo/brain/src/hippo_brain/enrichment.py` | P2-A, P2-B, P2-C, P3-C |
| `hippo/brain/src/hippo_brain/models.py` | P2-A |
| `hippo/brain/src/hippo_brain/server.py` | P2-B, P3-A (minor), P3-B |
| `hippo/brain/src/hippo_brain/__init__.py` | (done in Phase 0 — no writes in Phases 1-3) |
| `hippo/brain/src/hippo_brain/embeddings.py` | P3-B (Option A only) |
| `hippo/brain/README.md` | P3-B |
| `hippo/brain/tests/test_enrichment.py` | P2-A, P2-B, P2-C, P3-C |
| `hippo/brain/tests/test_server.py` | P2-B, P3-A, P3-B, P3-D |
| `hippo/brain/tests/test_init.py` | (done in Phase 0 — no writes in Phases 1-3) |
| `hippo/docs/schema-migration-strategy.md` | P3-A (new file) |
| `hippo/docs/architecture-review-tracker.md` | every task (status updates after completion) |

**Safe parallel combinations:**
- P1-A with P1-B (storage.rs ‖ daemon.rs — no overlap)
- P3-A with P3-B (storage.rs+schema.sql ‖ server.py+README — no overlap)

**Must be sequential:**
- P1-A → P1-D (both touch storage.rs)
- P1-B → P1-D (both touch daemon.rs)
- P2-A → P2-B → P2-C (all touch enrichment.py)
- P3-A → P3-C (both touch schema.sql)

---

## Tracker finding to task mapping

| Finding | Task | Phase |
|---|---|---|
| ARCH-R01 | P1-D | Phase 1 |
| ARCH-R02 | done (Phase 0) | — |
| ARCH-R03 | P1-B | Phase 1 |
| ARCH-R04 | P1-C | Phase 1 |
| ARCH-R05 | P1-A | Phase 1 |
| ARCH-R06 | P3-A | Phase 3 |
| ARCH-R07 | done (Phase 0) | — |
| ARCH-R08 | P3-B | Phase 3 |
| ARCH-R09 | P2-A | Phase 2 |
| ARCH-R10 | P2-B | Phase 2 |
| ARCH-R11 | P2-C | Phase 2 |
| ARCH-R12 | P3-C | Phase 3 |
| ARCH-R13 | in-progress (Phase 0) | close with P3-D |
| ARCH-R14 | P3-D | Phase 3 |