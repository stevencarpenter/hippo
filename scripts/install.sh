#!/usr/bin/env bash
# Hippo installer - downloads and installs all Hippo components.

set -euo pipefail

usage() {
    cat <<'EOF'
Hippo installer

Downloads and installs the daemon and brain from the latest GitHub
release. Re-running is safe: components whose installed checksum matches the
release's SHA256SUMS.txt are skipped.

USAGE:
    curl -fsSL https://github.com/stevencarpenter/hippo/releases/latest/download/install.sh | bash
    ./install.sh [--help]

OPTIONS:
    -h, --help    Show this help and exit

ENVIRONMENT:
    HIPPO_FORCE   When set to 1, true, yes (case-insensitive), force reinstall
                  of every component even if the receipt matches. Useful for
                  recovering from a partial install or a corrupted binary.
                  Example: HIPPO_FORCE=1 ./install.sh

    HIPPO_INSTALL_SKILLS
                  When set to 0, false, no (case-insensitive), skip installing
                  the bundled Claude Code skills. Skills are installed by
                  default; a skill already managed elsewhere (a symlink, or a
                  copy you have edited) is never overwritten regardless.
                  Example: HIPPO_INSTALL_SKILLS=0 ./install.sh

    HIPPO_INSTALL_DAEMON
                  When set to 0, false, no (case-insensitive), skip installing
                  the `hippo` binary. For hosts that get the daemon from
                  somewhere else (a nix flake, a package manager) while still
                  wanting this script to install the Python brain. The binary
                  must be on PATH before the brain and LaunchAgents are set up.
                  Example: HIPPO_INSTALL_DAEMON=0 ./install.sh

INSTALL LOCATIONS:
    ~/.local/bin/hippo                          daemon binary
    ~/.local/share/hippo-brain/                 brain package (includes scripts/,
                                                shell/ and claude-skill/)
    ~/.claude/skills/                           Claude Code skills (copies)
    ~/.local/state/hippo/install-receipts/      per-component install receipts
                                                (respects XDG_STATE_HOME)
    ~/.config/hippo/                            config
    ~/.local/share/hippo/                       runtime data (SQLite, logs)

REQUIREMENTS:
    macOS only. bash, curl, uv (Python package manager), python3.
EOF
}

for arg in "$@"; do
    case "${arg}" in
        -h|--help)
            usage
            exit 0
            ;;
        *)
            printf "Unknown argument: %s\n\n" "${arg}" >&2
            usage >&2
            exit 2
            ;;
    esac
done

