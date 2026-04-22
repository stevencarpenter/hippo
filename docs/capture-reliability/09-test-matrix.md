<!-- TL;DR: One row per capture-reliability failure mode. Every row carries a test. The status column tells you whether that test exists, ships in this PR, or is blocked on unlanded P0/P1/P2 infrastructure. When a mode recurs, its row's test must fire before a user notices data loss. -->

# Test Matrix for Capture Reliability

This matrix is the companion to [02-invariants.md](02-invariants.md) and
[08-anti-patterns.md](08-anti-patterns.md). It exists to make one question
answerable at a glance: **for every failure avenue we know about, is there a
test that would have caught it?**

Every failure mode in the 2026-04-22 sev1 investigation (issues #49–#53, #58,
plus the two hotfixes #54/#55) and every invariant from 02-invariants.md must
appear as a row. When a row is marked `blocked-on-*`, the test file and a
`#[ignore]` skeleton must still exist, so that enabling the infrastructure is
a one-line change rather than "remember to write the test later".

## Conventions

- **`existing`** — test was already on `main` before this PR
- **`existing (#NN)`** — test was added by issue/PR #NN (cross-reference only)
- **`new (this PR)`** — test added by the PR introducing this matrix
- **`added-by-#NN-fix`** — fix PR for #NN owns the regression test; cross-referenced here to avoid duplication
- **`blocked-on-P0.X`** — test skeleton exists with `#[ignore]`; fires when the named roadmap task lands
- **`source-change-required`** — test cannot be written without a source change that is outside the scope of this PR; noted here so the gap is explicit

"Invariant" refers to I-1..I-10 defined in 02-invariants.md.

## Failure modes

| # | Failure mode | Trigger / evidence | Test type | Location | Status | Invariant |
|---|---|---|---|---|---|---|
| F-1 | tmux hook `new-window` without `-t` lands in wrong session | #48 (1330113): `-t` flag removed; session creation fires but tmux window appears in whichever session was "current" on the isolated server, not the Claude session | shell integration | `tests/shell/test-claude-session-hook.sh` | existing (#55) | I-2 |
| F-2 | Firefox extension `dist/` absent at runtime | #54: build pipeline produced no `dist/*.js`; `hippo doctor` didn't flag it | rust integration (doctor check) | `crates/hippo-daemon/src/commands.rs` `#[cfg(test)] mod tests` | existing (#54) | I-4 |
| F-3 | `hippo ingest claude-session --batch` fires but `claude_sessions` rows are 0 | #58 | rust integration | `crates/hippo-daemon/tests/claude_session.rs` | added-by-#58-fix (in-flight, worktree `agent-ac306c6f`) | I-2 |
| F-4 | Redaction regex false-positives drop or corrupt legitimate events | #52 | rust unit (negative cases) | `crates/hippo-core/src/redaction.rs` (`mod tests`) | new (this PR) | I-5 |
| F-5 | `claade` / other wrappers break PID-chain assumption | #50 | shell integration | `tests/shell/test-hook-pid-ppid.sh` | new (this PR) — documents current `$PPID` behavior; fails if hook ever assumes deeper walk without updating this test | I-2 |
| F-6 | Native Messaging manifest path drifts after binary move | user moves binary; doctor never cross-checks manifest `path` field | rust integration (doctor check) | `crates/hippo-daemon/tests/nm_manifest_doctor.rs` | new (this PR) — skeleton `#[ignore]` until doctor grows the check | source-change-required |
| F-7 | Daemon restart during NM send silently drops browser visits | #51 | rust integration | `crates/hippo-daemon/tests/nm_restart_integration.rs` | new (this PR) — fallback-file-survives-restart exercised; end-to-end NM send across restart is `#[ignore]` pending a test harness for the NM stdio stream | I-4 |
| F-8 | Fallback JSONL accumulates > 24 h while daemon is up (drain broken) | design invariant I-9 | rust integration (doctor check) | `crates/hippo-daemon/tests/fallback_age_doctor.rs` | new (this PR) — skeleton `#[ignore]` with note that doctor currently only counts fallback files, does not inspect mtime | I-9 / source-change-required |
| F-9 | Apr 10–17 capture blackout (root cause unknown) | #49 | investigation pending | — | blocked-on-#49 | — |
| F-10 | Claude JSONL grows but no `claude_sessions` row within 5 min | invariant I-2 | rust integration (probe) | `crates/hippo-daemon/tests/capture_invariants.rs::i2_claude_session_end_to_end` | blocked-on-P2.1 (FS-watcher) + P0.1 (source_health) | I-2 |
| F-11 | Shell hook fires in 2 min but no `events` row appears | invariant I-1 | rust integration (probe) | `crates/hippo-daemon/tests/capture_invariants.rs::i1_shell_liveness` | blocked-on-P0.1 (source_health) | I-1 |
| F-12 | Synthetic probe round-trip > 15 min | invariant I-8 | rust integration | `crates/hippo-daemon/tests/capture_invariants.rs::i8_probe_round_trip` | blocked-on-P2.2 (synthetic probes) | I-8 |
| F-13 | Watchdog heartbeat stale > 180 s | invariant I-7 | rust integration | `crates/hippo-daemon/tests/capture_invariants.rs::i7_watchdog_heartbeat` | blocked-on-P1.1 (watchdog process) | I-7 |
| F-14 | `source_health` stops updating when brain is down | invariant I-10 (decoupling) | rust integration (kill-brain canary) | `crates/hippo-daemon/tests/capture_invariants.rs::i10_decoupled_from_brain` | blocked-on-P0.2 (`source_health` writes on every capture path) | I-10 |
| F-15 | Hippo's own CI / sev1 failures never graduate into `lessons` | #53 | brain unit (xfail) | `brain/tests/test_lessons_graduation_hippo.py` | new (this PR) — `@pytest.mark.xfail(reason="tracked in #53")`; fails-closed on fix | — |
| F-16 | Schema version drift between daemon and brain | v0.13.0 handshake incident | rust integration | `crates/hippo-daemon/tests/schema_handshake.rs` (existing) + negative case added | existing + new (this PR) | — |
| F-17 | Silent error swallowing via `.filter_map(Result::ok)` in capture paths | AP-11 in 08-anti-patterns.md; observed at `crates/hippo-core/src/storage.rs:805` | static analysis (semgrep) + regression test for the rule itself | `.semgrep.yml` + `tests/semgrep/silent_swallow_fixture.rs` | new (this PR) — rule file + fixture; wiring into CI (adding `.semgrep.yml` to the security workflow) is **follow-up** because `security.yml` is currently path-scoped to `shell/` only | AP-11 |
| F-18 | tmux `base-index != 0` causes "index N in use" | #48 (1330113, pre-fix path) | shell integration | `tests/shell/test-claude-session-hook.sh` | added-by-#55-fix | I-2 |
| F-19 | Session name with shell metacharacters (spaces, colons) breaks hook | defensive — not observed, but near a sev1 path | shell integration | `tests/shell/test-claude-session-hook-extended.sh` | new (this PR) | I-2 |
| F-20 | No tmux server running at hook time — batch fallback path | hook line 106-110 | shell integration | `tests/shell/test-claude-session-hook-extended.sh` | new (this PR) | I-2 |
| F-21 | `$TMUX_PANE` unset but tmux server is up — fallback hippo-session reuse | hook line 96-105 | shell integration | `tests/shell/test-claude-session-hook-extended.sh` | new (this PR) | I-2 |
| F-22 | `check_claude_session_hook_at` false-OK when settings.json is malformed / not-object | regression for #45, #46, #48 | rust unit | `crates/hippo-daemon/src/commands.rs` `mod tests` (`test_hook_check_structural_type_mismatch`, `test_hook_check_not_configured`, `test_hook_check_match_missing_script`) | existing | — |
| F-23 | Claude settings.json `hooks.SessionStart` array has multiple hippo entries, one stale one current | observed during #48 rollout | rust unit | same as F-22 (`test_hook_check_multiple_entries_one_exact_match`) | existing | — |
| F-24 | `hippo doctor` output for hook check is not behaviourally asserted — only smoke-tested ("does not panic") | code review of `commands.rs` `mod tests` | rust unit — assert on captured stdout | same as F-22 | source-change-required (would need `println!` → returning `String`, or a `writeln!(w, …)` injection) | — |

### Invariant coverage cross-check

| Invariant | Row(s) | Status |
|---|---|---|
| I-1 Shell liveness | F-11 | blocked-on-P0.1 |
| I-2 Claude-session end-to-end | F-1, F-3, F-5, F-10, F-18..F-21 | 5 shell tests + 1 rust test land in this PR / sibling PRs; F-10 (auto) blocked |
| I-3 Claude-tool liveness | — | not yet implemented; skeleton row TBD when invariant test design lands |
| I-4 Browser liveness | F-2, F-7 | fix PRs + new (this PR) |
| I-5 Redaction correctness (no over-redaction) | F-4 | new (this PR) |
| I-6 Daemon liveness | implicit in existing daemon start-up tests | existing |
| I-7 Watchdog heartbeat | F-13 | blocked-on-P1.1 |
| I-8 Probe round-trip | F-12 | blocked-on-P2.2 |
| I-9 Fallback recovery freshness | F-8 | skeleton; blocked on doctor growing an age check |
| I-10 Capture decoupled from enrichment | F-14 | blocked-on-P0.2 |

Any invariant without at least one `new (this PR)` or `existing` row is by
definition gated on a P0/P1/P2 task. If you see an invariant listed in
02-invariants.md that is not in the table above, that is a gap — open an
issue and add a row.

## Test coverage gaps

These are the failure modes that **cannot** be tested against `main` today:

- **F-6 NM manifest validation** — `hippo doctor` never reads the NM manifest. The doctor check would need ~20 lines in `commands.rs` (read JSON, resolve `path`, assert executable, assert `allowed_extensions` contains `hippo-browser@local`). Test skeleton exists; remove `#[ignore]` once the source check lands.
- **F-8 Fallback age in doctor** — `storage::list_fallback_files` returns paths sorted by name; doctor only prints a count. An age check requires either (a) reading each file's mtime in doctor, or (b) a new `storage::list_stale_fallback_files(dir, cutoff_ms)` helper. Test skeleton exists; source change tracked by follow-up issue.
- **F-10..F-14** — All require `source_health` table and the watchdog/probe subsystems from 01-source-health.md, 04-watchdog.md, 05-synthetic-probes.md. Skeletons live in `crates/hippo-daemon/tests/capture_invariants.rs`.
- **F-15** — `#53` is about the plumbing from "hippo CI failure" → `upsert_cluster`; the `lessons.py` logic itself has solid unit coverage (`brain/tests/test_lessons.py`). Our xfail test asserts the **end-to-end** pipeline. It will stay xfail until the plumbing ships.

## Running the tests

```bash
# Rust — the redaction negative cases, doctor unit tests, NM/fallback skeletons
cargo test -p hippo-core redaction::
cargo test -p hippo-daemon commands::tests::test_check_claude_session_hook
cargo test -p hippo-daemon --test nm_manifest_doctor
cargo test -p hippo-daemon --test nm_restart_integration
cargo test -p hippo-daemon --test fallback_age_doctor
cargo test -p hippo-daemon --test capture_invariants
cargo test -p hippo-daemon --test schema_handshake_negative

# Every `#[ignore]` skeleton — re-enable when its P0/P1/P2 dependency lands
cargo test -p hippo-daemon -- --ignored --nocapture

# Shell
bash tests/shell/test-claude-session-hook-extended.sh
bash tests/shell/test-hook-pid-ppid.sh

# Brain (xfail stays green; becomes pass on #53 fix)
uv run --project brain pytest brain/tests/test_lessons_graduation_hippo.py -v

# Static analysis (when wired into CI)
semgrep --config .semgrep.yml crates/ brain/
```

## How to extend

When you add a new capture path (say, iMessage ingestion) or discover a new
failure mode, you MUST:

1. Add a row to the **Failure modes** table above. Include:
   - A one-sentence description of the failure.
   - The trigger (issue number, commit, design-doc reference).
   - The chosen test type and file path.
   - An invariant reference, if any.
2. Write the test. If the test depends on infrastructure that does not exist
   yet, commit the test file with a `#[ignore = "blocked on <task-id>"]`
   attribute (Rust) or `@pytest.mark.skip(reason=...)` / `xfail` (Python) so
   the file compiles and the skeleton is visible.
3. If the test cannot be written at all without changing source, add a row to
   **Test coverage gaps** explaining why, and open a follow-up issue for the
   source change. Do not silently drop the failure mode.
4. Update the **Invariant coverage cross-check** if the new row fills a gap.

The matrix is the source of truth for "what do we test?" If a failure
recurs and its row's test did not fire, that is a bug in the test, not
additional justification to skip writing a test next time.
