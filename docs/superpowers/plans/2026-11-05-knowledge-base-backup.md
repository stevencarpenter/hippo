# Knowledge-Base Backup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `hippo backup`: scheduled, encrypted, storage-efficient backups of hippo state to a configured folder (the operator's Google Drive), with checkpoints before mutation and a safe restore.

**Architecture:** A `backup` module in `hippo-daemon` takes a one-step SQLite online-backup snapshot, writes weekly fulls and daily page-level deltas as `tar` + zstd + `age` artifacts into a local outbox, ships them to the destination, verifies, records a manifest, and prunes by tier. A hook in `hippo-core::storage::open_db` checkpoints before schema migrations. A launchd agent runs it hourly; watchdog and doctor report health.

**Tech Stack:** Rust 2024 (`rusqlite` with the `backup` feature, new crates `age`, `zstd`, `tar`; existing `sha2`, `libc`, `clap`, `serde_json`, `chrono`), launchd, 1Password `op` CLI, Python 3.14 helper for brain scripts.

**Spec:** `docs/superpowers/specs/2026-11-05-knowledge-base-backup-design.md`

## Global Constraints

- Rust edition 2024; `cargo clippy --all-targets -- -D warnings` and `cargo fmt --check` clean; `anyhow` for errors.
- All timestamps are Unix epoch milliseconds (`i64`).
- Snapshot: SQLite online backup API, all pages in one step (step size `-1`). Never `VACUUM INTO`, never the `sqlite3` shell `.backup`.
- Snapshot post-processing: `PRAGMA journal_mode=DELETE`, then `PRAGMA quick_check` must return `ok`.
- Artifact contents: `hippo.db` (full) or `hippo.db.pagediff` (delta), `config/config.toml`, `config/redact.toml` (symlinks followed), `enrichment-state.json`, `fallback/*.jsonl`, `meta.json`.
- Excluded: OTel data, logs, `vault/`, `metrics/`, `bench/`, legacy `hippo.db.*` copies.
- Compression: zstd level 9 with long-distance matching.
- Encryption: `age` X25519. Private key lives in 1Password; never written to disk by hippo. Encryption needs only the public recipient.
- Hippo never creates the destination directory.
- Every file written to the destination uses a temporary name, then rename.
- Retention: deltas 90 days; weekly fulls 365 days; first full of each UTC calendar month forever; checkpoints 365 days; never delete a full a retained delta uses; never delete the newest verified-and-uploaded full; prune only after the current run verifies.
- Scheduled run skips if a successful backup happened within the last 20 hours.
- Watchdog alarm: last success older than 36 hours, or an artifact shipped but not uploaded for more than 24 hours. Suppressed when `[backup]` is not configured.
- Doctor warns when no full verify has passed in 35 days.
- `[backup]` not configured: checkpoints are skipped with one warning; `hippo backup now` exits with code `3`.
- Pre-migration checkpoint failure: migration does not run; `open_db` returns the error.
- Payload GC does not touch backups; restore does not re-run GC.
- Local working directory: `<data_dir>/backup/` (the existing `<data_dir>/backups/` is a legacy manual copy; do not use or delete it).
- Docs ship in the same PR. No em dashes or en dashes in commit messages or docs prose. No AI, assistant, or tool attribution in commits.

## Review Focus

1. **Destination unavailable (Drive not mounted or paused).** Expected: artifact waits in the outbox, the run reports failure, nothing is pruned, and the next run ships it. Test in Task 6.
2. **Two backup operations at once** (hourly run during `hippo backup now` or a migration checkpoint). Expected: one holds `backup/lock`; a scheduled run exits 0 without work; `now` and checkpoints wait up to 10 minutes, then fail. Test in Task 6.
3. **Database page count or page size changes between base and delta** (growth, shrink after an operator `VACUUM`). Expected: growth and shrink produce correct deltas; a page-size change forces a full. Test in Task 2 and Task 6.
4. **A run fails partway** (outbox unwritable, encryption error). Expected: temp files removed, `pagehashes.bin` and `manifest.json` unchanged, older artifacts untouched. Test in Task 6.
5. **Restore across schema versions.** Expected: restoring an artifact with a schema newer than the binary's `EXPECTED_VERSION` is refused; an older one is restored and then migrated by the next `open_db`, which takes its own checkpoint. Test in Task 8.

---

### Task 1: Config section, dependencies, and the snapshot

