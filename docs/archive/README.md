# Archived documentation

Hippo's archive: docs that were accurate when written but now describe code paths, schema versions, or one-time efforts that have shipped, closed, or been superseded. Kept for historical reference; **not** maintained against present-day code.

If something here contradicts the live codebase, the live codebase is right.

## Subdirectories

### `v0.9-to-v0.16-history/`
Pre-v0.16 planning, retrospectives, and one-time install/audit work:
- `FEATURE_TIMELINE.md` — release-by-release feature progression v0.9 → v0.12.
- `agent-execution-plan.md` — multi-agent execution plan that completed.
- `architecture-review-tracker.md` — architecture-review working doc; superseded by `docs/capture-reliability/`.
- `schema-migration-strategy.md` — describes schema migrations as of v4. Live schema is v10+; the migration *mechanism* is unchanged but version numbers in the doc are out of date. See `crates/hippo-core/src/storage.rs` for current behavior.
- `smoke-test-and-risk-assessment.md` — 2026-03-27 install-impact assessment for a fresh-machine install. Useful as a historical record of what hippo touches on a Mac.

### `capture-reliability-overhaul/`
Design and decision records for the capture-reliability overhaul (P0 through P3). The overhaul shipped across PRs #67–#89; the active reference docs live in `docs/capture-reliability/`. This directory holds the artifacts that are now point-in-time records:
- `06-claude-session-watcher.md` — design proposal for the FS watcher (shipped in T-5/PR #86; default in T-7/PR #88; replaced tmux tailer in T-8/PR #89). Body describes the design as proposed; minor differences from the as-shipped form (notably the SessionStart hook) are documented in the project root `CLAUDE.md`.
- `m3-decision.md` — closed decision record for the M3 phase gate. The chosen option (D — empirical validation against live DB data) shipped via PR #88.
- `07-roadmap-review.csv` — review-tracking spreadsheet for the roadmap.

### `incident-2026-04-22/`
Closed sev1 postmortem: 21-day browser-capture outage and 8-day Claude-session outage. Root causes (tmux `-t` index error in the session hook; missing `extension/dist/` directory) were fixed; the systemic gap (no per-source health tracking) drove the capture-reliability overhaul. Kept as the forever-record of what happened.
