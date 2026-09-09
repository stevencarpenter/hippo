# Contributing to Hippo

Build, test, and submit changes to Hippo. See [AGENTS.md](AGENTS.md) for component
ownership and coding conventions, [capture architecture](docs/capture/architecture.md)
for runtime behavior, and [the schema contract](docs/schema.md) for cross-language
version compatibility.

## Setup

```bash
# 1. Clone
git clone https://github.com/stevencarpenter/hippo.git
cd hippo

# 2. Install build prerequisites
brew install rustup-init mise glow
rustup-init -y                # then `source ~/.cargo/env`
mise install                  # installs the rest of the tool chain via mise.toml

# 3. Build + test everything
mise run build:all            # Rust build + Python dependency sync
mise run test                 # full suite: Rust + Python + lint + format check
```

`mise run test` runs Rust and Python tests, clippy, ruff, and format checks.
See [CI behavior](#ci-behavior) for the additional CI checks.

## Test strategy

Tests are organized by layer. Cite the level you should add to when fixing a bug or adding a feature.

| Layer | Where | What it covers |
|---|---|---|
| Rust unit | `crates/<crate>/src/**/*.rs` `#[cfg(test)] mod tests` | Pure-function tests on `hippo-core` (redaction, config parsing, types) and `hippo-daemon` (envelope construction, native-messaging frame parsing). Fast (< 1 s). Run with `cargo test --lib`. |
| Rust integration | `crates/hippo-daemon/tests/*.rs` | End-to-end-style tests against a real SQLite DB. Source audit, capture invariants, NM round-trip, doctor checks, schema handshake. Run with `cargo test --test <name>` for one file or just `cargo test` for all. |
| Python unit | `brain/tests/test_*.py` | Pure-function tests on parsing, prompt construction, retrieval result shaping. Fast. |
| Python integration | `brain/tests/test_*.py` (the ones with `tmp_db` fixture) | End-to-end against a real SQLite DB seeded with the live schema. Enrichment writers, dedup script, RAG retrieval. |
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

Run `mise run test` before pushing. CI also checks lockfiles, feature variants,
dependency advisories, and changed extension or shell code.

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
| Release / version bump | `Cargo.toml`, `brain/pyproject.toml` — both move together | Lockstep. See [`docs/release.md`](docs/release.md). |

## CI behavior

Workflows run when their configured paths change. Rust, Python, extension, and
security jobs run on Linux for PRs and add macOS on pushes to `main`:

- **python.yml** — `ruff` + `pytest` on the brain when `brain/` changes.
- **rust.yml**: locked builds/tests, clippy with and without default features,
  and `cargo audit`. PR builds/tests disable default features; pushes enable them.
- **extension.yml** — Firefox extension build / checks when `extension/firefox/` changes.
- **security.yml** — repository-level security checks (note: this workflow does **not** currently run Semgrep; the `.semgrep.yml` rules are local-only).
- **release.yml** — fires on `v*.*.*` tags. See [`docs/release.md`](docs/release.md).

For exact CI invocations, read [.github/workflows/rust.yml](.github/workflows/rust.yml)
and [.github/workflows/python.yml](.github/workflows/python.yml).

If a test fails on CI but passes locally, the most common causes are:

- Cargo lockfile drift — run `cargo build --locked` locally to surface it.
- Brain Python dependency drift — `uv sync --project brain --frozen`.
- A test reading from `~/.local/share/hippo/` (probably contaminating between runs). Set `XDG_DATA_HOME` to a tmpdir for the test.

## Formatting and linting

Run the relevant tasks before pushing:

- Python: `mise run fmt:python` and `mise run lint:python`.
- Rust: `mise run fmt:rust` and `mise run lint:rust`.
- TOML: confirm edited files parse (any TOML-aware editor or `python -c "import tomllib; tomllib.load(open('FILE','rb'))"`).
- Markdown / shell / generic text: strip trailing whitespace.

## Code review expectations

| Path | Read this before touching |
|---|---|
| `crates/hippo-daemon/src/{daemon,commands,storage,native_messaging,watch_claude_sessions}.rs` | [`docs/capture/anti-patterns.md`](docs/capture/anti-patterns.md) — AP-1..AP-12 are review blockers |
| Anything in `crates/hippo-core/src/redaction.rs` | [`docs/redaction.md`](docs/redaction.md) |
| `brain/src/hippo_brain/{server,enrichment,claude_sessions,browser_enrichment}.py` | [`docs/capture/anti-patterns.md`](docs/capture/anti-patterns.md) (especially AP-2, AP-6, AP-11) |
| Schema migrations in `crates/hippo-core/src/storage.rs` | [`docs/schema.md`](docs/schema.md) — the migration playbook |
| MCP tool definitions in `brain/src/hippo_brain/mcp.py` | [`docs/mcp-reference.md`](docs/mcp-reference.md) — keep the doc in sync |

The ground rules:

- **Probe events are filtered out of every user-facing query.** Never write a query against `events`/`browser_events`/`agentic_sessions` without `AND probe_tag IS NULL`. A Semgrep rule exists for this in `.semgrep.yml`; run `semgrep --config .semgrep.yml crates/ brain/` locally before pushing capture-layer changes (Semgrep is not enforced in CI today). (AP-6.)
- **Capture and enrichment are decoupled.** `source_health` tracks capture health only; the brain's HTTP `/health` tracks enrichment health. Never couple them. (AP-2.)
- **No silent error swallowing.** `.filter_map(Result::ok)` and `.ok().unwrap_or_default()` in any capture write path are PR-blockers. Errors get a `warn!` log and a counter bump. (AP-11.)
- **Schema migrations are idempotent.** Every CREATE has `IF NOT EXISTS`; every ALTER goes through `add_column_if_missing`; every seed is `INSERT OR IGNORE`. A daemon that crashes mid-migration must complete cleanly on restart.

Read the [capture anti-patterns](docs/capture/anti-patterns.md) before changing capture code.

## PR conventions

**Title format.** Conventional Commits: `<type>(<scope>): <subject>`.

- `fix(brain): tolerate raw control chars in LLM JSON output`
- `feat(daemon): add browser-yield capture path`
- `chore(release): bump version to 0.20.1 across all relevant files`
- `docs(capture): add adding-a-source guide`

Types in use: `fix`, `feat`, `chore`, `docs`, `test`, `refactor`. Scopes in use: `daemon`, `brain`, `core`, `release`, `capture`, plus per-component scopes like `(re-enrich)` and `(mcp-reference)` when appropriate.

**Body.** What changed and why, in that order. Cite issue numbers (`Closes #N`) when applicable. Test plan as a checklist:

```markdown
## Test plan

- [x] `mise run test`
- [ ] Operator: run `mise run install --clean` post-merge to pick up the new binaries
```

**One change per PR.** A migration + a feature + a refactor in one PR is too much. Split.

**No --no-verify.** Fix hook failures instead of bypassing them.

**Co-Authored-By trailers** are welcome. `git commit -s` for sign-off if you prefer.

## Releasing

Maintainer-only. Contributors don't bump versions in their PRs — the release PR is a separate `chore(release)` PR per [`docs/release.md`](docs/release.md). Your feature PR can ride into whichever release the maintainer cuts next.

## Where to ask

- **Bugs**: [GitHub Issues](https://github.com/stevencarpenter/hippo/issues). Fill in the doctor output and reproduction steps.
- **Feature requests**: same; label `enhancement`.
- **Questions**: GitHub Discussions if enabled, otherwise an issue with `question` label.
- **Security**: do not file as a public issue. The repo has a security-scanning push protection that will catch obvious cases; for vulnerabilities that bypass it, use [GitHub's private security advisory flow](https://github.com/stevencarpenter/hippo/security/advisories/new).

## See also

- [README](README.md) — project overview and install
- [`AGENTS.md`](AGENTS.md): project structure, commands, and coding conventions
- [`docs/lifecycle.md`](docs/lifecycle.md) — end-to-end event trace
- [`docs/schema.md`](docs/schema.md) — schema changelog and migration playbook
- [`docs/mcp-reference.md`](docs/mcp-reference.md) — MCP tool reference
- [`docs/observability.md`](docs/observability.md) — OTel stack, dashboards, and alerts
- [`docs/redaction.md`](docs/redaction.md) — redaction reference
- [`docs/capture/`](docs/capture/) — capture-reliability stack docs
- [`docs/release.md`](docs/release.md) — release process