**Files:**
- Modify: `Cargo.toml` (workspace deps: `rusqlite` features `["bundled", "backup"]`; add `age`, `zstd`, `tar` at current stable versions)
- Modify: `crates/hippo-daemon/Cargo.toml` (use `age`, `zstd`, `tar`)
- Modify: `crates/hippo-core/src/config.rs` (add `BackupConfig`, field `backup` on `HippoConfig`)
- Modify: `config/config.default.toml` (commented `[backup]` block)
- Create: `crates/hippo-daemon/src/backup/mod.rs`, `crates/hippo-daemon/src/backup/snapshot.rs`
- Modify: `crates/hippo-daemon/src/lib.rs` (`pub mod backup;`)

**Interfaces:**
- Produces:
  - `hippo_core::config::BackupConfig { destination: String, recipient: String, identity_ref: String }`, all `#[serde(default)]` empty strings; `impl BackupConfig { pub fn is_configured(&self) -> bool }` (true when `destination` and `recipient` are both non-empty).
  - `HippoConfig::backup_work_dir(&self) -> PathBuf` = `<data_dir>/backup`.
  - `backup::snapshot::take_snapshot(src: &Path, dst: &Path) -> anyhow::Result<SnapshotInfo>`
  - `pub struct SnapshotInfo { pub page_size: u32, pub page_count: u64, pub sha256: [u8; 32], pub schema_version: i64, pub row_counts: BTreeMap<String, i64> }`
  - `row_counts` keys: `events`, `knowledge_nodes`, `agentic_sessions`, `browser_events` (tables that do not exist are omitted).

- [ ] **Step 1: Write failing tests in `snapshot.rs` `#[cfg(test)]`**
  - `snapshot_matches_source_rows`: fixture DB in WAL mode with an FTS5 table, a table `kv_vector_chunks00(rowid PRIMARY KEY, vectors BLOB NOT NULL)` with 500 rows, and `events` with 1,000 rows. Assert `info.row_counts["events"] == 1000`, the snapshot's `PRAGMA journal_mode` is `delete`, `PRAGMA quick_check` is `ok`, no `-wal` file exists next to `dst`, and `SELECT _rowid_, rowid FROM kv_vector_chunks00` is identical in source and snapshot.
  - `snapshot_completes_under_concurrent_writes`: a thread inserts into `events` in a loop with `busy_timeout=5000` while `take_snapshot` runs; assert it returns `Ok` within 10 s and `quick_check` is `ok`.
  - `config_backup_defaults_unconfigured`: `HippoConfig` parsed from an empty TOML has `!backup.is_configured()`.
- [ ] **Step 2: Run `cargo test -p hippo-daemon backup::snapshot` and `cargo test -p hippo-core config_backup`; expect failures (missing items).**
- [ ] **Step 3: Implement.** Open the source with `SQLITE_OPEN_READ_ONLY | SQLITE_OPEN_NOFOLLOW` and `busy_timeout=5000` (not `open_db`: it would run migrations and the Task 9 hook). Use `rusqlite::backup::Backup::step(-1)`. Write to `dst` with mode `0600`. SHA-256 the finished file.
- [ ] **Step 4: Run the tests; expect PASS.**
- [ ] **Step 5: Commit** `git commit -m "Add backup config section and one-step SQLite snapshot"`

### Task 2: Page hashes and page-level deltas

**Files:**
- Create: `crates/hippo-daemon/src/backup/pagediff.rs`

**Interfaces:**
- Consumes: `SnapshotInfo` (Task 1).
- Produces:
  - `pub struct PageHashes { pub base_id: String, pub page_size: u32, pub hashes: Vec<[u8; 32]> }`
  - `PageHashes::compute(db: &Path, base_id: &str) -> Result<PageHashes>`; `write(&self, path: &Path) -> Result<()>` (temp + rename); `read(path: &Path) -> Result<PageHashes>`
  - `pub fn write_diff(base: &PageHashes, new_db: &Path, out: &Path) -> Result<DiffSummary>`; `pub struct DiffSummary { pub changed_pages: u64, pub new_page_count: u64 }`
  - `pub fn apply_diff(base_db: &Path, diff: &Path, out_db: &Path) -> Result<()>`
  - `pub fn hashes_after_diff(base: &PageHashes, diff: &Path) -> Result<Vec<[u8; 32]>>` (re-reads the serialized diff; used for verification)
