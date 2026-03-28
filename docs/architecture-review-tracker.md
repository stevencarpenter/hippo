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
- [x] Pass 1: source-level deeper architecture audit completed; targeted empirical validation tests identified
- [x] Pass 1A: Cluster A audit completed for `ARCH-R01` through `ARCH-R05`
- [x] Pass 1B: Cluster C audit completed for `ARCH-R08`, `ARCH-R10`, and `ARCH-R11`
- [x] Pass 1C: Cluster B audit completed for `ARCH-R06`, `ARCH-R07`, `ARCH-R09`, and `ARCH-R12`
- [x] Pass 1D: Cluster D audit completed for `ARCH-R13` and `ARCH-R14`
- [x] Pass 2: prioritized remediation plan completed with phased workstreams, validation gates, and planned statuses
- [x] Pass 3A: Phase 0 implementation progress recorded for `ARCH-R02`, `ARCH-R07`, `ARCH-R13`, and `ARCH-R14`
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
| ARCH-R01 | daemon/sqlite | Single SQLite connection serializes daemon DB work | High | M | High | planned | TBD | 2026-03-28 |
| ARCH-R02 | daemon/status | `GetStatus` holds DB lock during external waits | High | S | High | done | TBD | 2026-03-28 |
| ARCH-R03 | daemon/ingest | In-memory ingest buffer is unbounded and `flush_batch_size` is unused | High | M | High | planned | TBD | 2026-03-28 |
| ARCH-R04 | daemon/durability | Fire-and-forget ingest does not guarantee persistence | High | M | High | planned | TBD | 2026-03-28 |
| ARCH-R05 | storage/contracts | Event queue writes and fallback recovery are not transactionally safe enough | High | M | High | planned | TBD | 2026-03-28 |
| ARCH-R06 | storage/migrations | Shared SQLite schema has no visible migration or versioning mechanism | High | L | High | planned | TBD | 2026-03-28 |
| ARCH-R07 | security/redaction | Custom redaction config is documented but not runtime-wired | High | S | High | done | TBD | 2026-03-28 |
| ARCH-R08 | brain/query | Query API is whole-query substring search, not semantic retrieval | High | M | High | planned | TBD | 2026-03-28 |
| ARCH-R09 | brain/contracts | Enrichment output contract is weakly validated | High | S | High | planned | TBD | 2026-03-28 |
| ARCH-R10 | brain/transactions | Mid-write failures can leave partial state and then commit retry state | High | S | High | planned | TBD | 2026-03-28 |
| ARCH-R11 | brain/batching | Batch claim and fetch logic can merge unrelated work and leave event order undefined | Med | M | High | planned | TBD | 2026-03-28 |
| ARCH-R12 | graph/coverage | Relational graph coverage is partial and per-event attribution is not implemented | Med | M | High | planned | TBD | 2026-03-28 |
| ARCH-R13 | observability | Observability surfaces expose liveness but not enough progress truth | Med | M | High | in-progress | TBD | 2026-03-28 |
| ARCH-R14 | testing | Architecture-level failure and concurrency tests are missing in the highest-risk seams | Med | M | High | in-progress | TBD | 2026-03-28 |

---

## Detailed findings

### ARCH-R01 — Single SQLite connection serializes daemon DB work

- Status: `planned`
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

- Status: `done`
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
- Pass 3A implementation completed.
- Implemented by taking the SQLite status snapshot under the DB lock and releasing the lock before external reachability awaits.
- Validated by the new daemon test proving the DB lock is released before external status waits.

---

### ARCH-R03 — In-memory ingest buffer is unbounded and `flush_batch_size` is unused

- Status: `planned`
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

- Status: `planned`
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

- Status: `planned`
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

- Status: `planned`
- Severity: `High`
- Effort: `L`
- Confidence: `High`
- Area: `storage/migrations`

#### Problem

Rust and Python share one SQLite schema, but there is no visible migration framework, ordered migration history, or schema version tracking.

#### Why it matters

This is a latent upgrade and compatibility risk. Today it is mostly dormant because the schema is still young, but the first nontrivial schema change can create mixed-version failures that surface late and opaquely across the daemon and brain.

#### Evidence

- `hippo/README.md#L8-L26`
- `hippo/crates/hippo-core/src/storage.rs#L12-L22`
- `hippo/crates/hippo-core/src/schema.sql#L1-L408`
- `hippo/brain/tests/conftest.py#L1-L21`
- `hippo/brain/src/hippo_brain/server.py#L42-L49`
- Search check: no matches for `migration`, `migrate`, `ALTER TABLE`, `schema_version`, or `PRAGMA user_version` across the implementation during this review.

#### Suggested first move

Introduce a schema versioning strategy before the next nontrivial DB change. The exact migration tool is less important than having a single explicit process.

#### Pass 1C triage notes

