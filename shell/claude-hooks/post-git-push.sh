#!/usr/bin/env bash
# Claude Code PostToolUse hook — registers a pushed SHA in the hippo watchlist.
# Invoked by Claude Code with JSON on stdin: { tool_name, tool_input, ... }
# Matcher (in settings.json): tool_name == "Bash" && command matches 'git push'
set -euo pipefail

input=$(cat)
cmd=$(echo "$input" | jq -r '.tool_input.command // ""')
if [[ "$cmd" != *"git push"* ]]; then
    exit 0
fi

cwd=$(echo "$input" | jq -r '.cwd // ""')
[[ -n "$cwd" ]] || exit 0
cd "$cwd" || exit 0

sha=$(git rev-parse HEAD 2>/dev/null) || exit 0
remote=$(git config --get remote.origin.url 2>/dev/null) || exit 0
repo=$(echo "$remote" | sed -E 's#(git@github\.com:|https://github\.com/)(.*)\.git#\2#' | head -1)
[[ -n "$repo" ]] || exit 0

hippo send-event watchlist --sha "$sha" --repo "$repo" --ttl 1200 >/dev/null 2>&1 || true
