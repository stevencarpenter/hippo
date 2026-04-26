#!/usr/bin/env bash
set -euo pipefail

# Claude Code SessionStart hook — tails the session JSONL into Hippo via a tmux window.
#
# Claude Code pipes JSON to stdin with these fields:
#   { "session_id": "...", "transcript_path": "/path/to/session.jsonl", ... }
#
# Install in ~/.claude/settings.json:
#   {
#     "hooks": {
#       "SessionStart": [{
#         "hooks": [{
#           "type": "command",
#           "command": "/path/to/hippo/shell/claude-session-hook.sh"
#         }]
#       }]
#     }
#   }

LOG_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/hippo"
DEBUG_LOG="$LOG_DIR/session-hook-debug.log"

# Ensure log directory exists; make logging best-effort so the hook
# still runs if the directory can't be created.
mkdir -p "$LOG_DIR" 2>/dev/null || true

log() {
    echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" >> "$DEBUG_LOG" 2>/dev/null || true
}

# Read hook JSON from stdin
INPUT=$(cat)
log "hook invoked, input=${INPUT}"

# Extract transcript_path and cwd from the JSON (two invocations to avoid eval)
TRANSCRIPT_PATH=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null)
HOOK_CWD=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('cwd',''))" 2>/dev/null)

if [ -z "$TRANSCRIPT_PATH" ]; then
    log "no transcript_path in input, exiting"
    exit 0
fi

log "transcript_path=$TRANSCRIPT_PATH"

# Resolve hippo binary — prefer release build, then debug, then PATH
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HIPPO_BIN="${SCRIPT_DIR}/../target/release/hippo"
if [ ! -x "$HIPPO_BIN" ]; then
    HIPPO_BIN="${SCRIPT_DIR}/../target/debug/hippo"
fi
if [ ! -x "$HIPPO_BIN" ]; then
    HIPPO_BIN="$(command -v hippo 2>/dev/null || true)"
fi
if [ -z "$HIPPO_BIN" ]; then
    log "hippo binary not found, exiting"
    exit 0
fi

log "hippo_bin=$HIPPO_BIN"

# Determine capture mode from config.
# Falls back to "tmux-tailer" if hippo can't be queried (safe default).
CAPTURE_MODE=$("$HIPPO_BIN" capture-mode 2>/dev/null || echo "tmux-tailer")
log "capture_mode=$CAPTURE_MODE"

# In watcher-only mode, skip tmux spawn — the FS watcher handles ingestion.
if [ "$CAPTURE_MODE" = "watcher" ]; then
    log "watcher mode: skipping tmux spawn"
    exit 0
fi

# Resolve Claude's PID. The hook runs as a direct child of Claude Code,
# so $PPID is the Claude process PID.
CLAUDE_PID="$PPID"

# Derive the window name: 🦛 + project directory name + short session ID.
# The session ID suffix disambiguates multiple sessions in the same project.
PROJECT_NAME="$(basename "${HOOK_CWD:-unknown}")"
SESSION_NAME="$(basename "$TRANSCRIPT_PATH" .jsonl)"
SHORT_ID="${SESSION_NAME:0:6}"
WINDOW_NAME="🦛 ${PROJECT_NAME}·${SHORT_ID}"

# Detect the tmux session Claude is running in so we create the window there.
TMUX_TARGET_SESSION=""
if [ -n "${TMUX_PANE:-}" ]; then
    TMUX_TARGET_SESSION=$(tmux display-message -t "$TMUX_PANE" -p '#{session_name}' 2>/dev/null || true)
fi

log "claude_pid=$CLAUDE_PID window_name=$WINDOW_NAME target_session=$TMUX_TARGET_SESSION"

# Build the tmux command with properly quoted paths (handles spaces/metacharacters).
# --wait-for-file 30: Claude fires the hook before creating the JSONL file, so the
# Rust binary polls for up to 30s inside the tmux window (never blocks this hook).
TMUX_CMD="HIPPO_WATCH_PID=${CLAUDE_PID} $(printf '%q' "$HIPPO_BIN") ingest claude-session --inline --wait-for-file 30 $(printf '%q' "$TRANSCRIPT_PATH")"

# Spawn the tailer in a detached tmux window inside Claude's own tmux session.
# tmux new-window -d returns immediately — the tail loop runs inside the new window,
# so this hook never blocks Claude Code from launching.
if [ -n "$TMUX_TARGET_SESSION" ]; then
    # Target Claude's tmux session. Trailing colon (`session:`) is the key: per
    # tmux(1), "An empty window name specifies the next unused index if
    # appropriate (for example the new-window ... commands)". A bare
    # `-t session_name` would be parsed as a target-window and collide with
    # the base-index (e.g. `index 1 in use` when base-index=1 and window 1
    # exists), which is the regression fixed here. Omitting `-t` entirely is
    # also wrong: this hook is a child of Claude Code, not of tmux, so $TMUX
    # may not be set and tmux would have no current session to target.
    tmux new-window -d -t "${TMUX_TARGET_SESSION}:" -n "$WINDOW_NAME" "$TMUX_CMD"
    log "spawned tmux window in session=$TMUX_TARGET_SESSION"
elif tmux list-sessions &>/dev/null; then
    # Not inside tmux but a tmux server is running — reuse the hippo session
    # if it already exists (from a prior fallback spawn), otherwise create it.
    if tmux has-session -t hippo 2>/dev/null; then
        tmux new-window -d -t "hippo:" -n "$WINDOW_NAME" "$TMUX_CMD"
        log "spawned fallback tmux window in existing session=hippo"
    else
        tmux new-session -d -s hippo -n "$WINDOW_NAME" "$TMUX_CMD"
        log "created fallback tmux session=hippo with tailer window"
    fi
else
    # No tmux server — wait for the file then batch-import in the background.
    ("$HIPPO_BIN" ingest claude-session --batch --wait-for-file 30 "$TRANSCRIPT_PATH" &>/dev/null &)
    log "no tmux server, batch-import (background, wait-for-file 30)"
fi