- Confirmed from code: Rust initializes from one embedded `schema.sql` snapshot, while Python runtime assumes a compatible DB already exists.
- Refined interpretation: this is more a latent evolution risk than an active runtime outage in the current tree.
- Current startup behavior is permissive and unversioned, which means future incompatibilities are likely to surface on specific query or write paths rather than at startup.

#### Acceptance criteria

- There is a canonical migration path with schema version tracking.
- Startup behavior for both Rust and Python is compatible with the chosen migration story.
- Upgrade and rollback expectations are documented.
- A current-runtime versus older-schema test has an explicit outcome: migrate successfully or fail fast with a clear incompatibility error.

#### Verification

- Pass 1C review completed.
- Recommended validation test: create a DB from an older schema fixture, then exercise current daemon startup plus one current brain path and require either a successful migration or a clear incompatibility failure.

---

### ARCH-R07 — Custom redaction config is documented but not runtime-wired

- Status: `done`
- Severity: `High`
- Effort: `S`
- Confidence: `High`
- Area: `security/redaction`

#### Problem

Docs and crate docs describe configurable `redact.toml` behavior, but the runtime paths inspected in this review construct `RedactionEngine::builtin()` directly.

#### Why it matters

This is a privacy and operator trust issue. The broken contract is specifically that custom redaction behavior can be configured, when the current runtime does not actually load or apply that configuration.

#### Evidence

- `hippo/README.md#L128-L129`
- `hippo/config/README.md#L23-L24`
- `hippo/crates/hippo-core/README.md#L8-L15`
- `hippo/crates/hippo-core/src/config.rs#L142-L155`
- `hippo/crates/hippo-core/src/config.rs#L186-L242`
- `hippo/crates/hippo-core/src/redaction.rs#L16-L35`
- `hippo/crates/hippo-daemon/src/daemon.rs#L266-L275`
- `hippo/crates/hippo-daemon/src/commands.rs#L114-L124`
- `hippo/crates/hippo-daemon/src/commands.rs#L377-L387`
- `hippo/config/redact.default.toml#L1-L24`

#### Suggested first move

Either wire `redact.toml` into runtime redaction or narrow the docs immediately so the product promise matches reality.

#### Pass 1C triage notes

- Strongly confirmed from code: `RedactConfig` and `RedactionEngine::new()` exist, but the inspected runtime callsites use `RedactionEngine::builtin()`.
- Important nuance: default installs are less risky than the original wording implied, because builtin patterns and `redact.default.toml` are aligned.
- The contract failure is specifically that custom patterns are modeled and documented but do not take effect at runtime.

#### Acceptance criteria

- Runtime redaction behavior matches the documented configuration story.
- There are tests for loading custom redact patterns and applying them on both normal and fallback paths.
- A custom-only secret format can be redacted through both daemon ingest and fallback handling.

#### Verification

- Pass 1C review completed.
- Pass 3A implementation completed.
- Runtime redaction now loads custom config where available and falls back to builtins only on load failure.
- Validated by new tests covering custom redaction on the fallback path and on the daemon flush path.

---

### ARCH-R08 — Query API is whole-query substring search, not semantic retrieval

- Status: `planned`
- Severity: `High`
- Effort: `M`
- Confidence: `High`
- Area: `brain/query`

#### Problem

The current `/query` implementation builds a single `%...%` pattern from the full input string and runs SQL `LIKE` over event commands plus knowledge node text fields. The codebase already contains an embeddings module and LanceDB support, and the docs describe semantic search, but the production query path is not wired to that capability.

#### Why it matters

This is both a capability gap and a docs-to-runtime mismatch. It is also weaker than ordinary keyword search because it relies on contiguous whole-query substring matching, which makes the natural-language “brain” experience brittle.

#### Evidence

- `hippo/brain/README.md#L3-L4`
- `hippo/brain/README.md#L51-L52`
- `hippo/README.md#L146-L152`
- `hippo/brain/src/hippo_brain/server.py#L59-L101`
- `hippo/brain/src/hippo_brain/embeddings.py#L28-L45`
- `hippo/brain/src/hippo_brain/embeddings.py#L99-L107`
- `hippo/crates/hippo-daemon/src/main.rs#L216-L233`

#### Suggested first move

Pick one path and make it explicit: either wire vector retrieval into `/query`, or relabel the current endpoint as lexical search until semantic retrieval is actually online.

#### Pass 1B triage notes

- Confirmed from code: `/query` is a SQLite text search path and does not generate query embeddings, open LanceDB, or perform vector retrieval.
- The vector seam is real, but it is isolated in `embeddings.py` and not connected to the production request path.
- Current server tests validate lexical and error-handling behavior, not semantic retrieval behavior.
- `hippo query` on the non-raw CLI path really does hit this endpoint, so the docs/runtime mismatch is user-visible.

#### Acceptance criteria

