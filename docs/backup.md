# Knowledge-base backup

`hippo backup` writes encrypted, verified copies of `hippo.db` to a destination directory, on a schedule and before known mutating operations. Use it to roll back an experiment, recover from unnoticed damage, or rebuild on a new machine.

Backups are off until `[backup] destination` and `recipient` are both set. Run `hippo backup init` to set them.

## What a backup contains

Included in every artifact:

- `hippo.db`, taken with the SQLite online backup API in one step (a delta stores `hippo.db.pagediff` instead).
- `config/config.toml` and `config/redact.toml` (symlinks are followed).
- `enrichment-state.json`.
- Undrained `fallback/*.jsonl` files.
- `meta.json`.

Excluded: OTel data (use `mise run otel:backup`), logs, `vault/`, `metrics/`, `bench/`, and the legacy `hippo.db.*` copies in the data directory.

Payload expiry (`hippo gc-event-payloads`) clears the live database only. Backups keep the payloads for the retention period below. Restore does not re-run GC.

## Setup

1. Choose the destination:
   - Omit `--destination` to use Google Drive for desktop. `init` looks for `~/Library/CloudStorage/GoogleDrive-*` folders that contain `My Drive`. With one account, it uses `<mount>/My Drive/Backups/hippo` and creates that folder. With several, it lists them by account (for example `GoogleDrive-<account>`) and asks for a number; any other input aborts with no changes. With several accounts and no terminal, or with no Drive folder, it fails and asks for `--destination`.
   - Or create a directory yourself and pass `--destination <DIR>`. Hippo does not create an explicit destination, and a scheduled run never creates the destination. A missing directory usually means the sync client is not mounted.
2. Run `hippo backup init [--destination <DIR>] [--op-vault <VAULT>]`.
3. Run `hippo backup now --label first`.
4. Run `hippo backup status`.
5. Run `hippo daemon install` if `com.hippo.backup` is not yet loaded.

`init`:

- generates an age X25519 key pair;
- stores the private key in 1Password through the `op` CLI;
- reads the item back and compares it to the generated key;
- appends `[backup]` (`destination`, `recipient`, `identity_ref`) to `config.toml`. The destination is written as its canonical path, with symlinks resolved.

`init` fails if `config.toml` already has a `[backup]` section or an explicit `--destination` is not an existing directory. Each `init` makes a new key pair.

If `op` fails and stdout is a terminal, `init` prints the private key once and asks you to type `stored`. It then writes an empty `identity_ref`. If stdout is not a terminal, `init` fails instead. Set `identity_ref` by hand later, or pass `--identity` to `restore`.

## Configuration

```toml
[backup]
destination = "/path/to/existing/dir"
recipient = "age1..."
identity_ref = "op://<vault_id>/<item_id>/private_key"
```

`identity_ref` is `op://...` or `file:<path>`. Encryption uses only `recipient`. Only `restore` and `verify` read the private key.

## Schedule and tiers

launchd job `com.hippo.backup` runs `hippo backup run` every hour. The run skips when a scheduled backup (a full or a delta) shipped in the last 20 hours, so about one backup is taken per day. A checkpoint does not delay the scheduled run.

`status.json` keeps two times:

- `last_scheduled_ms`: the last shipped full or delta. The 20-hour skip uses it. A `status.json` from an older hippo does not have it, so the next scheduled run does not skip.
- `last_success_ms` and `last_kind`: the last shipped artifact of any kind, checkpoints included. Watchdog I-21 and doctor use it for freshness.

| Tier | When | Contents |
|---|---|---|
| Full | First scheduled run of each ISO week | Standalone snapshot |
| Delta | Other scheduled runs | Page-level diff against that week's full. Never chained, so a restore needs at most two files |
| Checkpoint | `hippo backup now`, and automatic checkpoints | Standalone snapshot with a label |

Each run:

1. snapshots the database and runs `quick_check`;
2. compresses with zstd level 9 and encrypts with age;
3. writes the artifact to the local outbox, `<data_dir>/backup/outbox/` (the legacy `<data_dir>/backups/` is not used);
4. ships it to the destination under a temporary name, then renames it;
5. reads it back and compares the SHA-256;
6. updates `manifest.json` in the destination, then prunes.

Capture is not blocked by a run. If the destination is unavailable, the artifact stays in the outbox and ships on the next run. While artifacts wait for an unavailable destination, scheduled runs add no new artifact.

An outbox artifact that fails to ship for another reason (for example, a different file with the same name in the destination) stays in the outbox. The error goes to `last_error`, and scheduled runs continue to write new artifacts. A new scheduled artifact that fails to ship is removed from the outbox, so a destination that rejects every copy cannot fill the disk. The next run makes a fresh one.

A `.<name>.partial` file in the destination is left by a run that was killed during the copy. The next run, which holds the backup lock, replaces it.

Artifact files are named `<id>.tar.zst.age`. The id is `hippo-{full|delta|checkpoint}-{YYYYMMDDTHHMMSSZ}`, plus `-<label>` when labeled.

