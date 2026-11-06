//! `hippo backup verify` (decrypt and check an artifact) and `hippo backup restore`
//! (replace the live database with an artifact).
//!
//! Restore never leaves `hippo.db` half-replaced: the backup is reconstructed and
//! checked in `<work_dir>/restore-tmp`, the current database is checkpointed while the
//! services are stopped, and the swap is a single `rename` on the data directory.

use super::artifact::{ArtifactKind, ArtifactMeta, extract_artifact};
use super::keys::resolve_identity;
use super::manifest::{Manifest, ManifestEntry};
use super::pagediff::apply_diff;
use super::run::{self, RunReport, RunTmp, valid_entry};
use super::ship::sha256_of;
use super::snapshot::take_snapshot;
use super::status;
use anyhow::{Context, Result, bail, ensure};
use hippo_core::config::HippoConfig;
use hippo_core::storage::EXPECTED_VERSION;
use rusqlite::{Connection, OpenFlags};
use std::fs::{self, DirBuilder, File};
use std::io::{BufRead, Write};
use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
use std::path::{Path, PathBuf};
use std::time::Duration;

/// The artifact `restore` uses.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RestoreTarget {
    /// The newest verified artifact.
    Latest,
    Id(String),
    /// The newest verified artifact created before this time (epoch ms).
    Before(i64),
}

#[derive(Debug, Clone, Default)]
pub struct RestoreOpts {
    /// Skip the typed `yes` confirmation.
    pub yes: bool,
    /// Only reconstruct the artifact into this directory (missing or empty); the live
    /// database and the services are not touched.
    pub extract: Option<PathBuf>,
    /// Overrides `[backup] identity_ref`.
    pub identity: Option<String>,
    /// Overrides `[backup] destination`.
    pub destination: Option<PathBuf>,
}

/// Stops and starts the processes that hold the live database open.
pub trait ServiceControl {
    /// May stop some services before it fails; `start` must restart those.
    fn stop(&mut self) -> Result<()>;
    fn start(&mut self) -> Result<()>;
}

/// Parses `--before`: RFC 3339, or `YYYY-MM-DD` (midnight UTC).
pub fn parse_before(s: &str) -> Result<i64> {
    if let Ok(t) = chrono::DateTime::parse_from_rfc3339(s) {
        return Ok(t.timestamp_millis());
    }
    chrono::NaiveDate::parse_from_str(s, "%Y-%m-%d")
        .ok()
        .and_then(|d| d.and_hms_opt(0, 0, 0))
        .map(|t| t.and_utc().timestamp_millis())
        .with_context(|| format!("--before {s:?}: expected RFC 3339 or YYYY-MM-DD"))
}

/// Reconstructs artifact `id` into `out_dir` (which must hold no entries of the same
/// names) and returns the database path. A delta is applied over its base Full. The
/// database must match `meta.db_sha256` (this also rejects a wrong base), pass
/// `quick_check`, and have `meta.row_counts`.
pub fn materialize(
    dest: &Path,
    manifest: &Manifest,
    id: &str,
    identity: &age::x25519::Identity,
    out_dir: &Path,
) -> Result<PathBuf> {
    let entry = find(manifest, id)?;
    DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(out_dir)
        .with_context(|| format!("create {}", out_dir.display()))?;
    // Inside out_dir so the final moves are renames; removed on every exit path.
    let scratch = RunTmp::create(out_dir.join(".materialize.tmp"))?;
    let unpacked = scratch.0.join("artifact");
    let meta = unpack(dest, entry, identity, &unpacked)?;
    let db = unpacked.join("hippo.db");
    if meta.kind == ArtifactKind::Delta {
        let base_id = meta.base_id.as_deref().context("delta has no base_id")?;
        let base_dir = scratch.0.join("base");
        let base = unpack(dest, find(manifest, base_id)?, identity, &base_dir)?;
        ensure!(
            base.kind == ArtifactKind::Full,
            "delta base {base_id} is not a full backup"
        );
        let diff = unpacked.join("hippo.db.pagediff");
        // apply_diff truncates an existing file and keeps its mode.
        fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&db)
            .with_context(|| format!("create {}", db.display()))?;
        apply_diff(&base_dir.join("hippo.db"), &diff, &db)
            .with_context(|| format!("apply {id} over {base_id}"))?;
        fs::remove_file(&diff)?;
    }
    check_db(&db, &meta)?;
    for e in fs::read_dir(&unpacked)? {
        let e = e?;
        let to = out_dir.join(e.file_name());
        fs::rename(e.path(), &to).with_context(|| format!("move to {}", to.display()))?;
    }
    Ok(out_dir.join("hippo.db"))
}

