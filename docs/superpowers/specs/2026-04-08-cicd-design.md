# CI/CD Design for Hippo Open Source

**Date:** 2026-04-08
**Status:** Implemented
**Goal:** Comprehensive CI/CD to validate all PRs and ensure build quality for the open-source hippo project.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Artifact strategy | Quality gates only, no release binaries | Distribution story TBD; focus on correctness first |
| Python version in CI | `uv python install 3.14` | Python 3.14 is stable; uv manages its own builds reliably |
| Firefox extension | Include typecheck + build job | Cheap gate, catches TS errors before merge |
| Claude Code Action workflows | Keep on `ubuntu-latest` | API-bound, not compute-bound |
| OTel feature flag | Skip in CI | Dev-only feature for local performance validation |
| Dependency scanning | Rust only (`cargo audit`); Python deferred | `pip-audit` broken on Python 3.14 macOS (ensurepip SIGABRT); Dependabot covers Python CVEs |
| Runner platform | GitHub-hosted: `ubuntu-latest` + `macos-latest` matrix | Public repo = free runners; macOS validates the native daemon platform |
| Architecture | Component-split workflows with path filters | Only runs relevant checks per PR |

## Workflow Overview

6 workflow files total. 4 new, 2 existing (unchanged).

```
.github/workflows/
  rust.yml              # NEW - Rust format, clippy, test, audit (ubuntu + macOS matrix)
  python.yml            # NEW - Python ruff, pytest (ubuntu + macOS matrix)
  extension.yml         # NEW - Firefox extension typecheck + build (ubuntu + macOS matrix)
  security.yml          # NEW - Shell/zsh secret leak scanning (ubuntu + macOS matrix)
  claude.yml            # EXISTING - @claude bot (ubuntu-latest, unchanged)
  claude-code-review.yml # EXISTING - PR auto-review (ubuntu-latest, unchanged)
```

All new workflows:
- Trigger on `pull_request` + `push` to `main`, with path filters
- Use `concurrency: group: ${{ github.workflow }}-${{ github.ref }}` with `cancel-in-progress: true`
- Use `permissions: contents: read`
- Matrix: `ubuntu-latest` + `macos-latest` (except format job, which is platform-independent)

## Workflow 1: Rust CI (`rust.yml`)

### Triggers

```yaml
pull_request:
  paths:
    - "crates/**"
    - "shell/**"
    - "Cargo.toml"
    - "Cargo.lock"
    - ".github/workflows/rust.yml"
push:
  branches: [main]
  paths: [same as above]
```

`shell/**` included because `crates/hippo-daemon/tests/shell_hook.rs` sources `shell/hippo.zsh`.

### Job: `format`

- **Runner:** `ubuntu-latest` (single, not matrixed — formatting is platform-independent)
- **Condition:** `pull_request` only (skip on main push — already validated)
- **Timeout:** 5 minutes
- **Steps:**
  1. Checkout
  2. `dtolnay/rust-toolchain@stable` with `components: rustfmt`
  3. `cargo fmt --check`

### Job: `build-and-test`

- **Runner:** `ubuntu-latest` + `macos-latest` matrix
- **Condition:** Both PR and main push
- **Timeout:** 20 minutes
- **Steps:**
  1. Checkout
  2. Install zsh (Ubuntu only — macOS has it natively)
  3. `dtolnay/rust-toolchain@stable` with `components: clippy`
  4. `Swatinem/rust-cache@v2` with per-OS shared key
  5. `cargo build --all-targets --locked`
  6. `cargo clippy --all-targets --locked --no-deps -- -D warnings`
  7. `cargo test --locked --no-fail-fast`
  8. Install `cargo-audit` via `taiki-e/install-action@cargo-audit`
  9. `cargo audit --file Cargo.lock`

### Env vars

```yaml
env:
  CARGO_TERM_COLOR: always
  CARGO_INCREMENTAL: 0
  CARGO_NET_RETRY: 10
  RUST_BACKTRACE: short
  RUSTUP_MAX_RETRIES: 10
```