- The query implementation and docs describe the same behavior.
- If semantic retrieval is claimed, `/query` uses embeddings and vector search in the production path.
- Tests cover semantic retrieval and fallback behavior.

#### Verification

- Pass 1B review completed.
- Python test baseline passed during this audit.
- Recommended validation test: seed a semantically related but non-overlapping knowledge node, query with a natural-language question, and verify the node is returned only after real semantic retrieval is wired in.

---

### ARCH-R09 — Enrichment output contract is weakly validated

- Status: `planned`
- Severity: `High`
- Effort: `S`
- Confidence: `High`
- Area: `brain/contracts`

#### Problem

The enrichment model contract is represented in code, but the parser only strips code fences, runs `json.loads`, and fills a dataclass with `.get` defaults. Invalid shapes and wrong types can still flow into later write logic.

#### Why it matters

This creates avoidable runtime failures, makes the shared contract between prompt, model output, and storage less trustworthy, and materially increases the practical risk of `ARCH-R10`.

#### Evidence

- `hippo/brain/src/hippo_brain/models.py#L15-L42`
- `hippo/brain/src/hippo_brain/models.py#L23-L54`
- `hippo/brain/src/hippo_brain/enrichment.py#L48-L65`
- `hippo/brain/src/hippo_brain/enrichment.py#L186-L199`
- `hippo/crates/hippo-core/src/schema.sql#L247-L301`
- `hippo/brain/tests/test_enrichment.py#L31-L53`
- `hippo/brain/tests/test_enrichment_extended.py#L121-L147`

#### Suggested first move

Validate the parsed payload against a real schema or typed model before the write path touches it.

#### Pass 1C triage notes

- Strongly confirmed from code: `ENRICHMENT_SCHEMA` exists but is not enforced by the runtime parser.
- Invalid nested shapes can flow into storage logic and fail late, for example when non-string entity values reach `.lower()` in the write path.
- Current tests mostly prove happy-path parsing and even preserve some permissive malformed-entity behavior.

#### Acceptance criteria

- Invalid or malformed enrichment payloads fail cleanly before any DB write logic runs.
- Tests cover wrong field types, missing required fields, and invalid enum values.
- Invalid nested entity values are rejected before any write-side mutation occurs.

#### Verification

- Pass 1C review completed.
- Recommended validation test: feed invalid enum and invalid nested entity shapes through the parse→write path and verify no node, link, or enriched-event state is persisted.

---

### ARCH-R10 — Mid-write failures can leave partial state and then commit retry state

- Status: `planned`
- Severity: `High`
- Effort: `S`
- Confidence: `High`
- Area: `brain/transactions`

#### Problem

`write_knowledge_node` performs multiple inserts and updates before commit. If it raises midway, the worker catches the exception and then calls `mark_queue_failed` on the same connection, which commits retry state. That can persist partial writes together with a retryable queue state.

#### Why it matters

This risks duplicate knowledge nodes, inconsistent entity links, ambiguous recovery behavior after a retry, and duplicate node creation for the same event set on a later pass.

#### Evidence

- `hippo/brain/src/hippo_brain/enrichment.py#L132-L226`
- `hippo/brain/src/hippo_brain/enrichment.py#L228-L249`
- `hippo/brain/src/hippo_brain/server.py#L102-L134`
- `hippo/crates/hippo-core/src/schema.sql#L323-L379`

#### Suggested first move

Wrap the knowledge node write path in an explicit transaction boundary and rollback before retry bookkeeping if any part of the write fails.

#### Pass 1B triage notes

- Strongly confirmed from control flow: `_enrichment_loop()` calls `write_knowledge_node()`, and any exception in parse/write falls directly into `mark_queue_failed()` on the same connection.
- The write path is a multi-step unit that commits only at the end, which makes partial-state persistence possible if failure happens before commit and retry bookkeeping commits afterward.
- `ARCH-R09` materially increases the risk surface here because malformed nested payload values can pass parse and fail inside the write path.
- The schema allows the same event to be linked to multiple knowledge nodes, so retry-time duplication is structurally possible.

#### Acceptance criteria

- A failed node write leaves no partial node or link state behind.
- Retry bookkeeping is committed only after write rollback is complete.
- Tests simulate failures inside `write_knowledge_node` and verify rollback behavior.

#### Verification

- Pass 1B review completed.
- Python test baseline passed during this audit.
- Recommended validation test: drive `_enrichment_loop()` with a mocked LM payload containing an invalid nested entity value, then verify no partial node/link state persists and only retry state changes.

---

### ARCH-R11 — Batch claim and fetch logic can merge unrelated work and leave event order undefined

- Status: `planned`
- Severity: `Med`
- Effort: `M`
- Confidence: `High`
- Area: `brain/batching`

#### Problem

The worker claims multiple queue items based on queue metadata, then fetches matching events with `WHERE id IN (...)` and no `ORDER BY`. Those events are summarized together into one node.

#### Why it matters

