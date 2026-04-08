# Hippo: Installation Impact, Smoke Test Plan & Risk Assessment

## Current Machine State

As of 2026-03-27, your MacBook has **zero Hippo footprint**:
- No `~/.local/share/hippo/` data directory
- No `~/.config/hippo/` config directory
- No LaunchAgents installed
- No Hippo processes running
- Port 9175 is free
- Build artifacts already exist from development: `target/` (2.4 GB), `brain/.venv` (294 MB)

---

## Part 1: What `mise run build:all` Does to Your Machine

### Immediate Effects

`build:all` runs two sub-tasks:

| Step | Command | What It Does |
|------|---------|--------------|
| `build` | `cargo build` | Compiles Rust crates into `target/debug/`. Produces `target/debug/hippo` binary (~50 MB debug). Downloads/compiles crate dependencies on first run. |
| `build:brain` | `uv sync --project brain` | Creates `brain/.venv/` virtualenv, installs Python dependencies (httpx, uvicorn, starlette, lancedb, etc.) |

### Filesystem Impact (build only)

| Path | Size | Purpose | Removable? |
|------|------|---------|------------|
| `./target/` | ~2.4 GB | Rust build cache + debug binary | `cargo clean` |
| `./brain/.venv/` | ~294 MB | Python virtualenv | `rm -rf brain/.venv` |

**Nothing is installed system-wide.** No files are written outside the project directory. No services are started. No shell config is modified. This is strictly a local build step.

### What It Does NOT Do

- Does NOT create `~/.local/share/hippo/` or `~/.config/hippo/`
- Does NOT install LaunchAgents
- Does NOT start any daemon or server process
- Does NOT modify `.zshrc` or `.zshenv`
- Does NOT bind any sockets or ports
- Does NOT contact any external network services

---

## Part 2: Full Installation Footprint (If You Go Beyond Build)

After build, the README suggests three additional steps. Here is what each does:

### Step 1: Shell Hooks (manual — requires editing `.zshrc`/`.zshenv`)

**Files modified:**
- `~/.zshenv` — add `source /path/to/hippo/shell/hippo-env.zsh`
- `~/.zshrc` — add `source /path/to/hippo/shell/hippo.zsh`

**Effect:** Every shell command you run fires a backgrounded `hippo send-event shell ...` call. If the daemon isn't running, events are written to `~/.local/share/hippo/fallback/*.jsonl`. The hook process is fire-and-forget with a 100ms socket timeout — if the daemon is down, the hook exits almost instantly and doesn't block your shell.

**Environment variables set:**
- `HIPPO_SESSION_ID` — a UUID generated once per login session (persists across subshells)

### Step 2: Daemon (`mise run run:daemon` or LaunchAgent)

**Processes:** One Rust binary (`hippo daemon run`)

**Filesystem created on first run:**

| Path | Type | Purpose |
|------|------|---------|
| `~/.local/share/hippo/` | Directory | Data root |
| `~/.local/share/hippo/hippo.db` | File | SQLite database (WAL mode) |
| `~/.local/share/hippo/hippo.db-wal` | File | SQLite write-ahead log |
| `~/.local/share/hippo/hippo.db-shm` | File | SQLite shared memory |
| `~/.local/share/hippo/daemon.sock` | Socket | Unix domain socket for IPC |
| `~/.local/share/hippo/fallback/` | Directory | Offline event buffer (JSONL) |
| `~/.config/hippo/` | Directory | Config root (created on `config edit`) |

**Network:**
- Binds a **Unix domain socket** (not a TCP port) at `~/.local/share/hippo/daemon.sock`
- Makes outbound HTTP to `localhost:1234` (LM Studio) and `localhost:9175` (brain) for health checks **only when `hippo status` or `hippo doctor` is called**

**Resource usage:**
- Memory: ~5-15 MB resident (Rust async runtime, SQLite connection)
- CPU: Near-zero idle. Wakes every 100ms to check flush buffer (no-op if empty)
- Disk: SQLite DB grows with usage. ~1 KB per shell command captured.

### Step 3: Brain Server (`mise run run:brain` or LaunchAgent)

**Processes:** One Python process (`uvicorn` serving Starlette app)

**Filesystem created on first run:**

| Path | Type | Purpose |
|------|------|---------|
| `~/.local/share/hippo/vectors/` | Directory | LanceDB vector store |

**Network:**
- Binds `127.0.0.1:9175` (localhost only, not externally reachable)
- Makes outbound HTTP to LM Studio (`localhost:1234/v1/chat/completions` and `/v1/embeddings`)

**Resource usage:**
- Memory: ~80-150 MB resident (Python, LanceDB, httpx)
- CPU: Near-zero idle. Polls enrichment queue every 5 seconds (cheap SQLite COUNT query). Spikes when calling LM Studio for enrichment.
- Disk: LanceDB vectors grow with enrichment. ~2-5 KB per enriched event.

