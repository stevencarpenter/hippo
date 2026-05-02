# Contributing to Hippo

Hippo is a multi-language project (Rust + Python + Swift + TypeScript) with a strong opinion on capture reliability. This guide walks new contributors from "I have a clean checkout" to "I just shipped a PR" in order. The goal is for the first contribution to land with no surprises.

For the codebase's architectural shape, see the [README](README.md) and [`docs/capture/architecture.md`](docs/capture/architecture.md) first. This document is workflow-shaped — what to do, in what order, when contributing.

## The shape of the project

| Component | Language | Owns |
|---|---|---|
| `hippo-daemon` | Rust (workspace, edition 2024) | Capture from shell + Native Messaging + FSEvents. Stores events to SQLite. Runs the watchdog and probe LaunchAgents. Serves CLI queries over a Unix socket. |
| `hippo-core` | Rust (library) | Shared types, config schemas, the redaction engine, the SQLite migration runner. |
| `hippo-brain` | Python 3.14+ (uv-managed) | Polls per-source enrichment queues. Calls LM Studio. Writes knowledge nodes + entities. Embeds via sqlite-vec. Serves the brain HTTP server (`127.0.0.1:9175`) and the `hippo-mcp` MCP server. |
| `hippo-gui` | Swift (Xcode project + `HippoGUIKit` package) | Native macOS app for browsing knowledge. |
| `extension/firefox` | TypeScript (web-ext) | Firefox WebExtension that captures allowlisted browsing through native messaging. |
| `shell/` | zsh | The `preexec`/`precmd` hooks that send shell events to the daemon. |
| `crates/hippo-core/src/schema.sql` | SQL | The canonical schema for fresh installs. The migration runner in `storage.rs::open_db` keeps existing DBs in sync. |

The daemon and brain communicate **only through SQLite** (WAL mode, busy_timeout=5000). They never RPC each other. They share the schema version constant — see [`docs/schema.md`](docs/schema.md) for the version-handshake contract.

## Setup

```bash
# 1. Clone
git clone https://github.com/stevencarpenter/hippo.git
cd hippo

# 2. Install build prerequisites
brew install rustup-init mise glow swiftlint swift-format
rustup-init -y                # then `source ~/.cargo/env`
mise install                  # installs the rest of the tool chain via mise.toml

# 3. Build + test everything
mise run build                # cargo build (workspace)
mise run build:brain          # uv sync --project brain
mise run test                 # full suite: Rust + Python + lint + format check
```

The `mise run test` target is the most reliable single command. It runs the per-crate Rust suites (`hippo-core`, `hippo-daemon` lib + integration), the Python suite via `uv run --project brain pytest brain/tests --cov`, Swift package tests + lint via the `gui:test` / `gui:lint` task dependencies, `cargo clippy --all-targets -- -D warnings`, `cargo fmt --check`, and `ruff check` / `ruff format --check` against the brain.

