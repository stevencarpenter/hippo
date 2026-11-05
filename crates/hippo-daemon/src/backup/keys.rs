//! Backup key setup. The age private key lives in 1Password and is never written to disk,
//! logged, or passed on a command line.

use std::ffi::OsString;
use std::io::{BufRead, Write};
use std::path::Path;
use std::process::{Command, Stdio};

use age::secrecy::{ExposeSecret, SecretString};
use anyhow::{Context, Result, bail};

#[cfg(test)]
use std::ffi::OsStr;
#[cfg(test)]
use std::path::PathBuf;

const FIELD: &str = "private_key";

/// Secret store that holds the private key. Tests inject a fake; production uses `OnePassword`.
pub trait KeyStore {
    /// Saves `key` and returns the `identity_ref` that `read` resolves.
    fn store(&self, title: &str, vault: Option<&str>, key: &SecretString) -> Result<String>;
    fn read(&self, reference: &str) -> Result<SecretString>;
}

/// The `op` CLI. `path` overrides the `PATH` used to find it (tests).
#[derive(Default)]
pub struct OnePassword {
    path: Option<OsString>,
}

impl OnePassword {
    #[cfg(test)]
    fn with_path(path: &OsStr) -> Self {
        Self {
            path: Some(path.to_os_string()),
        }
    }

    fn run(&self, args: &[&str], stdin: Option<&[u8]>) -> Result<Vec<u8>> {
        let mut cmd = Command::new("op");
        cmd.args(args)
            .stdin(if stdin.is_some() {
                Stdio::piped()
            } else {
                Stdio::null()
            })
            .stdout(Stdio::piped())
            .stderr(Stdio::piped());
        if let Some(p) = &self.path {
            cmd.env("PATH", p);
        }
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
    fn store(&self, title: &str, vault: Option<&str>, key: &SecretString) -> Result<String> {
        let template = serde_json::json!({
            "title": title,
            "category": "SECURE_NOTE",
            "fields": [{
                "id": FIELD, "label": FIELD, "type": "CONCEALED",
                "value": key.expose_secret(),
            }],
        });
        let body = serde_json::to_vec(&template)?;
        let mut args = vec!["item", "create", "--format", "json"];
        if let Some(v) = vault {
            args.extend(["--vault", v]);
        }
        args.push("-");
        let out = self.run(&args, Some(&body))?;
        let created: serde_json::Value =
            serde_json::from_slice(&out).context("op returned unparseable JSON")?;
        let id = created["id"].as_str().context("op output has no item id")?;
        let vault_id = created["vault"]["id"]
            .as_str()
            .context("op output has no vault id")?;
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
pub fn init(config_path: &Path, destination: &Path, op_vault: Option<&str>) -> Result<()> {
    let host = hostname::get()
        .map(|h| h.to_string_lossy().into_owned())
        .unwrap_or_else(|_| "unknown-host".to_string());
    init_with(
        &OnePassword::default(),
        &mut std::io::stdin().lock(),
        &mut std::io::stdout(),
        config_path,
        destination,
        op_vault,
        &host,
    )
}

fn init_with(
    store: &dyn KeyStore,
    input: &mut dyn BufRead,
    out: &mut dyn Write,
    config_path: &Path,
    destination: &Path,
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
    if !destination.is_dir() {
        bail!(
            "destination {} is not an existing directory",
            destination.display()
        );
    }
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
        Err(e) => {
            eprintln!("hippo backup: 1Password storage failed ({e:#}); manual storage required");
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
        fn store(&self, title: &str, _vault: Option<&str>, key: &SecretString) -> Result<String> {
            if self.mode == "fail" {
                bail!("op unavailable");
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
        let mut out = Vec::new();
        let r = init_with(
            store,
            &mut Cursor::new(input.to_string()),
            &mut out,
            cfg,
            dest,
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
        assert_eq!(b["destination"].as_str().unwrap(), dest.to_str().unwrap());
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

    #[test]
    fn one_password_without_op_on_path_is_an_error() {
        let empty = tempfile::tempdir().unwrap();
        let op = OnePassword::with_path(empty.path().as_os_str());
        let key = age::x25519::Identity::generate().to_string();
        // A spawn failure (not an `op` exit status) proves the real binary was never run.
        let e = format!("{:#}", op.store("t", None, &key).unwrap_err());
        assert!(e.contains("failed to run `op`"), "{e}");
        let e = format!("{:#}", op.read("op://a/b/c").err().unwrap());
        assert!(e.contains("failed to run `op`"), "{e}");
    }

    #[test]
    fn init_without_op_falls_back_to_manual_storage() {
        let empty = tempfile::tempdir().unwrap();
        let op = OnePassword::with_path(empty.path().as_os_str());
        let (_d, cfg, dest) = setup("[a]\nx = 1\n");
        let (r, out) = run(&op, &cfg, &dest, "stored\n");
        r.unwrap();
        assert!(out.contains("AGE-SECRET-KEY-1"));
    }
}