### Upload detection

For a destination under `~/Library/CloudStorage/GoogleDrive-*`, a local read-back shows only that the file is in the sync folder. Hippo marks an artifact `uploaded` when the file has the Drive FS extended attribute `com.google.drivefs.item-id#S`. That this attribute appears only after upload has not been verified on a real mount. For other destinations, `uploaded` equals shipped.

## Retention

| Artifact | Kept |
|---|---|
| Delta | 90 days |
| Weekly full | 365 days |
| First full of each UTC month | Forever |
| Checkpoint | 365 days |

Pruning also keeps:

- a full that is the base of a retained delta;
- the newest full that is verified and uploaded.

Pruning runs only after the current run verifies. Only verified entries are pruned.

## Automatic checkpoints

Hippo takes a checkpoint before each of these. If the checkpoint fails or times out waiting for the backup lock, the operation does not run.

- A schema migration, label `pre-migration-v{from}-v{to}`, once per label.
- `hippo gc-event-payloads`.
- `brain/scripts/dedup-knowledge-nodes.py` (with `--apply`), `brain/scripts/dedup-entities.py`, and `brain/scripts/re-enrich-knowledge-nodes.py` (both unless `--dry-run` is given). The scripts take a checkpoint only when they mutate.
- The mise tasks `re-enrich`, `re-enrich:failed`, `re-enrich:claude`, and `re-embed`.

A checkpoint succeeds when the destination is unavailable. It stays verified in the outbox and ships later.

When `[backup]` is not configured, these operations skip the checkpoint and log one warning.

## Commands

| Command | Purpose |
|---|---|
| `hippo backup init [--destination DIR] [--op-vault V]` | Generate the key, store it, write `[backup]`. Without `--destination`, use Google Drive for desktop |
| `hippo backup run` | Scheduled run. Used by launchd |
| `hippo backup now [--label TEXT]` | Take a checkpoint (default label `manual`) |
| `hippo backup status` | Last success, last scheduled backup, pending uploads, newest artifact of each kind |
| `hippo backup verify [--id ID]` | Decrypt, rebuild, and check an artifact (default: newest verified) |
| `hippo backup restore [options]` | Replace the live database |

`hippo backup now` exits 3 when `[backup]` is not configured. It waits up to 600 s for the backup lock. If the wait times out, it exits 1.

### verify

`verify` decrypts the artifact, rebuilds the database in a temporary location, and checks it: the SHA-256 against the manifest, `quick_check`, `user_version`, and per-table row counts. It reads the private key through `identity_ref`. `hippo doctor` warns when no verify has passed in 35 days.

### restore

```
hippo backup restore [--id ID | --before TIME] [--yes] [--extract DIR]
                     [--identity REF] [--destination DIR]
```

- `--id ID`: restore this artifact.
- `--before TIME`: newest verified backup created before TIME (RFC 3339 or `YYYY-MM-DD`, UTC).
- With neither, restore uses the newest verified backup that is not a `pre-restore` checkpoint, so a second restore does not pick the state that the first one replaced. `--id` selects any artifact, `pre-restore` checkpoints included.
- `--yes`: skip the typed confirmation.
- `--extract DIR`: write a decrypted copy into a missing or empty directory. Nothing live is touched.
- `--identity REF`: private key reference (`op://...` or `file:<path>`). Overrides `identity_ref`.
- `--destination DIR`: directory that holds `manifest.json`. Overrides `destination`.

Before you restore, close Claude Code sessions. They run `hippo-mcp`, which holds `hippo.db` open.

Restore does this:

1. Prints the chosen artifact and a loss report: rows captured after the backup, per source. Agentic session sources are marked re-ingested from their source files if those files still exist. Other sources are marked lost.
2. Asks you to type `yes`.
3. Waits up to 600 s for the backup lock and holds it until the services restart, so stopping the services cannot kill a running backup. A timeout aborts the restore before anything is stopped.
4. Decrypts the artifact, applies the delta if needed, and checks the result. A schema newer than this hippo supports is refused.
5. Stops all `com.hippo.*` LaunchAgents.
6. If another process holds `hippo.db` open, refuses and lists the PIDs. This check runs before the checkpoint, so a refusal costs no artifact.
7. Takes a pre-restore checkpoint of the current database. If `[backup]` is not configured, it saves a snapshot to `<data_dir>/backup/pre-restore-<time>.db`.
8. Checks for other processes again, then folds the live WAL into `hippo.db`.
9. Renames the restored file over `hippo.db` and starts the services.

When there is no live `hippo.db`, restore moves any leftover `hippo.db-wal` and `hippo.db-shm` to `<data_dir>/backup/pre-restore-<time>.db-wal` and `.db-shm` before the rename. SQLite applies a `-wal` next to a database whatever the database header says, so an old WAL would damage the restored file. Restore never deletes these files.

#### Damaged live database

If the live database cannot be opened or fails `quick_check`, restore says that it is damaged and skips the loss report. The confirmation prompt says that the damaged database will be replaced. Then, with the services stopped:

