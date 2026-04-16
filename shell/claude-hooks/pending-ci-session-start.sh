#!/usr/bin/env bash
# Claude Code SessionStart hook — injects pending CI failure notices into session context.
# Invoked by Claude Code with JSON on stdin: { cwd, ... }
set -euo pipefail

input=$(cat)
cwd=$(echo "$input" | jq -r '.cwd // ""')
[[ -n "$cwd" ]] || exit 0
cd "$cwd" || exit 0

remote=$(git config --get remote.origin.url 2>/dev/null) || exit 0
repo=$(echo "$remote" | sed -E 's#(git@github\.com:|https://github\.com/)(.*)\.git#\2#' | head -1)
[[ -n "$repo" ]] || exit 0

pending=$(hippo gh-pending-notifications --repo "$repo" --ack 2>/dev/null || echo "")
if [[ -n "$pending" ]]; then
    jq -n --arg msg "$pending" '{
        hookSpecificOutput: {
            hookEventName: "SessionStart",
            additionalContext: $msg
        }
    }'
fi