## Workflow 2: Python CI (`python.yml`)

### Triggers

```yaml
pull_request:
  paths:
    - "brain/**"
    - ".github/workflows/python.yml"
push:
  branches: [main]
  paths: [same as above]
```

### Job: `lint-and-test`

- **Runner:** `ubuntu-latest` + `macos-latest` matrix
- **Timeout:** 15 minutes
- **Steps:**
  1. Checkout (with `fetch-depth: 0` for git describe in version stamp)
  2. Install uv via `astral-sh/setup-uv@v6`
  3. `uv python install 3.14`
  4. `uv sync --project brain`
  5. Stamp version (inline bash — same logic as `mise run stamp:version`)
  6. `uv run --project brain ruff check brain/`
  7. `uv run --project brain ruff format --check brain/src brain/tests`
  8. `uv run --project brain pytest brain/tests -v --cov=hippo_brain --cov-report=term-missing`

**Python dep scanning deferred:** `pip-audit` crashes on Python 3.14 macOS (ensurepip SIGABRT). Dependabot monitors Python deps via pyproject.toml. Re-enable when pip-audit adds 3.14 support.

**Test safety:** All brain tests use `MockLMStudioClient` or httpx mock transports. No tests require a live LM Studio instance.

## Workflow 3: Firefox Extension CI (`extension.yml`)

### Triggers

```yaml
pull_request:
  paths:
    - "extension/firefox/**"
    - ".github/workflows/extension.yml"
push:
  branches: [main]
  paths: [same as above]
```

### Job: `typecheck`

- **Runner:** `ubuntu-latest` + `macos-latest` matrix
- **Timeout:** 5 minutes
- **Steps:**
  1. Checkout
  2. `actions/setup-node@v4` with `node-version: 22`
  3. `npm ci` (working-directory: `extension/firefox`)
  4. `npm run build` (runs `tsc --noEmit && node build.mjs`)

## Workflow 4: Security Guardrails (`security.yml`)

### Triggers

```yaml
pull_request:
  paths:
    - "scripts/**"
    - "shell/**"
    - ".github/workflows/**"
push:
  branches: [main]
  paths: [same as above]
```

### Job: `shell-secret-leak-check`

- **Runner:** `ubuntu-latest` + `macos-latest` matrix
- **Timeout:** 5 minutes
- **Steps:**
  1. Checkout
  2. Install ripgrep (apt on Ubuntu, brew on macOS)
  3. Run `scripts/security/check-shell-secrets.sh`

The script scans `*.sh` and `*.zsh` files under `scripts/` and `shell/` for:
- Shell debug tracing (`set -x` / `xtrace`)
- Verbose curl flags (`curl -v` / `--verbose`)
- Direct printing of secret-named environment variables (with `# allow-secret-print` exemption)
- Static temp token dump patterns

## Existing Workflows (Unchanged)

### `claude.yml`
- **Runner:** `ubuntu-latest` (no change)
- @claude bot triggered by issue/PR comments containing `@claude`

### `claude-code-review.yml`
- **Runner:** `ubuntu-latest` (no change)
- Auto-review on PR open/sync/reopen

## Caching Strategy

| Component | Cache mechanism | Notes |
|-----------|----------------|-------|
| Rust | `Swatinem/rust-cache@v2` | Per-OS shared key per base branch |
| Python | uv internal cache | Fast enough without explicit caching |
| Node | None (npm ci) | 3 devDeps, sub-second install |

## Future Considerations (Not In Scope)

- **Release artifacts:** GitHub Releases with prebuilt binaries (macOS aarch64) when distribution story is decided
- **Required status checks:** Configure in repo settings after CI stability is proven
- **Coverage thresholds:** Enforce minimum coverage once baseline is established
- **Python dep audit:** Re-enable `pip-audit` when Python 3.14 macOS support lands
