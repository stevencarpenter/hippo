# Hippo Architecture Review Tracker

## Purpose

This file is the working source of truth for architecture review work.

We are starting with the risk register so future agents can:
- reference a stable list of architecture findings
- update status, owner, and notes in one place
- use the same evidence when we move into the deeper audit and remediation plan

## Agent usage rules

- Preserve stable finding IDs.
- Do not renumber findings.
- Update `Status`, `Owner`, `Last updated`, and `Verification` instead of rewriting history.
- Add new evidence as `path#Lx-Ly` references.
- When a finding is addressed, keep the finding and mark it `done` with verification notes.
- If a finding is intentionally deferred, mark it `wont-fix` and explain why.
- If a finding is split into multiple follow-up items, keep the original finding and cross-link the new IDs.

## Progress tracker

- [x] Pass 0: initial architecture risk register created
- [ ] Pass 1: deeper architecture audit with load and failure analysis
- [x] Pass 1A: Cluster A audit completed for `ARCH-R01` through `ARCH-R05`
- [ ] Pass 2: prioritized remediation plan with phases and sequencing
- [ ] Pass 3: implementation tracking against accepted remediation items

## Scope

### In scope

- `hippo/crates/hippo-core/**`
- `hippo/crates/hippo-daemon/**`
- `hippo/brain/**`
- Shared SQLite contract and queue behavior
- Query path, enrichment path, observability, and testing posture

### Out of scope for now

- Cross-platform portability
- Product feature prioritization beyond architecture risk
- UI or UX concerns

## Assessment summary

### Overall verdict

The architecture is fundamentally good for a local-first macOS tool.

The strongest parts are the decomposition and technology fit:
- Rust on the capture and daemon path is a good choice.
- Python on the enrichment and embeddings path is a good choice.
- SQLite plus a local socket is a pragmatic low-operations integration layer.
- The schema already models provenance, queue state, and future graph-style relationships.

The main weaknesses are operational rather than conceptual:
- daemon-side concurrency and durability semantics
- shared database contract evolution across Rust and Python
- enrichment robustness and partial-write behavior
- observability and architecture-level testing
- a gap between the semantic search story and the current lexical query implementation

### Strength snapshot

1. Clear process and module separation across shell capture, daemon, and brain.
   - Evidence: `hippo/README.md#L8-L26`, `hippo/crates/hippo-core/src/lib.rs#L1-L5`
2. Good SQLite baseline with WAL, foreign keys, and busy timeout on both sides.
   - Evidence: `hippo/crates/hippo-core/src/storage.rs#L12-L22`, `hippo/brain/tests/conftest.py#L11-L21`, `hippo/brain/src/hippo_brain/server.py#L42-L46`
3. Redaction is on the hot path and also applied before fallback writes.
   - Evidence: `hippo/crates/hippo-daemon/src/daemon.rs#L140-L155`, `hippo/crates/hippo-daemon/src/daemon.rs#L178-L188`, `hippo/crates/hippo-daemon/src/daemon.rs#L206-L207`
4. Queue-based decoupling between capture and enrichment is the right basic shape.
   - Evidence: `hippo/crates/hippo-core/src/storage.rs#L186-L189`, `hippo/brain/src/hippo_brain/enrichment.py#L73-L96`
5. The provenance model is strong: knowledge nodes link back to events and entities.
   - Evidence: `hippo/brain/src/hippo_brain/enrichment.py#L172-L219`, `hippo/crates/hippo-core/src/schema.sql#L245-L349`

## Severity, effort, and status legend

### Severity
- `High`: likely to cause correctness, privacy, durability, or operability issues
- `Med`: meaningful architectural drag or future risk, but not the most urgent failure mode
- `Low`: cleanup or alignment work that can wait

### Effort
- `S`: small, local change
- `M`: moderate multi-file change
- `L`: large cross-cutting change

### Status
- `new`: identified, not yet triaged
- `triaged`: accepted as a real item and clarified
- `planned`: scheduled into a workstream or phase
- `in-progress`: actively being worked
- `blocked`: cannot proceed yet
- `done`: addressed and verified
- `wont-fix`: intentionally deferred or rejected