fn find<'a>(manifest: &'a Manifest, id: &str) -> Result<&'a ManifestEntry> {
    manifest
        .entries
        .iter()
        .find(|e| e.meta.id == id && valid_entry(e))
        .with_context(|| format!("no valid manifest entry for {id}"))
}

fn unpack(
    dest: &Path,
    entry: &ManifestEntry,
    identity: &age::x25519::Identity,
    dir: &Path,
) -> Result<ArtifactMeta> {
    // `valid_entry` guarantees `file` is a plain `<id>.tar.zst.age` name.
    let path = dest.join(&entry.file);
    let sum = sha256_of(&path).with_context(|| format!("read {}", path.display()))?;
    ensure!(
        run::hex(&sum) == entry.ciphertext_sha256,
        "{}: ciphertext SHA-256 does not match the manifest",
        path.display()
    );
    let meta = extract_artifact(&path, identity, dir)
        .with_context(|| format!("decrypt and unpack {}", entry.file))?;
    ensure!(
        meta.id == entry.meta.id,
        "{} holds the meta of {}",
        entry.file,
        meta.id
    );
    Ok(meta)
}

fn check_db(db: &Path, meta: &ArtifactMeta) -> Result<()> {
    let sum = sha256_of(db).with_context(|| format!("read {}", db.display()))?;
    ensure!(
        run::hex(&sum) == meta.db_sha256,
        "{}: reconstructed database does not match its SHA-256 (corrupt artifact or wrong delta base)",
        meta.id
    );
    let c = open_read_only(db)?;
    let check: String = c.pragma_query_value(None, "quick_check", |r| r.get(0))?;
    ensure!(check == "ok", "{}: quick_check failed: {check}", meta.id);
    let version: i64 = c.pragma_query_value(None, "user_version", |r| r.get(0))?;
    ensure!(
        version == meta.schema_version,
        "{}: schema version {version} does not match meta ({})",
        meta.id,
        meta.schema_version
    );
    for (table, want) in &meta.row_counts {
        ensure!(
            table
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b == b'_'),
            "{}: bad table name {table:?} in meta",
            meta.id
        );
        let n: i64 = c.query_row(&format!("SELECT COUNT(*) FROM \"{table}\""), [], |r| {
            r.get(0)
        })?;
        ensure!(
            n == *want,
            "{}: {table} has {n} rows, meta records {want}",
            meta.id
        );
    }
    Ok(())
}

/// Plain read-only open (never `open_db`, which would migrate).
fn open_read_only(path: &Path) -> Result<Connection> {
    let c = open_plain(path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    c.busy_timeout(Duration::from_millis(5000))?;
    Ok(c)
}

fn open_plain(path: &Path, flags: OpenFlags) -> Result<Connection> {
    // NOFOLLOW rejects a symlinked ancestor such as macOS /var, so resolve the parent.
    let name = path.file_name().context("database path has no file name")?;
    let path = path
        .parent()
        .unwrap_or(Path::new("."))
        .canonicalize()?
        .join(name);
    Connection::open_with_flags(&path, flags | OpenFlags::SQLITE_OPEN_NOFOLLOW)
        .with_context(|| format!("open {}", path.display()))
}

/// A scratch directory in `work` whose name no concurrent verify or restore shares.
fn unique_tmp(work: &Path, prefix: &str) -> Result<RunTmp> {
    let ms = chrono::Utc::now().timestamp_millis();
    RunTmp::create(work.join(format!("{prefix}-{}-{ms}", std::process::id())))
}

fn select<'a>(manifest: &'a Manifest, target: &RestoreTarget) -> Result<&'a ManifestEntry> {
    let before = match target {
        RestoreTarget::Id(id) => return find(manifest, id),
        RestoreTarget::Latest => i64::MAX,
        RestoreTarget::Before(t) => *t,
    };
    manifest
        .entries
        .iter()
        .filter(|e| e.verified && valid_entry(e) && e.meta.created_ms < before)
        .max_by_key(|e| e.meta.created_ms)
        .context("no verified backup matches")
}

fn identity(config: &HippoConfig, override_ref: Option<&str>) -> Result<age::x25519::Identity> {
    let r = override_ref.unwrap_or(&config.backup.identity_ref);
    ensure!(
        !r.is_empty(),
        "[backup] identity_ref is empty; set it or pass --identity op://... or --identity file:<path>"
    );
    resolve_identity(r)
}

