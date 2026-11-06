//! Backup key setup. The age private key lives in 1Password and is never written to disk,
//! logged, or passed on a command line.

use std::ffi::OsString;
use std::io::{BufRead, IsTerminal, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};

use age::secrecy::{ExposeSecret, SecretString};
use anyhow::{Context, Result, bail};

const FIELD: &str = "private_key";

/// Why `KeyStore::store` failed. The two cases need different handling by the caller.
#[derive(Debug)]
pub enum StoreError {
    /// The store was unreachable or rejected the request; nothing was saved.
    Unavailable(String),
    /// The store may have saved the key but the result is unusable; the operator must check.
    MaybeCreated(String),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Unavailable(m) | Self::MaybeCreated(m) => f.write_str(m),
        }
    }
}

impl std::error::Error for StoreError {}

/// Secret store that holds the private key. Tests inject a fake; production uses `OnePassword`.
pub trait KeyStore {
    /// Saves `key` and returns the `identity_ref` that `read` resolves.
    fn store(
        &self,
        title: &str,
        vault: Option<&str>,
        key: &SecretString,
    ) -> Result<String, StoreError>;
    fn read(&self, reference: &str) -> Result<SecretString>;
}

/// The `op` CLI at an explicit program path ("op" in production, a fake in tests).
pub struct OnePassword {
    program: OsString,
}

impl Default for OnePassword {
    fn default() -> Self {
        Self {
            program: OsString::from("op"),
        }
    }
}

impl OnePassword {
    #[cfg(test)]
    fn with_program(program: impl Into<OsString>) -> Self {
        Self {
            program: program.into(),
        }
    }

    fn run(&self, args: &[&str], stdin: Option<&[u8]>) -> Result<Vec<u8>> {
        let mut cmd = Command::new(&self.program);
        cmd.args(args)
            .stdin(if stdin.is_some() {
                Stdio::piped()
            } else {
                Stdio::null()
            })
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        let mut child = cmd.spawn().context("failed to run `op`")?;
        if let Some(bytes) = stdin {
            // The secret goes over a pipe, never argv. Dropping the handle closes it.
            child
                .stdin
                .take()
                .context("op stdin unavailable")?
                .write_all(bytes)
                .context("failed to write to op stdin")?;
        }
        let out = child
            .wait_with_output()
            .context("failed to wait for `op`")?;
        if !out.status.success() {
            bail!(
                "op {} failed: {}",
                args.first().copied().unwrap_or(""),
                String::from_utf8_lossy(&out.stderr).trim()
            );
        }
        Ok(out.stdout)
    }
}

impl KeyStore for OnePassword {
    fn store(
        &self,
        title: &str,
        vault: Option<&str>,
        key: &SecretString,
    ) -> Result<String, StoreError> {
        let template = serde_json::json!({
            "title": title,
            "category": "SECURE_NOTE",
            "fields": [{
                "id": FIELD, "label": FIELD, "type": "CONCEALED",
                "value": key.expose_secret(),
            }],
        });
        let body =
            serde_json::to_vec(&template).map_err(|e| StoreError::Unavailable(e.to_string()))?;
        let mut args = vec!["item", "create", "--format", "json"];
        if let Some(v) = vault {
            args.extend(["--vault", v]);
        }
        args.push("-");
        let out = self
            .run(&args, Some(&body))
            .map_err(|e| StoreError::Unavailable(format!("{e:#}")))?;
        // `op` exited 0, so the item exists even if its output is unusable.
        let maybe = |detail: String| {
            StoreError::MaybeCreated(format!(
                "op created an item titled \"{title}\" but its output was unusable ({detail}); \
                 an item may exist in 1Password, check for it and delete it before retrying"
            ))
        };
        let created: serde_json::Value =
            serde_json::from_slice(&out).map_err(|_| maybe("not JSON".into()))?;
        let id = created["id"]
            .as_str()
            .ok_or_else(|| maybe("no item id".into()))?;
        let vault_id = created["vault"]["id"]
            .as_str()
            .ok_or_else(|| maybe(format!("no vault id, item id {id}")))?;
        Ok(format!("op://{vault_id}/{id}/{FIELD}"))
    }

    fn read(&self, reference: &str) -> Result<SecretString> {
        let out = self.run(&["read", reference], None)?;
        Ok(SecretString::from(
            String::from_utf8(out).context("op read returned non-UTF-8 data")?,
        ))
    }
}