- Diff format (little-endian): magic `HIPPOPD1`; `page_size: u32`; `base_page_count: u64`; `new_page_count: u64`; `base_id_len: u32` + UTF-8 bytes; then records `page_no: u64` (0-based) + `page_size` bytes, ascending, ending at EOF. `page_size` mismatch with base is an error (caller forces a full).

- [ ] **Step 1: Write failing tests**
  - `diff_roundtrip_unchanged`: identical DBs give `changed_pages == 0`; `apply_diff` output is byte-identical.
  - `diff_roundtrip_growth_and_shrink`: proptest over sequences of inserts and deletes followed by `VACUUM` on a copy; assert `apply_diff(base, diff) == new` byte-for-byte and `hashes_after_diff == PageHashes::compute(new).hashes`.
  - `diff_rejects_page_size_change`: base at 4096, new at 8192; `write_diff` returns `Err` containing `page_size`.
  - `hashes_file_roundtrip`: `write` then `read` gives an equal struct.
- [ ] **Step 2: Run `cargo test -p hippo-daemon backup::pagediff`; expect FAIL.**
- [ ] **Step 3: Implement** with buffered sequential reads; never load the whole DB into memory.
- [ ] **Step 4: Run tests; expect PASS.**
- [ ] **Step 5: Commit** `git commit -m "Add page-hash file and page-level delta format for backups"`

### Task 3: Encrypted artifact container

**Files:**
- Create: `crates/hippo-daemon/src/backup/artifact.rs`

**Interfaces:**
- Produces:
  - `pub enum ArtifactKind { Full, Delta, Checkpoint }` (serde lowercase)
  - `pub struct ArtifactMeta { pub id: String, pub kind: ArtifactKind, pub label: Option<String>, pub created_ms: i64, pub base_id: Option<String>, pub hippo_version: String, pub schema_version: i64, pub db_sha256: String /* hex, reconstructed DB */, pub row_counts: BTreeMap<String, i64> }`
  - `pub fn artifact_id(kind: ArtifactKind, created_ms: i64, label: Option<&str>) -> String` = `hippo-{kind}-{YYYYMMDDTHHMMSSZ}` plus `-{slug}` when labeled (slug: lowercase ASCII alphanumerics and `-`, max 40 chars). File name is `{id}.tar.zst.age`.
  - `pub struct ArtifactInputs<'a> { pub db_entry: DbEntry<'a>, pub config_dir: &'a Path, pub data_dir: &'a Path }`; `pub enum DbEntry<'a> { Full(&'a Path), Delta(&'a Path) }`
  - `pub fn write_artifact(meta: &ArtifactMeta, inputs: &ArtifactInputs<'_>, recipient: &age::x25519::Recipient, out: &Path) -> Result<[u8; 32] /* ciphertext sha256 */>`
  - `pub fn extract_artifact(path: &Path, identity: &age::x25519::Identity, dest_dir: &Path) -> Result<ArtifactMeta>`
- Stream: `tar` builder → `zstd::Encoder` (level 9, `long_distance_matching(true)`) → `age` encryptor → file. Tar entries in the order listed in Global Constraints, `meta.json` first.

- [ ] **Step 1: Write failing tests**
  - `artifact_roundtrip`: generate an identity, write a Full artifact from a temp data dir and config dir (config dir is a symlink to another temp dir), extract, assert `hippo.db` bytes, both config files, `enrichment-state.json`, and fallback files match, and `meta` equals the input.
  - `artifact_is_encrypted`: the ciphertext does not contain the plaintext marker string placed in the fixture DB and in `config.toml`.
  - `artifact_id_format`: `artifact_id(Checkpoint, 1793870000000, Some("Pre Re-Embed!"))` equals `hippo-checkpoint-20261105T091320Z-pre-re-embed`.
  - `standard_tools_decrypt_full` (`#[ignore]`, run manually): `age -d -i key | zstd -d | tar -t` lists `hippo.db`.
- [ ] **Step 2: Run `cargo test -p hippo-daemon backup::artifact`; expect FAIL.**
- [ ] **Step 3: Implement.** Missing optional inputs (`enrichment-state.json`, `fallback/`) are skipped.
- [ ] **Step 4: Run tests; expect PASS.**
- [ ] **Step 5: Commit** `git commit -m "Add encrypted tar+zstd backup artifact container"`

### Task 4: Manifest and retention

**Files:**
- Create: `crates/hippo-daemon/src/backup/manifest.rs`, `crates/hippo-daemon/src/backup/retention.rs`

