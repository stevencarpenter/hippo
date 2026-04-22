//! Skeletons for the capture-reliability invariants (I-1, I-2, I-7, I-8,
//! I-10) that cannot be exercised against `main` today because they depend
//! on infrastructure defined in `docs/capture-reliability/{01-source-health,
//! 04-watchdog, 05-synthetic-probes, 06-claude-session-watcher}.md` and not
//! yet implemented.
//!
//! Every test in this file is `#[ignore]` with an explanation pointing at
//! the blocking roadmap task (see `docs/capture-reliability/07-roadmap.md`).
//! The file is committed so that when each P0/P1/P2 phase lands, the test
//! is a one-line enable, not a "remember to write this later" TODO.
//!
//! Tracking: docs/capture-reliability/09-test-matrix.md rows F-10..F-14.

// ============================================================================
// F-11 / I-1 — shell liveness
// ============================================================================

#[test]
#[ignore = "blocked on P0.1 (source_health table) — the test cannot run until \
            the daemon writes `source_health WHERE source='shell'` on every \
            flush. Once the schema + write path land (01-source-health.md, \
            P0.1 in 07-roadmap.md), remove this attribute and fill in the \
            body using the daemon test harness."]
fn i1_shell_liveness() {
    // Given: daemon running, fresh source_health table.
    // When: 3 shell events flushed through send_event_fire_and_forget.
    // Then: SELECT FROM source_health WHERE source='shell' shows
    //         last_event_ts within 1 s of now_ms
    //         consecutive_failures = 0
    //         events_last_1h increased by >=3
    //         probe_ok = 1
    unimplemented!("implement once P0.1 lands");
}

#[test]
#[ignore = "blocked on P0.1 — source_health.probe_ok must be 0 when zsh not \
            running, per I-1 suppression rules in 02-invariants.md"]
fn i1_shell_liveness_suppressed_when_no_zsh_process() {
    // Given: no zsh process (watchdog probe sets probe_ok=0).
    // When: 60+ seconds pass with no events.
    // Then: source_health row has probe_ok=0 and the invariant does not fire.
    unimplemented!();
}

// ============================================================================
// F-10 / I-2 — Claude-session end-to-end
// ============================================================================

#[test]
#[ignore = "blocked on P2.1 (FS-watched session ingester, 06-claude-session-watcher.md) \
            AND P0.1 (source_health). Once both land, drive the watcher \
            with a JSONL that grows over time and assert the claude_sessions \
            row appears within 5 min + source_health updates."]
fn i2_claude_session_end_to_end() {
    // Given: a JSONL under ~/.claude/projects/<p>/<id>.jsonl with mtime < 5 min.
    // When: the FS-watched session ingester processes it.
    // Then: claude_sessions has a row with matching session_id
    //       source_health WHERE source='claude-session' is fresh.
    unimplemented!();
}

// ============================================================================
// F-13 / I-7 — watchdog heartbeat
// ============================================================================

#[test]
#[ignore = "blocked on P1.1 (watchdog process, 04-watchdog.md). When the \
            watchdog lands, verify its heartbeat row in source_health \
            updates within 60 s and goes stale (> 180 s) if the watchdog \
            is killed."]
fn i7_watchdog_heartbeat() {
    // Given: watchdog process running.
    // When: 60 seconds pass.
    // Then: source_health WHERE source='watchdog' has last_event_ts within 60 s of now_ms.
    // Then (negative): kill watchdog; after 180 s, doctor flags [!!] watchdog stale.
    unimplemented!();
}

// ============================================================================
// F-12 / I-8 — synthetic probe round-trip
// ============================================================================

#[test]
#[ignore = "blocked on P2.2 (synthetic probes, 05-synthetic-probes.md). When \
            probes land, inject a synthetic shell event with probe_tag set \
            and assert it round-trips into events + updates source_health \
            probe_latency_ms within 15 min threshold."]
fn i8_probe_round_trip() {
    // Given: probe scheduler running, synthetic event injected with probe_tag.
    // When: daemon processes it.
    // Then: events row exists with probe_tag matching; source_health.probe_latency_ms < 15 min;
    //       probe event is NOT exposed in user-facing queries (RAG, hippo ask).
    unimplemented!();
}

// ============================================================================
// F-14 / I-10 — capture decoupled from enrichment
// ============================================================================

#[test]
#[ignore = "blocked on P0.2 (source_health writes on every capture path). The \
            test kills the brain process, then sends shell events; \
            source_health must still update (capture is independent of \
            enrichment). When P0.2 lands, enable this and assert the \
            decoupling contract."]
fn i10_decoupled_from_brain() {
    // Given: daemon running, brain DOWN.
    // When: 3 shell events flushed.
    // Then: source_health WHERE source='shell' shows fresh last_event_ts
    //       (enrichment-queue depth may grow, but capture health is green).
    unimplemented!();
}