/// Parses the first `AGE-SECRET-KEY-` line (age-keygen files carry comment lines).
/// Errors never include the input.
fn parse_identity(text: &SecretString) -> Result<age::x25519::Identity> {
    text.expose_secret()
        .lines()
        .map(str::trim)
        .find(|l| l.starts_with("AGE-SECRET-KEY-"))
        .and_then(|l| l.parse().ok())
        .context("value does not contain a valid age secret key")
}

/// Fetches the private key named by `identity_ref`: `op://...` via `op read`, or
/// `file:<path>` for an operator-supplied key file.
pub fn resolve_identity(identity_ref: &str) -> Result<age::x25519::Identity> {
    if identity_ref.starts_with("op://") {
        parse_identity(&OnePassword::default().read(identity_ref)?)
            .with_context(|| format!("1Password item {identity_ref}"))
    } else if let Some(path) = identity_ref.strip_prefix("file:") {
        let text = std::fs::read_to_string(path)
            .with_context(|| format!("failed to read identity file {path}"))?;
        parse_identity(&SecretString::from(text)).with_context(|| format!("identity file {path}"))
    } else {
        bail!("unsupported identity_ref (expected op://... or file:<path>)")
    }
}

/// Generates the key pair, stores the private key, and appends `[backup]` to `config_path`.
/// Without `destination`, uses (and creates) `Backups/hippo` in the Google Drive for
/// desktop folder, asking which account when there are several.
pub fn init(config_path: &Path, destination: Option<&Path>, op_vault: Option<&str>) -> Result<()> {
    let host = hostname::get()
        .map(|h| h.to_string_lossy().into_owned())
        .unwrap_or_else(|_| "unknown-host".to_string());
    let mut stdin = std::io::stdin().lock();
    let stdout_is_terminal = std::io::stdout().is_terminal();
    let detected = match destination {
        Some(_) => None,
        None => {
            let root = dirs::home_dir()
                .context("cannot determine home directory")?
                .join("Library/CloudStorage");
            let interactive = stdout_is_terminal && std::io::stdin().is_terminal();
            Some(default_destination(
                &root,
                interactive,
                &mut stdin,
                &mut std::io::stdout(),
            )?)
        }
    };
    init_with(
        &OnePassword::default(),
        &mut stdin,
        &mut std::io::stdout(),
        stdout_is_terminal,
        config_path,
        detected
            .as_deref()
            .or(destination)
            .context("no destination")?,
        detected.is_some(),
        op_vault,
        &host,
    )
}

/// `<mount>/My Drive/Backups/hippo` for each Google Drive for desktop mount in
/// `cloud_storage` (a `GoogleDrive-*` directory that holds `My Drive`), sorted.
fn drive_candidates(cloud_storage: &Path) -> Vec<PathBuf> {
    let Ok(rd) = std::fs::read_dir(cloud_storage) else {
        return Vec::new();
    };
    let mut v: Vec<PathBuf> = rd
        .flatten()
        .filter(|e| e.file_name().to_string_lossy().starts_with("GoogleDrive-"))
        .map(|e| e.path().join("My Drive"))
        .filter(|p| p.is_dir())
        .map(|p| p.join("Backups/hippo"))
        .collect();
    v.sort();
    v
}

/// The destination `init` uses when `--destination` is omitted. Creates nothing.
fn default_destination(
    cloud_storage: &Path,
    interactive: bool,
    input: &mut dyn BufRead,
    out: &mut dyn Write,
) -> Result<PathBuf> {
    let candidates = drive_candidates(cloud_storage);
    // `<mount>/My Drive/Backups/hippo`: the mount name holds the account.
    let account = |p: &Path| {
        p.ancestors()
            .nth(3)
            .and_then(Path::file_name)
            .map(|n| n.to_string_lossy().into_owned())
            .unwrap_or_default()
    };
    let chosen = match candidates.as_slice() {
        [] => bail!(
            "Google Drive for desktop was not found (no GoogleDrive-*/My Drive in {}); \
             pass --destination",
            cloud_storage.display()
        ),
        [one] => one.clone(),
        several if !interactive => {
            let list: Vec<String> = several
                .iter()
                .map(|p| format!("  {}", p.display()))
                .collect();
            bail!(
                "several Google Drive accounts found; pass --destination with one of:\n{}",
                list.join("\n")
            )
        }
        several => {
            writeln!(out, "Google Drive accounts:")?;
            for (i, p) in several.iter().enumerate() {
                writeln!(out, "  {}. {}: {}", i + 1, account(p), p.display())?;
            }
            write!(out, "Back up to which one (number): ")?;
            out.flush()?;
            let mut line = String::new();
            input.read_line(&mut line)?;
            let n: usize = line.trim().parse().unwrap_or(0);
            several.get(n.wrapping_sub(1)).cloned().with_context(|| {
                format!(
                    "aborted: {:?} is not a listed number; nothing was changed",
                    line.trim()
                )
            })?
        }
    };
    writeln!(out, "destination: {}", chosen.display())?;
    Ok(chosen)
}

