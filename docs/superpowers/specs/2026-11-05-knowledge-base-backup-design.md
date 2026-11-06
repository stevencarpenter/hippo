# Knowledge-base backup design

**Date:** 2026-11-05
**Status:** Draft, awaiting approval

## Intent

`hippo.db` holds almost a year of captured shell, agent-session, and browser
history plus the knowledge nodes and embeddings enriched from it. Much of the
raw history cannot be captured again: shell and browser events exist only in
hippo, and agent harnesses delete old session files on their own schedules.
Hippo's usefulness depends on that corpus. Losing it means months before hippo
is useful again.

Nothing has damaged the corpus yet. The operator now wants to run experiments
that mutate it on purpose (re-enrichment, re-embedding, dedup, and similar),
and wants a known-good copy before each one. A bug in hippo or an operator
mistake could also damage the corpus without anyone noticing for some time.

This is a hippo feature, not personal tooling. Any hippo user gets it. The
operator's own configuration points the destination at a Google Drive folder.

## Scenarios

1. **Checkpoint before an experiment.** The operator, or a mutating command,
   takes a checkpoint. If the experiment goes badly, the operator restores it.
2. **Recovery from unnoticed damage.** Scheduled backups with thinning
   retention let the operator go back to a point before the damage, even if the
   damage is found weeks later.
3. **Lost or dead machine.** The destination is off-machine and the decryption
   key is in 1Password, so a new machine can restore.

## Goals

- Scheduled, unattended backups with no operator action after setup.
- On-demand labeled checkpoints, and an automatic checkpoint before schema
  migrations and the known mutating commands.
- Storage-efficient: weekly full snapshots plus small daily deltas.
- Encrypted at the destination. The private key lives in 1Password, never on
  this machine's disk.
- A tidy backup surface: one destination folder, one naming scheme, automatic
  pruning, and one small local working directory.
- No data loss from the backup system itself: every artifact is verified before
  it counts, and pruning never removes something a retained backup depends on.
- A restore command that is safe to run: it checkpoints the current state first
  and says what will be lost before it acts.

## Non-goals

- Restoring only one layer (for example, knowledge nodes and embeddings while
  keeping newer raw events). Restore is whole-database in this version.
- Backing up OTel data (`mise run otel:backup` covers it), logs, the generated
  `vault/` export, `metrics/` (operator usage measurement, not written by
  hippo), and `bench/`.
- Migrating or deleting the existing hand-made `hippo.db.*.bak` and `pre-*`
  copies in the data directory (about 4 GB). See "Legacy copies".
- Multi-host operation. Hippo runs on one machine.
- Backing up the agent harness source files (`~/.claude/projects`, `~/.codex`,
  and others). They belong to other tools.

## Anti-goals

These make the feature a failure even if backups technically get written:

- A backup run blocks or slows capture. (Same principle as AP-1.)
- Backups stop and nobody is told.
- The only copy of the private key is on this machine.
- Plaintext corpus data reaches the destination.
- Pruning deletes a full snapshot that a retained delta needs, or deletes the
  newest verified full.
- A restore overwrites the current database with no way back.

## Constraints and established facts

- Single macOS host. `hippo.db` is WAL-mode SQLite with `PRAGMA
  busy_timeout=5000`. The daemon and brain write to it continuously.
- Vectors live inside `hippo.db` as `vec0` virtual tables
  (`knowledge_vectors*` shadow tables). FTS5 tables are also inside.
- Measurements on the 1.0 GB database, 2026-11-05:

  | Operation | Result |
  |---|---|
  | `sqlite3` shell `.backup` on the live DB | Did not finish in 10 min. It copies 100 pages per step and restarts after every concurrent write. |
  | `VACUUM INTO` | About 7 s. 38,203 vector rows, unchanged shadow-table rowids, 40 of 40 KNN queries identical to live. |
  | SQLite online backup API, all pages in one step (Python `Connection.backup()`) | 1.7 s, `quick_check` ok. |
  | Full snapshot, `zstd -9 --long=27` | 275 MB, 2.6 s |
  | Full snapshot, `zstd -19 --long=27` | 255 MB, 43 s |
  | Delta vs the 2026-10-27 snapshot, `zstd -9 --patch-from` | 19 MB for 9 days of change, 2.9 s |

- The database grew from 354 MB (2026-05-30) to 1.0 GB (2026-11-05).
- Hippo's other scheduled jobs run as launchd agents rendered from
  `launchd/*.plist` templates and installed by `hippo daemon install`
  (`crates/hippo-daemon/src/install.rs`, `main.rs`).
- `hippo doctor` and the watchdog invariants (`docs/capture/architecture.md`)
  are the alerting surfaces.
- Documentation ships in the same PR as the code.

## Decisions

### Snapshot method (decided)