It is **not** byte-for-byte CI: `mise run test` does **not** add the `--locked` / `--no-fail-fast` flags that CI passes to `cargo test`, does **not** run `cargo audit`, and does **not** build the Firefox extension. Treat it as the high-leverage local smoke test, not a guarantee. See [CI behavior](#ci-behavior) below for the differences.

## Test strategy

Tests are organized by layer. Cite the level you should add to when fixing a bug or adding a feature.

| Layer | Where | What it covers |
|---|---|---|
| Rust unit | `crates/<crate>/src/**/*.rs` `#[cfg(test)] mod tests` | Pure-function tests on `hippo-core` (redaction, config parsing, types) and `hippo-daemon` (envelope construction, native-messaging frame parsing). Fast (< 1 s). Run with `cargo test --lib`. |
| Rust integration | `crates/hippo-daemon/tests/*.rs` | End-to-end-style tests against a real SQLite DB. Source audit, capture invariants, NM round-trip, doctor checks, schema handshake. Run with `cargo test --test <name>` for one file or just `cargo test` for all. |
| Python unit | `brain/tests/test_*.py` | Pure-function tests on parsing, prompt construction, retrieval result shaping. Fast. |
| Python integration | `brain/tests/test_*.py` (the ones with `tmp_db` fixture) | End-to-end against a real SQLite DB seeded with the live schema. Enrichment writers, dedup script, RAG retrieval. |
| Swift | `hippo-gui/HippoGUIKitTests/` | Swift package tests via `mise run gui:test` or `swift test` from `hippo-gui/`. |
| Shell | `tests/shell/*.sh` | Shell-hook integration tests. Limited (the watcher pattern replaced most tmux-era tests). |
| Semgrep | `.semgrep.yml` + `tests/semgrep/*.rs` fixture | Static-analysis rules currently scoped to the Rust capture paths plus `brain/src/hippo_brain/` (run locally with `semgrep --config .semgrep.yml crates/ brain/`). Semgrep is **not** wired into CI today; it's a local-only / code-review check. |

When fixing a bug, add a regression test at the same layer the bug lived. For a Rust silent-error bug, the regression goes in `crates/hippo-daemon/tests/`; for a brain prompt regression, in `brain/tests/`. The [test matrix](docs/capture/test-matrix.md) maps every known capture-side failure mode to a test — your bug probably has a row there.

## The dev loop

Fast feedback recipes for common changes.

| You changed… | Run |
|---|---|
| A Rust file in `hippo-core` | `cargo test -p hippo-core --lib` (subsecond) |
| A Rust file in `hippo-daemon` | `cargo test -p hippo-daemon --test <name>` for one integration test, or `cargo test -p hippo-daemon` for all |
| A Python file in `brain` | `uv run --project brain pytest brain/tests/test_<name>.py -v` |
| Just one Python test function | `uv run --project brain pytest brain/tests/test_x.py::test_y -v` |
| A redaction pattern | `hippo redact test "your candidate string"` (after `mise run restart` if you also changed the engine) |
| The SQLite schema | Bump `EXPECTED_VERSION` in `storage.rs` AND `EXPECTED_SCHEMA_VERSION` in `brain/src/hippo_brain/schema_version.py` in the same PR. See [`docs/schema.md`](docs/schema.md) for the migration playbook. |
| Anything user-facing | `mise run install` (rebuild + reinstall services), then `hippo doctor` to verify |

Pre-push: `mise run test` once. If that's green, CI will be too.

## Cross-language coupling

Most changes live entirely in one language. The exceptions:

| Cross-cut | What's required | Why |
|---|---|---|
| Schema migration | Bump both `EXPECTED_VERSION` (Rust) and `EXPECTED_SCHEMA_VERSION` (Python) in the same PR | Daemon refuses to bind if they disagree. |
| New entity type (extending `entities.type` CHECK) | Schema migration + brain enrichment prompt update + RAG entity-surfacing list update | The brain emits the type in `entities` field; the CHECK constraint enforces it. |
| New event source kind | Migration + brain `is_enrichment_eligible` + brain `_enrich_<source>_batches` + watchdog invariant + doctor check | See [`docs/capture/adding-a-source.md`](docs/capture/adding-a-source.md). |
| Adding a column to `events` | Migration + Rust write path + Python read path | Both daemon and brain query `events`. |
| New MCP tool | Brain only (`brain/src/hippo_brain/mcp.py`); daemon doesn't know about MCP | Update [`docs/mcp-reference.md`](docs/mcp-reference.md). |
| New config key | `config/config.default.toml` + Rust `HippoConfig` (in `hippo-core/src/config.rs`) + brain settings loader (`brain/src/hippo_brain/__init__.py::_load_runtime_settings`) | Both sides read from `~/.config/hippo/config.toml`. |
| Release / version bump | `Cargo.toml`, `brain/pyproject.toml`, `hippo-gui/VERSION` — all three move together | Lockstep. See [`docs/release.md`](docs/release.md). |

## CI behavior

`.github/workflows/` runs on every PR (file names are the live filenames in this repo):

- **python.yml** — `ruff` + `pytest` on the brain when `brain/` changes.
- **rust.yml** — `cargo build --all-targets --locked`, `cargo clippy --all-targets --locked --no-deps -- -D warnings`, `cargo test --locked --no-fail-fast`, and `cargo audit --file Cargo.lock` when Rust paths change.
- **gui.yml** — Swift package tests when `hippo-gui/` changes.
- **extension.yml** — Firefox extension build / checks when `extension/firefox/` changes.
- **security.yml** — repository-level security checks (note: this workflow does **not** currently run Semgrep; the `.semgrep.yml` rules are local-only).
- **release.yml** — fires on `v*.*.*` tags. See [`docs/release.md`](docs/release.md).

Reproducing CI locally: `mise run test` covers the core checks most contributors need before opening a PR, but it is not byte-for-byte CI. In particular, CI runs `cargo test` / `cargo clippy` / `cargo build` with `--locked` (and `cargo test` with `--no-fail-fast`), and runs `cargo audit` plus the extension and security workflows on top. To match CI on the Rust side run `cargo test --locked --no-fail-fast` and `cargo clippy --all-targets --locked --no-deps -- -D warnings` directly.

If a test fails on CI but passes locally, the most common causes are:

- Cargo lockfile drift — run `cargo build --locked` locally to surface it.
- Brain Python dependency drift — `uv sync --project brain --frozen`.
- A test reading from `~/.local/share/hippo/` (probably contaminating between runs). Set `XDG_DATA_HOME` to a tmpdir for the test.

## Formatting and linting

This repository does not currently ship a checked-in [pre-commit](https://pre-commit.com) configuration. Run the relevant tooling directly for the files you changed before pushing:

- Python (anything under `brain/`): `uv run --project brain ruff format brain/src brain/tests` and `uv run --project brain ruff check brain/`.
- Rust: `cargo fmt` and `cargo clippy --all-targets -- -D warnings`.
- TOML: confirm edited files parse (any TOML-aware editor or `python -c "import tomllib; tomllib.load(open('FILE','rb'))"`).
- Markdown / shell / generic text: strip trailing whitespace.

If CI fails on formatting or linting, fix the issue locally and rerun the relevant tool before pushing review updates. (If a future PR adds a `.pre-commit-config.yaml` to this repo, this section will replace these direct commands with `pre-commit install` + the hook list — but as of v0.20 there is no such config to install.)

## Code review expectations

| Path | Read this before touching |
|---|---|
| `crates/hippo-daemon/src/{daemon,commands,storage,native_messaging,watch_claude_sessions}.rs` | [`docs/capture/anti-patterns.md`](docs/capture/anti-patterns.md) — AP-1..AP-12 are review blockers |
| Anything in `crates/hippo-core/src/redaction.rs` | [`docs/redaction.md`](docs/redaction.md) |
| `brain/src/hippo_brain/{server,enrichment,claude_sessions,browser_enrichment}.py` | [`docs/capture/anti-patterns.md`](docs/capture/anti-patterns.md) (especially AP-2, AP-6, AP-11) |
| Schema migrations in `crates/hippo-core/src/storage.rs` | [`docs/schema.md`](docs/schema.md) — the migration playbook |
| MCP tool definitions in `brain/src/hippo_brain/mcp.py` | [`docs/mcp-reference.md`](docs/mcp-reference.md) — keep the doc in sync |

The ground rules:

- **Probe events are filtered out of every user-facing query.** Never write a query against `events`/`browser_events`/`claude_sessions` without `AND probe_tag IS NULL`. A Semgrep rule exists for this in `.semgrep.yml`; run `semgrep --config .semgrep.yml crates/ brain/` locally before pushing capture-layer changes (Semgrep is not enforced in CI today). (AP-6.)
- **Capture and enrichment are decoupled.** `source_health` tracks capture health only; the brain's HTTP `/health` tracks enrichment health. Never couple them. (AP-2.)
- **No silent error swallowing.** `.filter_map(Result::ok)` and `.ok().unwrap_or_default()` in any capture write path are PR-blockers. Errors get a `warn!` log and a counter bump. (AP-11.)
- **Schema migrations are idempotent.** Every CREATE has `IF NOT EXISTS`; every ALTER goes through `add_column_if_missing`; every seed is `INSERT OR IGNORE`. A daemon that crashes mid-migration must complete cleanly on restart.

The full review-blocker list is [`docs/capture/anti-patterns.md`](docs/capture/anti-patterns.md). It's twelve items with rationale and the right alternative; read it before your first capture-layer PR.

## PR conventions

**Title format.** Conventional Commits: `<type>(<scope>): <subject>`.

- `fix(brain): tolerate raw control chars in LLM JSON output`
- `feat(daemon): add browser-yield capture path`
- `chore(release): bump version to 0.20.1 across all relevant files`
- `docs(capture): add adding-a-source guide`

Types in use: `fix`, `feat`, `chore`, `docs`, `test`, `refactor`. Scopes in use: `daemon`, `brain`, `gui`, `core`, `release`, `capture`, plus per-component scopes like `(re-enrich)` and `(mcp-reference)` when appropriate.

**Body.** What changed and why, in that order. Cite issue numbers (`Closes #N`) when applicable. Test plan as a checklist:

```markdown
## Test plan

- [x] `cargo test --workspace --locked --no-fail-fast`
- [x] `uv run --project brain pytest brain/tests`
- [x] `mise run lint`
- [ ] Operator: run `mise run install --clean` post-merge to pick up the new binaries
```

**One change per PR.** A migration + a feature + a refactor in one PR is too much. Split.

**No --no-verify.** Even though there's no checked-in pre-commit config today, any local hook a contributor or maintainer adds (or future `.pre-commit-config.yaml`) is in service of CI. If something complains, fix what it found rather than bypassing.

**Co-Authored-By trailers** are welcome. `git commit -s` for sign-off if you prefer.

## Releasing

Maintainer-only. Contributors don't bump versions in their PRs — the release PR is a separate `chore(release)` PR per [`docs/release.md`](docs/release.md). Your feature PR can ride into whichever release the maintainer cuts next.

## Where to ask

- **Bugs**: [GitHub Issues](https://github.com/stevencarpenter/hippo/issues). Fill in the doctor output and reproduction steps.
- **Feature requests**: same; label `enhancement`.
- **Questions**: GitHub Discussions if enabled, otherwise an issue with `question` label.
- **Security**: do not file as a public issue. The repo has a security-scanning push protection that will catch obvious cases; for vulnerabilities that bypass it, use [GitHub's private security advisory flow](https://github.com/stevencarpenter/hippo/security/advisories/new) (a checked-in `SECURITY.md` is open follow-up work).

## See also

- [README](README.md) — project overview and install
- [`CLAUDE.md`](CLAUDE.md) — code-style notes for AI-assisted contribution
- [`docs/lifecycle.md`](docs/lifecycle.md) — end-to-end event trace
- [`docs/schema.md`](docs/schema.md) — schema changelog and migration playbook
- [`docs/mcp-reference.md`](docs/mcp-reference.md) — MCP tool reference
- [`docs/redaction.md`](docs/redaction.md) — redaction reference
- [`docs/capture/`](docs/capture/) — capture-reliability stack docs
- [`docs/release.md`](docs/release.md) — release process
