#!/usr/bin/env bash
# Register the xcode-proxy HTTP endpoint with codex. Idempotent: skips if
# an `xcode-proxy` entry already exists in codex's MCP registry.
set -euo pipefail

if ! command -v codex >/dev/null 2>&1; then
  echo "setup-codex-mcp: codex CLI not on PATH" >&2
  exit 1
fi

if codex mcp list 2>/dev/null | grep -q "^xcode-proxy\b"; then
  exit 0
fi

codex mcp add --url http://localhost:9876/mcp xcode-proxy