**Interfaces:**
- Consumes: `ArtifactKind`, `ArtifactMeta` (Task 3).
- Produces:
  - `pub struct ManifestEntry { pub meta: ArtifactMeta, pub file: String, pub ciphertext_sha256: String, pub verified: bool, pub shipped_ms: Option<i64>, pub uploaded: bool }`
  - `pub struct Manifest { pub version: u32 /* 1 */, pub entries: Vec<ManifestEntry> }`
  - `Manifest::load(dest: &Path) -> Result<Manifest>` (missing file = empty); `Manifest::save(&self, dest: &Path) -> Result<()>` (writes `.manifest.json.tmp`, fsync, rename to `manifest.json`)
  - `pub fn plan_prune(entries: &[ManifestEntry], now_ms: i64) -> Vec<String>` (ids to delete)

- [ ] **Step 1: Write failing tests in `retention.rs`**, one per rule, with fixed `now_ms`:
  - `prune_delta_after_90_days`, `keep_delta_at_89_days`
  - `prune_weekly_full_after_365_days_unless_first_of_month`
  - `keep_first_full_of_utc_month_forever` (a 3-year-old full that is the earliest Full in its UTC month)
  - `prune_checkpoint_after_365_days`
  - `keep_full_referenced_by_retained_delta` (full older than 365 days, delta 10 days old with `base_id` = that full)
  - `keep_newest_verified_uploaded_full` (all fulls older than 365 days and not first-of-month)
  - `newest_full_rule_ignores_not_uploaded` (newest full has `uploaded=false`; the newest uploaded one is kept)
  - `manifest_save_is_atomic`: after `save`, no `.manifest.json.tmp` remains and `load` returns equal data.
- [ ] **Step 2: Run `cargo test -p hippo-daemon backup::retention backup::manifest`; expect FAIL.**
- [ ] **Step 3: Implement** `plan_prune` as a pure function. Only `verified` entries count toward retention rules; "first full of month" considers `ArtifactKind::Full` only.
- [ ] **Step 4: Run tests; expect PASS.**
- [ ] **Step 5: Commit** `git commit -m "Add backup manifest and tiered retention planner"`

### Task 5: Outbox shipping and upload state

**Files:**
- Create: `crates/hippo-daemon/src/backup/ship.rs`

**Interfaces:**
- Produces:
  - `pub fn ship(outbox_file: &Path, dest: &Path, expected_sha256: &[u8; 32]) -> Result<ShipOutcome>`; `pub enum ShipOutcome { Shipped { dest_file: PathBuf }, DestinationUnavailable }`. Copies to `dest/.{name}.partial`, fsyncs, renames, reads back and compares SHA-256, then deletes the outbox file. A missing `dest` returns `DestinationUnavailable` and leaves the outbox file.
  - `pub fn is_drive_fs(dest: &Path) -> bool` (path contains `/Library/CloudStorage/GoogleDrive-`)
  - `pub fn is_uploaded(file: &Path, drive_fs: bool) -> Result<bool>`: `true` when `!drive_fs`; otherwise `true` when the `com.google.drivefs.item-id#S` extended attribute exists (`libc::getxattr`).
  - `pub const DRIVE_UPLOADED_XATTR: &str = "com.google.drivefs.item-id#S";`

- [ ] **Step 1: Write failing tests**
  - `ship_moves_and_verifies`, `ship_missing_dest_keeps_outbox`, `ship_hash_mismatch_errors_and_removes_partial`.
  - `uploaded_detects_xattr`: temp file, `is_uploaded(_, true)` is `false`; set the xattr with `libc::setxattr`; now `true`. `is_uploaded(_, false)` is always `true`.
- [ ] **Step 2: Run `cargo test -p hippo-daemon backup::ship`; expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run tests; expect PASS.**
- [ ] **Step 5: Commit** `git commit -m "Ship backup artifacts via outbox with read-back and upload state"`

### Task 6: Run orchestration, status, and CLI (`run`, `now`, `status`)

**Files:**
- Create: `crates/hippo-daemon/src/backup/run.rs`, `crates/hippo-daemon/src/backup/status.rs`
- Modify: `crates/hippo-daemon/src/cli.rs` (add `Backup { action: BackupAction }` to `Commands`; `BackupAction::{Run, Now { label: Option<String> }, Status, Init, Verify { id: Option<String> }, Restore { .. }}`; `Init`, `Verify`, `Restore` are wired in Tasks 7 and 8)
- Modify: `crates/hippo-daemon/src/main.rs` (dispatch)
- Test: `crates/hippo-daemon/tests/backup_run.rs`