Unrelated events can be merged into one enrichment result, event order inside the prompt is not guaranteed, and one problematic event can force unrelated good events to retry as a group.

#### Evidence

- `hippo/brain/src/hippo_brain/enrichment.py#L68-L125`
- `hippo/brain/src/hippo_brain/server.py#L105-L134`
- `hippo/crates/hippo-core/src/schema.sql#L345-L379`

#### Suggested first move

Define batching semantics explicitly. If batches are meant to represent coherent work, the claim and fetch logic needs ordering and grouping rules.

#### Pass 1B triage notes

- Confirmed from code: batching is queue-order-aware, not coherence-aware. It does not group by session, cwd, git repo, or timestamp.
- Important nuance: the “reorder” concern is more precisely “no event order guarantee” because the fetch query does not specify ordering.
- One failure currently applies to the entire claimed batch because retry bookkeeping is invoked with the full `event_ids` set.
- Fix effort is smaller if the goal is only deterministic ordering, but remains medium if the goal is meaningful same-work-unit batching.

#### Acceptance criteria

- Event order is stable within a batch.
- Batching rules are documented and tested.
- Retry behavior is appropriate for batch-level failure.

#### Verification

- Pass 1B review completed.
- Python test baseline passed during this audit.
- Recommended validation test: seed queue entries from different sessions or repos, capture the prompt sent to `client.chat()`, and verify current merge semantics plus event order behavior.

---

### ARCH-R12 — Relational graph coverage is partial and per-event attribution is not implemented

- Status: `planned`
- Severity: `Med`
- Effort: `M`
- Confidence: `High`
- Area: `graph/coverage`

#### Problem

The current write path does populate `knowledge_nodes`, `knowledge_node_events`, and `knowledge_node_entities`, but the richer relational graph pieces in the schema are only partially used. `relationships` are stored inside node JSON rather than in the `relationships` table, and `event_entities` is not populated at all.

#### Why it matters

This creates drift between the intended data model and the runtime model. It weakens SQL queryability, leaves relationship storage non-relational, and makes future graph features more expensive to complete. It also means true per-event entity attribution is not available today.

#### Evidence

- `hippo/crates/hippo-core/src/schema.sql#L166-L245`
- `hippo/crates/hippo-core/src/schema.sql#L247-L345`
- `hippo/brain/src/hippo_brain/models.py#L40-L49`
- `hippo/brain/src/hippo_brain/enrichment.py#L20-L24`
- `hippo/brain/src/hippo_brain/enrichment.py#L139-L146`
- `hippo/brain/src/hippo_brain/enrichment.py#L170-L205`

#### Suggested first move

Decide whether those richer relational tables are part of the real near-term design. If yes, populate them. If not, remove or clearly defer them so the schema tells the truth.

#### Pass 1C triage notes

- Refined interpretation: the issue is not that the graph layer is entirely unused. The node/event/entity link model is active today.
- The concrete gap is that `relationships` and `event_entities` are not populated by the current writer.
- Important nuance: true `event_entities` support is harder than “add inserts,” because the current enrichment contract produces one batch-level result across many events and does not carry enough per-event attribution to fill that table faithfully.

#### Acceptance criteria

- The runtime write path matches the intended graph model.
- Unused schema elements are either implemented or intentionally deferred with documentation.
- If `event_entities` is kept, the enrichment contract supports correct per-event attribution.

#### Verification

- Pass 1C review completed.
- Recommended validation test: write a node with non-empty relationships and assert current behavior explicitly: `knowledge_node_events` and `knowledge_node_entities` populate, while `relationships` and `event_entities` remain empty.

---

### ARCH-R13 — Observability surfaces expose liveness but not enough progress truth

- Status: `in-progress`
- Severity: `Med`
- Effort: `M`
- Confidence: `High`
- Area: `observability`

#### Problem

Hippo has basic liveness and counter observability, but it does not expose enough progress-oriented observability for a multi-process background system. The daemon surfaces `status` and `doctor`, and the brain exposes `/health`, but the signals are mostly “is it up?” rather than “is it making progress?”

#### Why it matters

This is the main operational maturity gap in the system. The current surfaces make it hard to distinguish:
- brain process is up but enrichment is stalled
- brain is reachable but LM Studio is unavailable
- queue is present but not draining
- logging is available through run-mode-specific behavior but not through one consistent first-party contract

#### Evidence

- `hippo/crates/hippo-daemon/src/commands.rs#L209-L237`
- `hippo/crates/hippo-daemon/src/commands.rs#L388-L455`
- `hippo/crates/hippo-core/src/storage.rs#L321-L365`
- `hippo/crates/hippo-daemon/src/main.rs#L22-L30`
- `hippo/crates/hippo-daemon/src/main.rs#L123-L127`
- `hippo/crates/hippo-core/src/config.rs#L167-L171`
- `hippo/brain/src/hippo_brain/server.py#L49-L58`
- `hippo/brain/src/hippo_brain/server.py#L105-L139`
- `hippo/brain/src/hippo_brain/__init__.py#L4-L15`
- `hippo/README.md#L146-L152`