## Finding index

| ID | Area | Title | Severity | Effort | Confidence | Status | Owner | Last updated |
|---|---|---|---|---|---|---|---|---|
| ARCH-R01 | daemon/sqlite | Single SQLite connection serializes daemon DB work | High | M | High | triaged | TBD | 2026-03-28 |
| ARCH-R02 | daemon/status | `GetStatus` holds DB lock during external waits | High | S | High | triaged | TBD | 2026-03-28 |
| ARCH-R03 | daemon/ingest | In-memory ingest buffer is unbounded and `flush_batch_size` is unused | High | M | High | triaged | TBD | 2026-03-28 |
| ARCH-R04 | daemon/durability | Fire-and-forget ingest does not guarantee persistence | High | M | High | triaged | TBD | 2026-03-28 |
| ARCH-R05 | storage/contracts | Event queue writes and fallback recovery are not transactionally safe enough | High | M | High | triaged | TBD | 2026-03-28 |
| ARCH-R06 | storage/migrations | Shared SQLite schema has no visible migration or versioning mechanism | High | L | High | new | TBD | 2026-03-28 |
| ARCH-R07 | security/redaction | Docs promise custom redaction config but runtime appears to use builtins only | High | S-M | High | new | TBD | 2026-03-28 |
| ARCH-R08 | brain/query | Query API is lexical, not semantic, despite docs and vector seam | High | M | High | new | TBD | 2026-03-28 |
| ARCH-R09 | brain/contracts | Enrichment output contract is weakly validated | High | S | High | new | TBD | 2026-03-28 |
| ARCH-R10 | brain/transactions | Mid-write failures can leave partial state and then commit retry state | High | S | High | new | TBD | 2026-03-28 |
| ARCH-R11 | brain/batching | Batch claim and fetch logic can merge unrelated work and reorder events | Med | M | High | new | TBD | 2026-03-28 |
| ARCH-R12 | graph/coverage | Graph tables and richer schema are only partially used | Med | M | High | new | TBD | 2026-03-28 |
| ARCH-R13 | observability | Logs and health surfaces are too thin for a multi-process background system | Med | M | High | new | TBD | 2026-03-28 |
| ARCH-R14 | testing | Architecture-level failure and load tests are missing in the highest-risk seams | Med | M | High | new | TBD | 2026-03-28 |

---

## Detailed findings

### ARCH-R01 — Single SQLite connection serializes daemon DB work

- Status: `triaged`
- Severity: `High`
- Effort: `M`
- Confidence: `High`
- Area: `daemon/sqlite`

#### Problem

All daemon database work is funneled through one synchronous `rusqlite::Connection` wrapped in an async mutex. Ingest flushes, status calls, session and event reads, entity reads, and raw queries all compete for the same resource.

#### Why it matters

This is the main concurrency bottleneck on the Rust side. If one path takes longer than expected, unrelated paths stall behind it. It also makes the daemon more sensitive to spikes in event volume or slower-than-normal SQLite operations.

#### Evidence

- `hippo/crates/hippo-daemon/src/daemon.rs#L20-L29`
- `hippo/crates/hippo-daemon/src/daemon.rs#L79-L117`
- `hippo/crates/hippo-daemon/src/daemon.rs#L126-L138`

#### Suggested first move

Separate hot-path ingest from read-oriented requests. At minimum, stop holding one shared synchronous connection behind a single async mutex for every request type.

#### Pass 1A triage notes

- Confirmed from code: the daemon holds one shared `Mutex<Connection>` and all DB-oriented request paths plus flush work serialize through it.
- Refined wording: this finding is specifically about serialized daemon DB work, not all daemon work.
- Nuance: interactive shell latency is somewhat insulated because `IngestEvent` appends to `event_buffer` and does not take the DB lock directly.
- Likely impact: persistence/admin responsiveness degrades first, especially when long flushes and read/status requests overlap.

#### Acceptance criteria

