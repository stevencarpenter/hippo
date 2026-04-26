#!/usr/bin/env bash
set -euo pipefail
# Claude Code SessionStart hook — no-op since T-8 (2026-04-25).
#
# Ingestion is handled by the FS watcher (com.hippo.claude-session-watcher).
# This hook is kept only so doctor's `check_session_hook_log` can verify hook
# activity and so existing ~/.claude/settings.json entries don't 404.
#
# Manual recovery if the watcher is wedged:
#   hippo ingest claude-session <path>
LOG_DIR="${XDG_DATA_HOME:-${HOME:-/tmp}/.local/share}/hippo"
mkdir -p "$LOG_DIR" 2>/dev/null || true
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) hook invoked" >> "$LOG_DIR/session-hook-debug.log" 2>/dev/null || true
exit 0