### Step 4: LaunchAgents (optional — auto-start on login)

If installed, these are **templates with placeholders** that must be manually edited:

| File | Installed To | Effect |
|------|-------------|--------|
| `com.hippo.daemon.plist` | `~/Library/LaunchAgents/` | Auto-starts daemon at login, restarts on crash |
| `com.hippo.brain.plist` | `~/Library/LaunchAgents/` | Auto-starts brain at login, restarts on crash |

**`KeepAlive: true` + `RunAtLoad: true`** means launchd will:
- Start the process at login
- Restart it if it crashes
- Keep trying to restart indefinitely

---

## Part 3: How to Kill Everything

### Kill Running Processes

```bash
# Stop the daemon gracefully (sends shutdown via socket)
hippo daemon stop
# OR if the binary isn't on PATH:
cargo run --bin hippo -- daemon stop

# Kill brain server
pkill -f "hippo-brain serve"
# OR
pkill -f "uvicorn.*hippo"

# Nuclear option — kill anything hippo-related
pkill -f hippo
```

### Unload LaunchAgents (if installed)

```bash
# Stop and prevent restart
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.hippo.daemon.plist
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.hippo.brain.plist

# Remove the plist files
rm ~/Library/LaunchAgents/com.hippo.daemon.plist
rm ~/Library/LaunchAgents/com.hippo.brain.plist
```

### Remove Shell Hooks

Remove these lines from your shell config:
- From `~/.zshenv`: the `source .../hippo-env.zsh` line
- From `~/.zshrc`: the `source .../hippo.zsh` line

### Remove All Data

```bash
# Data directory (database, socket, logs, vectors, fallback files)
rm -rf ~/.local/share/hippo/

# Config directory
rm -rf ~/.config/hippo/

# Stale socket fallback (if it was ever used)
rm -f /tmp/hippo-daemon.sock
```

### Remove Build Artifacts

```bash
# From the project directory
cargo clean
rm -rf brain/.venv brain/.pytest_cache brain/.ruff_cache
```

### Verify Clean State

```bash
# Should all return nothing
pgrep -fl hippo
launchctl list | grep hippo
ls ~/.local/share/hippo/ 2>/dev/null
ls ~/.config/hippo/ 2>/dev/null
lsof -nP -i :9175
ls /tmp/hippo-daemon.sock 2>/dev/null
```

---

## Part 4: Smoke Test Plan

### Phase 0: Build Only (zero risk)

```bash
mise run build:all
```

**Verify:** Binary exists at `target/debug/hippo`. Python venv exists at `brain/.venv`.

```bash
# Confirm binary works
cargo run --bin hippo -- --help

# Test redaction engine (no daemon needed)
cargo run --bin hippo -- redact test "export API_KEY=sk-1234567890abcdef"
# Should show: Matched patterns: generic_secret
# Should show: export API_KEY=[REDACTED]
```

### Phase 1: Daemon in Foreground (easily killable, Ctrl-C)

Run the daemon in the foreground so you can see logs and kill it with Ctrl-C:

```bash
# Terminal 1: Start daemon
RUST_LOG=info cargo run --bin hippo -- daemon run
```

**Expected output:**
```
INFO daemon listening on "~/.local/share/hippo/daemon.sock"
```

**Verify from a second terminal:**

```bash
# Check status
cargo run --bin hippo -- status

# Check doctor
cargo run --bin hippo -- doctor

# Manually send a test event
cargo run --bin hippo -- send-event shell \
  --cmd "echo hello" --exit 0 --cwd /tmp --duration-ms 42

# Verify it was stored
cargo run --bin hippo -- events --since 1h

# Check sessions
cargo run --bin hippo -- sessions --today
```

**Kill:** Ctrl-C in Terminal 1, or `cargo run --bin hippo -- daemon stop` from Terminal 2.

**Verify cleanup:**
```bash
ls ~/.local/share/hippo/daemon.sock  # Should not exist after clean shutdown
```

### Phase 2: Shell Hook Test (one terminal only)

With daemon running in Terminal 1:

```bash
# Terminal 2: Source the hooks
source shell/hippo-env.zsh
source shell/hippo.zsh

# Run a few commands
echo "test command 1"
ls /tmp
date

# Check they were captured
cargo run --bin hippo -- events --since 1m
```

**Expected:** You should see your commands listed with timestamps, exit codes, durations, and cwds.

**Verify redaction works:**
```bash
export API_KEY=sk-test1234567890abcdef
# Run any command, then check:
cargo run --bin hippo -- events --since 1m
# The command should NOT contain the actual key
```

**Undo:** Close that terminal (hooks only apply to shells that sourced them).

### Phase 3: Brain Server (optional — requires LM Studio)

Only test this if you have LM Studio running with a loaded model:

```bash
# Terminal 3: Start brain
uv run --project brain hippo-brain serve
```

**Expected:** `Uvicorn running on http://127.0.0.1:9175`