- The daemon no longer serializes all DB work behind one shared mutex-protected connection.
- Ingest and read-style requests can proceed without blocking each other in common cases.
- A regression test or benchmark demonstrates improved concurrency under mixed load.

#### Verification

- Pass 1A review completed.
- Recommended validation test: hold `state.db.lock().await`, concurrently start `handle_request(GetEvents)` and `flush_events(&state)`, and verify both remain blocked until the lock is released.

---

### ARCH-R02 — `GetStatus` holds DB lock during external waits

- Status: `triaged`
- Severity: `High`
- Effort: `S`
- Confidence: `High`
- Area: `daemon/status`

#### Problem

The status path acquires the DB lock, computes local metrics, and then performs LM Studio and brain reachability checks while that lock is still held.

#### Why it matters

A slow or unreachable local service can turn a simple status request into an avoidable stall for unrelated daemon work.

#### Evidence

- `hippo/crates/hippo-daemon/src/daemon.rs#L38-L78`

#### Suggested first move

Drop the DB lock as soon as the local status snapshot is computed, then do external reachability checks afterward.

#### Pass 1A triage notes

- Strongly confirmed from code: `GetStatus` keeps the DB lock while doing local file work and while awaiting two HTTP reachability checks.
- This affects more than the human-facing status command because socket probing also depends on the same status path.
- This is the cleanest and smallest Cluster A fix with immediate operational value.

#### Acceptance criteria

- No external `await` happens while the daemon holds the DB lock.
- A test confirms status checks do not block ingest or simple reads when LM Studio or the brain is slow.

#### Verification

- Pass 1A review completed.
- Recommended validation test: run hanging local HTTP responders for LM Studio and brain, trigger `GetStatus`, and verify a concurrent DB-backed request is blocked before the fix and not blocked after the fix.

---

### ARCH-R03 — In-memory ingest buffer is unbounded and `flush_batch_size` is unused

- Status: `triaged`
- Severity: `High`
- Effort: `M`
- Confidence: `High`
- Area: `daemon/ingest`

#### Problem

Ingested events are appended to an in-memory `Vec`, and flush drains the entire buffer at once. The config exposes `flush_batch_size`, but the runtime path does not appear to use it.

#### Why it matters

This creates memory growth risk under bursty traffic and makes flush latency more variable. It also creates a mismatch between configuration surface and actual behavior.

#### Evidence

- `hippo/crates/hippo-daemon/src/daemon.rs#L20-L29`
- `hippo/crates/hippo-daemon/src/daemon.rs#L33-L37`
- `hippo/crates/hippo-daemon/src/daemon.rs#L126-L129`
- `hippo/crates/hippo-core/src/config.rs#L48-L73`

#### Suggested first move

Introduce a bounded queue or explicit backpressure policy and either honor `flush_batch_size` or remove it from config until it is real.

#### Pass 1A triage notes

- Confirmed from code: the in-memory queue is an uncapped `Vec`, ingest always pushes, and flush drains the entire buffer in one pass.
- Refined interpretation: the design issue is clearly real, but the practical severity depends on workload shape.
- Nuance: human-scale shell usage may hide the issue for longer; automation-heavy or bursty usage will amplify it quickly.
- This finding amplifies `ARCH-R01` because a large drained backlog lengthens the DB critical section.

#### Acceptance criteria

- The daemon has an explicit policy for queue capacity and overload behavior.
- `flush_batch_size` is either implemented or removed.
- Tests cover buffer growth and flush behavior under burst load.

#### Verification

- Pass 1A review completed.
- Recommended validation test: set `flush_batch_size = 1`, enqueue multiple events, run one flush, and verify all events still drain and persist, proving the configured batch size is currently ignored.

---

### ARCH-R04 — Fire-and-forget ingest does not guarantee persistence

- Status: `triaged`
- Severity: `High`
- Effort: `M`
- Confidence: `High`
- Area: `daemon/durability`

#### Problem

The shell side treats a successful socket connect and write as success. On the daemon side, ingest only appends the event to memory and does not send a durability acknowledgment.

#### Why it matters

If the daemon crashes after accept and before the next flush, the event can be lost even though the sender already believed delivery succeeded.