#### Suggested first move

Define a minimal observability contract for both processes: first-party progress signals, richer health semantics, and logging behavior that is consistent with config and docs.

#### Pass 1D triage notes

- Refined interpretation: this is not an absence-of-observability problem. It is a progress-truth and consistency problem.
- The daemon already exposes useful counters, but the brain health surface is shallow and does not reflect queue state, last success, or DB health.
- Important nuance: launchd redirection likely makes logs usable in some modes, but `log_path()` is still unused and the runtime/config story is inconsistent.
- Additional nuance: the standalone brain `serve` path hardcodes port `9175`, so health reporting can diverge from configured expectations if the port changes.

#### Acceptance criteria

- Operators can inspect queue depth, failed queue count, last successful enrichment, and service reachability from first-party surfaces.
- Health endpoints and doctor/status flows distinguish liveness from progress.
- Log behavior matches the documented logging story.
- Common failure modes can be diagnosed without attaching a debugger.

#### Verification

- Pass 1D review completed.
- Pass 3A implementation in progress.
- Brain health now exposes queue depth, failed queue count, DB reachability, and last success/error fields.
- Doctor output now parses richer brain health JSON and reports queue and dependency details.
- Remaining gap: progress truth is improved, but the logging/config/runtime contract is not fully closed yet.

---

### ARCH-R14 — Architecture-level failure and concurrency tests are missing in the highest-risk seams

- Status: `in-progress`
- Severity: `Med`
- Effort: `M`
- Confidence: `High`
- Area: `testing`

#### Problem

Hippo is not missing tests in general. It is missing destructive, rollback, and concurrency tests at the exact process-and-storage boundaries where the architecture is weakest: daemon crash windows around accepted events, split-write atomicity in storage, mid-write rollback in the brain, and contention between status, ingest, and queue work.

#### Why it matters

This project lives at process and storage boundaries. Those are the places where architecture-level tests provide the most value. Current coverage proves many happy paths and some recovery paths, but it still does not characterize the failure windows that matter most.

#### Evidence

- Positive baseline: `hippo/crates/hippo-daemon/src/daemon.rs#L376-L520`, `hippo/crates/hippo-daemon/tests/shell_hook.rs#L1-L48`, `hippo/crates/hippo-daemon/src/commands.rs#L481-L539`, `hippo/crates/hippo-core/src/storage.rs#L528-L555`, `hippo/crates/hippo-core/src/storage.rs#L687-L752`, `hippo/brain/tests/conftest.py#L1-L21`, `hippo/brain/tests/test_enrichment.py#L56-L160`, `hippo/brain/tests/test_server.py#L240-L328`
- Risk seams with no direct test evidence found during this review: `hippo/crates/hippo-daemon/src/commands.rs#L96-L110`, `hippo/crates/hippo-daemon/src/daemon.rs#L31-L64`, `hippo/crates/hippo-daemon/src/daemon.rs#L125-L215`, `hippo/crates/hippo-core/src/storage.rs#L130-L189`, `hippo/brain/src/hippo_brain/enrichment.py#L132-L249`, `hippo/brain/src/hippo_brain/server.py#L102-L139`

#### Suggested first move

Add a small architecture test matrix focused on crash, rollback, and contention boundaries rather than broad feature count.

#### Pass 1D triage notes

- Refined interpretation: the repo already has meaningful tests. The gap is concentrated in destructive and concurrent seam testing.
- Important nuance: many Rust storage tests are in-memory and validate logic more than real shared-file/WAL/process behavior.
- The highest-value missing test remains the crash-loss characterization between sender success and durable flush.
- The next most valuable tests are rollback/atomicity tests for Rust event→queue writes and Python knowledge-node writes.

#### Acceptance criteria

- Tests exist for accepted-but-unflushed event loss windows.
- Tests exist for split-write atomicity in Rust storage.
- Tests exist for partial-write rollback in the brain.
- Tests exist for contention or blocking around daemon status and ingest.
- Tests exist for schema evolution expectations once a migration path is selected.

#### Verification

- Pass 1D review completed.
- Pass 3A implementation in progress.
- Added new seam tests for Phase 0, including:
  - status lock-scope validation on the daemon path
  - custom redaction behavior on fallback and normal flush paths
  - richer brain health endpoint coverage
  - config-driven brain serve entrypoint behavior
- Remaining gap: the broader crash, rollback, atomicity, and contention matrix is still planned for later phases.

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

### Pass 1B — Cluster C audit notes

#### Scope completed

- `ARCH-R08`
- `ARCH-R10`
- `ARCH-R11`

#### What was validated

- Python test baseline passed during this audit:
  - `63` tests in `brain/tests`