**Interfaces:**
- Consumes: Tasks 1 to 5.
- Produces:
  - `pub enum RunMode { Scheduled, Checkpoint { label: String } }`
  - `pub fn run_backup(config: &HippoConfig, mode: RunMode, now_ms: i64) -> Result<RunReport>`; `pub enum RunReport { NotConfigured, Skipped { reason: String }, Done { id: String, kind: ArtifactKind, shipped: bool } }`
  - `pub struct BackupStatus { pub last_success_ms: Option<i64>, pub last_kind: Option<ArtifactKind>, pub last_error: Option<String>, pub last_verify_ms: Option<i64>, pub pending_upload: Vec<PendingUpload>, pub outbox_count: u32 }`; `pub struct PendingUpload { pub id: String, pub shipped_ms: i64 }`
  - `status::load(work_dir: &Path) -> BackupStatus` (missing or unreadable file = default); `status::save(work_dir: &Path, s: &BackupStatus) -> Result<()>` (atomic) at `<work_dir>/status.json`.
  - `pub fn checkpoint(config: &HippoConfig, label: &str) -> Result<RunReport>` = `run_backup(config, RunMode::Checkpoint { label }, now)`
  - Exit codes for `hippo backup now`: `0` done, `3` not configured, `1` error.
- Run algorithm (one `flock` on `<work_dir>/lock`; Scheduled uses `LOCK_NB` and returns `Skipped` when held; Checkpoint waits up to 600 s):
  1. Ship any outbox files first; refresh `uploaded` for manifest entries with `uploaded=false`.
  2. Scheduled only: skip if `last_success_ms` is within 20 h.
  3. Snapshot into a temp dir inside `<work_dir>`.
  4. Kind: Checkpoint for `RunMode::Checkpoint`; else Full when `pagehashes.bin` is missing, its `base_id` is not a verified manifest entry, its page size differs, or the base's ISO week (UTC) differs from now; else Delta.
  5. Verify: `quick_check` already done; for Delta, `hashes_after_diff` must equal `PageHashes::compute(snapshot)`.
  6. Write artifact to `<work_dir>/outbox/`, ship, add manifest entry (`verified=true`), save manifest.
  7. Full only: write the new `pagehashes.bin` after the manifest save.
  8. Prune with `plan_prune`; delete those destination files, then save the manifest.
  9. Update `status.json`. On any error: delete temp files, keep `pagehashes.bin` and the manifest unchanged, record `last_error`, return `Err`.
- `hippo backup status` prints the `BackupStatus` fields and the latest manifest entry per kind.

- [ ] **Step 1: Write failing integration tests in `tests/backup_run.rs`** (temp data dir, temp config dir, temp destination, generated recipient):
  - `first_run_writes_full_then_delta`: run Scheduled at `t`, then at `t + 21h` in the same ISO week; assert kinds Full then Delta and that the Delta's `base_id` is the Full's id.
  - `scheduled_run_skips_within_20h`.
  - `new_iso_week_forces_full`.
  - `page_size_change_forces_full` (Review Focus 3).
  - `destination_missing_keeps_outbox_and_does_not_prune` (Review Focus 1): rename the destination away, run, assert `Err`, one file in outbox, manifest unchanged; restore the destination, run again, assert shipped.
  - `failed_run_leaves_state_unchanged` (Review Focus 4): make `<work_dir>/outbox` read-only; assert `Err`, `pagehashes.bin` and `manifest.json` byte-identical to before, no temp files.
  - `scheduled_run_skips_when_locked` and `checkpoint_waits_for_lock` (Review Focus 2).
  - `now_unconfigured_exits_3`: run the `hippo` binary with `HIPPO_CONFIG` pointing at a config without `[backup]`; assert exit code 3.
- [ ] **Step 2: Run `cargo test -p hippo-daemon --test backup_run`; expect FAIL.**
- [ ] **Step 3: Implement `run.rs`, `status.rs`, CLI wiring.** Check how existing integration tests point the binary at a temp config and reuse that mechanism instead of `HIPPO_CONFIG` if it differs.
- [ ] **Step 4: Run tests; expect PASS.**
- [ ] **Step 5: Commit** `git commit -m "Add backup run orchestration, status, and hippo backup run/now/status"`

### Task 7: Key setup with 1Password (`hippo backup init`)

