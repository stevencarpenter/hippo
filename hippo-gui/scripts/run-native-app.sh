#!/usr/bin/env bash
set -euo pipefail

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