**Verify:**
```bash
# Health check
curl http://localhost:9175/health

# Query (if events have been enriched)
curl -X POST http://localhost:9175/query \
  -H "Content-Type: application/json" \
  -d '{"text": "test"}'
```

**Without LM Studio:** Brain starts fine but enrichment loop will log errors every 5 seconds when it can't reach `localhost:1234`. Events queue up as `pending` and will be enriched when LM Studio becomes available. No data loss.

**Kill:** Ctrl-C in Terminal 3.

### Phase 4: Full Integration

After confirming each piece independently:

1. Start daemon in foreground (Terminal 1)
2. Start brain in foreground (Terminal 2)
3. Source hooks in Terminal 3
4. Run commands, watch daemon logs, check brain enrichment
5. Run `hippo doctor` — all checks should be green
6. Ctrl-C everything when done

---

## Part 5: Known Risks

### Low Risk

| Risk | Impact | Mitigation |
|------|--------|------------|
| **SQLite WAL files left behind** | Harmless `.db-wal` and `.db-shm` files in data dir | Cleaned up on next clean open, or `rm -rf ~/.local/share/hippo/` |
| **Stale socket file** | If daemon crashes, `daemon.sock` may linger; next start cleans it up automatically (daemon.rs:221) | `rm ~/.local/share/hippo/daemon.sock` |
| **Shell hook adds ~5-10ms latency per command** | The `git status --porcelain` call in the hook can be slow in very large repos | Hook is backgrounded and fire-and-forget; visible latency is from the `git` calls in `precmd`, not the socket send |
| **Disk usage growth** | SQLite DB grows ~1 KB/command. 100 commands/day = ~36 KB/day, ~13 MB/year | Negligible for any modern disk |
| **Port 9175 conflict** | Brain won't start if another service uses this port | Configurable in `config.toml` under `[brain] port` |

### Medium Risk

| Risk | Impact | Mitigation |
|------|--------|------------|
| **LaunchAgent `KeepAlive: true` respawn loop** | If daemon crashes repeatedly, launchd will keep restarting it, potentially consuming CPU in a tight crash loop | launchd has built-in throttling (10-second minimum between restarts). If it's a problem: `launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.hippo.daemon.plist` |
| **Brain server memory with large LanceDB** | Over time with heavy enrichment, the vector store could grow. LanceDB loads indexes into memory. | Monitor with `ls -lh ~/.local/share/hippo/vectors/`. For a year of shell commands this would still be small (~50-100 MB). |
| **Sensitive command capture** | Commands containing passwords/tokens typed directly (e.g., `mysql -p password123`) will be captured. Redaction patterns catch common formats but not all. | Review `config/redact.default.toml` patterns. The allowlist approach for env vars is solid. Custom patterns can be added to `~/.config/hippo/redact.toml`. |

### Things That Are NOT Risks

| Concern | Why It's Fine |
|---------|---------------|
| **Network exposure** | Daemon uses Unix socket (no TCP). Brain binds `127.0.0.1` only. Nothing is externally reachable. |
| **Data leaving machine** | Zero cloud calls. Only outbound HTTP is to `localhost:1234` (LM Studio). |
| **System stability** | Both processes are userspace, run under your UID, and have no elevated privileges. A crash affects only Hippo. |
| **Build step side effects** | `mise run build:all` only writes to `target/` and `brain/.venv/` inside the project directory. |

---

## Part 6: Code Issues Identified (No Changes Made)

1. **LaunchAgent plists are templates, not installable as-is.** The `__HIPPO_BIN__`, `__HOME__`, `__PATH__`, `__DATA_DIR__`, `__UV_BIN__`, `__BRAIN_DIR__` placeholders must be manually replaced. The `hippo daemon install` command just prints "Copy plist to ~/Library/LaunchAgents/ and load with launchctl." — it doesn't do the substitution. This means the install task (`mise run install`) does not actually produce a working LaunchAgent.

2. **`toml` dependency version has semver metadata warning.** Every cargo build prints: `warning: version requirement '1.1.0+spec-1.1.0' for dependency 'toml' includes semver metadata`. The `+spec-1.1.0` suffix should be removed from `Cargo.toml`.

3. **Daemon `stop` command doesn't verify shutdown.** `hippo daemon stop` sends the shutdown signal but doesn't wait for or confirm the process actually exited. If the socket is gone but the process is stuck, you'd need `pkill`.

4. **Brain server has no graceful shutdown signal path.** Unlike the daemon (which has a socket-based shutdown command), the brain server can only be stopped with SIGTERM/SIGINT (Ctrl-C or `kill`). There's no `hippo brain stop` equivalent.

5. **`hippo daemon restart` blocks forever.** The restart action (main.rs:39-53) sends shutdown, sleeps 1 second, then calls `daemon::run()` which enters the accept loop and never returns. This works as intended for foreground usage but means the CLI command hangs — it's a restart-in-place, not a detached restart.