fn work_dir(config: &HippoConfig) -> Result<PathBuf> {
    let work = config.backup_work_dir();
    DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(&work)
        .with_context(|| format!("create {}", work.display()))?;
    Ok(work)
}

/// Decrypts artifact `id` (newest verified when `None`), reconstructs its database, and
/// checks it. Records `last_verify_ms` in `status.json` on success.
pub fn verify(config: &HippoConfig, id: Option<&str>) -> Result<()> {
    ensure!(
        config.backup.is_configured(),
        "[backup] is not configured (set destination and recipient)"
    );
    let dest = Path::new(&config.backup.destination);
    ensure!(
        dest.is_dir(),
        "backup destination {} is not available",
        dest.display()
    );
    let identity = identity(config, None)?;
    let manifest = Manifest::load(dest)?;
    let target = id.map_or(RestoreTarget::Latest, |i| RestoreTarget::Id(i.to_owned()));
    let entry = select(&manifest, &target)?;
    let work = work_dir(config)?;
    let tmp = unique_tmp(&work, "verify-tmp")?;
    materialize(dest, &manifest, &entry.meta.id, &identity, &tmp.0)?;
    drop(tmp);

    // Under the run lock, so a concurrent run cannot overwrite `last_verify_ms`.
    let lock = run::open_lock(&work)?;
    run::acquire(&lock, Some(run::CHECKPOINT_LOCK_WAIT))?;
    let mut st = status::load(&work);
    st.last_verify_ms = Some(chrono::Utc::now().timestamp_millis());
    status::save(&work, &st)?;
    println!("verified {}", entry.meta.id);
    Ok(())
}

/// Rows in the live database captured at or after `since_ms`, per source: `shell`
/// (`events`), `browser` (`browser_events`), `knowledge` (`knowledge_nodes`), and
/// `agentic:<harness>` (`agentic_sessions`, counting a session that was created or grew
/// after the cutoff). Probe rows are excluded. A missing table is left out; a table
/// without the columns needed to count it is listed with `None` (skipped).
pub fn loss_report(live_db: &Connection, since_ms: i64) -> Result<Vec<(String, Option<i64>)>> {
    let columns = |table: &str| -> Result<Vec<String>> {
        let mut st = live_db.prepare("SELECT name FROM pragma_table_info(?1)")?;
        let cols = st
            .query_map([table], |r| r.get(0))?
            .collect::<rusqlite::Result<_>>()?;
        Ok(cols)
    };
    let has = |cols: &[String], c: &str| cols.iter().any(|x| x == c);
    let no_probes = |cols: &[String]| {
        if has(cols, "probe_tag") {
            " AND probe_tag IS NULL"
        } else {
            ""
        }
    };
    let mut out = Vec::new();
    for (source, table) in [
        ("shell", "events"),
        ("browser", "browser_events"),
        ("knowledge", "knowledge_nodes"),
    ] {
        let cols = columns(table)?;
        if cols.is_empty() {
            continue;
        }
        let n = if has(&cols, "created_at") {
            let sql = format!(
                "SELECT COUNT(*) FROM {table} WHERE created_at >= ?1{}",
                no_probes(&cols)
            );
            Some(live_db.query_row(&sql, [since_ms], |r| r.get(0))?)
        } else {
            None
        };
        out.push((source.to_owned(), n));
    }
    let cols = columns("agentic_sessions")?;
    if cols.is_empty() {
        return Ok(out);
    }
    if !["harness", "created_at", "end_time"]
        .iter()
        .all(|c| has(&cols, c))
    {
        out.push(("agentic".to_owned(), None));
    } else {
        let sql = format!(
            "SELECT harness, COUNT(*) FROM agentic_sessions
             WHERE MAX(created_at, end_time) >= ?1{} GROUP BY harness ORDER BY harness",
            no_probes(&cols)
        );
        let mut st = live_db.prepare(&sql)?;
        for row in st.query_map([since_ms], |r| {
            Ok((
                format!("agentic:{}", r.get::<_, String>(0)?),
                Some(r.get(1)?),
            ))
        })? {
            out.push(row?);
        }
    }
    Ok(out)
}

fn when(ms: i64) -> String {
    chrono::DateTime::from_timestamp_millis(ms).map_or_else(|| ms.to_string(), |t| t.to_rfc3339())
}