Use the SQLite online backup API with all pages copied in a single step
(`rusqlite` backup with step size `-1`, or the equivalent). This holds one read
transaction, so concurrent writes cannot restart it, and it copies pages
verbatim. Do not use the `sqlite3` shell `.backup` (it stalls under write load).
Do not use `VACUUM INTO`: it worked in testing, but SQLite documents that
`VACUUM` may renumber rowids in tables without an `INTEGER PRIMARY KEY`, and the
`vec0` shadow tables `knowledge_vectors_vector_chunks*` declare `rowid PRIMARY
KEY` without the `INTEGER` type.

After the copy, set the snapshot's journal mode to `DELETE` so it is one
self-contained file, and run `PRAGMA quick_check`.

### What a backup contains

- `hippo.db` snapshot.
- `config.toml` and `redact.toml` from the config directory. Follow symlinks;
  the operator's config directory is a symlink into dotfiles.
- `enrichment-state.json`.
- Undrained fallback JSONL files from the data directory's `fallback/`.

The non-database files are small. They ride along with every artifact (full,
delta, and checkpoint) in plaintext form before encryption; they do not need
delta treatment.

### Storage scheme

- **Full:** the first scheduled run of each ISO week, every labeled
  checkpoint, and every pre-migration or pre-mutation checkpoint. A full is a
  standalone compressed snapshot.
- **Delta:** every other scheduled run. A delta is diffed against that week's
  full. It is never chained to another delta, so a restore needs at most two
  files.