# Configuration
REPO="stevencarpenter/hippo"
INSTALL_DIR="${HOME}/.local"
BIN_DIR="${INSTALL_DIR}/bin"
BRAIN_DIR="${INSTALL_DIR}/share/hippo-brain"
CONFIG_DIR="${HOME}/.config/hippo"
DATA_DIR="${HOME}/.local/share/hippo"
# Receipts live under XDG_STATE_HOME (not DATA_DIR) so a user wipe of Hippo's
# runtime data doesn't desynchronize them from the actual installed binaries.
RECEIPTS_DIR="${XDG_STATE_HOME:-${HOME}/.local/state}/hippo/install-receipts"

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
        tag="$(curl -fsSL "${url}" | grep '"tag_name":' | LC_ALL=C sed -E 's/.*"([^"]+)".*/\1/')"
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

# Check whether a component is already installed at the expected checksum.
# True when the user asked for a forced reinstall via HIPPO_FORCE.
force_requested() {
    case "${HIPPO_FORCE:-}" in
        1|true|TRUE|True|yes|YES|Yes) return 0 ;;
    esac
    return 1
}

# Returns 0 (skip) if the receipt matches and the install target exists,
# 1 otherwise. Set HIPPO_FORCE=1 (or true/yes) to bypass and always reinstall.
check_receipt() {
    local component="$1"
    local expected_checksum="$2"
    local target_path="$3"
    local receipt="${RECEIPTS_DIR}/${component}.sha256"

    force_requested && return 1

    if [ ! -f "${receipt}" ] || [ ! -e "${target_path}" ]; then
        return 1
    fi

    local stored
    stored="$(cat "${receipt}" 2>/dev/null || true)"
    [ "${stored}" = "${expected_checksum}" ]
}

# Record the artifact checksum that produced the current install of a component.
# Writes atomically via a same-directory temp file + rename, so a crash mid-write
# can never leave a truncated receipt. On any failure after mktemp, the orphan
# temp file is cleaned up.
write_receipt() {
    local component="$1"
    local checksum="$2"
    local receipt="${RECEIPTS_DIR}/${component}.sha256"
    local tmp

    mkdir -p "${RECEIPTS_DIR}"
    tmp="$(mktemp "${RECEIPTS_DIR}/.${component}.sha256.XXXXXX")"
    if ! printf "%s\n" "${checksum}" > "${tmp}" || ! mv "${tmp}" "${receipt}"; then
        rm -f "${tmp}"
        log_error "Failed to write receipt for ${component}"
        exit 1
    fi
}

# Install daemon binary
install_daemon() {
    local arch="$1"

    case "${HIPPO_INSTALL_DAEMON:-1}" in
        0|false|FALSE|False|no|NO|No)
            if ! command -v hippo >/dev/null 2>&1 && [ ! -x "${BIN_DIR}/hippo" ]; then
                log_error "HIPPO_INSTALL_DAEMON=0 but no hippo binary is on PATH."
                log_error "Install the daemon first, or unset the variable."
                exit 1
            fi
            log_info "Skipping daemon install (HIPPO_INSTALL_DAEMON=${HIPPO_INSTALL_DAEMON})"
            return 0
            ;;
    esac
    local tag="$2"
    local checksums_file="$3"
    local temp_dir="$4"

    local daemon_filename="hippo-darwin-${arch}"
    local expected_checksum
    expected_checksum="$(parse_checksum "${checksums_file}" "${daemon_filename}")"

    if check_receipt "daemon" "${expected_checksum}" "${BIN_DIR}/hippo"; then
        log_info "Daemon already at ${tag}, skipping"
        return 0
    fi

    local daemon_path="${temp_dir}/${daemon_filename}"
    download_file "${tag}" "${daemon_filename}" "${daemon_path}"
    verify_checksum "${daemon_path}" "${expected_checksum}"

    log_info "Installing daemon to ${BIN_DIR}/hippo..."
    mkdir -p "${BIN_DIR}"
    install -m 755 "${daemon_path}" "${BIN_DIR}/hippo"

    write_receipt "daemon" "${expected_checksum}"
    log_success "Daemon installed"
}

# Probe the deployed brain venv for the imports the brain process needs at
# startup. Returns 0 on success, 1 on any import failure. Catches the
# half-installed-namespace bug (dist-info present, package contents empty)
# that surfaces only at brain-startup time as a generic ImportError.
verify_brain_imports() {
    local brain_dir="$1"
    # Importing `create_app` exercises the full startup import graph
    # (starlette, uvicorn, httpx, sqlite_vec, opentelemetry, psutil, plus the
    # hippo_brain.* internal modules). A strict superset of probing the
    # third-party packages individually — protects against the same shape of
    # bug surfacing in any startup-path dep, not just opentelemetry.
    local probe='
import sys
try:
    from hippo_brain.server import create_app  # noqa: F401
    sys.exit(0)
except Exception as exc:
    # Print to stdout: any CI capture that records only stdout still gets
    # the diagnostic, and the non-zero exit already signals failure.
    print(f"brain import probe failed: {exc!r}")
    sys.exit(1)
'
    (cd "${brain_dir}" && uv run --no-sync python -c "${probe}")
}

