# Release Process

This document describes the automated release pipeline for Hippo.

## Overview

When a version tag is pushed (format: `v*.*.*`), GitHub Actions automatically builds all components, creates a GitHub Release, and attaches installable artifacts with checksums.

## Triggering a Release

Hippo's daemon, brain, and GUI ship in lockstep: one tag → one GitHub Release
with all three artifacts. Because the daemon and brain share a SQLite schema,
their versions must move together (see CLAUDE.md for the rationale).

1. Bump the version in **both** manifests to the same `X.Y.Z`:
   ```bash
   # Rust workspace (covers hippo-core + hippo-daemon)
   vim Cargo.toml                  # [workspace.package].version

   # Python brain
   vim brain/pyproject.toml        # [project].version
   ```
   Lockfiles (`Cargo.lock`, `brain/uv.lock`) refresh on the next build.

2. Open a PR with the version bump and whatever feature work rides with the
   release. Get it reviewed and merged to `main`.

3. After the merge lands on `main`, tag `main` — **not the feature branch**:
   ```bash
   git checkout main && git pull
   git tag vX.Y.Z                  # e.g. v0.13.0
   git push origin vX.Y.Z
   ```
   Why tag `main` rather than the feature branch? The release workflow builds
   from whatever the tag points at. Squash or rebase merges rewrite the SHA,
   so a tag on the branch HEAD can point at a commit that isn't on `main` —
   the release artifacts then diverge from what's actually shipped.

4. The release workflow will automatically:
   - Build the daemon binary for macOS (aarch64)
   - Package the brain Python project
   - Build the HippoGUI macOS app
   - Create SHA256 checksums for all artifacts
   - Create a GitHub Release with all artifacts attached
   - Include the `install.sh` script for one-liner installation

## Release Artifacts

Each release includes:

| Artifact | Description | Example |
|----------|-------------|---------|
| `hippo-darwin-arm64` | Daemon binary for macOS Apple Silicon | `hippo-darwin-arm64` |
| `hippo-brain-{version}.tar.gz` | Python brain project (including uv.lock, scripts, and runtime dependencies resolved via `uv` during install) | `hippo-brain-X.Y.Z.tar.gz` |
| `HippoGUI-{version}-{build}.zip` | GUI app bundle ready for `/Applications` | `HippoGUI-X.Y.Z-N.zip` |
| `SHA256SUMS.txt` | Checksums for all artifacts | Contains SHA-256 hashes |
| `install.sh` | One-liner installation script | Downloads and verifies all components |

## Workflow Jobs

The release workflow consists of three parallel build jobs and a final release job:

### 1. `build-daemon` (macOS runner)
- Builds the Rust daemon binary for `aarch64-apple-darwin`
- Strips debug symbols to reduce size
- Generates SHA-256 checksum
- Uploads artifact for release job

### 2. `build-brain` (macOS runner)
- Builds the Python brain package using `uv`
- Creates a tarball with the wheel, source files, `uv.lock` (for reproducible installs), and runtime scripts
- Generates SHA-256 checksum
- Uploads artifact for release job

### 3. `build-gui` (macOS runner)
- Builds the GUI app using the existing `release-gui.sh` script
- Creates a versioned ZIP archive with the `.app` bundle
- Generates SHA-256 checksum
- Uploads artifact for release job

### 4. `release` (macOS runner)
- Depends on all three build jobs
- Downloads all artifacts
- Creates `SHA256SUMS.txt` with all checksums
- Generates release notes with installation instructions
- Creates GitHub Release via `gh` CLI
- Attaches all artifacts to the release

## Installation Script

The `scripts/install.sh` script provides automated installation:

```bash
curl -fsSL https://github.com/stevencarpenter/hippo/releases/latest/download/install.sh | bash
```

The script:
1. Detects macOS architecture (`uname -m`)
2. Fetches the latest release tag from GitHub API
3. Downloads `SHA256SUMS.txt` for verification
4. Downloads each component and verifies its checksum
5. Installs components to standard locations:
   - Daemon: `~/.local/bin/hippo`
   - Brain: `~/.local/share/hippo-brain/`
   - GUI: `/Applications/HippoGUI.app`
6. Sets up configuration at `~/.config/hippo/`
7. Installs LaunchAgents via `hippo daemon install`

## Testing the Release Workflow

To test the workflow without creating a real release:

1. Create a test tag locally:
   ```bash
   git tag v0.0.0-test
   ```

2. Push to a test branch first to verify workflows pass:
   ```bash
   git checkout -b test-release
   git push origin test-release
   ```

3. Only push the tag when ready:
   ```bash
   git push origin v0.0.0-test
   ```

4. Delete test releases via GitHub UI or CLI:
   ```bash
   gh release delete v0.0.0-test --yes
   git tag -d v0.0.0-test
   git push origin :refs/tags/v0.0.0-test
   ```

## Caching

The workflow uses caching to speed up builds:

- **Rust cache**: `Swatinem/rust-cache@v2` caches Cargo dependencies
- **Xcode cache**: Xcode derived data is implicitly cached by the macOS runner

## Security

- All artifacts are verified with SHA-256 checksums
- The `install.sh` script verifies checksums before installation
- No secrets or credentials are embedded in artifacts
- Code signing uses ad-hoc signing (`codesign --sign -`)

## Troubleshooting

### Build failures

- Check the workflow run logs in GitHub Actions
- Verify all dependencies are available on the runner
- Ensure version numbers are correctly formatted

### Missing artifacts

- Check that all three build jobs completed successfully
- Verify the artifact upload steps didn't fail
- Check the release job logs for download issues

### Checksum verification failures

- Ensure artifacts weren't modified after upload
- Check that the checksum generation step completed
- Verify the `SHA256SUMS.txt` format is correct

## Future Enhancements

Potential improvements to the release pipeline:

- [ ] Add x86_64 (Intel) macOS builds
- [ ] Create DMG instead of ZIP for GUI app
- [ ] Add automatic changelog generation
- [ ] Sign artifacts with Developer ID certificate
- [ ] Notarize the GUI app with Apple
- [ ] Add Linux builds for daemon and brain
- [ ] Create Homebrew formula
- [ ] Add release notes from git commits