**Files:**
- Create: `crates/hippo-daemon/src/backup/keys.rs`
- Modify: `crates/hippo-daemon/src/main.rs` (dispatch `BackupAction::Init { destination: PathBuf, op_vault: Option<String> }`)

**Interfaces:**
- Produces:
  - `pub fn resolve_identity(identity_ref: &str) -> Result<age::x25519::Identity>`: `op://...` runs `op read <ref>`; `file:<path>` reads a file the operator supplies (recovery and tests). Anything else is an error.
  - `pub fn init(config_path: &Path, destination: &Path, op_vault: Option<&str>) -> Result<()>`.
- `init` behavior:
  1. Refuse if `[backup]` is already configured.
  2. Refuse if `destination` does not exist.
  3. Generate the identity in memory.
  4. If `op` is on `PATH`: `op item create --category "Secure Note" --title "hippo backup key (<hostname>)" [--vault V] 'private_key[password]=<AGE-SECRET-KEY-...>'`, then the reference is `op://<vault>/<title>/private_key`. Confirm with `op read` that it resolves to the same key.
  5. If `op` is missing or fails: print the private key once and require the operator to type `stored`. The reference is then empty, and restore needs `--identity file:<path>`.
  6. Append a `[backup]` block with `destination`, `recipient`, `identity_ref` to `config.toml`, following the symlink. Never rewrite existing content.
- The private key is never written to disk.

- [ ] **Step 1: Write failing tests:** `resolve_identity_file_ref`, `resolve_identity_rejects_unknown_scheme`, `init_refuses_when_configured`, `init_appends_block_without_touching_existing_lines` (op absent: set `PATH` to an empty temp dir and feed `stored` on stdin; assert the original lines are intact and the new block parses).
- [ ] **Step 2: Run `cargo test -p hippo-daemon backup::keys`; expect FAIL.**
- [ ] **Step 3: Implement.** Pass the secret to `op` through an argument list, never through a shell string.
- [ ] **Step 4: Run tests; expect PASS.**
- [ ] **Step 5: Commit** `git commit -m "Add hippo backup init with 1Password key storage"`

### Task 8: Verify and restore

**Files:**
- Create: `crates/hippo-daemon/src/backup/restore.rs`
- Modify: `crates/hippo-daemon/src/main.rs` (dispatch `Verify { id }` and `Restore { id: Option<String>, before: Option<String>, yes: bool, extract: Option<PathBuf>, identity: Option<String>, destination: Option<PathBuf> }`)
- Test: `crates/hippo-daemon/tests/backup_restore.rs`

**Interfaces:**
- Consumes: Tasks 2 to 7; `install::service_bootout` and the existing service list used by `mise run stop`.
- Produces:
  - `pub fn materialize(dest: &Path, manifest: &Manifest, id: &str, identity: &Identity, out_dir: &Path) -> Result<PathBuf /* db path */>`: extracts the full (and applies the delta), checks `db_sha256`, `quick_check`, and `row_counts` against `meta`.
  - `pub fn verify(config: &HippoConfig, id: Option<&str>) -> Result<()>`: latest verified entry when `id` is `None`; records `last_verify_ms` in status on success.
  - `pub fn loss_report(live_db: &Connection, since_ms: i64) -> Result<Vec<(String, i64)>>`: counts newer rows per source: `shell` (`events`), `browser` (`browser_events`), and `agentic:<harness>` (`agentic_sessions`). Agentic rows print "re-ingested from source files if still present"; shell and browser rows print "lost".
  - `pub fn restore(config: &HippoConfig, target: RestoreTarget, opts: RestoreOpts) -> Result<()>`
- Restore order:
  1. Select an artifact (`--id`, or the newest verified entry with `created_ms < --before`).
  2. Refuse if `meta.schema_version > EXPECTED_VERSION`.
  3. Print the loss report and require `--yes` or typed confirmation.
  4. Materialize into `<work_dir>/restore-tmp`.
  5. Stop `com.hippo.*` agents.
  6. Checkpoint the current DB (label `pre-restore`). If `[backup]` is unconfigured, copy it to `<work_dir>/pre-restore-<ts>.db` instead.
  7. Rename the materialized DB over `hippo.db` and remove `hippo.db-wal` and `hippo.db-shm`.
  8. Start the agents. Print that MCP clients must be restarted to see the restored DB.
- `--extract DIR` only extracts and applies the delta into `DIR`. It does not stop services or replace files.
- `--identity` overrides `identity_ref`. `--destination` overrides the configured destination (new machine without config).