# Install brain package
install_brain() {
    local tag="$1"
    local checksums_file="$2"
    local temp_dir="$3"

    # Extract version from tag (remove 'v' prefix)
    local version="${tag#v}"
    local brain_filename="hippo-brain-${version}.tar.gz"
    local expected_checksum
    expected_checksum="$(parse_checksum "${checksums_file}" "${brain_filename}")"

    if check_receipt "brain" "${expected_checksum}" "${BRAIN_DIR}"; then
        log_info "Brain already at ${tag}, skipping"
        return 0
    fi

    local brain_path="${temp_dir}/${brain_filename}"
    download_file "${tag}" "${brain_filename}" "${brain_path}"
    verify_checksum "${brain_path}" "${expected_checksum}"

    log_info "Installing brain to ${BRAIN_DIR}..."
    local share_dir
    share_dir="$(dirname "${BRAIN_DIR}")"
    mkdir -p "${share_dir}"

    # Stage the new install alongside BRAIN_DIR (same filesystem) so the final
    # swap is a single rename. The brain tarball has one top-level entry
    # `brain/` which contains the python package plus a `scripts/` subdir
    # consumed by LaunchAgents. Clears any staging path left behind by a
    # prior aborted run.
    local brain_staging="${BRAIN_DIR}.new"
    rm -rf "${brain_staging}"
    mkdir -p "${brain_staging}"
    tar -xzf "${brain_path}" -C "${brain_staging}" --strip-components=1

    # Both scripts/ and shell/ are required: scripts/ is consumed by the
    # xcode-ingest LaunchAgents, shell/ is sourced from the user's zsh
    # config per the "Next steps" hint below. A tarball missing either
    # would install silently and surface as a runtime failure later.
    for required in scripts shell; do
        if [ ! -d "${brain_staging}/${required}" ]; then
            log_error "Brain tarball is missing expected ${required}/ subdir"
            rm -rf "${brain_staging}"
            exit 1
        fi
    done

    rm -rf "${BRAIN_DIR}"
    mv "${brain_staging}" "${BRAIN_DIR}"

    # Eagerly build the venv at install time and verify imports. Without this
    # step `uv run` lazy-creates the venv on first launchd start, which has
    # in the wild left a half-installed namespace (dist-info present but the
    # package contents missing). The brain then runs alongside a "telemetry
    # disabled" warning and every Grafana panel fed by brain metrics goes
    # dark. Recovered by `uv sync --reinstall`; we now do that proactively.
    if command -v uv >/dev/null 2>&1; then
        log_info "Syncing brain Python dependencies..."
        if ! (cd "${BRAIN_DIR}" && uv sync 2>&1); then
            log_error "uv sync failed in ${BRAIN_DIR}"
            exit 1
        fi

        log_info "Verifying brain imports..."
        if ! verify_brain_imports "${BRAIN_DIR}"; then
            log_warning "Brain imports failed after sync; retrying with --reinstall..."
            if ! (cd "${BRAIN_DIR}" && uv sync --reinstall 2>&1); then
                log_error "uv sync --reinstall failed in ${BRAIN_DIR}"
                exit 1
            fi
            if ! verify_brain_imports "${BRAIN_DIR}"; then
                log_error "Brain venv at ${BRAIN_DIR} is unusable even after --reinstall."
                log_error "Manual recovery: rm -rf '${BRAIN_DIR}/.venv' && cd '${BRAIN_DIR}' && uv sync"
                exit 1
            fi
        fi
        log_success "Brain dependencies verified"
    else
        log_warning "uv not found; skipping eager brain venv build (will lazy-init on first launch)"
    fi

    write_receipt "brain" "${expected_checksum}"
    log_success "Brain installed"

    # One-time cleanup: prior releases installed scripts at ~/.local/share/scripts/
    # (a generic path). The new layout places them under ${BRAIN_DIR}/scripts.
    # Remove the orphaned legacy tree so it doesn't linger on disk.
    local legacy_scripts="${share_dir}/scripts"
    # Only nuke the legacy path if it looks hippo-owned. Match ANY hippo-*.py
    # rather than a single pinned filename so a future rename doesn't leave
    # orphaned trees behind.
    if [ -d "${legacy_scripts}" ] && [ -n "$(find "${legacy_scripts}" -maxdepth 1 -name 'hippo-*.py' -print -quit 2>/dev/null)" ]; then
        log_info "Removing legacy scripts directory at ${legacy_scripts}..."
        rm -rf "${legacy_scripts}"
    fi
}

