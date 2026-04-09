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

TMUX_SESSION="hippo"
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

# Extract transcript_path from the JSON
TRANSCRIPT_PATH=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null)

if [ -z "$TRANSCRIPT_PATH" ]; then
    log "no transcript_path in input, exiting"
    exit 0
fi

log "transcript_path=$TRANSCRIPT_PATH"

# Wait briefly for the transcript file to be created (Claude fires the hook before writing it)
for i in 1 2 3 4 5; do
    [ -f "$TRANSCRIPT_PATH" ] && break
    sleep 0.2
done

if [ ! -f "$TRANSCRIPT_PATH" ]; then
    log "transcript file not found after waiting, exiting"
    exit 0
fi

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

# Derive a short window name from the session file
SESSION_NAME="$(basename "$TRANSCRIPT_PATH" .jsonl)"
SHORT_ID="${SESSION_NAME:0:8}"
WINDOW_NAME="hippo:${SHORT_ID}"

# Resolve Claude's PID. The hook runs as a direct child of Claude Code,
# so $PPID is the Claude process PID.
CLAUDE_PID="$PPID"
log "claude_pid=$CLAUDE_PID window_name=$WINDOW_NAME"

# Build the tmux command with properly quoted paths (handles spaces/metacharacters).
TMUX_CMD="HIPPO_WATCH_PID=${CLAUDE_PID} $(printf '%q' "$HIPPO_BIN") ingest claude-session --inline $(printf '%q' "$TRANSCRIPT_PATH")"

# Spawn the tailer in a detached tmux window.
# tmux new-window -d returns immediately — the tail loop runs inside the new window,
# so this hook never blocks Claude Code from launching.
if tmux has-session -t "$TMUX_SESSION" 2>/dev/null; then
    tmux new-window -d -t "$TMUX_SESSION" -n "$WINDOW_NAME" "$TMUX_CMD"
    log "spawned tmux window in session=$TMUX_SESSION"
elif tmux list-sessions &>/dev/null; then
    # hippo session doesn't exist but tmux is running — create it
    tmux new-session -d -s "$TMUX_SESSION" -n "$WINDOW_NAME" "$TMUX_CMD"
    log "created tmux session=$TMUX_SESSION with tailer window"
else
    # No tmux server — batch-import what's already in the file and exit.
    # Use setsid to fully detach from the hook's process group.
    ("$HIPPO_BIN" ingest claude-session --batch "$TRANSCRIPT_PATH" &>/dev/null &)
    log "no tmux server, batch-imported"
fi
