#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIGURATION="release"
BUILD_SCRIPT="${ROOT_DIR}/scripts/build-native-app.sh"
PLIST_BUDDY="/usr/libexec/PlistBuddy"
OUTPUT_MODE="human"

usage() {
    cat >&2 <<'EOF'
Usage: release-gui.sh [debug|release] [--json] [--markdown]

Options:
  debug|release  Build configuration to package (default: release)
  --json         Emit release metadata as JSON to stdout
  --markdown     Emit Markdown release notes to stdout
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        debug|release)
            CONFIGURATION="$1"
            ;;
        --json)
            OUTPUT_MODE="json"
            ;;
        --markdown)
            OUTPUT_MODE="markdown"
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            usage
            exit 64
            ;;
    esac
    shift
done

case "${CONFIGURATION}" in
  debug|release)
    ;;
  *)
    usage
    exit 64
    ;;
esac

if [ ! -x "${BUILD_SCRIPT}" ]; then
    echo "Missing executable build script at ${BUILD_SCRIPT}" >&2
    exit 66
fi

APP_OUTPUT="$(${BUILD_SCRIPT} "${CONFIGURATION}")"
if [ "${OUTPUT_MODE}" = "human" ]; then
    printf '%s\n' "${APP_OUTPUT}"
else
    printf '%s\n' "${APP_OUTPUT}" >&2
fi

APP_PATH="$(printf '%s\n' "${APP_OUTPUT}" | awk -F': ' '/Created native app bundle at/ {print $2}')"

if [ -z "${APP_PATH}" ] || [ ! -d "${APP_PATH}" ]; then
    echo "Failed to locate built app bundle." >&2
    exit 1
fi

PLIST_PATH="${APP_PATH}/Contents/Info.plist"
VERSION="$(${PLIST_BUDDY} -c 'Print :CFBundleShortVersionString' "${PLIST_PATH}")"
BUILD="$(${PLIST_BUDDY} -c 'Print :CFBundleVersion' "${PLIST_PATH}")"
IDENTIFIER="$(${PLIST_BUDDY} -c 'Print :CFBundleIdentifier' "${PLIST_PATH}")"
ARCHIVE_PATH="$(dirname "${APP_PATH}")/HippoGUI-${VERSION}-${BUILD}.zip"
CHECKSUM_PATH="${ARCHIVE_PATH}.sha256"
NOTES_PATH="${ARCHIVE_PATH%.zip}.release-notes.md"

if ! command -v ditto >/dev/null 2>&1; then
    echo "The 'ditto' tool is required to create a release archive." >&2
    exit 69
fi

rm -f "${ARCHIVE_PATH}"
ditto -c -k --sequesterRsrc --keepParent "${APP_PATH}" "${ARCHIVE_PATH}"

if ! command -v shasum >/dev/null 2>&1; then
    echo "The 'shasum' tool is required to create a SHA-256 checksum." >&2
    exit 69
fi

CHECKSUM="$(shasum -a 256 "${ARCHIVE_PATH}" | awk '{print $1}')"
printf '%s  %s\n' "${CHECKSUM}" "$(basename "${ARCHIVE_PATH}")" > "${CHECKSUM_PATH}"

VERIFY_COMMAND="cd \"$(dirname "${ARCHIVE_PATH}")\" && shasum -a 256 -c \"$(basename "${CHECKSUM_PATH}")\""

if ! (
    cd "$(dirname "${ARCHIVE_PATH}")"
    shasum -a 256 -c "$(basename "${CHECKSUM_PATH}")" >/dev/null
); then
    echo "Generated checksum verification failed for ${ARCHIVE_PATH}" >&2
    exit 70
fi

SIGNATURE="unknown"
if command -v codesign >/dev/null 2>&1; then
    SIGNATURE="$(codesign -dv --verbose=2 "${APP_PATH}" 2>&1 | awk -F= '/^Signature=/{print $2; exit}')"
fi

MARKDOWN_OUTPUT=$(cat <<EOF
# HippoGUI Release ${VERSION} (${BUILD})

- **Version:** ${VERSION}
- **Build:** ${BUILD}
- **Configuration:** ${CONFIGURATION}
- **Bundle ID:** ${IDENTIFIER}
- **Signature:** ${SIGNATURE}
- **App bundle:** \`${APP_PATH}\`
- **Archive:** \`${ARCHIVE_PATH}\`
- **Checksum file:** \`${CHECKSUM_PATH}\`
- **SHA-256:** \`${CHECKSUM}\`

## Verify archive

```bash
${VERIFY_COMMAND}
```
EOF
)

printf '%s\n' "${MARKDOWN_OUTPUT}" > "${NOTES_PATH}"

emit_json() {
    APP_PATH="${APP_PATH}" \
    ARCHIVE_PATH="${ARCHIVE_PATH}" \
    CHECKSUM_PATH="${CHECKSUM_PATH}" \
    NOTES_PATH="${NOTES_PATH}" \
    VERSION="${VERSION}" \
    BUILD="${BUILD}" \
    CONFIGURATION="${CONFIGURATION}" \
    IDENTIFIER="${IDENTIFIER}" \
    CHECKSUM="${CHECKSUM}" \
    VERIFY_COMMAND="${VERIFY_COMMAND}" \
    SIGNATURE="${SIGNATURE}" \
    python3 - <<'PY'
import json
import os

payload = {
    "app_path": os.environ["APP_PATH"],
    "archive_path": os.environ["ARCHIVE_PATH"],
    "checksum_path": os.environ["CHECKSUM_PATH"],
    "release_notes_path": os.environ["NOTES_PATH"],
    "version": os.environ["VERSION"],
    "build": os.environ["BUILD"],
    "configuration": os.environ["CONFIGURATION"],
    "bundle_id": os.environ["IDENTIFIER"],
    "sha256": os.environ["CHECKSUM"],
    "checksum_verified": True,
    "verify_command": os.environ["VERIFY_COMMAND"],
    "signature": os.environ["SIGNATURE"],
}

print(json.dumps(payload, indent=2, sort_keys=True))
PY
}

case "${OUTPUT_MODE}" in
    human)
        printf '\nRelease artifact summary\n'
        printf '  App: %s\n' "${APP_PATH}"
        printf '  Archive: %s\n' "${ARCHIVE_PATH}"
        printf '  Checksum file: %s\n' "${CHECKSUM_PATH}"
        printf '  Release notes: %s\n' "${NOTES_PATH}"
        printf '  Version: %s\n' "${VERSION}"
        printf '  Build: %s\n' "${BUILD}"
        printf '  Configuration: %s\n' "${CONFIGURATION}"
        printf '  Bundle ID: %s\n' "${IDENTIFIER}"
        printf '  SHA-256: %s\n' "${CHECKSUM}"
        printf '  Checksum verified: yes\n'
        printf '  Verify command: %s\n' "${VERIFY_COMMAND}"
        printf '  Signature: %s\n' "${SIGNATURE}"
        ;;
    markdown)
        printf '%s\n' "${MARKDOWN_OUTPUT}"
        ;;
    json)
        emit_json
        ;;
esac
