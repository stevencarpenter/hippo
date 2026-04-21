#!/usr/bin/env bash
# Build the HippoGUI .app bundle from the Swift sources via swift build +
# bundle assembly. Stamps the marketing version via stamp-app-version.sh.
set -euo pipefail

usage() {
    cat <<'EOF'
build-native-app — build HippoGUI.app bundle

USAGE:
    build-native-app.sh [debug|release]
    build-native-app.sh --help

ARGUMENTS:
    debug|release   Build configuration (default: debug)

Outputs the bundle to hippo-gui/dist/<configuration>/HippoGUI.app and
prints "Created native app bundle at: <path>" on success.
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
esac

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIGURATION="${1:-debug}"
PRODUCT_NAME="HippoGUI"
APP_NAME="${PRODUCT_NAME}.app"
DIST_DIR="${ROOT_DIR}/dist/${CONFIGURATION}"
APP_DIR="${DIST_DIR}/${APP_NAME}"
CONTENTS_DIR="${APP_DIR}/Contents"
MACOS_DIR="${CONTENTS_DIR}/MacOS"
RESOURCES_DIR="${CONTENTS_DIR}/Resources"
PLIST_TEMPLATE="${ROOT_DIR}/AppBundle/Info.plist"
ASSET_CATALOG_DIR="${ROOT_DIR}/Resources/Assets.xcassets"
STAMP_VERSION_SCRIPT="${ROOT_DIR}/scripts/stamp-app-version.sh"

case "${CONFIGURATION}" in
  debug|release)
    ;;
  *)
    echo "Usage: $0 [debug|release]" >&2
    exit 64
    ;;
esac

if [ ! -f "${PLIST_TEMPLATE}" ]; then
  echo "Missing Info.plist template at ${PLIST_TEMPLATE}" >&2
  exit 66
fi

if [ ! -x "${STAMP_VERSION_SCRIPT}" ]; then
  echo "Missing executable version stamp script at ${STAMP_VERSION_SCRIPT}" >&2
  exit 66
fi

swift build --package-path "${ROOT_DIR}" --configuration "${CONFIGURATION}" --product "${PRODUCT_NAME}"
BIN_DIR="$(swift build --package-path "${ROOT_DIR}" --configuration "${CONFIGURATION}" --show-bin-path)"
EXECUTABLE_PATH="${BIN_DIR}/${PRODUCT_NAME}"

if [ ! -x "${EXECUTABLE_PATH}" ]; then
  echo "Built executable not found at ${EXECUTABLE_PATH}" >&2
  exit 1
fi

rm -rf "${APP_DIR}"
mkdir -p "${MACOS_DIR}" "${RESOURCES_DIR}"
cp "${PLIST_TEMPLATE}" "${CONTENTS_DIR}/Info.plist"
"${STAMP_VERSION_SCRIPT}" "${CONTENTS_DIR}/Info.plist"
install -m 755 "${EXECUTABLE_PATH}" "${MACOS_DIR}/${PRODUCT_NAME}"

if [ -d "${ASSET_CATALOG_DIR}" ] && command -v xcrun >/dev/null 2>&1; then
  bundle_identifier="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "${CONTENTS_DIR}/Info.plist")"
  tmp_plist="$(mktemp)"
  xcrun actool "${ASSET_CATALOG_DIR}" \
    --compile "${RESOURCES_DIR}" \
    --output-format human-readable-text \
    --app-icon AppIcon \
    --enable-on-demand-resources NO \
    --development-region en \
    --target-device mac \
    --minimum-deployment-target 26.0 \
    --platform macosx \
    --bundle-identifier "${bundle_identifier}" \
    --output-partial-info-plist "${tmp_plist}" >/dev/null
  rm -f "${tmp_plist}"
fi

if command -v codesign >/dev/null 2>&1; then
  codesign --force --sign - "${APP_DIR}" >/dev/null 2>&1 || true
fi

echo "Created native app bundle at: ${APP_DIR}"
echo "Bundle identifier: $(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "${CONTENTS_DIR}/Info.plist")"
echo "Bundle version: $(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "${CONTENTS_DIR}/Info.plist") ($(/usr/libexec/PlistBuddy -c 'Print :CFBundleVersion' "${CONTENTS_DIR}/Info.plist"))"
