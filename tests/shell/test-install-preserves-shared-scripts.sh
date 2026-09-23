#!/usr/bin/env bash
# Run: bash tests/shell/test-install-preserves-shared-scripts.sh
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

mkdir -p "$tmp/home/.local/share/scripts" "$tmp/release/brain/scripts" "$tmp/release/brain/shell"
printf 'unrelated\n' > "$tmp/home/.local/share/scripts/other-tool.py"
printf 'legacy\n' > "$tmp/home/.local/share/scripts/hippo-legacy.py"
printf 'new\n' > "$tmp/release/brain/scripts/hippo-new.py"
printf 'hook\n' > "$tmp/release/brain/shell/hippo.zsh"
tar -czf "$tmp/hippo-brain-1.2.3.tar.gz" -C "$tmp/release" brain
shasum -a 256 "$tmp/hippo-brain-1.2.3.tar.gz" | sed "s|$tmp/||" > "$tmp/SHA256SUMS.txt"

HOME="$tmp/home" bash -c '
    set -euo pipefail
    repo_root="$1"
    release_dir="$2"
    set --
    source <(sed "\$d" "$repo_root/scripts/install.sh")
    download_file() { :; }
    uv() { :; }
    verify_brain_imports() { :; }
    install_brain v1.2.3 "$release_dir/SHA256SUMS.txt" "$release_dir"
    test -f "$HOME/.local/share/scripts/other-tool.py"
    test -f "$HOME/.local/share/scripts/hippo-legacy.py"
    test -f "$HOME/.local/share/hippo-brain/scripts/hippo-new.py"
' _ "$repo_root" "$tmp"

echo 'PASS: release install preserves shared scripts and installs brain'
