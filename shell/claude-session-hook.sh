#!/usr/bin/env bash
set -euo pipefail

# Claude Code SessionStart hook — auto-tails the session JSONL into Hippo.
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

# Resolve hippo binary — prefer one next to this script (dev build), then PATH
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
HIPPO_BIN="${SCRIPT_DIR}/../target/debug/hippo"
if [ ! -x "$HIPPO_BIN" ]; then
    HIPPO_BIN="${SCRIPT_DIR}/../target/release/hippo"
fi
if [ ! -x "$HIPPO_BIN" ]; then
    HIPPO_BIN="$(command -v hippo 2>/dev/null || true)"
fi
if [ -z "$HIPPO_BIN" ]; then
    exit 0
fi

# Launch the tailer. If in tmux, it spawns a new window automatically.
# The & and disown ensure the hook returns immediately so Claude Code isn't blocked.
"$HIPPO_BIN" ingest claude-session "$TRANSCRIPT_PATH" &>/dev/null &
disown