#### Evidence

- `hippo/crates/hippo-daemon/src/commands.rs#L96-L110`
- `hippo/crates/hippo-daemon/src/daemon.rs#L33-L37`
- `hippo/crates/hippo-daemon/src/daemon.rs#L353-L363`

#### Suggested first move

Decide whether the product wants best-effort ingest or durable ingest semantics. Then make the protocol and fallback behavior match that decision explicitly.

#### Pass 1A triage notes

- Confirmed from code: sender-side success means socket connect plus frame write, not durable storage.
- Important nuance: this is primarily an abrupt-failure problem, not a graceful-shutdown problem, because accepted ingest is flushed on orderly shutdown.
- Fallback helps when the sender cannot connect or write, but not when the daemon dies after accepting bytes and before flush.

#### Acceptance criteria

- The ingest contract is documented as either durable or best-effort.
- The runtime behavior matches the documented contract.
- Tests cover daemon crash or shutdown windows around accepted-but-unflushed events.

#### Verification

- Pass 1A review completed.
- Recommended validation test: start the daemon with a long flush interval, send one event, hard-kill the daemon before flush, restart, and verify the accepted event is absent from both SQLite and fallback.

---

### ARCH-R05 — Event queue writes and fallback recovery are not transactionally safe enough

- Status: `triaged`
- Severity: `High`
- Effort: `M`
- Confidence: `High`
- Area: `storage/contracts`

#### Problem

`insert_event_at` writes the event and then writes the enrichment queue entry as separate statements. Fallback recovery also renames a source file to `.done` even when some lines fail to recover.

#### Why it matters

A failure between event insert and queue insert can leave stored events that never get enriched. A partial fallback replay can also bury recoverable input after only partial success.

#### Evidence

- `hippo/crates/hippo-core/src/storage.rs#L157-L189`
- `hippo/crates/hippo-core/src/storage.rs#L399-L447`

#### Suggested first move

Wrap event insert plus queue insert in a transaction. Rework fallback replay to preserve partially failed input for retry instead of renaming the full file unconditionally.

#### Pass 1A triage notes

- Confidence raised to `High`: the split event/queue write and unconditional `.done` rename are both directly visible in code.
- Refined interpretation: the main risk is not SQLite interleaving on the current daemon path, but mid-path fault handling and replay retirement semantics.
- Additional nuance: because `envelope_id` is not persisted into the events table, replay is not naturally idempotent.

#### Acceptance criteria

- Event insert and queue insert are atomic.
- Fallback replay distinguishes complete success from partial success.
- Tests cover failure between event and queue writes and partial fallback recovery.

#### Verification

- Pass 1A review completed.
- Recommended validation test: install a trigger that forces `INSERT INTO enrichment_queue` to fail during replay, then verify current behavior can produce an event row without a queue row while still renaming the fallback file to `.done`.

---

### ARCH-R06 — Shared SQLite schema has no visible migration or versioning mechanism

- Status: `new`
- Severity: `High`
- Effort: `L`
- Confidence: `High`
- Area: `storage/migrations`

#### Problem

Rust and Python share one SQLite schema, but there is no visible migration framework, ordered migration history, or schema version tracking.

#### Why it matters

Cross-language systems get brittle quickly when schema evolution is informal. This becomes a major upgrade and compatibility risk once the schema starts changing in place.

#### Evidence

- `hippo/README.md#L8-L26`
- `hippo/crates/hippo-core/src/storage.rs#L12-L22`
- `hippo/crates/hippo-core/src/schema.sql#L1-L408`
- Search check: no matches for `migration`, `migrate`, `ALTER TABLE`, or `PRAGMA user_version` across the project during this review.

#### Suggested first move

Introduce a schema versioning strategy before the next nontrivial DB change. The exact migration tool is less important than having a single explicit process.

#### Acceptance criteria

- There is a canonical migration path with schema version tracking.
- Startup behavior for both Rust and Python is compatible with the chosen migration story.
- Upgrade and rollback expectations are documented.

#### Verification

- Not started.

---

### ARCH-R07 — Docs promise custom redaction config but runtime appears to use builtins only