# Hash a directory's contents (paths + bytes), so an unchanged skill is a
# no-op on re-run and a user's local edit is detectable.
checksum_dir() {
    local dir="$1"
    ( cd "${dir}" && find . -type f -print0 \
        | LC_ALL=C sort -z \
        | xargs -0 shasum -a 256 \
        | shasum -a 256 \
        | cut -d' ' -f1 )
}

# Install the Claude Code skills shipped in the brain tarball.
#
# Unlike the `install:skill` mise task, which symlinks a source checkout, a
# release install has no checkout to point at — so these are copied. Each skill
# gets its own receipt keyed on the source content hash, which makes re-runs
# no-ops and lets an upgrade tell "unchanged", "new version available", and
# "the user edited this" apart.
#
# Set HIPPO_INSTALL_SKILLS=0 to skip entirely.
install_skills() {
    local src_root="${BRAIN_DIR}/claude-skill"
    local dest_root="${HOME}/.claude/skills"

    case "${HIPPO_INSTALL_SKILLS:-1}" in
        0|false|FALSE|False|no|NO|No)
            log_info "Skipping Claude Code skills (HIPPO_INSTALL_SKILLS=${HIPPO_INSTALL_SKILLS})"
            return 0
            ;;
    esac

    if [ ! -d "${src_root}" ]; then
        log_info "No Claude Code skills in this release; skipping"
        return 0
    fi

    log_info "Installing Claude Code skills..."
    mkdir -p "${dest_root}"

    local installed=0 skipped=0 src name dst checksum stored current
    for src in "${src_root}"/*/; do
        [ -d "${src}" ] || continue
        src="${src%/}"
        name="$(basename "${src}")"
        dst="${dest_root}/${name}"
        checksum="$(checksum_dir "${src}")"

        if check_receipt "skill-${name}" "${checksum}" "${dst}"; then
            skipped=$((skipped + 1))
            continue
        fi

        if [ -L "${dst}" ]; then
            # A symlink is someone else's management (a dotfiles repo, or the
            # mise task pointing at a checkout). Never replace it.
            log_warning "  ${name}: symlinked elsewhere, leaving alone"
            skipped=$((skipped + 1))
            continue
        fi

        if [ -d "${dst}" ]; then
            stored="$(cat "${RECEIPTS_DIR}/skill-${name}.sha256" 2>/dev/null || true)"
            if [ -z "${stored}" ]; then
                log_warning "  ${name}: already present and not installed by hippo, leaving alone"
                skipped=$((skipped + 1))
                continue
            fi
            current="$(checksum_dir "${dst}")"
            if [ "${current}" != "${stored}" ] && ! force_requested; then
                log_warning "  ${name}: locally modified, leaving alone (HIPPO_FORCE=1 to overwrite)"
                skipped=$((skipped + 1))
                continue
            fi
            rm -rf "${dst}"
        fi

        cp -R "${src}" "${dst}"
        write_receipt "skill-${name}" "${checksum}"
        log_info "  ${name}: installed"
        installed=$((installed + 1))
    done

    log_success "Claude Code skills: ${installed} installed, ${skipped} unchanged or skipped"
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
        # daemon install only restarts services that were already running (upgrade path).
        # For fresh installs the plists are written but services aren't bootstrapped yet.
        # daemon start is idempotent: skips services that are already loaded.
        "${BIN_DIR}/hippo" daemon start || true
    fi

    log_success "Services installed"
}

# Run end-to-end health check so a fresh install fails loud rather than
# leaving the user to discover broken state later via `hippo doctor`.
# launchd has just bootstrapped the plists; brain in particular can take
# 10–30s to import torch and bind its HTTP port, so we poll until the
# daemon's socket is live before firing the full doctor run.
verify_installation() {
    log_info "Verifying installation (this can take ~30s on a cold start)..."

    if [ ! -x "${BIN_DIR}/hippo" ]; then
        log_warning "Skipping verification — daemon binary not executable at ${BIN_DIR}/hippo"
        return 0
    fi

    # Brain port: prefer config.toml's [brain] port, fall back to the
    # daemon default (9175). A simple awk over the [brain] section beats
    # taking a TOML parser dependency for one integer.
    local brain_port=9175
    if [ -f "${CONFIG_DIR}/config.toml" ]; then
        local parsed_port
        parsed_port="$(awk '
            /^\[brain\]/ { in_section = 1; next }
            /^\[/        { in_section = 0 }
            in_section && /^[[:space:]]*port[[:space:]]*=/ {
                gsub(/[^0-9]/, "", $0); print; exit
            }
        ' "${CONFIG_DIR}/config.toml" 2>/dev/null || true)"
        if [[ "${parsed_port}" =~ ^[0-9]+$ ]]; then
            brain_port="${parsed_port}"
        fi
    fi

    # Track elapsed wall-clock seconds via $SECONDS, not iteration count:
    # a single loop tick can take >1s (curl --max-time 2 + sleep 1 + hippo
    # status latency), so an iteration-based budget would silently extend
    # the timeout by 3x. `hippo status` resolves the socket path via
    # config.socket_path() internally — including the long-path /tmp
    # fallback and any [storage].data_dir override — so we rely on its
    # exit code rather than probing a hardcoded socket path.
    local started_at=${SECONDS}
    local max_wait=60
    local daemon_up=0
    local brain_up=0
    while [ $((SECONDS - started_at)) -lt "${max_wait}" ]; do
        if [ "${daemon_up}" -eq 0 ] \
            && "${BIN_DIR}/hippo" status >/dev/null 2>&1; then
            daemon_up=1
        fi
        if [ "${brain_up}" -eq 0 ] \
            && curl -fsS --max-time 2 "http://127.0.0.1:${brain_port}/health" \
                >/dev/null 2>&1; then
            brain_up=1
        fi
        if [ "${daemon_up}" -eq 1 ] && [ "${brain_up}" -eq 1 ]; then
            break
        fi
        sleep 1
    done

    if [ "${daemon_up}" -ne 1 ]; then
        log_warning "Daemon did not respond within ${max_wait}s; running doctor anyway"
    fi
    if [ "${brain_up}" -ne 1 ]; then
        log_warning "Brain HTTP did not respond on port ${brain_port} within ${max_wait}s; running doctor anyway"
    fi

    echo ""
    if "${BIN_DIR}/hippo" doctor; then
        log_success "Hippo doctor: all checks passed"
        return 0
    fi

    echo ""
    log_warning "Hippo doctor reported issues. Common causes:"
    log_info "  - Brain still warming up: rerun 'hippo doctor' in 30s"
    log_info "  - LM Studio not running or model not loaded"
    log_info "  - Firefox extension not loaded (about:debugging → Load Temporary Add-on)"
    log_info "  - Stale shell config: review the warnings above, then 'exec zsh'"
    log_info "Re-run with 'hippo doctor --explain' for CAUSE/FIX/DOC details on each failure."
    # Return success so a partial-but-recoverable install doesn't abort
    # the script before the "Next steps" hint is printed.
    return 0
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

# Warn if the user's zsh config still points at stale release-install paths.
warn_on_stale_shell_hook_sources() {
    local expected_env="${BRAIN_DIR}/shell/hippo-env.zsh"
    local expected_hook="${BRAIN_DIR}/shell/hippo.zsh"
    local stale_found=0
    local config_path line source_path resolved_path

    # Expand the common shell path tokens ($HOME, ${HOME}, leading ~) that
    # zsh would have expanded at source-time. Without this, a `source
    # "$HOME/..."` line gets compared against the literal string `$HOME/...`
    # and falsely reported as missing. Bail (treat as resolved-but-unknown)
    # if any other unresolved $VAR remains, since we can't safely eval
    # arbitrary content from a user's shell config.
    #
    # The `$HOME` form needs token-boundary care: a naive global substring
    # replace would also chew through `$HOME_DIR` etc., destroying the `$`
    # the unresolved-var skip relies on. `${HOME}` is fine to substring-
    # replace because `}` is itself the token terminator.
    expand_shell_path() {
        local p="$1"
        p="${p//\$\{HOME\}/${HOME}}"
        p="${p//\$HOME\//${HOME}/}"
        # $HOME at end-of-string only — we don't replace $HOME followed
        # by an identifier char (e.g. $HOMEDIR). Glob is single-quoted so
        # `$` is a literal here, not a parameter expansion.
        case "${p}" in
            *'$HOME') p="${p%\$HOME}${HOME}" ;;
        esac
        case "${p}" in
            '~') p="${HOME}" ;;
            '~/'*)
                # Bash doesn't strip `~/` reliably via ${p#~/}; the case
                # arm guarantees the first two chars are `~/`, so substring
                # offset is unambiguous.
                p="${HOME}/${p:2}"
                ;;
        esac
        printf '%s' "${p}"
    }

    scan_shell_source_file() {
        local config_path="$1"
        [ -f "${config_path}" ] || return 0

        while IFS= read -r line; do
            source_path="$(printf '%s\n' "${line}" | LC_ALL=C sed -nE "s/.*source[[:space:]]+['\"]?([^'\"[:space:]]*hippo(-env)?\\.zsh)['\"]?.*/\\1/p")"
            [ -n "${source_path}" ] || continue
            resolved_path="$(expand_shell_path "${source_path}")"

            case "${resolved_path}" in
                "${expected_env}"|"${expected_hook}")
                    continue
                    ;;
                "${INSTALL_DIR}/share/shell/"*)
                    stale_found=1
                    log_warning "Shell config ${config_path} still uses legacy path: ${source_path}"
                    ;;
                *'$'*)
                    # Unresolved env var we don't know how to expand — skip
                    # the existence check rather than emit a false-positive.
                    continue
                    ;;
                *)
                    if [ ! -e "${resolved_path}" ]; then
                        stale_found=1
                        log_warning "Shell config ${config_path} sources missing file: ${source_path}"
                    fi
                    ;;
            esac
        done < "${config_path}"
    }

    scan_shell_source_file "${HOME}/.zshenv"
    scan_shell_source_file "${HOME}/.zshrc"

    if [ -d "${HOME}/.config/zsh" ]; then
        while IFS= read -r config_path; do
            scan_shell_source_file "${config_path}"
        done < <(find "${HOME}/.config/zsh" -type f 2>/dev/null)
    fi

    if [ "${stale_found}" -ne 0 ]; then
        log_info "Release installs source shell hooks from:"
        log_info "  source ${expected_env}"
        log_info "  source ${expected_hook}"
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

    install_skills
    echo ""

    warn_on_stale_shell_hook_sources
    echo ""

    # Setup
    setup_config
    echo ""

    install_services
    echo ""

    check_dependencies
    echo ""

    verify_installation
    echo ""

    # Success message
    log_success "Hippo installation complete!"
    echo ""
    log_info "Next steps:"
    log_info "  1. Add shell hooks to your zsh config:"
    log_info "       echo 'source ${BRAIN_DIR}/shell/hippo-env.zsh' >> ~/.zshenv"
    log_info "       echo 'source ${BRAIN_DIR}/shell/hippo.zsh' >> ~/.zshrc"
    log_info "       exec zsh  # reload shell"
    log_info "  2. Configure your LM Studio model: hippo config edit"
    log_info "  3. Verify installation: hippo doctor"
    echo ""
    log_info "For full documentation, visit: https://github.com/${REPO}"
}

main "$@"
