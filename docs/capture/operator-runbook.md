# Capture Operator Runbook

First-aid recipes for "something looks wrong with capture." Companion to [`architecture.md`](architecture.md) (the system reference) and [`sources.md`](sources.md) (per-source detail).

For an architectural overview of what each tool does, see [`architecture.md`](architecture.md). The recipes here assume you already understand the layers.

## At-a-glance: which tool answers which question?

| Question | Tool |
|---|---|
| Is capture broken right now? | `hippo doctor` (~2 s, exit code = fail count) |
| What's broken and what should I do about it? | `hippo doctor --explain` (CAUSE / FIX / DOC per failure) |
| Has anything quietly broken in the last hour? | `hippo alarms list` (exits 1 if any unacknowledged) |
| Is a specific source healthy right now? | `hippo probe --source <name>` (synthetic round-trip) |
| Is the brain enriching properly? | `hippo doctor` (the brain section) — capture and enrichment are decoupled (I-10) |
| What did I just lose? | `~/.local/share/hippo/*.fallback.jsonl` — fallback files; replayed on next daemon start |

## Doctor

`hippo doctor` runs ten checks in under 2 seconds. Each emits one of `[OK]`, `[WW]` (warning), `[!!]` (failure), or `[--]` (informational, e.g., "no rows ever"). Exit code is the count of `[!!]` failures.

Use `--explain` to get CAUSE / FIX / DOC per failure. The DOC link points back into this directory.

```bash
hippo doctor             # snapshot
hippo doctor --explain   # snapshot + remediation per failure
```

A clean run looks like this:

```
[OK] CLI version: 0.20.0
[OK] Daemon is running (uptime 12h)
[OK] Daemon version matches CLI
[OK] Database exists (167 MB)
[OK] Brain queue depth: 0 pending, 0 failed
...
```

A failed run will list the specific check, the source, and (with `--explain`) what to do.

## Alarms

`capture_alarms` is an append-only ledger of invariant violations. The watchdog writes; you acknowledge.

```bash
hippo alarms list                    # unacknowledged alarms; exit 1 if any
hippo alarms ack <id> --note "..."   # acknowledge with a note
hippo alarms prune                   # clear auto-resolved alarms
```

Acknowledgment is permanent. Use `--note` to record what you did about it.

## Probes

`hippo probe --source <name>` runs one synthetic round-trip on demand. Useful when you've just changed a configuration and want to confirm the source still lands. Probe rows are tagged with `probe_tag IS NOT NULL` and never appear in user-facing queries (see [`anti-patterns.md`](anti-patterns.md) AP-6).

The launchd `com.hippo.probe` job runs probes every 5 minutes automatically; manual invocation is for confirming a specific source after operator action.

## Recipes

### "I ran a command but it's not in `hippo events`"

```bash
# 1. Is the daemon up?
hippo doctor

# 2. Did the event land?
sqlite3 ~/.local/share/hippo/hippo.db "
  SELECT id, command, timestamp
  FROM events
  WHERE source_kind = 'shell'
    AND timestamp > strftime('%s','now') * 1000 - 600000
  ORDER BY id DESC
  LIMIT 10;
"

# 3. If not, is the source healthy per the watchdog?
sqlite3 ~/.local/share/hippo/hippo.db "
  SELECT source, last_event_ts, consecutive_failures, probe_ok, probe_lag_ms
  FROM source_health
  WHERE source = 'shell';
"

# 4. Is the shell hook actually sourced?
grep -l 'hippo.zsh' ~/.zshrc ~/.zshenv ~/.config/zsh/*.zsh 2>/dev/null

# 5. Is the daemon socket responsive?
hippo probe --source shell
```

If the probe lands but the original command didn't, the hook silently dropped the frame — check the fallback files:

```bash
ls -la ~/.local/share/hippo/*.fallback.jsonl 2>/dev/null
```

A fallback file existing means the daemon was unreachable; the next daemon start will replay it.

### "Doctor shows red"

```bash
hippo doctor --explain
```

Pick the first `[!!]` failure. The CAUSE/FIX/DOC block will tell you which file in this directory documents the relevant invariant. For example:

- `[!!] shell events: 8m ago (FAIL)` → I-1 violation. See [`architecture.md`](architecture.md) I-1; check whether your shell session has been idle (suppression) or whether the hook actually fired (run `hippo probe --source shell`).
- `[!!] watchdog heartbeat: 4m ago (FAIL)` → I-7 violation. Watchdog crashed or its launchd job is missing. Check `launchctl list | grep hippo`. If `com.hippo.watchdog` is missing, run `hippo daemon install --force`.
- `[!!] fallback files: 5 files > 24h (recovery broken)` → I-9 violation. Daemon is up but old fallback files aren't being drained. Check `journalctl`-equivalent logs at `~/.local/share/hippo/*.log` for write errors.

### "Brain queue is backing up"

Capture and enrichment are decoupled (I-10). A backed-up brain queue is an enrichment problem, not a capture problem; events are still landing.

```bash
sqlite3 ~/.local/share/hippo/hippo.db "
  SELECT status, COUNT(*) FROM enrichment_queue GROUP BY status;
"

# Live brain log
tail -f ~/.local/share/hippo/brain.stderr.log
```

Common causes:
- LM Studio model unloaded — load the model in LM Studio or set it to stay loaded.
- LM Studio model swapped — `[models].enrichment` in `~/.config/hippo/config.toml` doesn't match a loaded model.
- Brain crashed — `mise run restart` (or `launchctl bootout/bootstrap` the brain agent).

The watchdog reaper handles transient locks (rows stuck in `processing` for > `lock_timeout_secs`); see [`docs/brain-watchdog.md`](../brain-watchdog.md). A persistent backlog is operator-visible — neither the watchdog nor doctor will silently drop work.

### "Schema mismatch — daemon refuses to bind"

The daemon's startup handshake (`crates/hippo-daemon/src/schema_handshake.rs`) requires the daemon and brain schema versions to match exactly. If they don't, the daemon refuses to bind its socket.

```bash
# What does the live DB say?
sqlite3 ~/.local/share/hippo/hippo.db "PRAGMA user_version;"

# What version does the daemon binary expect?
hippo daemon version

# What version does the brain expect?
uv run --project brain python -c "from hippo_brain.schema_version import EXPECTED_SCHEMA_VERSION; print(EXPECTED_SCHEMA_VERSION)"
```

All three numbers must match. If they don't, `mise run install` (or `mise run install --clean`) brings everything to the same version. Don't manually `PRAGMA user_version = N` on the DB — migrations have to run.

### "Probe lag is climbing"

`source_health.probe_lag_ms` is the end-to-end latency for a synthetic round-trip. Healthy: tens to hundreds of milliseconds for shell, low seconds for browser/claude-session. Climbing lag suggests the daemon is starving (load, disk pressure, or socket backlog).

```bash
sqlite3 ~/.local/share/hippo/hippo.db "
  SELECT source, probe_lag_ms, datetime(probe_last_run_ts/1000, 'unixepoch', 'localtime')
  FROM source_health
  WHERE probe_lag_ms IS NOT NULL
  ORDER BY probe_lag_ms DESC;
"
```

If lag exceeds the I-8 threshold (15 min for `probe_last_run_ts`), the watchdog will fire I-8 alarm. Climbing-but-under-threshold lag is informational only.

## Recovery: manual operations

| Operation | Command |
|---|---|
| Backfill a specific Claude JSONL | `hippo ingest claude-session <path>` |
| Run a probe on demand | `hippo probe --source <name>` |
| Force install (overwrite plists, native messaging manifest, shell-hook config) | `hippo daemon install --force` |
| Stop everything (preserves data) | `mise run stop` |
| Stop everything hard (SIGKILL, preserves data) | `mise run nuke` |
| Start everything | `mise run start` |
| Full clean reinstall (rebuild + reinstall) | `mise run install --clean` |

## When to escalate to a follow-up issue

If `hippo doctor --explain` doesn't tell you what to do, or you're seeing a failure mode that isn't in this runbook, file a GitHub issue with:

- The exact `hippo doctor --explain` output
- The relevant `source_health` row(s)
- Any unacknowledged `capture_alarms`
- Recent `~/.local/share/hippo/*.log` lines

[`anti-patterns.md`](anti-patterns.md) AP-1..AP-12 are the review blockers; [`test-matrix.md`](test-matrix.md) is the failure-mode-to-test reference for adding regression tests.