- The current non-raw query path is production-wired, but it is a SQLite text search path, not semantic retrieval.
- The vector-search seam exists, but it is not connected to production query handling.
- The enrichment write path plus retry bookkeeping is non-atomic at the application level.
- Batch construction is queue-order-aware, not work-unit-aware.

#### Observed versus theoretical split

- Observed directly from code:
  - `/query` builds one `%...%` pattern from the full input and uses SQL `LIKE`
  - query handling does not generate embeddings or open LanceDB
  - `write_knowledge_node()` performs multiple writes before commit
  - retry bookkeeping commits on the same connection after write-path failure
  - claimed batches are fetched without `ORDER BY`
  - one failed event can force the entire claimed batch through retry handling
- Still theoretical until explicit fault or query-quality tests are added:
  - exact user-visible retrieval quality under real data
  - exact frequency of malformed model payloads that fail mid-write
  - exact user impact of undefined event ordering within a batch

#### Dependencies surfaced

- `ARCH-R09` materially increases the practical risk of `ARCH-R10` because weak output validation makes malformed nested values more likely to fail inside the write path rather than before it.

#### Most targeted validation tests to add next

1. Semantic-query regression test using non-overlapping but semantically related wording.
2. Mid-write rollback regression test using an invalid nested entity value from the mocked LM response.
3. Batch-semantics test proving mixed-session or mixed-repo events currently get merged into one prompt.
4. Ordering regression test proving event order inside a batch is currently unspecified.

### Pass 1C — Cluster B audit notes

#### Scope completed

- `ARCH-R06`
- `ARCH-R07`
- `ARCH-R09`
- `ARCH-R12`

#### What was validated

- The project still has no visible schema migration or versioning mechanism for the shared Rust/Python SQLite database.
- Custom redaction configuration is documented and modeled, but not wired into the inspected runtime paths.
- The enrichment output schema exists in code, but runtime parsing does not enforce it.
- The current graph layer is partially active, but the richer relational graph tables are not populated by the writer.
- `ARCH-R09` is now clearly a practical amplifier for `ARCH-R10`.

#### Observed versus theoretical split

- Observed directly from code:
  - one embedded `schema.sql` snapshot drives Rust DB setup
  - Python runtime assumes a compatible DB already exists
  - no visible schema version tracking or ordered migration path
  - runtime redaction callsites use builtin patterns
  - `ENRICHMENT_SCHEMA` is not enforced at parse time
  - `relationships` and `event_entities` are not populated by the current write path
- Still theoretical until explicit compatibility or contract tests are added:
  - exact mixed-version failure modes after a future schema change
  - exact frequency of malformed model payloads in production
  - exact urgency of relational graph backfill for real product queries

#### Dependencies surfaced

- `ARCH-R09` materially increases the practical risk of `ARCH-R10`.
- `ARCH-R12` likely requires enrichment-contract changes if true per-event attribution is desired.
- `ARCH-R06` becomes much more urgent as soon as any nontrivial schema change lands.

#### Most targeted validation tests to add next

1. Old-schema compatibility test that requires either successful migration or a clear incompatibility failure.
2. Custom redaction integration test proving a custom-only secret format is redacted in runtime behavior.
3. Parse-boundary validation test proving malformed enrichment payloads fail before any write-side mutation.
4. Relational graph coverage test proving current behavior for `relationships` and `event_entities`.

### Pass 1D — Cluster D audit notes

#### Scope completed

- `ARCH-R13`
- `ARCH-R14`

#### What was validated

- The daemon already exposes `status` and `doctor`, so the observability gap is not absence but insufficient progress truth.
- Brain health reporting is shallow and mostly liveness-oriented.
- Logging behavior exists, but the config/runtime/docs contract is inconsistent.
- The repo already has meaningful unit and integration coverage.
- The test gap is concentrated in destructive, rollback, and contention seams rather than broad functional coverage.

#### Observed versus theoretical split

- Observed directly from code:
  - daemon status surfaces expose counters and reachability checks
  - brain `/health` exposes only minimal liveness information
  - standalone brain serve path hardcodes runtime behavior that can diverge from config
  - high-risk seam tests are still missing for crash windows, rollback, and contention
- Still theoretical until explicit validation tests are added:
  - exact operational confusion rate under real incidents
  - exact latency effect of status/ingest contention under realistic workloads
  - exact failure frequency of crash-loss or partial-write scenarios in production

#### Most targeted validation tests or diagnostics to add next

1. Extend `hippo doctor` so it can distinguish liveness from progress.
2. Add a crash-loss characterization test for accepted-but-unflushed events.
3. Add a trigger-based atomicity test for Rust event and queue writes.
4. Add an injected-failure rollback test for Python knowledge-node writes.
5. Add a contention test for daemon status versus ingest/query paths.

### Pass 1 — deeper architecture audit

#### Goals