- Status: `new`
- Severity: `High`
- Effort: `S-M`
- Confidence: `High`
- Area: `security/redaction`

#### Problem

Docs and crate docs describe configurable `redact.toml` behavior, but the runtime paths inspected in this review construct `RedactionEngine::builtin()` directly.

#### Why it matters

This is a privacy and operator trust issue. It creates a docs-to-runtime mismatch around one of the most sensitive system behaviors.

#### Evidence

- `hippo/README.md#L128-L129`
- `hippo/crates/hippo-core/README.md#L8-L15`
- `hippo/crates/hippo-core/src/config.rs#L186-L242`
- `hippo/crates/hippo-core/src/redaction.rs#L16-L35`
- `hippo/crates/hippo-daemon/src/daemon.rs#L266-L275`
- `hippo/crates/hippo-daemon/src/commands.rs#L114-L124`
- `hippo/crates/hippo-daemon/src/commands.rs#L377-L387`

#### Suggested first move

Either wire `redact.toml` into runtime redaction or narrow the docs immediately so the product promise matches reality.

#### Acceptance criteria

- Runtime redaction behavior matches the documented configuration story.
- There are tests for loading custom redact patterns and applying them on both normal and fallback paths.

#### Verification

- Not started.

---

### ARCH-R08 — Query API is lexical, not semantic, despite docs and vector seam

- Status: `new`
- Severity: `High`
- Effort: `M`
- Confidence: `High`
- Area: `brain/query`

#### Problem

The current `/query` implementation uses SQL `LIKE` over event commands and knowledge node text. The codebase already contains an embeddings module and LanceDB support, and the docs describe semantic search, but the query path is not wired to that capability.

#### Why it matters

This is both a capability gap and a docs-to-runtime mismatch. It also means the current query quality will degrade as data grows or when queries rely on paraphrase instead of keyword overlap.

#### Evidence

- `hippo/brain/README.md#L3-L4`
- `hippo/brain/README.md#L51-L52`
- `hippo/README.md#L146-L152`
- `hippo/brain/src/hippo_brain/server.py#L73-L90`
- `hippo/brain/src/hippo_brain/embeddings.py#L28-L45`
- `hippo/brain/src/hippo_brain/embeddings.py#L99-L107`

#### Suggested first move

Pick one path and make it explicit: either wire vector retrieval into `/query`, or relabel the current endpoint as lexical search until semantic retrieval is actually online.

#### Acceptance criteria

- The query implementation and docs describe the same behavior.
- If semantic retrieval is claimed, `/query` uses embeddings and vector search in the production path.
- Tests cover semantic retrieval and fallback behavior.

#### Verification

- Not started.

---

### ARCH-R09 — Enrichment output contract is weakly validated

- Status: `new`
- Severity: `High`
- Effort: `S`
- Confidence: `High`
- Area: `brain/contracts`

#### Problem

The enrichment model contract is represented in code, but the parser only strips code fences, runs `json.loads`, and fills a dataclass with `.get` defaults. Invalid shapes and wrong types can still flow into later write logic.

#### Why it matters

This creates avoidable runtime failures and makes the shared contract between prompt, model output, and storage less trustworthy.

#### Evidence

- `hippo/brain/src/hippo_brain/models.py#L15-L42`
- `hippo/brain/src/hippo_brain/enrichment.py#L49-L65`
- `hippo/brain/src/hippo_brain/enrichment.py#L187-L204`

#### Suggested first move

Validate the parsed payload against a real schema or typed model before the write path touches it.

#### Acceptance criteria

- Invalid or malformed enrichment payloads fail cleanly before any DB write logic runs.
- Tests cover wrong field types, missing required fields, and invalid enum values.

#### Verification

- Not started.

---

### ARCH-R10 — Mid-write failures can leave partial state and then commit retry state

- Status: `new`
- Severity: `High`
- Effort: `S`
- Confidence: `High`
- Area: `brain/transactions`

#### Problem

`write_knowledge_node` performs multiple inserts and updates before commit. If it raises midway, the worker catches the exception and then calls `mark_queue_failed` on the same connection, which commits retry state. That can persist partial writes together with a retryable queue state.

