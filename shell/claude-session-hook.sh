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

# Read hook JSON from stdin
INPUT=$(cat)

# Extract transcript_path from the JSON
TRANSCRIPT_PATH=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null)

if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
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
    exit 0
fi

# Derive a short window name from the session file
SESSION_NAME="$(basename "$TRANSCRIPT_PATH" .jsonl)"
SHORT_ID="${SESSION_NAME:0:8}"
WINDOW_NAME="hippo:${SHORT_ID}"

# Spawn the tailer in a detached tmux window.
# tmux new-window -d returns immediately — the tail loop runs inside the new window,
# so this hook never blocks Claude Code from launching.
if tmux list-sessions &>/dev/null; then
    tmux new-window -d -n "$WINDOW_NAME" "$HIPPO_BIN ingest claude-session --inline $TRANSCRIPT_PATH"
else
    # No tmux server — batch-import what's already in the file and exit.
    # Use setsid to fully detach from the hook's process group.
    setsid "$HIPPO_BIN" ingest claude-session --batch "$TRANSCRIPT_PATH" &>/dev/null &
fi