/// Restores `target` over the live database, or with `opts.extract` only reconstructs
/// it. `input` supplies the confirmation line when `opts.yes` is false. On an error after
/// `services.stop()`, the services are still restarted and both errors are reported.
pub fn restore_with(
    config: &HippoConfig,
    target: RestoreTarget,
    opts: &RestoreOpts,
    services: &mut dyn ServiceControl,
    input: &mut dyn BufRead,
) -> Result<()> {
    let dest = opts
        .destination
        .clone()
        .unwrap_or_else(|| PathBuf::from(&config.backup.destination));
    ensure!(
        !dest.as_os_str().is_empty(),
        "no backup destination: set [backup] destination or pass --destination"
    );
    ensure!(
        dest.is_dir(),
        "backup destination {} is not available",
        dest.display()
    );
    let manifest = Manifest::load(&dest)?;
    let entry = select(&manifest, &target)?;
    let id = entry.meta.id.as_str();

    if let Some(dir) = &opts.extract {
        let empty = match fs::read_dir(dir) {
            Ok(mut rd) => rd.next().is_none(),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => true,
            Err(e) => return Err(e).with_context(|| format!("read {}", dir.display())),
        };
        ensure!(empty, "--extract directory {} is not empty", dir.display());
        let identity = identity(config, opts.identity.as_deref())?;
        let db = materialize(&dest, &manifest, id, &identity, dir)?;
        println!("extracted {id} to {}", db.display());
        return Ok(());
    }

    let newer = |v: i64| {
        format!(
            "{id} has schema version {v}, newer than this hippo supports ({EXPECTED_VERSION}); \
             upgrade hippo first"
        )
    };
    ensure!(
        entry.meta.schema_version <= EXPECTED_VERSION,
        "{}",
        newer(entry.meta.schema_version)
    );
    let identity = identity(config, opts.identity.as_deref())?;
    let live = config.db_path();
    println!(
        "Restore {id} ({:?}, created {}) over {}",
        entry.meta.kind,
        when(entry.meta.created_ms),
        live.display()
    );
    if live.exists() {
        println!("Rows captured since that backup:");
        for (source, n) in loss_report(&open_read_only(&live)?, entry.meta.created_ms)? {
            let fate = if source.starts_with("agentic") {
                "re-ingested from source files if still present"
            } else {
                "lost"
            };
            match n {
                Some(n) => println!("  {source}: {n} ({fate})"),
                None => println!("  {source}: skipped (its table has no capture-time column)"),
            }
        }
    }
    if !opts.yes {
        print!("Type yes to replace the live database: ");
        std::io::stdout().flush()?;
        let mut line = String::new();
        input.read_line(&mut line)?;
        ensure!(line.trim() == "yes", "restore aborted");
    }

    let work = work_dir(config)?;
    let tmp = unique_tmp(&work, "restore-tmp")?;
    let db = materialize(&dest, &manifest, id, &identity, &tmp.0)?;
    // The artifact's own meta is authoritative; `check_db` matched it to the file.
    let version: i64 =
        open_read_only(&db)?.pragma_query_value(None, "user_version", |r| r.get(0))?;
    ensure!(version <= EXPECTED_VERSION, "{}", newer(version));

    let swapped = services.stop().and_then(|()| {
        println!(
            "hippo services are stopped. If this restore is interrupted, restart them with `mise run start`."
        );
        replace_live(config, &work, &db)
    });
    let started = services.start();
    drop(tmp);
    match (swapped, started) {
        (Ok(()), Ok(())) => {}
        (Ok(()), Err(e)) => {
            return Err(e.context(format!(
                "restored {id}, but restarting the services failed; run `mise run start`"
            )));
        }
        (Err(e), Ok(())) => return Err(e),
        (Err(e), Err(s)) => {
            bail!("restore failed: {e:#}; restarting the services also failed: {s:#}")
        }
    }
    println!(
        "Restored {id}. Restart MCP clients (for example Claude Code) so they open the restored database."
    );
    Ok(())
}