#### Why it matters

This risks duplicate knowledge nodes, inconsistent entity links, and ambiguous recovery behavior after a retry.

#### Evidence

- `hippo/brain/src/hippo_brain/enrichment.py#L132-L226`
- `hippo/brain/src/hippo_brain/enrichment.py#L228-L249`
- `hippo/brain/src/hippo_brain/server.py#L122-L134`

#### Suggested first move

Wrap the knowledge node write path in an explicit transaction boundary and rollback before retry bookkeeping if any part of the write fails.

#### Acceptance criteria

- A failed node write leaves no partial node or link state behind.
- Retry bookkeeping is committed only after write rollback is complete.
- Tests simulate failures inside `write_knowledge_node` and verify rollback behavior.

#### Verification

- Not started.

---

### ARCH-R11 — Batch claim and fetch logic can merge unrelated work and reorder events

- Status: `new`
- Severity: `Med`
- Effort: `M`
- Confidence: `High`
- Area: `brain/batching`

#### Problem

The worker claims multiple queue items, then fetches matching events with `WHERE id IN (...)` and no `ORDER BY`. Those events are summarized together into one node.

#### Why it matters

Unrelated events can be merged into one enrichment result, and the effective chronology can drift. One problematic event can also cause the whole batch to retry.

#### Evidence

- `hippo/brain/src/hippo_brain/enrichment.py#L73-L96`
- `hippo/brain/src/hippo_brain/enrichment.py#L101-L125`
- `hippo/brain/src/hippo_brain/server.py#L105-L134`

#### Suggested first move

Define batching semantics explicitly. If batches are meant to represent coherent work, the claim and fetch logic needs ordering and grouping rules.

#### Acceptance criteria

- Event order is stable within a batch.
- Batching rules are documented and tested.
- Retry behavior is appropriate for batch-level failure.

#### Verification

- Not started.

---

### ARCH-R12 — Graph tables and richer schema are only partially used

- Status: `new`
- Severity: `Med`
- Effort: `M`
- Confidence: `High`
- Area: `graph/coverage`

#### Problem

The schema includes `relationships` and `event_entities`, and the enrichment contract includes relationships, but the Python write path stores relationships only inside knowledge node JSON. Dedicated relational tables are not populated.

#### Why it matters

This creates drift between the intended data model and the runtime model. It also limits queryability and makes future graph features more expensive to complete.

#### Evidence

- `hippo/crates/hippo-core/src/schema.sql#L166-L214`
- `hippo/brain/src/hippo_brain/enrichment.py#L20-L24`
- `hippo/brain/src/hippo_brain/enrichment.py#L57-L65`
- `hippo/brain/src/hippo_brain/enrichment.py#L139-L146`
- `hippo/brain/src/hippo_brain/enrichment.py#L172-L204`

#### Suggested first move

Decide whether those tables are part of the real near-term design. If yes, populate them. If not, remove or clearly defer them so the schema tells the truth.

#### Acceptance criteria

- The runtime write path matches the intended graph model.
- Unused schema elements are either implemented or intentionally deferred with documentation.

#### Verification

- Not started.

---

### ARCH-R13 — Logs and health surfaces are too thin for a multi-process background system

- Status: `new`
- Severity: `Med`
- Effort: `M`
- Confidence: `High`
- Area: `observability`

#### Problem

The daemon initializes a basic `tracing_subscriber::fmt()` logger even though config exposes a log path and docs describe log files. The brain exposes only a minimal health response and coarse logs. Neither side currently provides rich queue lag, last success, retry churn, or correlation-style visibility.

#### Why it matters

This is the main operational maturity gap in the system. When the daemon and brain disagree or the queue stalls, there is not enough built-in visibility to diagnose the problem quickly.

#### Evidence

