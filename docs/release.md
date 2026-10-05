# Release Process

Hippo publishes one daemon binary and one brain package per stable `vX.Y.Z`
tag. Publication requires matching package versions and successful Rust,
Python, and installer checks on the tagged commit. Manual candidate runs
build the same artifacts without creating or updating a GitHub Release.

These checks establish the release's build and regression-test status.
The [v1.0 readiness assessment](research/2026-09-24-v1-readiness.md),
[current acceptance contract](../config/agent-benefit-v2.json), and
[current acceptance status](research/2026-10-04-v1-acceptance-status.md)
define the separate empirical evidence requirements. The
[October 1 evaluation](research/2026-10-01-mcp-context-evaluation.md)
records a successful selected known-history task. It does not establish a
population effect or satisfy the acceptance gate.

## Preparing a release

1. Set `[workspace.package].version` in `Cargo.toml` and `[project].version`
   in `brain/pyproject.toml` to the same `X.Y.Z`. Run `mise run build:all`
   and include the refreshed `Cargo.lock` and `brain/uv.lock` in the change.

2. Validate the intended tag locally, substituting the selected version:

   ```bash
   mise run release:check-version vX.Y.Z
   mise run test
   ```

   The version check requires a stable tag with no leading zeroes, prerelease
   suffix, or build metadata. It compares both manifests and each internal
   package entry in the lockfiles. Package versions and [schema compatibility](schema.md)
   are separate requirements; a matching package version does not validate
   a schema migration.

3. Merge the reviewed change to `main`, then tag the merged commit:

   ```bash
   git switch main
   git pull --ff-only
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

   Tagging the merged commit keeps the release source consistent with `main`
   after a squash or rebase merge.

## Publication gates

The [release workflow](../.github/workflows/release.yml) runs these jobs:

| Job | Required result |
| --- | --- |
| `validate-version` | Tag, Rust/Python manifests, and internal lockfile versions match. |
| `rust` | Reused Rust CI passes formatting, clippy, tests, and dependency audit. Linux and macOS test default features and the release's `--no-default-features` configuration. |
| `python` | Reused Python CI audits runtime and build dependencies separately, validates the committed lockfile, and passes lint, formatting, and tests with the configured coverage threshold on Linux and macOS. |
| `installer` | Reused installer CI passes isolated upgrade, rollback, and shell-path tests, including daemon selection from `PATH`. |
| `build-daemon`, `build-brain` | Both artifacts build and upload successfully. |

The reusable workflows resolve from the same commit as the release workflow.
Calls retain the caller's event, so branch and path filters cannot skip the
release checks. Tag pushes and manual candidates both run Linux and macOS
tests, including both Rust feature configurations. Each called workflow has
a distinct concurrency group.

External workflow actions are pinned to verified commits. Python build and
test jobs use uv 0.12.17. The brain build pins Hatchling and its transitive
build dependencies by version and hash in `brain/build-constraints.txt` and
`brain/pyproject.toml`. Release builds require hashes, and source installation
with `uv sync --locked --no-editable` enforces the same build constraints.
The package requires uv 0.12.17 or newer and rejects older versions before
installation. Keep both build-constraint declarations aligned when updating
the build environment.

Python advisory checks run on pull requests, release workflow calls, and a
weekly schedule. The two OSV scans fail independently for advisories or invalid
inputs and retain their JSON reports. A successful core scan does not clear
the optional dependency findings in the
[dependency disposition](research/2026-10-05-dependency-disposition.md).

Artifact builds and checks run in parallel after version validation.
`prepare-release` requires all six successful jobs, verifies checksums, and
smoke tests the packaged installation before uploading the complete bundle.
The smoke uses private HOME/XDG directories, installs the supplied artifacts,
checks configuration and reinstall preservation, and runs the installed daemon,
brain, shell capture, and offline lexical retrieval. It registers no LaunchAgents
and uses no model service. The `release` job consumes that bundle only for
tag-push events and is the only job granted `contents: write`. Manual runs
skip that job, including when dispatched against an existing tag.

The daemon artifact targets `aarch64-apple-darwin` and disables default features,
including the OTel metrics exporter. Build from source with `mise run build:release`
for the default feature set.

## Release artifacts

| Artifact | Contents |
| --- | --- |
| `hippo-darwin-arm64` | Daemon and CLI for macOS Apple Silicon. |
| `hippo-brain-X.Y.Z.tar.gz` | Brain wheel, source distribution, source files, `uv.lock`, hashed build constraints, runtime scripts, shell hooks, and Claude skills. Runtime dependencies are installed with `uv`. |
| `SHA256SUMS.txt` | SHA-256 checksums for the daemon and brain archives. |
| `install.sh` | Installer that downloads and verifies the daemon and brain artifacts. |

The installer places the daemon at `~/.local/bin/hippo`, the brain at
`~/.local/share/hippo-brain/`, and configuration under the [configured XDG roots](../README.md#data-storage).
It installs LaunchAgents through `hippo daemon install`, preserving the selected
daemon's stable path with `--binary-path`, including package-managed symlinks.
See [capture architecture](capture/architecture.md#claude-session-watcher) for
watcher reconciliation and shutdown behavior.

```bash
curl -fsSL https://github.com/stevencarpenter/hippo/releases/latest/download/install.sh | bash
```

## Checking workflow changes locally

Run the version-validator regressions and installer tests without publishing:

```bash
mise run test:python:focused brain/tests/test_release_version.py -q
mise run test:install
actionlint .github/workflows/release.yml .github/workflows/rust.yml .github/workflows/python.yml .github/workflows/installer.yml
```

A pushed tag triggers publication when all gates pass. `v0.0.0-test` is rejected
by version validation and is not a dry-run mechanism.

## Checking a hosted candidate

1. Dispatch the existing release workflow against the pushed candidate branch,
   supplying a stable tag that matches its manifests and lockfiles:

   ```bash
   npx -y gh-axi workflow run release.yml --ref CANDIDATE_BRANCH --field candidate_tag=vX.Y.Z
   ```

   The input selects the version for validation and artifact names. It does
   not create a Git tag. A branch build retains its normal development version
   metadata in the daemon binary.

2. Find the successful run and download its bundle:

   ```bash
   npx -y gh-axi run list --workflow release.yml --branch CANDIDATE_BRANCH --event workflow_dispatch
   npx -y gh-axi run download RUN_ID --name release-bundle-vX.Y.Z --dir candidate-release
   ```

3. Exercise the downloaded bundle on macOS with Python 3.14, uv 0.12.17+, and zsh:

   ```bash
   mise run release:smoke candidate-release/release-bundle.tar.gz
   ```

   The command verifies checksums before installation and prints the retained
   `results.json` path. Dependency installation may download locked wheels.
   Runtime checks use an isolated database and an unreachable inference endpoint.
   A failed smoke exits nonzero and retains its logs. The hosted run also retains
   its result and logs as `release-smoke-vX.Y.Z`.

The bundle contains `release-files/` with the four installable release assets
and `release-notes.md`. GitHub retains it for seven days. The generated notes
describe the prospective tagged release; their download URL is published only
by a successful tag-push run.
