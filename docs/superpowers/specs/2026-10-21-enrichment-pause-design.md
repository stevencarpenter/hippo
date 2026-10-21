# Operator enrichment pause: design

Status: proposed, awaiting approval (2026-10-21).

## Intent

The operator wants Hippo to capture everything all day without the local 35B enrichment model running the MacBook fans while they work at the keyboard. Enrichment becomes a switch they flip by hand: paused during the day, resumed at bedtime so the night works through the backlog at full speed in another room.

The operator's reasoning, in their words: "having some lag of a few hours up to a day or two in enrichment is okay. The biggest benefits hippo provides is not knowledge that is still in context of the agent session, but context from months ago."

## Behavior

- `hippo enrichment pause` stops LLM enrichment. `hippo enrichment resume` starts it again. `hippo enrichment status` shows the state, when it last changed, and the pending count for each enrichment queue.
- While paused, every capture path keeps writing and every enrichment queue keeps growing. No queued item is dropped, expired, or marked failed because of the pause.
- On resume, the brain works through the entire backlog and then keeps enriching new work as it arrives. It never pauses itself. The operator pauses it again by hand, including in the morning if the backlog is not finished.
- The paused or running state survives brain restarts, daemon restarts, reboots, `hippo daemon install`, and `mise run install`. A fresh install with no state on disk is running (today's behavior).
- A batch already in flight when the operator pauses finishes normally; nothing is cancelled. Cancelling would let the stale-lock reaper increment retry counts and eventually mark rows failed, which is an anti-goal. (Design call.)
- The CLI works whether or not the brain is running, and a running brain notices a change within one poll interval (default 5 s) without a restart.

## What the pause covers

Paused: every LLM enrichment path in the brain enrichment loop: shell, agentic sessions (Claude, Codex, Cursor, opencode, pi), browser, workflow runs, and auto-memory documents.

Not paused, by the operator's decision:

- Capture: daemon, watchers, pollers, native messaging, probes.
- Embeddings: the embed orphan reaper and query embeddings. The embedding model is small and served by oMLX, so oMLX stays running during the day.
- Queries: `hippo ask`, `hippo query`, and every MCP tool, including `ask`, which uses the 35B model briefly. The operator may revisit this if load stays high; it is not part of this change.
- Jev classification and Jev reranking (remote API, no local load).
- The watchdog, doctor, and telemetry.

## Relationship to the existing bench pause

The brain already has `POST /control/pause` and `/control/resume`, used by hippo-bench through a lockfile (`~/.local/share/hippo-bench/pause.lock`, BT-06/BT-16). That pause is a full quiesce for benchmark isolation: it also rejects queries with 503, stops the embed reaper, and stops classification. It is in memory only.

The operator pause is a separate, independent state with a narrower scope. Neither may clear the other: a bench resume must not resume operator-paused enrichment, and `hippo enrichment resume` must not lift a bench pause. Enrichment runs only when neither pause is active. The bench pause behavior is unchanged.

## Alarms and doctor

A deliberate pause is not an outage.

- Watchdog I-12 (brain preflight stuck) must not fire while the operator pause is active, regardless of the order in which the operator paused and stopped oMLX. When enrichment resumes, I-12 evaluates only fresh preflight results, so a failure count recorded before or during the pause cannot raise an alarm by itself.
- `hippo doctor` shows one line stating that enrichment is paused by the operator and since when. That line does not count as a failure. Doctor checks that would fail or warn only because enrichment is paused (inference reachability, pending enrichment backlog, auto-memory enrichment pending) report informational status (`[--]`) instead while paused. Checks for real defects, such as failed queue rows, still report as today.
- Watchdog I-19 (stale auto-memory projection) must not fire while the operator pause is active. After resume, a document's staleness is measured from the later of its last update and the resume time, so the overnight drain does not alarm on items that were only waiting for the pause to end. (Builder-check finding; settled as a design call because it follows from the "no alarm noise" anti-goal.)
- No other watchdog invariant depends on enrichment progress: I-14 still holds because embeddings keep running, and I-16, I-20, and the probes are unaffected (builder check, 2026-10-21).
- Because the watchdog (Rust) must read the pause state, the state must be readable from both Rust and Python without the brain running.
- An inference server that is down while enrichment is running is still an outage and still alarms exactly as today.
- The brain `/health` response reports the operator pause state so doctor and other readers do not have to infer it.

## Constraints

- Single host; no multi-host coordination (project policy).
- No new required service. Storing the state must not require a running brain.
- Follows existing conventions: Rust CLI subcommand in `hippo-daemon`, anyhow errors, Unix epoch milliseconds, XDG data paths.
- Probe rows remain excluded from user-facing queries (AP-6); nothing in this change touches them.
- Documentation is updated in the same PR: README command list, operator runbook, `docs/capture/architecture.md` I-12 row, and the brain watchdog doc where it describes pause.

## Left to the builder

- Where the state lives. A file in the hippo data directory (the precedent is the bench lockfile, chosen to avoid a migration) or a SQLite row (would require a schema migration and a minor version bump). Either is acceptable if the behavior above holds; prefer the option that needs no migration unless there is a concrete reason.
- How the brain observes changes (poll the state on each loop iteration, or a control endpoint plus persisted state).
- Exact CLI output wording and whether `status` also prints the oldest pending item age.
- Test layout, including behavioral tests for: queue growth without loss while paused, survival across a brain restart, independence from the bench pause in both directions, and no I-12 alarm for a pause taken after oMLX was already down.

## Non-goals

- Scheduled or automatic pause/resume (time windows, idle detection, power source). The operator flips it by hand.
- Throttling, rate limits, or partial enrichment while paused.
- Pausing or throttling queries or MCP `ask`.
- Stopping or starting oMLX. The keeper change in PR #323 is separate.

## Anti-goals

These would make the change a failure even if it technically works:

- Any captured event lost, or any queued item marked failed or expired, because of a pause.
- A pause that silently reverts to running after a restart, so the fans start at the keyboard.
- Alarm or doctor noise for a deliberate pause.
- A bench run silently resuming operator-paused enrichment.