- `hippo/crates/hippo-core/src/config.rs#L167-L171`
- `hippo/crates/hippo-daemon/src/main.rs#L22-L30`
- `hippo/crates/hippo-daemon/src/main.rs#L123-L127`
- `hippo/README.md#L146-L152`
- `hippo/crates/hippo-core/src/storage.rs#L341-L365`
- `hippo/brain/src/hippo_brain/server.py#L54-L58`
- `hippo/brain/src/hippo_brain/server.py#L122-L139`
- `hippo/brain/src/hippo_brain/server.py#L156-L159`

#### Suggested first move

Define a minimal observability contract for both processes: structured logs, queue metrics, last-success timestamps, and a richer status or health surface.

#### Acceptance criteria

- Operators can inspect queue depth, failed queue count, last successful enrichment, and service reachability from first-party surfaces.
- Log behavior matches the documented logging story.
- Common failure modes can be diagnosed without attaching a debugger.

#### Verification

- Not started.

---

### ARCH-R14 — Architecture-level failure and load tests are missing in the highest-risk seams

- Status: `new`
- Severity: `Med`
- Effort: `M`
- Confidence: `High`
- Area: `testing`

#### Problem

There are meaningful unit and integration tests already, but the current suite does not appear to cover the highest-risk architecture seams: daemon crash windows around accepted events, mid-write transaction failure in the brain, schema migration behavior, or mixed load and contention between status, ingest, and queue work.

#### Why it matters

This project lives at process and storage boundaries. Those are the places where architecture-level tests provide the most value.

#### Evidence

- Positive baseline: `hippo/crates/hippo-daemon/src/daemon.rs#L376-L520`, `hippo/crates/hippo-daemon/tests/shell_hook.rs#L1-L48`, `hippo/brain/tests/conftest.py#L1-L21`
- Risk seams with no direct test evidence found during this review: `hippo/crates/hippo-daemon/src/commands.rs#L96-L110`, `hippo/crates/hippo-core/src/storage.rs#L157-L189`, `hippo/brain/src/hippo_brain/enrichment.py#L132-L249`, `hippo/brain/src/hippo_brain/server.py#L105-L139`

#### Suggested first move

Add a small architecture test matrix focused on failure and recovery boundaries rather than broad feature count.

#### Acceptance criteria

- Tests exist for accepted-but-unflushed event loss windows.
- Tests exist for partial-write rollback in the brain.
- Tests exist for contention or blocking around daemon status and ingest.
- Tests exist for schema evolution expectations once a migration path is selected.

#### Verification

- Not started.

---

## Cross-cutting notes

### Known theme clusters

#### Cluster A — daemon hot path and durability
- `ARCH-R01`
- `ARCH-R02`
- `ARCH-R03`
- `ARCH-R04`
- `ARCH-R05`

#### Cluster B — shared contract and schema evolution
- `ARCH-R06`
- `ARCH-R07`
- `ARCH-R09`
- `ARCH-R12`

#### Cluster C — brain correctness and query quality
- `ARCH-R08`
- `ARCH-R10`
- `ARCH-R11`

#### Cluster D — operational maturity
- `ARCH-R13`
- `ARCH-R14`

### Recommended working principle

Prefer fixes that:
1. reduce ambiguity in the system contract,
2. improve failure visibility,
3. make persistence semantics explicit, and
4. add tests around the seam being changed.

## Suggested sequencing for later remediation planning

This section is intentionally lightweight for now. It exists to make the future remediation pass easier.

### Probable phase 0

- `ARCH-R13` observability
- `ARCH-R02` status lock scope cleanup
- `ARCH-R07` redaction doc and runtime alignment

### Probable phase 1

- `ARCH-R01` daemon DB concurrency
- `ARCH-R03` ingest buffering and backpressure
- `ARCH-R04` ingest durability contract
- `ARCH-R05` transactional safety on event queue writes and fallback replay

### Probable phase 2

- `ARCH-R09` enrichment contract validation
- `ARCH-R10` brain transaction boundaries
- `ARCH-R11` batching semantics

### Probable phase 3

- `ARCH-R06` migration strategy
- `ARCH-R08` semantic query implementation or doc correction
- `ARCH-R12` schema coverage alignment
- `ARCH-R14` architecture-level test matrix

## Future audit sections

