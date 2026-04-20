#!/usr/bin/env bash
# Start mcp-proxy wrapping `xcrun mcpbridge` so all agents share one
# long-lived Xcode MCP connection. The first invocation per Xcode launch
# triggers the macOS "allow" dialog once; subsequent agent sessions reuse
# the running proxy and avoid re-prompting.
#
# Idempotent: no-op if the proxy is already listening on :9876.
set -euo pipefail

PORT=9876
LOG=/tmp/xcode-mcp-proxy.log

if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  exit 0
fi

if ! command -v mcp-proxy >/dev/null 2>&1; then
  if command -v vp >/dev/null 2>&1; then
    vp install -g mcp-proxy >/dev/null
  elif command -v npm >/dev/null 2>&1; then
    npm install -g mcp-proxy >/dev/null
  else
    echo "xcode-mcp-proxy: neither vp nor npm available to install mcp-proxy" >&2
    exit 1
  fi
fi

nohup mcp-proxy --port "$PORT" -- xcrun mcpbridge >"$LOG" 2>&1 &
disown || true