/// Checkpoints the live database, folds its WAL into it, then renames `db` over it.
/// Services must be stopped. Every refusal happens before the rename.
fn replace_live(config: &HippoConfig, work: &Path, db: &Path) -> Result<()> {
    let live = config.db_path();
    let ts = chrono::Utc::now().format("%Y%m%dT%H%M%SZ").to_string();
    let previous = if live.exists() {
        let saved = save_current(config, work, &live, &ts)?;
        fold_live_wal(&live)?;
        saved
    } else {
        // SQLite applies a -wal found next to a database whatever its header says.
        move_sidecars_aside(&live, work, &ts)?;
        "none (there was no live database)".to_owned()
    };
    File::open(db)
        .and_then(|f| f.sync_all())
        .with_context(|| format!("sync {}", db.display()))?;
    let data_dir = &config.storage.data_dir;
    let staged = data_dir.join("hippo.db.restoring");
    remove_if_present(&staged)?;
    fs::rename(db, &staged).with_context(|| format!("move to {}", staged.display()))?;
    fs::rename(&staged, &live).with_context(|| format!("rename over {}", live.display()))?;
    File::open(data_dir)
        .and_then(|d| d.sync_all())
        .with_context(|| {
            format!(
                "sync {}: hippo.db was already replaced by the restored backup; \
                 the previous database is {previous}",
                data_dir.display()
            )
        })
}

/// Saves the live database before it is replaced and describes where it went.
fn save_current(config: &HippoConfig, work: &Path, live: &Path, ts: &str) -> Result<String> {
    if !config.backup.is_configured() {
        let saved = work.join(format!("pre-restore-{ts}.db"));
        take_snapshot(live, &saved).context("pre-restore snapshot")?;
        println!("Saved the current database to {}.", saved.display());
        return Ok(saved.display().to_string());
    }
    match run::checkpoint(config, "pre-restore").context("pre-restore checkpoint")? {
        RunReport::Done { id, shipped, .. } => {
            let place = if shipped { "shipped" } else { "in the outbox" };
            println!("Checkpointed the current database as {id} ({place}).");
            Ok(format!("checkpoint {id}"))
        }
        other => bail!("pre-restore checkpoint did not run: {other:?}"),
    }
}

fn sidecar(live: &Path, suffix: &str) -> PathBuf {
    let mut p = live.as_os_str().to_owned();
    p.push(suffix);
    PathBuf::from(p)
}

/// Moves the `-wal` and `-shm` of `live` (whichever exist) into `work` as
/// `pre-restore-<ts>.db-wal` and `.db-shm`. Never deletes or replaces a file.
fn move_sidecars_aside(live: &Path, work: &Path, ts: &str) -> Result<()> {
    for suffix in ["-wal", "-shm"] {
        let from = sidecar(live, suffix);
        if fs::symlink_metadata(&from).is_err() {
            continue;
        }
        let to = work.join(format!("pre-restore-{ts}.db{suffix}"));
        ensure!(
            fs::symlink_metadata(&to).is_err(),
            "{} already exists; refusing to replace it",
            to.display()
        );
        fs::rename(&from, &to)
            .with_context(|| format!("move {} to {}", from.display(), to.display()))?;
        println!("Moved {} to {}.", from.display(), to.display());
    }
    Ok(())
}

const STILL_OPEN: &str = "close hippo-mcp clients (Claude Code sessions) and retry";

/// Refuses while another process or connection has the live database open, then
/// checkpoints its WAL into the main file and deletes it, so no old WAL frame can be
/// applied to the restored file.
fn fold_live_wal(live: &Path) -> Result<()> {
    let (wal, shm) = (sidecar(live, "-wal"), sidecar(live, "-shm"));
    let present: Vec<&Path> = [live, wal.as_path(), shm.as_path()]
        .into_iter()
        .filter(|p| p.exists())
        .collect();
    match std::process::Command::new("lsof")
        .arg("-t")
        .arg("--")
        .args(&present)
        .output()
    {
        Ok(out) => {
            let me = std::process::id();
            let pids: Vec<&str> = std::str::from_utf8(&out.stdout)
                .unwrap_or_default()
                .lines()
                .map(str::trim)
                .filter(|l| l.parse::<u32>().is_ok_and(|p| p != me))
                .collect();
            ensure!(
                pids.is_empty(),
                "the live database is still open by process {}; {STILL_OPEN}",
                pids.join(", ")
            );
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            eprintln!("warning: lsof not found; relying on SQLite locking to detect other openers");
        }
        Err(e) => return Err(e).context("run lsof"),
    }

    let c = open_plain(live, OpenFlags::SQLITE_OPEN_READ_WRITE)?;
    c.busy_timeout(Duration::from_millis(200))?;
    let mode: String = match c.query_row("PRAGMA journal_mode=DELETE", [], |r| r.get(0)) {
        Ok(m) => m,
        Err(rusqlite::Error::SqliteFailure(f, _))
            if matches!(
                f.code,
                rusqlite::ErrorCode::DatabaseBusy | rusqlite::ErrorCode::DatabaseLocked
            ) =>
        {
            bail!("the live database is still open by another connection; {STILL_OPEN}")
        }
        Err(e) => return Err(e).context("fold the live WAL (PRAGMA journal_mode=DELETE)"),
    };
    ensure!(
        mode.eq_ignore_ascii_case("delete"),
        "the live database is still open (journal_mode stayed {mode}); {STILL_OPEN}"
    );
    c.close()
        .map_err(|(_, e)| e)
        .context("close the live database")?;
    ensure!(
        !wal.exists(),
        "{} still exists after folding it; refusing to swap",
        wal.display()
    );
    remove_if_present(&shm)
}

