#!/usr/bin/env bash
# Regression guard for release installs: warn when zsh config still sources a
# stale hook path instead of ~/.local/share/hippo-brain/shell/*.zsh.
#
# Run: bash tests/shell/test-install-shell-source-paths.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
INSTALL_SCRIPT="$REPO_ROOT/scripts/install.sh"

if [ ! -f "$INSTALL_SCRIPT" ]; then
    echo "FAIL: installer not found at $INSTALL_SCRIPT" >&2
    exit 1
fi

PASS=0
FAIL=0

assert() {
    local desc="$1"
    local cond="$2"
    if eval "$cond"; then
        echo "  [PASS] $desc"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $desc" >&2
        echo "         condition: $cond" >&2
        FAIL=$((FAIL + 1))
    fi
}

TMP_DIR="$(mktemp -d -t hippo-install-shell.XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

INSTALL_HELPERS="$TMP_DIR/install-no-main.sh"
grep -v '^main "\$@"$' "$INSTALL_SCRIPT" >"$INSTALL_HELPERS"

run_path_check() {
    local test_home="$1"
    env HOME="$test_home" INSTALL_HELPERS="$INSTALL_HELPERS" bash -lc '
        set -euo pipefail
        set --
        . "$INSTALL_HELPERS"
        warn_on_stale_shell_hook_sources
    '
}

echo "Test 1: stale legacy source path is warned about"
HOME1="$TMP_DIR/home-1"
mkdir -p "$HOME1/.config/zsh/profile.d"
cat >"$HOME1/.config/zsh/profile.d/personal-shell-functions.zsh" <<EOF
source $HOME1/.local/share/shell/hippo-env.zsh
source $HOME1/.local/share/shell/hippo.zsh
EOF
output1="$(run_path_check "$HOME1")"

assert "warns on legacy hippo-env.zsh path" \
    "printf '%s\n' \"\$output1\" | grep -q 'legacy path: $HOME1/.local/share/shell/hippo-env.zsh'"
assert "warns on legacy hippo.zsh path" \
    "printf '%s\n' \"\$output1\" | grep -q 'legacy path: $HOME1/.local/share/shell/hippo.zsh'"
assert "prints expected release-install hook path" \
    "printf '%s\n' \"\$output1\" | grep -q \"source $HOME1/.local/share/hippo-brain/shell/hippo-env.zsh\""

echo
echo "Test 2: missing non-legacy source path is warned about"
HOME2="$TMP_DIR/home-2"
mkdir -p "$HOME2/.config/zsh/profile.d"
cat >"$HOME2/.config/zsh/profile.d/personal-shell-functions.zsh" <<'EOF'
source /does/not/exist/hippo-env.zsh
EOF
output2="$(run_path_check "$HOME2")"

assert "warns on missing hook file" \
    "printf '%s\n' \"\$output2\" | grep -q 'sources missing file: /does/not/exist/hippo-env.zsh'"

echo
echo "Test 3: correct release-install path is quiet"
HOME3="$TMP_DIR/home-3"
mkdir -p "$HOME3/.config/zsh/profile.d" "$HOME3/.local/share/hippo-brain/shell"
printf '# hippo-env.zsh\n' >"$HOME3/.local/share/hippo-brain/shell/hippo-env.zsh"
printf '# hippo.zsh\n' >"$HOME3/.local/share/hippo-brain/shell/hippo.zsh"
cat >"$HOME3/.config/zsh/profile.d/personal-shell-functions.zsh" <<EOF
source $HOME3/.local/share/hippo-brain/shell/hippo-env.zsh
source $HOME3/.local/share/hippo-brain/shell/hippo.zsh
EOF
output3="$(run_path_check "$HOME3")"

assert "does not warn for correct release-install path" \
    "[ -z \"\$output3\" ]"

echo
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -ne 0 ]; then
    exit 1
fi