1. Restore refuses if another process holds `hippo.db` open.
2. It copies `hippo.db`, `hippo.db-wal`, and `hippo.db-shm` (whichever exist) byte for byte into `<data_dir>/backup/pre-restore-<time>/` (directory mode 0700, files 0600, synced). This copy replaces the pre-restore checkpoint, which cannot read a damaged database.
3. It tries to fold the WAL. If the fold fails, it moves the `-wal` and `-shm` aside as above instead of refusing.
4. It renames the restored file over `hippo.db`.

Do not move a damaged `hippo.db` away by hand before a restore. Run the restore directly.

Restore and verify remove `verify-tmp-*` and `restore-tmp-*` directories in `<data_dir>/backup/` that a killed process left behind. Each holds a plaintext database copy. A backup run removes them too.

A process that opens `hippo.db` between the WAL fold and the swap can leave the restored database damaged. Close Claude Code sessions (hippo-mcp clients) before you restore to prevent this. After a restore, run `sqlite3 ~/.local/share/hippo/hippo.db 'PRAGMA quick_check'`. If the output is not `ok`, run the restore again with `--id <ID>` of the same backup. Without `--id`, restore would not pick the same backup if a newer one exists.

If restore is interrupted, run `mise run start`. After a restore, restart MCP clients so they open the restored database.

Config files and fallback files in the artifact are not restored. Use `--extract` to read them.

Before decryption, restore checks each artifact's SHA-256 against the manifest. That check catches corruption. It does not detect a forged manifest.

### Recover on a new machine

1. Install hippo, `op`, and sign in to 1Password.
2. Wait for the destination folder to sync. It must contain `manifest.json`.
3. Run `hippo backup restore --destination <DIR> --identity '<old identity_ref>'`. The old `identity_ref` is the `op://<vault>/<item>/private_key` reference of the key that encrypted the backups.

If `config.toml` does not exist yet, `--destination` and `--identity` supply the values `restore` needs.

`hippo backup init` makes a NEW key pair. Backups made before it still need the old key: pass `--identity <old identity_ref>` to restore them. To keep using the old key instead, do not run `init`. Run `hippo backup restore --extract <DIR> ...`, then copy `recipient` and `identity_ref` from `<DIR>/config/config.toml` into the `[backup]` section of your `config.toml`, with `destination`.

### Recover a full with stock tools

Use this when hippo does not run. It works for a full or checkpoint. A delta also needs hippo, because `hippo.db.pagediff` is a hippo format.

```sh
age -d -i <(op read "<identity_ref>") FILE.tar.zst.age | zstd -d | tar -x
```

The process substitution passes the key through a pipe, so it never touches disk.

## Health

- Watchdog invariant I-21 raises an alarm when no artifact (checkpoints included, `last_success_ms`) has shipped for over 36 hours, or an artifact has waited for upload for over 24 hours. It is silent when `[backup]` is not configured.
- `hippo doctor` reports:
  - `[OK] backup: last <kind> <age> ago` for the newest shipped artifact of any kind;
  - `[!!]` when the last success is older than 36 hours or missing;
  - `[WW]` when artifacts wait in the outbox;
  - `[WW]` when an upload has been pending over 24 hours;
  - `[WW]` when no verify has passed in 35 days;
  - `[--] backup: not configured` when `[backup]` is unset.
- After the first uploaded full, doctor lists legacy `hippo.db.*.bak`, `hippo.db.pre-*`, `hippo.db.bak*`, `hippo.db.v5-backup*`, `hippo.db-wal.*`, and `hippo.db-shm.*` copies (for example `hippo.db-wal.pre-dedup-20260529.bak`) with their total size. The live `hippo.db-wal` and `hippo.db-shm` never count. Doctor does not delete them. Delete them by hand.
- Doctor does not call `op`.

Run `hippo doctor --explain` for the cause and fix of each failure.

## Security

- The private key lives only in 1Password. Hippo never writes it to disk and never passes it in argv. The stock-tools recovery above keeps it in a pipe.
- age encryption gives confidentiality only. Anyone who has the public recipient can create an artifact. A backup is not authenticated as coming from this machine.
- The manifest is not encrypted. It holds artifact ids, labels, times, versions, and hashes, not corpus content.

## Troubleshooting

| Symptom | Check |
|---|---|
| `hippo backup now` exits 3 | `[backup]` is missing `destination` or `recipient` |
| Artifacts stay in the outbox | Destination is missing or unmounted. Run `hippo backup status` |
| Upload pending over 24 hours | Drive is not syncing. Check the Drive client |
| Run fails | `~/.local/share/hippo/backup.stderr.log` |
| Restore refuses with PIDs | Close the listed processes (usually Claude Code sessions) and retry |
| `last_error` names an outbox artifact | That artifact cannot ship. New backups continue. Run `hippo backup status`, fix the cause, and the next run retries it |
| Restore says the live database is damaged | Expected for a corrupt `hippo.db`. Type `yes`. The raw copy is in `<data_dir>/backup/pre-restore-<time>/` |
