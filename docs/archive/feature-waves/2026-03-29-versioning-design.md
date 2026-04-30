# Versioning Strategy for Hippo

## Context

Hippo is a local knowledge capture daemon with two processes (Rust daemon + Python brain) that share a SQLite database. There is currently no way to determine what version of the code is running locally — `hippo --version` shows the static `0.1.0` from `Cargo.toml`, there are no git tags, no build metadata is embedded, and the Python brain has no version reporting at all. This makes it difficult to correlate behavior with code state, especially when testing snapshot builds.

## Decisions

- **Semver tags + dev snapshots**: Tag releases (`v0.2.0`), dev builds auto-label with git metadata
- **Single project-wide version**: Cargo.toml workspace version is canonical; Python derives from it
- **Doctor version-match check**: `hippo doctor` verifies daemon and brain report the same version

## Version String Format

```
0.2.0                              # exactly at v0.2.0 tag
0.2.0-dev.3+g63ea88d               # 3 commits after v0.2.0, clean working tree
0.2.0-dev.3+g63ea88d.dirty         # 3 commits after v0.2.0, dirty working tree
```

The `g` prefix on the hash follows `git describe` convention (indicates a git SHA).

## Canonical Version Source

- **Primary**: `Cargo.toml` workspace `version` field (currently line 6: `version = "0.1.0"`)
- **Python**: `brain/pyproject.toml` `version` field — updated manually alongside Cargo.toml when bumping
- **Runtime version strings**: Generated at build time by combining the base semver with git metadata

## Rust: Custom `build.rs`

**File**: `crates/hippo-daemon/build.rs` (~30 lines, zero new dependencies)

The build script:
1. Reads `CARGO_PKG_VERSION` (set by Cargo from workspace version)
2. Runs `git describe --tags --always --dirty` to get tag-relative info
3. Parses the output to determine: commits since last tag, short SHA, dirty state
4. Composes the full version string:
   - At a tag: uses base version as-is (`0.2.0`)
   - After a tag: `{base}-dev.{commits}+g{sha}` or `{base}-dev.{commits}+g{sha}.dirty`
   - No tags exist: `{base}-dev+g{sha}[.dirty]` (bootstrapping case)
5. Emits `cargo:rustc-env=HIPPO_VERSION_FULL=<composed string>`
6. Emits `cargo:rerun-if-changed` for `.git/HEAD` and `.git/refs/` to trigger rebuilds on git state changes

**CLI change** in `crates/hippo-daemon/src/cli.rs` (line 4):
```rust
#[command(name = "hippo", version = env!("HIPPO_VERSION_FULL"), about = "Local knowledge capture daemon")]
```

## Python: mise Stamp Task

**New task**: `stamp:version` in `mise.toml`

A ~10-line bash script that:
1. Reads the base version from `Cargo.toml` (grep + cut, no extra tooling)
2. Runs the same `git describe --tags --always --dirty` logic
3. Composes the identical version string format as the Rust build.rs
4. Writes `brain/src/hippo_brain/_version.py`:
   ```python
   __version__ = "0.2.0-dev.3+g63ea88d"
   ```

**Wiring**:
- `_version.py` is added to `.gitignore` (generated artifact)
- The stamp task runs as a dependency of `build:brain` and within the `install` task (before step 4, after step 3)
- A committed `brain/src/hippo_brain/version.py` module exposes `get_version()`:
  - Tries `from hippo_brain._version import __version__`
  - Falls back to `importlib.metadata.version("hippo-brain")` if `_version.py` is missing (e.g. running without a build)

## Brain `/health` Version Field

**File**: `brain/src/hippo_brain/server.py`, `health()` method (line 80)

Add `"version"` to the existing health JSON response:
```python
return JSONResponse({
    "status": ...,
    "version": get_version(),
    # ... existing fields unchanged ...
})
```

Existing health tests need updating to account for the new field.

## Doctor Version Check

**File**: `crates/hippo-daemon/src/commands.rs`, `print_brain_health_details()` (line 132)

After the existing brain health JSON is parsed, extract the `"version"` field and compare to `env!("HIPPO_VERSION_FULL")`:

```
[OK] Daemon version: 0.2.0-dev.3+g63ea88d
[OK] Brain version match
```
or:
```
[OK] Daemon version: 0.2.0-dev.3+g63ea88d
[!!] Brain version mismatch: brain=0.2.0-dev.2+g1111111, daemon=0.2.0-dev.3+g63ea88d
```

If the brain is unreachable, the version check is skipped (the existing reachability failure already covers that case).

## Install Flow Changes

The `mise run install` task (`mise.toml` line 189) gains one new step between the current steps 3 and 4:

```
# ── 3b. Stamp brain version ───────────────────────────────────────
echo "==> Stamping brain version..."
mise run stamp:version
```

## Initial Baseline Tag

Create `git tag v0.1.0` on the current HEAD to seed `git describe`. Without this, the first dev builds would use the no-tag fallback format.

## Version Bumping Workflow

For now, manual:
1. Edit `Cargo.toml` workspace version (line 6)
2. Edit `brain/pyproject.toml` version (line 3)
3. Commit: `chore: bump version to 0.2.0`
4. Tag: `git tag v0.2.0`

A `mise run bump` task could automate this later, but two lines in two files is fine for a personal tool.

## Files to Create or Modify

| File | Action | Purpose |
|------|--------|---------|
| `crates/hippo-daemon/build.rs` | Create | Rust version embedding via `cargo:rustc-env` |
| `crates/hippo-daemon/src/cli.rs` | Modify line 4 | Wire `HIPPO_VERSION_FULL` into clap |
| `mise.toml` | Add `stamp:version` task, update `install` | Python version stamping |
| `brain/src/hippo_brain/_version.py` | Generated | Stamped version (gitignored) |
| `brain/src/hippo_brain/version.py` | Create | `get_version()` with fallback |
| `brain/src/hippo_brain/server.py` | Modify `health()` | Add version to `/health` response |
| `brain/tests/test_server.py` | Modify | Update health tests for version field |
| `crates/hippo-daemon/src/commands.rs` | Modify `print_brain_health_details()` | Version comparison in doctor |
| `.gitignore` | Modify | Add `brain/src/hippo_brain/_version.py` |

## Verification

1. `cargo build` — confirm `hippo --version` shows version with git metadata
2. `mise run stamp:version` — confirm `_version.py` is written with matching version string
3. `hippo doctor` — confirm version line appears and matches between daemon and brain
4. `cargo test` + `uv run --project brain pytest` — all tests pass
5. Tag `v0.1.0`, make a commit, rebuild — confirm version string changes to `0.1.0-dev.1+g<new-sha>`
6. Dirty the working tree, rebuild — confirm `.dirty` suffix appears