- [ ] **Step 1: Write failing tests:**
  - `verify_full_and_delta` (uses `file:` identity).
  - `restore_replaces_db_and_checkpoints_previous` (stub service control behind a trait so tests do not touch launchd).
  - `restore_refuses_newer_schema` (Review Focus 5).
  - `restore_older_schema_then_open_db_migrates` (Review Focus 5): set `user_version` lower in the fixture artifact.
  - `loss_report_counts_rows_after_cutoff`.
  - `extract_does_not_touch_live_db`.
- [ ] **Step 2: Run `cargo test -p hippo-daemon --test backup_restore`; expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run tests; expect PASS.**
- [ ] **Step 5: Commit** `git commit -m "Add hippo backup verify and restore"`

### Task 9: Checkpoints before migrations and mutating commands

**Files:**
- Modify: `crates/hippo-core/src/storage.rs` (hook in `open_db`)
- Modify: `crates/hippo-daemon/src/main.rs` (register the hook at startup; checkpoint in `Commands::GcEventPayloads` before `gc_event_payloads`)
- Create: `brain/src/hippo_brain/backup_checkpoint.py`; Test: `brain/tests/test_backup_checkpoint.py`
- Modify: `brain/scripts/dedup-knowledge-nodes.py` (when `--apply`), `brain/scripts/dedup-entities.py` (unless `--dry-run`), `brain/scripts/re-enrich-knowledge-nodes.py` (unless `--dry-run`)
- Modify: `mise.toml`: add `hippo backup now --label <task-name>` as the first command of `re-enrich`, `re-enrich:failed`, `re-enrich:claude`, `re-embed`, treating exit code 3 as a warning; **delete the interim `[tasks.backup]`**
- Test: `crates/hippo-core/tests/pre_migration_hook.rs`

**Interfaces:**
- Produces:
  - `hippo_core::storage::set_pre_migration_hook(hook: fn(db: &Path, from: i64, to: i64) -> anyhow::Result<()>)` (stored in a `OnceLock`; second registration is ignored).
  - `open_db` calls the hook once when `0 < user_version < EXPECTED_VERSION`, before the first migration statement. An `Err` is returned from `open_db` with context `pre-migration checkpoint failed; migration not run`.
  - Daemon hook: loads `HippoConfig`; unconfigured logs one `warn!` and returns `Ok`; otherwise `backup::run::checkpoint(config, &format!("pre-migration-v{from}-v{to}"))`, skipping when a verified manifest entry with that label already exists (several processes start together after an upgrade).
  - Python: `checkpoint_before_mutation(label: str, db_path: Path) -> None`. Runs only when `db_path` resolves to the configured DB. Calls `hippo backup now --label <label>`. Exit 3 logs a warning; any other non-zero exit raises `SystemExit` with the stderr text.
- [ ] **Step 1: Write failing tests**
  - Rust `hook_runs_before_migration_and_blocks_on_error`: v28 fixture, hook returns `Err`; `open_db` errors and `user_version` stays 28. With the hook returning `Ok`, it is called once with `(28, 29)`.
  - `hook_not_called_for_fresh_or_current_db`.
  - Existing migration tests (`schema_v5_migration.rs` and others) pass unchanged with no hook registered.
  - Python: `test_skips_other_db`, `test_exit_3_warns`, `test_failure_raises` (monkeypatch `subprocess.run`).
- [ ] **Step 2: Run `cargo test -p hippo-core` and `uv run --project brain pytest brain/tests/test_backup_checkpoint.py -v`; expect FAIL.**
- [ ] **Step 3: Implement the hook, registration, GC checkpoint, Python helper, script calls, and mise task lines.**
- [ ] **Step 4: Run `cargo test` and `uv run --project brain pytest brain/tests -v`; expect PASS.**
- [ ] **Step 5: Commit** `git commit -m "Checkpoint before schema migrations, payload GC, and mutating scripts"`

### Task 10: Scheduling, watchdog, and doctor

**Files:**
- Create: `launchd/com.hippo.backup.plist` (same shape as `com.hippo.watchdog.plist`; arguments `backup run`; `StartInterval` 3600; `RunAtLoad` true; logs `__DATA_DIR__/backup.stdout.log` and `backup.stderr.log`)
- Modify: `crates/hippo-daemon/src/main.rs` (install, uninstall, and doctor plist lists next to `com.hippo.watchdog`)
- Modify: `crates/hippo-daemon/src/watchdog.rs` (new `check_i21_backup_freshness`)
- Modify: `crates/hippo-daemon/src/commands.rs` (new `check_backup(config, explain) -> u32` in the doctor sequence)