- **Delta method (author's call, not measured):** a page-level diff. The backup
  API copies SQLite pages verbatim, so the delta is the list of page numbers
  that differ from the base plus those pages, then compressed. zstd
  `--patch-from` gave 19 MB for 9 days, but its match window ends at 2 GiB
  (`windowLog` 31). At about 4 MB of growth per day the database reaches that
  size in about 9 months, after which `--patch-from` deltas approach full size.
  The page diff has no size limit. The builder measures page-diff size against
  `--patch-from` on the real database. If the page diff is more than about 3
  times larger, the builder reports back before choosing.
- **Compression:** zstd level 9 with long-distance matching. Level 19 saved 7%
  for 17 times the time; not worth it.
- **Delta base, kept lean:** deltas are computed against plaintext, before
  encryption, but hippo does not keep a plaintext copy of the base. When it
  writes a weekly full, it records a hash of every page (about 250,000 pages
  for 1 GB, a few MB of hashes). A delta run snapshots the database, hashes its
  pages, and keeps the pages whose hash differs. Scheduled runs never need the
  private key, and the only standing local cost is the hash file.
- **Transient local space:** each run needs room for one temporary snapshot
  (the database size) and the artifact. Both are deleted when the run ends,
  whether it passes or fails.
- **Schedule:** daily at a fixed local time through a launchd agent using
  `StartCalendarInterval` (launchd runs a missed job at wake). A run is skipped
  if a successful backup already happened within the last 20 hours.

### Encryption and the key

- Format: `age`, with an X25519 key pair. Any artifact can be decrypted with the
  stock `age` CLI, with no working hippo needed.
- **1Password is the primary key location.** `hippo backup init` generates the
  key pair, stores the private key in 1Password through the `op` CLI, and
  writes the public recipient and the `op://` reference to `config.toml`. The
  private key is never written to disk.
- If `op` is not installed or not signed in, `init` prints the private key once
  and requires the operator to confirm it was stored before it writes config.
- Encryption needs only the public key. Restore and full verification read the
  private key through `op` at the moment they need it.
- The manifest (below) is not encrypted. It holds no corpus content.

### Destination layout and the manifest

- One configured destination directory (`[backup] destination` in
  `config.toml`). The operator sets it to
  `~/Library/CloudStorage/GoogleDrive-<account>/My Drive/Backups/hippo`.
  Scheduled runs never create the destination: a missing directory means the
  sync client is not mounted, and a local look-alike would never sync.
  `hippo backup init` without `--destination` detects the Drive mounts and
  creates `My Drive/Backups/hippo` inside the one the operator chooses.
- Every artifact is written under a temporary name and renamed when complete,
  so the sync client never uploads a partial file.
- A manifest in the destination lists each artifact: kind (full, delta,
  checkpoint), label, base artifact for deltas, created time, hippo version,
  schema version, plaintext SHA-256, ciphertext SHA-256, and verification
  status. Pruning and restore use the manifest; they do not need the key to
  decide what exists.

### Local working directory

One directory under the data directory holds the page-hash file and an
outbox. Artifacts are always written to the outbox first, then shipped to the
destination. If the destination is unavailable (Drive not mounted), the
artifact waits in the outbox and ships on the next run; doctor reports it. A
`backups/` directory already exists in the data directory with an old manual
copy; the builder picks a name that does not collide with it.

### Verification

Every run, unattended:

1. `quick_check` on the snapshot and row counts for the main tables.
2. For a delta: confirm that the base page hashes, with the delta's pages
   applied, equal the snapshot's page hashes, before encryption.
3. After shipping: read the file back from the destination and confirm its
   SHA-256 equals the ciphertext written.

An artifact counts as a backup only after all three pass.

**Upload confirmation (author's call).** For a File Provider destination under
`~/Library/CloudStorage`, a local read-back proves only that the file is in the
sync folder, not that Google Drive uploaded it. The manifest records a separate
`uploaded` state. Uploaded files observed on 2026-11-05 carry a
`com.google.drivefs.item-id#S` extended attribute. Whether that attribute
appears only after upload is not verified, and the builder confirms or replaces
this signal. For other destinations, `uploaded` equals shipped. Rules:

- Pruning treats a full as "newest verified" only when it is also uploaded.
- An artifact that stays shipped but not uploaded for more than 24 hours raises
  the watchdog alarm and shows in doctor.

`hippo backup verify` performs a full decrypt-and-restore test into a temporary
location, including `quick_check` and a vector KNN spot check. It needs the
private key through `op`, so it runs when the operator runs it. Doctor warns if
no full verify has passed in 35 days.

### Retention and pruning

The operator set the storage rule: destination space is plentiful (about 4 TB
of Google storage available), local space is not. Save more than might be
needed at the destination and keep this machine lean.

- Deltas: keep 90 days.
- Weekly fulls: keep 1 year.
- Monthly fulls (the first full of each month): keep forever.
- Labeled and pre-mutation checkpoints: keep 1 year.
- Never delete a full that a retained delta uses as its base.
- Never delete the newest verified full.
- Pruning runs only after the current run's artifact passes verification.

Estimate at today's size: 52 weekly fulls at about 275 MB is about 14 GB, plus
monthlies and deltas. That grows with the database and stays well inside the
available space.

Locally, only the page-hash file and any unshipped outbox artifacts persist.

### Payload expiry (operator's decision)

Backups are the archive. `hippo gc-event-payloads` clears expired `stdout`,
`stderr`, and env snapshots in the live database only, to keep this machine
lean. Backups keep those payloads under the retention above, including
monthly fulls kept forever. A restore does not re-run GC.
`gc-event-payloads --confirm` is a mutating command and takes a checkpoint
before it deletes anything.

### Checkpoints before mutation

- `hippo backup now [--label <text>]` takes a full checkpoint on demand.
- A schema migration takes a checkpoint before it changes anything. Migrations
  run in `open_db` (`crates/hippo-core/src/storage.rs`). The checkpoint is
  written to the local outbox, so it does not depend on the destination being
  available. **If the checkpoint fails, the migration does not run and the
  error is reported (author's call).** Because `open_db` and the brain refuse a
  database at an older schema version, hippo stays down until the operator
  fixes the cause. Losing data is worse than downtime. With the checkpoint
  written locally, the expected causes are a full disk or an unreadable
  database, and both need the operator anyway.
- **When `[backup]` is not configured (author's call),** migrations and
  mutating commands skip the checkpoint and log one warning, which is today's
  behavior. Doctor reports backups as not configured. This keeps upgrades
  working for users who never set up backups, and keeps tests that migrate old
  fixtures (`crates/hippo-core/tests/schema_v5_migration.rs`) hermetic.
- The known mutating operations take a checkpoint first: `mise run re-enrich`
  and `re-embed`, `brain/scripts/re-enrich-knowledge-nodes.py`,
  `dedup-knowledge-nodes.py`, and `dedup-entities.py`. The builder finds any
  others with the same effect.

### Restore

`hippo backup restore` with an artifact ID or `--before <time>`:

1. Show the chosen artifact, and count the events captured after it, per
   source, that the restore will remove. Note which sources re-ingest from
   their own files (agentic sessions, through their stored cursors) and which
   do not (shell, browser). Require confirmation.
2. Stop the hippo services.
3. Take a checkpoint of the current database.
4. Read the key through `op`, decrypt, apply the delta if needed, and verify.
5. Replace `hippo.db` and remove its `-wal` and `-shm` files.
6. Start the services.

Config files and fallback files from the artifact are not restored
automatically. `restore --extract <dir>` writes the whole artifact contents to
a directory for manual use.

### Health and alerting

- Watchdog invariant: last successful backup older than 36 hours raises an
  alarm. Suppressed when `[backup]` is not configured.
- Doctor lines: last successful backup and its kind, outbox backlog, last full
  verify, and whether the key reference resolves in 1Password (checked only
  when `op` is signed in; otherwise reported as unchecked, not as a failure).
- A failed run keeps every existing artifact and leaves the page-hash file as
  it was.

### Legacy copies

After the first verified full lands, doctor lists the old hand-made copies in
the data directory (`hippo.db.*.bak`, `hippo.db.pre-*`, and their `-shm`/`-wal`
files) with their total size, as a warning. Hippo does not delete them. The
operator deletes them.

### Interim tooling

The uncommitted `mise run backup` task added on 2026-11-05 is removed when this
feature lands. It hard-codes one operator's Drive path in a shared repo and has
no encryption or retention.

## Left to the builder

- Crate choices for `age`, zstd, and the page hash. Prefer libraries over
  shelling out to CLIs.
- Artifact file names, manifest format, and the working-directory name.
- Invariant ID, doctor wording, CLI flag names beyond those above.
- The launchd schedule time.
- Test strategy, including a restore round trip in tests with a small fixture
  database that contains `vec0` and FTS5 tables.
