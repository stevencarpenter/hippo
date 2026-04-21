#!/usr/bin/env bash
# Build (if needed) and open the HippoGUI native app bundle.
set -euo pipefail

usage() {
    cat <<'EOF'
run-native-app — build and open HippoGUI

USAGE:
    run-native-app.sh [debug|release]
    run-native-app.sh --help

ARGUMENTS:
    debug|release   Build configuration (default: debug)
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
esac

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIGURATION="${1:-debug}"
BUILD_SCRIPT="${ROOT_DIR}/scripts/build-native-app.sh"

APP_OUTPUT="$(${BUILD_SCRIPT} "${CONFIGURATION}" | awk -F': ' '/Created native app bundle at/ {print $2}')"

if [ -z "${APP_OUTPUT}" ] || [ ! -d "${APP_OUTPUT}" ]; then
  echo "Failed to locate built app bundle." >&2
  exit 1
fi

open "${APP_OUTPUT}"
echo "Opened ${APP_OUTPUT}"
