#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GUI_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_DIR="$(cd "${GUI_DIR}/.." && pwd)"
PLIST_PATH="${1:-}"
CARGO_TOML="${REPO_DIR}/Cargo.toml"

usage() {
  echo "Usage: $0 /path/to/Info.plist" >&2
}

fail() {
  echo "$*" >&2
  exit 1
}

if [ -z "${PLIST_PATH}" ]; then
  usage
  exit 64
fi

if [ ! -f "${PLIST_PATH}" ]; then
  fail "Info.plist not found at ${PLIST_PATH}"
fi

if [ ! -f "${CARGO_TOML}" ]; then
  fail "Cargo.toml not found at ${CARGO_TOML}"
fi

resolve_marketing_version() {
  if [ -n "${HIPPO_MARKETING_VERSION:-}" ]; then
    printf '%s\n' "${HIPPO_MARKETING_VERSION}"
    return
  fi

  VERSION_FILE="${GUI_DIR}/VERSION"
  if [ -f "${VERSION_FILE}" ]; then
    ver="$(tr -d '[:space:]' < "${VERSION_FILE}")"
    if [ -n "${ver}" ]; then
      printf '%s\n' "${ver}"
      return
    fi
  fi

  awk '
    BEGIN { in_section = 0 }
    /^[[:space:]]*\[workspace\.package\][[:space:]]*$/ { in_section = 1; next }
    /^[[:space:]]*\[/ { if (in_section) exit }
    in_section && /^[[:space:]]*version[[:space:]]*=/ {
      if (match($0, /"[^"]+"/)) {
        print substr($0, RSTART + 1, RLENGTH - 2)
        exit
      }
    }
  ' "${CARGO_TOML}"
}

resolve_build_number() {
  if [ -n "${HIPPO_BUILD_NUMBER:-}" ]; then
    printf '%s\n' "${HIPPO_BUILD_NUMBER}"
    return
  fi

  if [ -n "${BUILD_NUMBER:-}" ]; then
    printf '%s\n' "${BUILD_NUMBER}"
    return
  fi

  if git -C "${REPO_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "${REPO_DIR}" rev-list --count HEAD
    return
  fi

  printf '1\n'
}

set_plist_key() {
  local key="$1"
  local type="$2"
  local value="$3"

  if /usr/libexec/PlistBuddy -c "Print :${key}" "${PLIST_PATH}" >/dev/null 2>&1; then
    /usr/libexec/PlistBuddy -c "Set :${key} ${value}" "${PLIST_PATH}" >/dev/null
  else
    /usr/libexec/PlistBuddy -c "Add :${key} ${type} ${value}" "${PLIST_PATH}" >/dev/null
  fi
}

MARKETING_VERSION="$(resolve_marketing_version)"
BUILD_VERSION="$(resolve_build_number)"

if [ -z "${MARKETING_VERSION}" ]; then
  fail "Failed to resolve marketing version from ${GUI_DIR}/VERSION or ${CARGO_TOML}"
fi

if [[ ! "${MARKETING_VERSION}" =~ ^[0-9]+(\.[0-9]+){1,2}$ ]]; then
  fail "Marketing version '${MARKETING_VERSION}' must be numeric dotted version like 0.11.0"
fi

if [[ ! "${BUILD_VERSION}" =~ ^[0-9]+$ ]]; then
  fail "Build number '${BUILD_VERSION}' must be numeric"
fi

set_plist_key CFBundleShortVersionString string "${MARKETING_VERSION}"
set_plist_key CFBundleVersion string "${BUILD_VERSION}"

echo "Stamped ${PLIST_PATH} with version ${MARKETING_VERSION} (${BUILD_VERSION})"