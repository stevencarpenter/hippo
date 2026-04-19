#!/usr/bin/env bash
# Hippo installer - downloads and installs all Hippo components
# Usage: curl -fsSL https://github.com/stevencarpenter/hippo/releases/latest/download/install.sh | bash

set -euo pipefail

# Configuration
REPO="stevencarpenter/hippo"
INSTALL_DIR="${HOME}/.local"
BIN_DIR="${INSTALL_DIR}/bin"
BRAIN_DIR="${INSTALL_DIR}/share/hippo-brain"
CONFIG_DIR="${HOME}/.config/hippo"
DATA_DIR="${HOME}/.local/share/hippo"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Logging functions
log_info() {
    printf "${BLUE}[INFO]${NC} %s\n" "$1"
}

log_success() {
    printf "${GREEN}[SUCCESS]${NC} %s\n" "$1"
}

log_warning() {
    printf "${YELLOW}[WARNING]${NC} %s\n" "$1"
}

log_error() {
    printf "${RED}[ERROR]${NC} %s\n" "$1" >&2
}

# Detect OS and architecture
detect_platform() {
    local os="$(uname -s)"
    local arch="$(uname -m)"

    if [ "${os}" != "Darwin" ]; then
        log_error "This installer only supports macOS. Detected: ${os}"
        exit 1
    fi

    case "${arch}" in
        x86_64)
            echo "x86_64"
            ;;
        arm64|aarch64)
            # Normalize both to 'arm64' to match Rust's aarch64-apple-darwin build artifact naming
            echo "arm64"
            ;;
        *)
            log_error "Unsupported architecture: ${arch}"
            exit 1
            ;;
    esac
}