#[expect(
    clippy::too_many_arguments,
    reason = "every argument is an injected seam (store, io, tty flag); a struct would only rename them"
)]
fn init_with(
    store: &dyn KeyStore,
    input: &mut dyn BufRead,
    out: &mut dyn Write,
    stdout_is_terminal: bool,
    config_path: &Path,
    destination: &Path,
    create_destination: bool,
    op_vault: Option<&str>,
    host: &str,
) -> Result<()> {
    let existing = match std::fs::read_to_string(config_path) {
        Ok(s) => s,
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(e) => {
            return Err(e).with_context(|| format!("failed to read {}", config_path.display()));
        }
    };
    // Parse errors can quote file content, so report only the path.
    let table: toml::Table = toml::from_str(&existing)
        .map_err(|_| anyhow::anyhow!("{} is not valid TOML", config_path.display()))?;
    if table.contains_key("backup") {
        bail!(
            "{} already has a [backup] section; edit it by hand",
            config_path.display()
        );
    }
    if create_destination {
        std::fs::create_dir_all(destination)
            .with_context(|| format!("create {}", destination.display()))?;
    }
    if !destination.is_dir() {
        bail!(
            "destination {} is not an existing directory",
            destination.display()
        );
    }
    // Absolute, because a relative path would resolve against launchd's working directory.
    // Canonical, so a symlink cannot hide a Google Drive path from `is_drive_fs`.
    let destination = destination
        .canonicalize()
        .with_context(|| format!("cannot resolve {}", destination.display()))?;
    let dest_str = destination
        .to_str()
        .context("destination is not valid UTF-8")?;

    let identity = age::x25519::Identity::generate();
    let recipient = identity.to_public();
    let key = identity.to_string();
    drop(identity);

    let title = format!("hippo backup key ({host})");
    let identity_ref = match store.store(&title, op_vault, &key) {
        Ok(reference) => {
            let back = store.read(&reference).with_context(|| {
                format!("item was created but could not be read back ({reference})")
            })?;
            let same = parse_identity(&back)
                .map(|i| i.to_public().to_string() == recipient.to_string())
                .unwrap_or(false);
            if !same {
                bail!(
                    "1Password read-back does not match the generated key ({reference}); config not written"
                );
            }
            reference
        }
        Err(e @ StoreError::MaybeCreated(_)) => bail!("{e}"),
        Err(e) => {
            if !stdout_is_terminal {
                bail!(
                    "1Password storage failed ({e}) and stdout is not a terminal; \
                     run `hippo backup init` in an interactive terminal so the key is not written to a file"
                );
            }
            eprintln!("hippo backup: 1Password storage failed ({e}); manual storage required");
            writeln!(
                out,
                "Store this private key somewhere safe. It is shown only once.\n"
            )?;
            writeln!(out, "{}\n", key.expose_secret())?;
            write!(out, "Type 'stored' once it is saved: ")?;
            out.flush()?;
            let mut answer = String::new();
            input.read_line(&mut answer)?;
            if answer.trim() != "stored" {
                bail!("aborted: key not confirmed stored; config not written");
            }
            String::new()
        }
    };
    drop(key);

    let quote = |s: &str| toml::Value::String(s.to_string()).to_string();
    let lead = if existing.is_empty() || existing.ends_with('\n') {
        ""
    } else {
        "\n"
    };
    let block = format!(
        "{lead}\n[backup]\ndestination = {}\nrecipient = {}\nidentity_ref = {}\n",
        quote(dest_str),
        quote(&recipient.to_string()),
        quote(&identity_ref),
    );
    // Append mode follows symlinks and never rewrites existing content.
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(config_path)
        .and_then(|mut f| f.write_all(block.as_bytes()))
        .with_context(|| format!("failed to append to {}", config_path.display()))?;
    writeln!(out, "\nbackup configured; recipient {recipient}")?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::RefCell;
    use std::io::Cursor;

    /// Stand-in for 1Password. `mode` picks the behavior; never touches the real `op`.
    struct FakeStore {
        mode: &'static str,
        stored: RefCell<Option<SecretString>>,
        argv_title: RefCell<String>,
    }

    impl FakeStore {
        fn new(mode: &'static str) -> Self {
            Self {
                mode,
                stored: RefCell::new(None),
                argv_title: RefCell::new(String::new()),
            }
        }
    }

    impl KeyStore for FakeStore {
        fn store(
            &self,
            title: &str,
            _vault: Option<&str>,
            key: &SecretString,
        ) -> Result<String, StoreError> {
            if self.mode == "fail" {
                return Err(StoreError::Unavailable("op unavailable".into()));
            }
            if self.mode == "maybe" {
                return Err(StoreError::MaybeCreated(format!(
                    "an item may exist titled \"{title}\""
                )));
            }
            *self.argv_title.borrow_mut() = title.to_string();
            *self.stored.borrow_mut() = Some(key.clone());
            Ok("op://V1/I1/private_key".to_string())
        }
        fn read(&self, reference: &str) -> Result<SecretString> {
            assert_eq!(reference, "op://V1/I1/private_key");
            if self.mode == "wrong" {
                return Ok(age::x25519::Identity::generate().to_string());
            }
            Ok(self.stored.borrow().clone().unwrap())
        }
    }

    fn setup(existing: &str) -> (tempfile::TempDir, PathBuf, PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let cfg = dir.path().join("config.toml");
        std::fs::write(&cfg, existing).unwrap();
        let dest = dir.path().join("dest");
        std::fs::create_dir(&dest).unwrap();
        (dir, cfg, dest)
    }

    fn run(store: &dyn KeyStore, cfg: &Path, dest: &Path, input: &str) -> (Result<()>, String) {
        run_tty(store, cfg, dest, input, true)
    }

    fn run_tty(
        store: &dyn KeyStore,
        cfg: &Path,
        dest: &Path,
        input: &str,
        tty: bool,
    ) -> (Result<()>, String) {
        let mut out = Vec::new();
        let r = init_with(
            store,
            &mut Cursor::new(input.to_string()),
            &mut out,
            tty,
            cfg,
            dest,
            false,
            None,
            "testhost",
        );
        (r, String::from_utf8(out).unwrap())
    }

    #[test]
    fn resolve_identity_file_ref() {
        let id = age::x25519::Identity::generate();
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("key.txt");
        std::fs::write(
            &p,
            format!("# created: x\n{}\n", id.to_string().expose_secret()),
        )
        .unwrap();
        let got = resolve_identity(&format!("file:{}", p.display())).unwrap();
        assert_eq!(got.to_public().to_string(), id.to_public().to_string());
    }

    #[test]
    fn resolve_identity_rejects_unknown_scheme() {
        assert!(resolve_identity("vault://x").is_err());
        assert!(resolve_identity("").is_err());
    }

    #[test]
    fn resolve_identity_file_error_hides_contents() {
        let dir = tempfile::tempdir().unwrap();
        let p = dir.path().join("key.txt");
        std::fs::write(&p, "super-secret-garbage").unwrap();
        let err = format!(
            "{:#}",
            resolve_identity(&format!("file:{}", p.display()))
                .err()
                .unwrap()
        );
        assert!(!err.contains("super-secret-garbage"));
    }

    #[test]
    fn init_refuses_when_configured() {
        let (_d, cfg, dest) = setup("[backup]\ndestination = \"/x\"\nrecipient = \"age1x\"\n");
        let before = std::fs::read_to_string(&cfg).unwrap();
        let (r, _) = run(&FakeStore::new("ok"), &cfg, &dest, "stored\n");
        assert!(r.is_err());
        assert_eq!(std::fs::read_to_string(&cfg).unwrap(), before);
    }

    #[test]
    fn init_refuses_existing_empty_backup_table() {
        let (_d, cfg, dest) = setup("[backup]\n");
        let (r, _) = run(&FakeStore::new("ok"), &cfg, &dest, "stored\n");
        assert!(r.is_err());
    }

    #[test]
    fn init_refuses_missing_destination() {
        let (d, cfg, _dest) = setup("");
        let (r, _) = run(
            &FakeStore::new("ok"),
            &cfg,
            &d.path().join("nope"),
            "stored\n",
        );
        assert!(r.is_err());
        assert_eq!(std::fs::read_to_string(&cfg).unwrap(), "");
    }

    #[test]
    fn init_with_store_writes_ref_and_never_prints_key() {
        let (_d, cfg, dest) = setup("[a]\nx = 1");
        let store = FakeStore::new("ok");
        let (r, out) = run(&store, &cfg, &dest, "");
        r.unwrap();
        assert!(!out.contains("AGE-SECRET-KEY"));
        assert_eq!(*store.argv_title.borrow(), "hippo backup key (testhost)");
        let parsed: toml::Table = toml::from_str(&std::fs::read_to_string(&cfg).unwrap()).unwrap();
        let b = parsed["backup"].as_table().unwrap();
        assert_eq!(
            b["identity_ref"].as_str().unwrap(),
            "op://V1/I1/private_key"
        );
        assert_eq!(
            b["destination"].as_str().unwrap(),
            dest.canonicalize().unwrap().to_str().unwrap()
        );
        assert!(b["recipient"].as_str().unwrap().starts_with("age1"));
    }

    #[test]
    fn init_rejects_readback_mismatch_and_writes_no_config() {
        let (_d, cfg, dest) = setup("[a]\nx = 1\n");
        let (r, out) = run(&FakeStore::new("wrong"), &cfg, &dest, "stored\n");
        assert!(r.is_err());
        assert!(!out.contains("AGE-SECRET-KEY"));
        assert_eq!(std::fs::read_to_string(&cfg).unwrap(), "[a]\nx = 1\n");
    }

    #[test]
    fn init_appends_block_without_touching_existing_lines() {
        let original = "[a]\nx = 1\n# keep me\n[b]\ny = \"two\"";
        let (_d, cfg, dest) = setup(original);
        let (r, out) = run(&FakeStore::new("fail"), &cfg, &dest, "stored\n");
        r.unwrap();
        assert!(out.contains("AGE-SECRET-KEY-1"));
        let text = std::fs::read_to_string(&cfg).unwrap();
        assert!(text.starts_with(original));
        let parsed: toml::Table = toml::from_str(&text).unwrap();
        assert_eq!(parsed["backup"]["identity_ref"].as_str().unwrap(), "");
        assert_eq!(parsed["b"]["y"].as_str().unwrap(), "two");
    }

    #[test]
    fn init_fallback_requires_stored() {
        let (_d, cfg, dest) = setup("[a]\nx = 1\n");
        let (r, _) = run(&FakeStore::new("fail"), &cfg, &dest, "nope\n");
        assert!(r.is_err());
        assert_eq!(std::fs::read_to_string(&cfg).unwrap(), "[a]\nx = 1\n");
    }

    #[test]
    fn init_appends_through_symlink() {
        let (d, cfg, dest) = setup("[a]\nx = 1\n");
        let link = d.path().join("link.toml");
        std::os::unix::fs::symlink(&cfg, &link).unwrap();
        let (r, _) = run(&FakeStore::new("ok"), &link, &dest, "");
        r.unwrap();
        assert!(link.symlink_metadata().unwrap().file_type().is_symlink());
        assert!(std::fs::read_to_string(&cfg).unwrap().contains("[backup]"));
    }

    /// Writes a fake `op` that records argv and stdin under `dir` and prints `create_out`
    /// for `item create`. For `read` it echoes the key found in the recorded stdin template.
    fn fake_op(dir: &Path, create_out: &str) -> PathBuf {
        use std::os::unix::fs::PermissionsExt;
        let script = dir.join("op");
        let body = format!(
            "#!/bin/sh\necho \"$@\" > '{d}/argv'\nif [ \"$1\" = item ]; then cat > '{d}/stdin'; printf '%s' '{create_out}'; \
             else sed -n 's/.*\"value\":\"\\([^\"]*\\)\".*/\\1/p' '{d}/stdin'; fi\n",
            d = dir.display()
        );
        std::fs::write(&script, body).unwrap();
        std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
        script
    }

    #[test]
    fn nonexistent_program_fails_at_spawn() {
        let dir = tempfile::tempdir().unwrap();
        let op = OnePassword::with_program(dir.path().join("no-such-op"));
        let key = age::x25519::Identity::generate().to_string();
        let e = op.store("t", None, &key).unwrap_err();
        assert!(matches!(e, StoreError::Unavailable(_)), "{e}");
        assert!(e.to_string().contains("failed to run `op`"), "{e}");
        let e = format!("{:#}", op.read("op://a/b/c").err().unwrap());
        assert!(e.contains("failed to run `op`"), "{e}");
    }

    #[test]
    fn init_with_unavailable_op_falls_back_to_manual_storage() {
        let dir = tempfile::tempdir().unwrap();
        let op = OnePassword::with_program(dir.path().join("no-such-op"));
        let (_d, cfg, dest) = setup("[a]\nx = 1\n");
        let (r, out) = run(&op, &cfg, &dest, "stored\n");
        r.unwrap();
        assert!(out.contains("AGE-SECRET-KEY-1"));
    }

    #[test]
    fn fake_op_gets_secret_on_stdin_only_and_round_trips() {
        let bin = tempfile::tempdir().unwrap();
        let op =
            OnePassword::with_program(fake_op(bin.path(), r#"{"id":"I1","vault":{"id":"V1"}}"#));
        let (_d, cfg, dest) = setup("");
        let (r, out) = run(&op, &cfg, &dest, "");
        r.unwrap();
        assert!(!out.contains("AGE-SECRET-KEY"));
        let argv = std::fs::read_to_string(bin.path().join("argv")).unwrap();
        assert!(!argv.contains("AGE-SECRET-KEY"), "{argv}");
        let stdin = std::fs::read_to_string(bin.path().join("stdin")).unwrap();
        assert!(stdin.contains("\"AGE-SECRET-KEY-1"));
        assert!(stdin.contains("\"category\":\"SECURE_NOTE\""));
        let text = std::fs::read_to_string(&cfg).unwrap();
        assert!(text.contains("identity_ref = \"op://V1/I1/private_key\""));
    }

    #[test]
    fn unparseable_create_output_is_maybe_created_and_does_not_print_key() {
        for bad in ["not json", r#"{"id":"I9"}"#, r#"{"vault":{"id":"V"}}"#] {
            let bin = tempfile::tempdir().unwrap();
            let op = OnePassword::with_program(fake_op(bin.path(), bad));
            let (_d, cfg, dest) = setup("[a]\nx = 1\n");
            let (r, out) = run(&op, &cfg, &dest, "stored\n");
            let e = format!("{:#}", r.unwrap_err());
            assert!(
                e.contains("may exist") && e.contains("hippo backup key (testhost)"),
                "{e}"
            );
            assert!(!out.contains("AGE-SECRET-KEY") && !e.contains("AGE-SECRET-KEY"));
            assert_eq!(std::fs::read_to_string(&cfg).unwrap(), "[a]\nx = 1\n");
        }
    }

    #[test]
    fn maybe_created_from_store_never_falls_back() {
        let (_d, cfg, dest) = setup("");
        let (r, out) = run(&FakeStore::new("maybe"), &cfg, &dest, "stored\n");
        assert!(r.is_err());
        assert!(!out.contains("AGE-SECRET-KEY"));
        assert_eq!(std::fs::read_to_string(&cfg).unwrap(), "");
    }

    #[test]
    fn fallback_refuses_when_stdout_is_not_a_terminal() {
        let (_d, cfg, dest) = setup("[a]\nx = 1\n");
        let (r, out) = run_tty(&FakeStore::new("fail"), &cfg, &dest, "stored\n", false);
        let e = format!("{:#}", r.unwrap_err());
        assert!(e.contains("interactive terminal"), "{e}");
        assert!(!out.contains("AGE-SECRET-KEY"));
        assert_eq!(std::fs::read_to_string(&cfg).unwrap(), "[a]\nx = 1\n");
    }

    #[test]
    fn init_writes_absolute_destination_for_relative_input() {
        let (_d, cfg, _dest) = setup("");
        let (r, _) = run(&FakeStore::new("ok"), &cfg, Path::new("src"), "");
        r.unwrap();
        let parsed: toml::Table = toml::from_str(&std::fs::read_to_string(&cfg).unwrap()).unwrap();
        let want = std::env::current_dir().unwrap().join("src");
        assert_eq!(
            parsed["backup"]["destination"].as_str().unwrap(),
            want.to_str().unwrap()
        );
    }

    /// A CloudStorage root with one `GoogleDrive-<account>/My Drive` per account.
    fn cloud(accounts: &[&str]) -> tempfile::TempDir {
        let root = tempfile::tempdir().unwrap();
        for a in accounts {
            std::fs::create_dir_all(root.path().join(format!("GoogleDrive-{a}/My Drive"))).unwrap();
        }
        root
    }

    fn pick(root: &Path, interactive: bool, input: &str) -> (Result<PathBuf>, String) {
        let mut out = Vec::new();
        let r = default_destination(
            root,
            interactive,
            &mut Cursor::new(input.to_string()),
            &mut out,
        );
        (r, String::from_utf8(out).unwrap())
    }

    #[test]
    fn default_destination_without_drive_says_to_pass_destination() {
        let root = cloud(&[]);
        // Not a mount: no My Drive folder.
        std::fs::create_dir_all(root.path().join("GoogleDrive-x@y.z")).unwrap();
        std::fs::create_dir_all(root.path().join("Dropbox/My Drive")).unwrap();
        for r in [root.path().to_path_buf(), root.path().join("absent")] {
            let e = format!("{:#}", pick(&r, true, "1\n").0.unwrap_err());
            assert!(e.contains("Google Drive for desktop was not found"), "{e}");
            assert!(e.contains("--destination"), "{e}");
        }
    }

    #[test]
    fn default_destination_with_one_mount_uses_it() {
        let root = cloud(&["a@b.c"]);
        let (r, out) = pick(root.path(), false, "");
        let want = root.path().join("GoogleDrive-a@b.c/My Drive/Backups/hippo");
        assert_eq!(r.unwrap(), want);
        assert!(out.contains(want.to_str().unwrap()), "{out}");
    }

    #[test]
    fn default_destination_with_several_mounts_needs_a_terminal() {
        let root = cloud(&["a@b.c", "d@e.f"]);
        let e = format!("{:#}", pick(root.path(), false, "1\n").0.unwrap_err());
        assert!(e.contains("--destination"), "{e}");
        assert!(
            e.contains("GoogleDrive-a@b.c/My Drive/Backups/hippo"),
            "{e}"
        );
        assert!(
            e.contains("GoogleDrive-d@e.f/My Drive/Backups/hippo"),
            "{e}"
        );
    }

    #[test]
    fn default_destination_with_several_mounts_asks_for_a_number() {
        let root = cloud(&["a@b.c", "d@e.f"]);
        let (r, out) = pick(root.path(), true, "2\n");
        assert_eq!(
            r.unwrap(),
            root.path().join("GoogleDrive-d@e.f/My Drive/Backups/hippo")
        );
        assert!(out.contains("1. GoogleDrive-a@b.c"), "{out}");
        assert!(out.contains("2. GoogleDrive-d@e.f"), "{out}");
        for bad in ["0\n", "3\n", "x\n", "\n", ""] {
            let e = format!("{:#}", pick(root.path(), true, bad).0.unwrap_err());
            assert!(e.contains("aborted"), "{bad:?}: {e}");
        }
    }

    #[test]
    fn init_creates_a_detected_destination_only_after_its_checks() {
        let (d, cfg, _) = setup("[backup]\n");
        let new = d.path().join("My Drive/Backups/hippo");
        let r = init_with(
            &FakeStore::new("ok"),
            &mut Cursor::new(String::new()),
            &mut Vec::new(),
            true,
            &cfg,
            &new,
            true,
            None,
            "testhost",
        );
        assert!(r.is_err());
        assert!(!new.exists(), "a refused init creates nothing");

        let (d, cfg, _) = setup("");
        let new = d.path().join("My Drive/Backups/hippo");
        init_with(
            &FakeStore::new("ok"),
            &mut Cursor::new(String::new()),
            &mut Vec::new(),
            true,
            &cfg,
            &new,
            true,
            None,
            "testhost",
        )
        .unwrap();
        assert!(new.is_dir());
        let parsed: toml::Table = toml::from_str(&std::fs::read_to_string(&cfg).unwrap()).unwrap();
        assert_eq!(
            parsed["backup"]["destination"].as_str().unwrap(),
            new.canonicalize().unwrap().to_str().unwrap()
        );
    }
}