- measure where the daemon actually blocks under mixed ingest and status load
- trace event lifecycle from shell send to DB persistence to queue consumption
- validate transaction boundaries and crash windows on both Rust and Python paths
- review schema ownership and evolution expectations across both runtimes
- verify how much of LanceDB and embeddings are intended for v1 versus later

#### Deliverables

- updated finding severities or confidence where needed
- any new findings discovered during source-level audit and failure analysis
- concrete diagrams or flow summaries for the hottest paths
- validation of which current risks are theoretical versus observed
- a prioritized list of targeted empirical validation tests for pass 2

#### Completion note

Pass 1 is complete as a source-level deep architecture audit. The remaining work is not more broad audit discovery; it is targeted empirical validation and remediation planning.

#### Questions answered in this pass

- The daemon-side contention model is now clear from code, even though empirical load characterization is still pending.
- The accepted-event durability loss window is real and now precisely scoped.
- The brain can plausibly produce duplicate or partial knowledge state under mid-write failure.
- The current graph/vector model is partly active but not fully wired into the richer intended design.
- The hardest operator truth gaps are around progress visibility, not basic liveness.

## Remediation planning section

### Pass 2 — prioritized remediation plan

#### Planning constraints

- preserve current local-first architecture
- keep macOS as the primary operating environment
- prefer improvements to operability and correctness before large redesign
- avoid expanding scope into unrelated product work
- validate by seam, not only by component

#### Status update

All audited findings `ARCH-R01` through `ARCH-R14` now move from `triaged` to `planned`.

#### Phase overview

| Phase | Goal | Findings | Rough effort | Primary gate |
|---|---|---|---|---|
| Phase 0 | fast containment and progress visibility | `ARCH-R02`, `ARCH-R07`, `ARCH-R13`, portions of `ARCH-R14` | ~1 sprint | status/doctor/redaction characterization |
| Phase 1 | daemon ingest reliability and durability | `ARCH-R01`, `ARCH-R03`, `ARCH-R04`, `ARCH-R05`, portions of `ARCH-R14` | ~1–2 sprints | crash, atomicity, and contention characterization |
| Phase 2 | brain write-path correctness and batching | `ARCH-R09`, `ARCH-R10`, `ARCH-R11`, portions of `ARCH-R14` | ~1 sprint | parse-boundary, rollback, and batching tests |
| Phase 3 | shared contract evolution and product-truth alignment | `ARCH-R06`, `ARCH-R08`, `ARCH-R12`, close `ARCH-R14` | ~1–2 sprints | migration, query-contract, and graph-model tests |

#### Phase 0 — fast containment and progress visibility

##### Goals

- remove the easiest high-risk stalls and contract mismatches
- make the system report progress, not only liveness
- improve operator trust before deeper runtime changes land

##### Findings included

- `ARCH-R02`
- `ARCH-R07`
- `ARCH-R13`
- cross-cutting portions of `ARCH-R14`

##### Why this order

- these are the smallest high-confidence fixes
- they reduce operator confusion immediately
- they make later daemon and brain work safer to validate

##### Exit criteria

- `GetStatus` no longer holds the DB lock across external awaits
- runtime redaction behavior matches the documented configuration story
- first-party surfaces expose queue depth, failed queue count, last successful enrichment, and dependency reachability
- health/doctor flows distinguish liveness from progress
- tests cover status contention and custom redaction behavior

##### Gating validation

- characterize `GetStatus` behavior while dependency checks are slow
- extend `hippo doctor` so it can distinguish “alive” from “not progressing”
- add a custom-redaction integration test if runtime redaction wiring is implemented

#### Phase 1 — daemon ingest reliability and durability

##### Goals

- make ingest behavior explicit and safe under load and failure
- remove the main daemon-side persistence and queueing risks

##### Findings included

- `ARCH-R01`
- `ARCH-R03`
- `ARCH-R04`
- `ARCH-R05`
- cross-cutting portions of `ARCH-R14`

##### Why this order

- this is the highest-risk runtime path
- brain-side correctness depends on events being captured and queued correctly first
- Phase 0 makes this work easier to observe and validate

##### Exit criteria

- common-case daemon DB work is no longer serialized behind one shared mutex
- queue capacity and backpressure policy exists and `flush_batch_size` is real
- ingest semantics are explicit and implemented accordingly
- event insert plus queue insert are atomic
- fallback replay preserves partially failed input for retry
- tests cover accepted-but-unflushed loss windows and partial replay failure

##### Gating validation

- mixed-load ingest/read/flush progress characterization
- accepted-but-unflushed event crash-window test
- trigger-based atomicity test for event and queue writes
- fallback replay partial-failure retention test

#### Phase 2 — brain write-path correctness and batching

##### Goals

- prevent malformed model output or mid-write failures from producing inconsistent enrichment state
- make batching deterministic and understandable

##### Findings included

