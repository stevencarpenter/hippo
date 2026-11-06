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

1. Create the destination directory yourself. Hippo never creates it. A missing directory usually means the sync client is not mounted.
2. Run `hippo backup init --destination <DIR> [--op-vault <VAULT>]`.
3. Run `hippo backup now --label first`.
4. Run `hippo backup status`.
5. Run `hippo daemon install` if `com.hippo.backup` is not yet loaded.

`init`:

- generates an age X25519 key pair;
- stores the private key in 1Password through the `op` CLI;
- reads the item back and compares it to the generated key;
- appends `[backup]` (`destination`, `recipient`, `identity_ref`) to `config.toml`.

`init` fails if `config.toml` already has a `[backup]` section or the destination is not an existing directory.

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

launchd job `com.hippo.backup` runs `hippo backup run` every hour. The run skips when a backup succeeded in the last 20 hours, so about one backup is taken per day. A shipped checkpoint also counts as a recent success and delays the next scheduled run.

| Tier | When | Contents |
|---|---|---|
| Full | First scheduled run of each ISO week | Standalone snapshot |
| Delta | Other scheduled runs | Page-level diff against that week's full. Never chained, so a restore needs at most two files |
| Checkpoint | `hippo backup now`, and automatic checkpoints | Standalone snapshot with a label |

Each run:

1. snapshots the database and runs `quick_check`;
2. compresses with zstd level 9 and encrypts with age;
3. writes the artifact to the local outbox, `<data_dir>/backup/` (the legacy `<data_dir>/backups/` is not used);
4. ships it to the destination under a temporary name, then renames it;
5. reads it back and compares the SHA-256;
6. updates `manifest.json` in the destination, then prunes.

Capture is not blocked by a run. If the destination is unavailable, the artifact stays in the outbox and ships on the next run.

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
| `hippo backup init --destination DIR [--op-vault V]` | Generate the key, store it, write `[backup]` |
| `hippo backup run` | Scheduled run. Used by launchd |
| `hippo backup now [--label TEXT]` | Take a checkpoint (default label `manual`) |
| `hippo backup status` | Last run, pending uploads, newest artifact of each kind |
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
- With neither, restore uses the newest verified backup.
- `--yes`: skip the typed confirmation.
- `--extract DIR`: write a decrypted copy into a missing or empty directory. Nothing live is touched.
- `--identity REF`: private key reference (`op://...` or `file:<path>`). Overrides `identity_ref`.
- `--destination DIR`: directory that holds `manifest.json`. Overrides `destination`.

Before you restore, close Claude Code sessions. They run `hippo-mcp`, which holds `hippo.db` open.

Restore does this:

1. Prints the chosen artifact and a loss report: rows captured after the backup, per source. Agentic session sources are marked re-ingested from their source files if those files still exist. Other sources are marked lost.
2. Asks you to type `yes`.
3. Decrypts the artifact, applies the delta if needed, and checks the result. A schema newer than this hippo supports is refused.
4. Stops all `com.hippo.*` LaunchAgents.
5. Takes a pre-restore checkpoint of the current database. If `[backup]` is not configured, it saves a snapshot to `<data_dir>/backup/pre-restore-<time>.db`.
6. Folds the live WAL into `hippo.db`. If another process holds `hippo.db` open, restore refuses and lists the PIDs.
7. Renames the restored file over `hippo.db` and starts the services.

A process that opens `hippo.db` between the WAL fold and the swap can leave the restored database damaged. Close Claude Code sessions (hippo-mcp clients) before you restore to prevent this. After a restore, run `sqlite3 ~/.local/share/hippo/hippo.db 'PRAGMA quick_check'`. If the output is not `ok`, run the restore again from the same backup.

If restore is interrupted, run `mise run start`. After a restore, restart MCP clients so they open the restored database.

Config files and fallback files in the artifact are not restored. Use `--extract` to read them.

Before decryption, restore checks each artifact's SHA-256 against the manifest. That check catches corruption. It does not detect a forged manifest.

### Recover on a new machine

1. Install hippo, `op`, and sign in to 1Password.
2. Wait for the destination folder to sync. It must contain `manifest.json`.
3. Run `hippo backup restore --destination <DIR> --identity 'op://<vault>/<item>/<field>'`.

If `config.toml` does not exist yet, `--destination` and `--identity` supply the values `restore` needs.

### Recover a full with stock tools

Use this when hippo does not run. It works for a full or checkpoint. A delta also needs hippo, because `hippo.db.pagediff` is a hippo format.

```sh
op read 'op://<vault>/<item>/<field>' > key
age -d -i key FILE.tar.zst.age | zstd -d | tar -x
rm key
```

The key file holds the private key in plaintext. Delete `key` as soon as the command ends.

## Health

- Watchdog invariant I-21 raises an alarm when no backup has succeeded for over 36 hours, or an artifact has waited for upload for over 24 hours. It is silent when `[backup]` is not configured.
- `hippo doctor` reports:
  - `[!!]` when the last success is older than 36 hours or missing;
  - `[WW]` when artifacts wait in the outbox;
  - `[WW]` when an upload has been pending over 24 hours;
  - `[WW]` when no verify has passed in 35 days;
  - `[--] backup: not configured` when `[backup]` is unset.
- After the first uploaded full, doctor lists legacy `hippo.db.*.bak`, `hippo.db.pre-*`, `hippo.db.bak*`, and `hippo.db.v5-backup*` copies with their total size. Doctor does not delete them. Delete them by hand.
- Doctor does not call `op`.

Run `hippo doctor --explain` for the cause and fix of each failure.

## Security

- The private key lives only in 1Password. Hippo never writes it to disk and never passes it in argv. The stock-tools recovery above writes it to a file, which you must delete.
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
