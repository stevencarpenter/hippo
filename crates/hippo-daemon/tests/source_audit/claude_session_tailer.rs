//! Source #4 — Claude session segments via the tailer (live JSONL follow).
//!
//! Production path: the `SessionStart` Claude Code hook spawns a tmux
//! window that runs `hippo ingest claude-session --inline <path>` which
//! calls `claude_session::ingest_tail`. The tailer is supposed to (a)
//! send tool-call events over the daemon socket AND (b) re-run the
//! segment extractor over the whole JSONL on every non-empty tick plus
//! on the final drain so the segments land in `claude_sessions`.
//!
//! As of 2026-04-22, `ingest_tail` on main has signature
//! `(path, socket_path, timeout_ms)` — it does NOT take a `db_path` and
//! does NOT call `write_session_segments`. The batch path already does.
//!
//! A parallel agent is bringing the tailer to parity with the batch
//! writer. Once it lands, enable `tailer_writes_claude_sessions_row_when_file_grows`
//! below by removing the `#[ignore]` attribute.

/// The full end-to-end test that should exist once the parallel tailer
/// fix lands. Currently blocked because `ingest_tail` does not accept a
/// `db_path` parameter and does not invoke `write_session_segments`.
///
/// When the fix lands, replace the body with the version tracked in
/// the PR description (lives in git history if backed out), or follow
/// this skeleton:
///
/// 1. Write a short Claude JSONL at `<projects>/<encoded>/<uuid>.jsonl`.
/// 2. Spawn `ingest_tail(&path, &socket, timeout, &db)` in a tokio task
///    bounded by a `tokio::time::timeout` of ~5–6s.
/// 3. After the tailer starts, append a few more lines to the JSONL so
///    the tailer observes file growth and triggers a segment flush.
/// 4. Assert `SELECT COUNT(*) FROM claude_sessions WHERE session_id = ?`
///    is ≥1 and that `claude_enrichment_queue` has a matching row.
/// 5. Abort the tailer task and shut the daemon down.
#[test]
#[ignore = "blocked on parallel tailer-fix PR that extends ingest_tail to take \
            db_path and call write_session_segments. Remove this attribute \
            once the fix is merged to main and the signature matches \
            ingest_batch(path, socket, timeout, db_path)."]
fn tailer_writes_claude_sessions_row_when_file_grows() {
    // Intentionally empty — see doc-comment.
    //
    // Once the fix lands, reinstate the test body that:
    //   * lays a fixture JSONL,
    //   * spawns `ingest_tail(...)` in a tokio task bounded by a
    //     `tokio::time::timeout`,
    //   * appends to the JSONL mid-run,
    //   * asserts ≥1 row in `claude_sessions` with the fixture
    //     session_id and a matching `claude_enrichment_queue` row.
    unimplemented!("blocked on tailer fix");
}