- `ARCH-R09`
- `ARCH-R10`
- `ARCH-R11`
- cross-cutting portions of `ARCH-R14`

##### Why this order

- once ingest is trustworthy, enrichment correctness becomes the next risk multiplier
- `ARCH-R09` directly reduces the practical risk of `ARCH-R10`
- `ARCH-R11` should be settled before graph/query expansion

##### Exit criteria

- enrichment payloads are schema-validated or type-validated before any write path runs
- failed knowledge-node writes leave no partial node or link state behind
- retry bookkeeping commits only after rollback
- batch ordering is deterministic and batching/retry rules are documented and tested

##### Gating validation

- invalid-payload rejection test before write-side mutation
- injected-failure rollback test for `write_knowledge_node()`
- batch-order and batch-grouping contract tests

#### Phase 3 — shared contract evolution and product-truth alignment

##### Goals

- make shared DB evolution safe
- align graph/query behavior with what Hippo claims to do
- close the full architecture seam matrix

##### Findings included

- `ARCH-R06`
- `ARCH-R08`
- `ARCH-R12`
- close `ARCH-R14`

##### Why this order

- migration/versioning should exist before nontrivial schema/runtime expansion
- semantic retrieval and graph completion matter, but they should not outrank correctness and durability
- `ARCH-R12` is easier to implement correctly once batching semantics are defined

##### Exit criteria

- canonical migration/versioning path exists and startup behavior is defined
- `relationships` and `event_entities` are either implemented correctly or explicitly deferred so the schema tells the truth
- `/query` and docs match: either real semantic retrieval is wired in, or the endpoint is clearly positioned as lexical search
- CI includes architecture-level tests for crash loss, atomicity, rollback, contention, and migration expectations

##### Gating validation

- older-schema compatibility test with explicit outcome
- query-contract test matching the chosen product behavior
- relational graph coverage test matching the chosen data model
- full seam-test matrix required in CI

#### Minimal empirical validation set

Run these as the smallest useful cross-stack baseline before or during implementation:

- `cargo test -p hippo-core`
- `cargo test -p hippo-daemon`
- `uv run --project brain pytest brain/tests/test_enrichment.py brain/tests/test_enrichment_extended.py brain/tests/test_server.py brain/tests/test_embeddings.py -q`

#### Validation strategy by seam

1. before touching a risky seam, add one characterization test for that seam
2. prefer file-backed SQLite in WAL mode for architecture tests
3. use real async or subprocess boundaries where the risk depends on them
4. treat `ARCH-R14` as cross-cutting and close it only after the seam matrix is enforced

#### Existing baseline confidence

Current tests already provide baseline confidence for:
- Rust redaction basics and daemon-unavailable fallback
- storage happy path and fallback recovery
- daemon lifecycle basics
- brain happy path, retry marking, and enrichment-loop behavior
- current lexical query behavior and vector seam in isolation

#### New tests that are essential

- status/health contention characterization for `ARCH-R02` and `ARCH-R13`
- crash-loss characterization for `ARCH-R04`
- trigger-based atomicity test for `ARCH-R05`
- parse-boundary validation plus rollback test for `ARCH-R09` and `ARCH-R10`
- batching/order contract test for `ARCH-R11`
- old-schema compatibility test for `ARCH-R06`
- query-contract test for `ARCH-R08`
- relational graph coverage test for `ARCH-R12`

#### Immediate implementation queue

These items should be the next implementation focus:

1. `ARCH-R02`
2. `ARCH-R07`
3. `ARCH-R13`
4. `ARCH-R01`
5. `ARCH-R03`
6. `ARCH-R04`
7. `ARCH-R05`
8. `ARCH-R09`
9. `ARCH-R10`
10. `ARCH-R11`

`ARCH-R06`, `ARCH-R08`, and `ARCH-R12` remain planned, but should queue behind the runtime-correctness phases above.

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
- 2026-03-28: Pass 1B Cluster C audit completed for `ARCH-R08`, `ARCH-R10`, and `ARCH-R11`, including Python test baseline confirmation and triage notes.
- 2026-03-28: Pass 1C Cluster B audit completed for `ARCH-R06`, `ARCH-R07`, `ARCH-R09`, and `ARCH-R12`, including shared-contract and schema-evolution triage notes.
- 2026-03-28: Pass 1D Cluster D audit completed for `ARCH-R13` and `ARCH-R14`, and Pass 1 was marked complete as a source-level deep architecture audit with targeted empirical validation work queued for pass 2.
- 2026-03-28: Pass 2 remediation plan completed with phased workstreams, validation gates, and all audited findings moved from `triaged` to `planned`.
- 2026-03-28: Pass 3A Phase 0 implementation progress recorded. `ARCH-R02` and `ARCH-R07` moved to `done`; `ARCH-R13` and `ARCH-R14` moved to `in-progress` after health, doctor, config, and seam-test improvements.