**Interfaces:**
- Consumes: `status::load`, `BackupConfig::is_configured`.
- Produces:
  - `pub fn check_i21_backup_freshness(status: &BackupStatus, configured: bool, now_ms: i64) -> Option<InvariantViolation>`, `invariant_id` `"I-21"`, `source` `"backup"`. Violates when `last_success_ms` is `None` or older than 36 h, or any `pending_upload.shipped_ms` is older than 24 h. `details` names which.
  - Doctor lines: `[--] backup: not configured`; `[OK] backup: last <kind> <age> ago`; `[WW]` for outbox backlog > 0, any pending upload > 24 h, or last verify older than 35 days (or never); `[!!]` for last success older than 36 h. After the first uploaded full, `[WW] legacy DB copies: <n> files, <size>` lists `hippo.db.*.bak`, `hippo.db.pre-*`, `hippo.db.bak*`, `hippo.db.v5-backup*` and their `-shm`/`-wal` files in `data_dir`. Doctor does not call `op` (it can prompt for authentication); the last successful `verify` stands in for "key resolves".
- [ ] **Step 1: Write failing unit tests** for `check_i21_backup_freshness`: unconfigured gives `None`; 35 h gives `None`; 37 h gives a violation; pending 25 h gives a violation. Doctor: `check_backup` on a temp status file for each severity, and legacy-file listing.
- [ ] **Step 2: Run `cargo test -p hippo-daemon i21 check_backup`; expect FAIL.**
- [ ] **Step 3: Implement and register the plist.** Check that `hippo daemon install --force` renders it (existing install tests list expected plists; add it there).
- [ ] **Step 4: Run `cargo test`; expect PASS.**
- [ ] **Step 5: Commit** `git commit -m "Schedule hippo backups and report their health in watchdog and doctor"`

### Task 11: Documentation

**Files:**
- Create: `docs/backup.md` (operator runbook: what is backed up and excluded, `hippo backup init`, schedule, tiers, `status`, `verify`, `now --label`, `restore` with the loss report, new-machine recovery with `--destination` and `--identity`, recovering a full with stock tools: `op read <ref> > key; age -d -i key FILE | zstd -d | tar -x`, then delete `key`)
- Modify: `CLAUDE.md` (Commands: `hippo backup init|run|now|status|verify|restore`; Architecture: one paragraph pointing to `docs/backup.md`)
- Modify: `docs/capture/architecture.md` (I-21 row in the invariant table; update the invariant count sentence)
- Modify: `config/config.default.toml` (finalize the `[backup]` comments from Task 1)
- [ ] **Step 1: Write the docs.**
- [ ] **Step 2: Check:** `rg -n '—|–' docs/backup.md CLAUDE.md docs/capture/architecture.md` shows no new matches, and each command in `docs/backup.md` matches `hippo backup --help` output.
- [ ] **Step 3: Commit** `git commit -m "Document hippo backup"`

### Task 12: Operator verification on the real system

These steps touch the operator's Drive and 1Password. The implementer runs them only with the operator present.

- [ ] **Step 1: Delta size check.** Build release, then run a scratch measurement (not committed): snapshot today's DB, compute `PageHashes`, wait one day of normal use (or use the 2026-10-27 Drive backup as base), and compare `write_diff` size, after zstd, against `zstd -9 --patch-from`. If the page diff is more than 3 times larger, stop and report to the operator before continuing.
- [ ] **Step 2: Drive upload signal.** Write a 1 MB test file into the destination with Drive paused, and record whether `com.google.drivefs.item-id#S` is present. Resume Drive, wait for upload, and record again. If the attribute appears before upload, replace `is_uploaded` with a signal that does not, and update the spec and `docs/backup.md`. Delete the test file.
- [ ] **Step 3: Live setup.** `hippo backup init --destination "<Drive>/My Drive/Backups/hippo"`, then `hippo daemon install --force`, then `hippo backup now --label first`, then `hippo backup verify`, then `hippo doctor`. Expected: `[OK] backup` and a verified artifact in Drive.
- [ ] **Step 4: Full checks.** `cargo test`, `cargo clippy --all-targets -- -D warnings`, `cargo fmt --check`, `uv run --project brain pytest brain/tests -v`, `uv run --project brain ruff check brain/`, `uv run --project brain ruff format --check brain/`.