### Pass 1A — Cluster A audit notes

#### Scope completed

- `ARCH-R01`
- `ARCH-R02`
- `ARCH-R03`
- `ARCH-R04`
- `ARCH-R05`

#### What was validated

- Rust test baseline passed during this audit:
  - `53` tests in `hippo-core`
  - `6` tests in `hippo-daemon`
  - `1` shell hook integration test
- The shell hook is clearly backgrounded and disowned, so command capture does not wait for durable persistence.
- The protocol includes `Ack`, but ingest intentionally suppresses response delivery.
- Graceful shutdown behavior is better than the raw durability risk might imply because accepted ingest is flushed on shutdown.
- The cleanest first fix in Cluster A is still `ARCH-R02`.

#### Observed versus theoretical split

- Observed directly from code:
  - single serialized daemon DB handle
  - status path holds DB lock across external waits
  - uncapped in-memory ingest queue with drain-all flush
  - sender success means socket write, not durable persistence
  - event insert and queue insert are split
  - fallback replay retires files even after partial failure
- Still theoretical until explicit fault or load tests are added:
  - exact user-visible latency impact under realistic mixed load
  - exact event loss window frequency in practice
  - exact prevalence of replay inconsistency under fault conditions

#### Most targeted validation tests to add next

1. Lock-contention test proving DB-backed request paths and flush serialize behind the shared DB mutex.
2. Status-path regression test proving no external await happens while holding the DB lock.
3. Batch-size regression test proving `flush_batch_size` is currently ignored.
4. Process-level crash-window test for accepted-but-unflushed event loss.
5. Replay fault-injection test for event insert without queue insert plus unconditional `.done` retirement.

### Pass 1 — deeper architecture audit

#### Goals

- measure where the daemon actually blocks under mixed ingest and status load
- trace event lifecycle from shell send to DB persistence to queue consumption
- validate transaction boundaries and crash windows on both Rust and Python paths
- review schema ownership and evolution expectations across both runtimes
- verify how much of LanceDB and embeddings are intended for v1 versus later

#### Deliverables

- updated finding severities or confidence where needed
- any new findings discovered during load or failure analysis
- concrete diagrams or flow summaries for the hottest paths
- validation of which current risks are theoretical versus observed

#### Questions to answer

- Is daemon-side contention noticeable under realistic shell activity?
- What exact loss window exists between accepted event and durable event?
- Can the brain produce duplicate or partial knowledge state today?
- Which parts of the graph/vector model are intentional future work versus incomplete wiring?
- What operator workflows are currently hardest to debug?

## Future remediation planning section

### Pass 2 — prioritized remediation plan

#### Output should include

- phased workstreams
- dependency order
- rough effort by item
- acceptance criteria per workstream
- recommended tests to add alongside each fix

#### Planning constraints

- preserve current local-first architecture
- keep macOS as the primary operating environment
- prefer improvements to operability and correctness before large redesign
- avoid expanding scope into unrelated product work

#### Candidate workstreams

1. **Observability and operator trust**
   - `ARCH-R13`
   - `ARCH-R07`
   - portions of `ARCH-R14`

2. **Daemon durability and concurrency**
   - `ARCH-R01`
   - `ARCH-R02`
   - `ARCH-R03`
   - `ARCH-R04`
   - `ARCH-R05`

3. **Brain correctness and contract hardening**
   - `ARCH-R09`
   - `ARCH-R10`
   - `ARCH-R11`

4. **Shared schema and retrieval alignment**
   - `ARCH-R06`
   - `ARCH-R08`
   - `ARCH-R12`

## Update template for future edits

When updating a finding, prefer this pattern:

- `Status`: current state
- `Owner`: person or agent
- `Last updated`: date
- `What changed`: concise note
- `Verification`: how the change was validated
- `Follow-ups`: remaining work, if any

## Change log

- 2026-03-28: Initial tracker created from source inspection of Rust and Python architecture plus delegated implementation review.
- 2026-03-28: Pass 1A Cluster A audit completed for `ARCH-R01` through `ARCH-R05`, including Rust test baseline confirmation and triage notes.