fn remove_if_present(path: &Path) -> Result<()> {
    match fs::remove_file(path) {
        Err(e) if e.kind() != std::io::ErrorKind::NotFound => {
            Err(e).with_context(|| format!("remove {}", path.display()))
        }
        _ => Ok(()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeMap;

    fn meta_for(db: &Path) -> ArtifactMeta {
        ArtifactMeta {
            id: "hippo-full-20261105T000000Z".into(),
            kind: ArtifactKind::Full,
            label: None,
            created_ms: 1,
            base_id: None,
            hippo_version: "0".into(),
            schema_version: 7,
            db_sha256: run::hex(&sha256_of(db).unwrap()),
            row_counts: BTreeMap::from([("events".to_owned(), 2)]),
        }
    }

    #[test]
    fn check_db_rejects_hash_count_and_version_mismatch() {
        let dir = tempfile::tempdir().unwrap();
        let db = dir.path().join("hippo.db");
        let c = Connection::open(&db).unwrap();
        c.execute_batch(
            "CREATE TABLE events(id INTEGER PRIMARY KEY);
             INSERT INTO events DEFAULT VALUES; INSERT INTO events DEFAULT VALUES;
             PRAGMA user_version = 7;",
        )
        .unwrap();
        drop(c);
        let good = meta_for(&db);
        check_db(&db, &good).unwrap();

        let bad_hash = ArtifactMeta {
            db_sha256: "00".repeat(32),
            ..good.clone()
        };
        let err = check_db(&db, &bad_hash).unwrap_err();
        assert!(format!("{err:#}").contains("SHA-256"), "{err:#}");

        let bad_rows = ArtifactMeta {
            row_counts: BTreeMap::from([("events".to_owned(), 3)]),
            ..good.clone()
        };
        assert!(check_db(&db, &bad_rows).is_err());
        let bad_table = ArtifactMeta {
            row_counts: BTreeMap::from([("x\"; DROP".to_owned(), 0)]),
            ..good.clone()
        };
        assert!(check_db(&db, &bad_table).is_err());
        let bad_version = ArtifactMeta {
            schema_version: 8,
            ..good
        };
        assert!(check_db(&db, &bad_version).is_err());
    }

    #[test]
    fn select_picks_newest_verified_valid_before_cutoff() {
        let mut m = Manifest::default();
        for (id, ms, verified) in [
            ("hippo-full-a", 10, true),
            ("hippo-delta-b", 20, true),
            ("hippo-delta-c", 30, false),
        ] {
            let mut e = crate::backup::manifest::tests::entry(id);
            e.meta.created_ms = ms;
            e.verified = verified;
            m.entries.push(e);
        }
        let mut bad = crate::backup::manifest::tests::entry("hippo-full-z");
        bad.meta.created_ms = 40;
        bad.file = "../escape.tar.zst.age".into();
        m.entries.push(bad);

        let id = |t| select(&m, &t).unwrap().meta.id.clone();
        assert_eq!(id(RestoreTarget::Latest), "hippo-delta-b");
        assert_eq!(id(RestoreTarget::Before(20)), "hippo-full-a");
        assert_eq!(
            id(RestoreTarget::Id("hippo-delta-c".into())),
            "hippo-delta-c"
        );
        assert!(select(&m, &RestoreTarget::Before(10)).is_err());
        assert!(select(&m, &RestoreTarget::Id("hippo-full-z".into())).is_err());
    }

    #[test]
    fn parse_before_accepts_rfc3339_and_date() {
        assert_eq!(parse_before("1970-01-02").unwrap(), 86_400_000);
        assert_eq!(parse_before("1970-01-01T00:00:01Z").unwrap(), 1000);
        assert!(parse_before("yesterday").is_err());
    }
}