# Get latest release tag from GitHub
get_latest_release() {
    local url="https://api.github.com/repos/${REPO}/releases/latest"
    local tag

    if command -v curl >/dev/null 2>&1; then
        tag="$(curl -fsSL "${url}" | grep '"tag_name":' | sed -E 's/.*"([^"]+)".*/\1/')"
    else
        log_error "curl is required but not installed"
        exit 1
    fi

    if [ -z "${tag}" ]; then
        log_error "Failed to fetch latest release tag"
        exit 1
    fi

    echo "${tag}"
}

# Download file from GitHub releases
download_file() {
    local tag="$1"
    local filename="$2"
    local output_path="$3"
    local url="https://github.com/${REPO}/releases/download/${tag}/${filename}"

    log_info "Downloading ${filename}..."
    if ! curl -fsSL -o "${output_path}" "${url}"; then
        log_error "Failed to download ${filename}"
        exit 1
    fi
}

# Verify checksum
verify_checksum() {
    local file="$1"
    local expected_checksum="$2"
    local actual_checksum

    actual_checksum="$(shasum -a 256 "${file}" | awk '{print $1}')"

    if [ "${actual_checksum}" != "${expected_checksum}" ]; then
        log_error "Checksum verification failed for ${file}"
        log_error "Expected: ${expected_checksum}"
        log_error "Got:      ${actual_checksum}"
        exit 1
    fi

    log_success "Checksum verified for ${file}"
}

# Parse checksums file
parse_checksum() {
    local checksums_file="$1"
    local filename="$2"
    local checksum

    # Use exact filename match; shasum -c format is "<hash>  <filename>" (two spaces)
    checksum="$(grep "^[a-f0-9][a-f0-9]*[[:space:]][[:space:]]*${filename}$" "${checksums_file}" | awk '{print $1}')"

    if [ -z "${checksum}" ]; then
        log_error "Checksum entry not found or ambiguous for: ${filename}"
        exit 1
    fi

    # Verify we got exactly one match
    local match_count
    match_count="$(grep -c "^[a-f0-9][a-f0-9]*[[:space:]][[:space:]]*${filename}$" "${checksums_file}")"
    if [ "${match_count}" -ne 1 ]; then
        log_error "Ambiguous checksum entry for ${filename} (found ${match_count} matches)"
        exit 1
    fi

    echo "${checksum}"
}

# Install daemon binary
install_daemon() {
    local arch="$1"
    local tag="$2"
    local checksums_file="$3"
    local temp_dir="$4"

    local daemon_filename="hippo-darwin-${arch}"
    local daemon_path="${temp_dir}/${daemon_filename}"

    download_file "${tag}" "${daemon_filename}" "${daemon_path}"

    local expected_checksum
    expected_checksum="$(parse_checksum "${checksums_file}" "${daemon_filename}")"
    verify_checksum "${daemon_path}" "${expected_checksum}"

    log_info "Installing daemon to ${BIN_DIR}/hippo..."
    mkdir -p "${BIN_DIR}"
    install -m 755 "${daemon_path}" "${BIN_DIR}/hippo"

    log_success "Daemon installed"
}

# Install brain package
install_brain() {
    local tag="$1"
    local checksums_file="$2"
    local temp_dir="$3"

    # Extract version from tag (remove 'v' prefix)
    local version="${tag#v}"
    local brain_filename="hippo-brain-${version}.tar.gz"
    local brain_path="${temp_dir}/${brain_filename}"

    download_file "${tag}" "${brain_filename}" "${brain_path}"

    local expected_checksum
    expected_checksum="$(parse_checksum "${checksums_file}" "${brain_filename}")"
    verify_checksum "${brain_path}" "${expected_checksum}"

    log_info "Installing brain to ${BRAIN_DIR}..."
    mkdir -p "$(dirname "${BRAIN_DIR}")"
    rm -rf "${BRAIN_DIR}"

    tar -xzf "${brain_path}" -C "$(dirname "${BRAIN_DIR}")"
    mv "$(dirname "${BRAIN_DIR}")/brain" "${BRAIN_DIR}"

    log_success "Brain installed"
}

# Install GUI application
install_gui() {
    local tag="$1"
    local checksums_file="$2"
    local temp_dir="$3"

    # Extract version from tag (remove 'v' prefix)
    local version="${tag#v}"

    # The GUI artifact name includes the build number, so we need to find it from checksums
    local gui_pattern="HippoGUI-${version}-"
    local gui_filename="$(grep "${gui_pattern}" "${checksums_file}" | awk '{print $2}' | head -n 1)"

    if [ -z "${gui_filename}" ]; then
        log_warning "GUI artifact not found in checksums file, skipping GUI installation"
        return 0
    fi

    local gui_path="${temp_dir}/${gui_filename}"

    download_file "${tag}" "${gui_filename}" "${gui_path}"

    local expected_checksum
    expected_checksum="$(parse_checksum "${checksums_file}" "${gui_filename}")"
    verify_checksum "${gui_path}" "${expected_checksum}"

    log_info "Installing HippoGUI to /Applications..."

    # Extract the .app from the zip
    unzip -q "${gui_path}" -d "${temp_dir}"

    # Find the .app bundle (BSD/macOS find does not support -maxdepth)
    local app_bundle
    app_bundle="$(find "${temp_dir}" -name "HippoGUI.app" -type d | head -n 1)"

    if [ -z "${app_bundle}" ] || [ ! -d "${app_bundle}" ]; then
        log_warning "HippoGUI.app not found in archive, skipping GUI installation"
        return 0
    fi

    # Remove existing app if present
    if [ -d "/Applications/HippoGUI.app" ]; then
        log_info "Removing existing HippoGUI.app..."
        rm -rf "/Applications/HippoGUI.app"
    fi

    # Copy to Applications
    cp -R "${app_bundle}" "/Applications/"

    log_success "HippoGUI installed to /Applications"
}

# Setup configuration
setup_config() {
    log_info "Setting up configuration..."

    mkdir -p "${CONFIG_DIR}"
    mkdir -p "${DATA_DIR}"

    if [ ! -f "${CONFIG_DIR}/config.toml" ]; then
        log_info "Creating default config.toml..."
        "${BIN_DIR}/hippo" config init 2>/dev/null || true
    fi

    log_success "Configuration setup complete"
}

# Install LaunchAgents
install_services() {
    log_info "Installing LaunchAgents..."

    if [ -x "${BIN_DIR}/hippo" ]; then
        "${BIN_DIR}/hippo" daemon install --force --brain-dir "${BRAIN_DIR}" || {
            log_warning "Failed to install LaunchAgents automatically"
            log_info "You can install them manually later with: hippo daemon install --brain-dir '${BRAIN_DIR}'"
        }
    fi

    log_success "Services installed"
}

# Check dependencies
check_dependencies() {
    local missing_deps=()

    if ! command -v uv >/dev/null 2>&1; then
        missing_deps+=("uv (Python package manager)")
    fi

    if ! command -v python3 >/dev/null 2>&1; then
        missing_deps+=("python3")
    fi

    if [ ${#missing_deps[@]} -gt 0 ]; then
        log_warning "The following dependencies are not installed:"
        for dep in "${missing_deps[@]}"; do
            log_warning "  - ${dep}"
        done
        log_info "Install uv with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    fi
}

# Main installation flow
main() {
    log_info "Hippo Installer"
    log_info "==============="
    echo ""

    # Detect platform
    local arch="$(detect_platform)"
    log_info "Detected architecture: ${arch}"

    # Get latest release
    local tag="$(get_latest_release)"
    log_info "Latest release: ${tag}"
    echo ""

    # Create temporary directory
    local temp_dir="$(mktemp -d)"
    trap "rm -rf ${temp_dir}" EXIT

    # Download checksums file
    log_info "Downloading checksums..."
    download_file "${tag}" "SHA256SUMS.txt" "${temp_dir}/SHA256SUMS.txt"
    echo ""

    # Install components
    install_daemon "${arch}" "${tag}" "${temp_dir}/SHA256SUMS.txt" "${temp_dir}"
    echo ""

    install_brain "${tag}" "${temp_dir}/SHA256SUMS.txt" "${temp_dir}"
    echo ""

    install_gui "${tag}" "${temp_dir}/SHA256SUMS.txt" "${temp_dir}"
    echo ""

    # Setup
    setup_config
    echo ""

    install_services
    echo ""

    check_dependencies
    echo ""

    # Success message
    log_success "Hippo installation complete!"
    echo ""
    log_info "Next steps:"
    log_info "  1. Add shell hooks to your zsh config (see README)"
    log_info "  2. Configure your LM Studio model: hippo config edit"
    log_info "  3. Verify installation: hippo doctor"
    log_info "  4. Start services: hippo daemon start"
    echo ""
    log_info "For full documentation, visit: https://github.com/${REPO}"
}

main "$@"
