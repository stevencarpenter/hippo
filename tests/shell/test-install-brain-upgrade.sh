#!/usr/bin/env bash
# Run: bash tests/shell/test-install-brain-upgrade.sh
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/release/brain/scripts" "$tmp/release/brain/shell"
printf 'new\n' > "$tmp/release/brain/new-version"
tar -czf "$tmp/hippo-brain-1.2.3.tar.gz" -C "$tmp/release" brain
shasum -a 256 "$tmp/hippo-brain-1.2.3.tar.gz" | sed "s|$tmp/||" > "$tmp/SHA256SUMS.txt"

for scenario in sync-fails imports-fail relocated-imports-fail receipt-fails cleanup-fails success; do
    brain_dir="$tmp/$scenario/brain"
    bin_dir="$tmp/$scenario/bin"
    receipts_dir="$tmp/$scenario/receipts"
    mkdir -p "$brain_dir" "$bin_dir" "$receipts_dir"
    printf 'working\n' > "$brain_dir/old-version"
    printf 'old-daemon\n' > "$bin_dir/hippo"
    printf 'old-checksum\n' > "$receipts_dir/brain.sha256"
    printf 'old-daemon-checksum\n' > "$receipts_dir/daemon.sha256"
    if bash -c '
        set -euo pipefail
        repo_root="$1"; release_dir="$2"; brain_dir="$3"; receipts_dir="$4"; scenario="$5"
        set --
        source <(sed "\$d" "$repo_root/scripts/install.sh")
        BRAIN_DIR="$brain_dir"
        BIN_DIR="$(dirname "$brain_dir")/bin"
        RECEIPTS_DIR="$receipts_dir"
        HIPPO_FORCE=1
        download_file() { :; }
        install_daemon() {
            printf "new-daemon\n" > "$BIN_DIR/hippo"
            printf "new-daemon-checksum\n" > "$RECEIPTS_DIR/daemon.sha256"
        }
        if [[ "$scenario" == receipt-fails ]]; then
            write_receipt() { exit 1; }
        fi
        rm() {
            if [[ "$scenario" == cleanup-fails && "${*: -1}" == *.previous ]]; then
                printf "injected backup cleanup failure\n" >&2
                return 1
            fi
            command rm "$@"
        }
        uv() { [[ "$scenario" != sync-fails ]]; }
        verify_brain_imports() {
            [[ "$scenario" != imports-fail ]] || return 1
            [[ "$scenario" != relocated-imports-fail || "$1" != "$BRAIN_DIR" ]]
        }
        install_components arm64 v1.2.3 "$release_dir/SHA256SUMS.txt" "$release_dir"
    ' _ "$repo_root" "$tmp" "$brain_dir" "$receipts_dir" "$scenario"; then
        [[ "$scenario" == success || "$scenario" == cleanup-fails ]]
        if [[ "$scenario" == cleanup-fails ]]; then
            backups=("$brain_dir".new.*.previous)
            test -f "${backups[0]}/old-version"
        fi
        test -f "$brain_dir/new-version"
        test ! -f "$brain_dir/old-version"
        [[ "$(cat "$receipts_dir/brain.sha256")" != old-checksum ]]
        [[ "$(cat "$bin_dir/hippo")" == new-daemon ]]
        [[ "$(cat "$receipts_dir/daemon.sha256")" == new-daemon-checksum ]]
    else
        [[ "$scenario" != success && "$scenario" != cleanup-fails ]]
        test -f "$brain_dir/old-version"
        test ! -f "$brain_dir/new-version"
        [[ "$(cat "$receipts_dir/brain.sha256")" == old-checksum ]]
        [[ "$(cat "$bin_dir/hippo")" == old-daemon ]]
        [[ "$(cat "$receipts_dir/daemon.sha256")" == old-daemon-checksum ]]
    fi
done

echo 'PASS: failed upgrades preserve both components and their receipts'
