//! Resolve an `owner/repo` identifier for a working directory.
//!
//! The output matches the `owner/repo` shape stored in `workflow_runs.repo`
//! so the retrieval `project` filter can join `events.git_repo` against
//! the same value users see in GitHub URLs.

use std::ffi::OsStr;
use std::path::Path;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

/// Upper bound on any single `git` subprocess. `git config --get` and
/// `git rev-parse` are local-only reads, but a stuck credential helper,
/// hung filesystem, or exotic pager config can hang indefinitely — cap
/// the hot path so a broken repo can't stall event capture.
const GIT_TIMEOUT: Duration = Duration::from_millis(500);

/// Derive a repo identifier for `cwd`.
///
/// Preference order:
///   1. `owner/repo` parsed from `git config --get remote.origin.url`
///   2. basename of `git rev-parse --show-toplevel` (repo with no remote)
///   3. `None` when `cwd` is not inside a git worktree
pub fn derive_git_repo(cwd: &Path) -> Option<String> {
    if let Some(url) = remote_origin_url(cwd)
        && let Some(slug) = parse_owner_repo(&url)
    {
        return Some(slug);
    }
    toplevel_basename(cwd)
}

fn run_git(cwd: &Path, args: &[&str]) -> Option<String> {
    let mut child = Command::new("git")
        .arg("-C")
        .arg(cwd.as_os_str())
        .args(args)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;

    let deadline = Instant::now() + GIT_TIMEOUT;
    let status = loop {
        match child.try_wait() {
            Ok(Some(s)) => break s,
            Ok(None) => {
                if Instant::now() >= deadline {
                    let _ = child.kill();
                    let _ = child.wait();
                    return None;
                }
                std::thread::sleep(Duration::from_millis(5));
            }
            Err(_) => return None,
        }
    };
    if !status.success() {
        return None;
    }

    let output = child.wait_with_output().ok()?;
    let s = String::from_utf8(output.stdout).ok()?;
    let trimmed = s.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed.to_string())
    }
}

fn remote_origin_url(cwd: &Path) -> Option<String> {
    run_git(cwd, &["config", "--get", "remote.origin.url"])
}

fn toplevel_basename(cwd: &Path) -> Option<String> {
    let top = run_git(cwd, &["rev-parse", "--show-toplevel"])?;
    Path::new(&top)
        .file_name()
        .and_then(OsStr::to_str)
        .map(str::to_string)
}

/// Parse `owner/repo` from a git remote URL.
///
/// Handles the common shapes GitHub / GitLab / Bitbucket emit:
///   - `git@github.com:owner/repo.git`
///   - `https://github.com/owner/repo.git`
///   - `https://github.com/owner/repo`
///   - `ssh://git@github.com/owner/repo.git`
///
/// Returns `None` for malformed input.
///
/// Note: for enterprise hosts with nested groups
/// (e.g. `https://gitlab.example.com/group/subgroup/owner/repo`) this
/// returns only the last two path segments (`owner/repo`) and drops the
/// group hierarchy. GitHub.com — the primary target and the only shape
/// `workflow_runs.repo` currently carries — has no groups, so the join
/// stays correct. Enterprise GitLab users wanting full-path matching
/// will need to revisit this.
pub fn parse_owner_repo(url: &str) -> Option<String> {
    let trimmed = url.trim();
    let stripped = trimmed.strip_suffix(".git").unwrap_or(trimmed);

    // scp-like: `host:path`. Disambiguated from URL schemes by the `//`
    // that always follows `scheme:` in RFC-3986 URLs.
    if let Some((pre, post)) = stripped.split_once(':')
        && !post.starts_with('/')
        && !pre.contains('/')
        && let Some((owner, repo)) = post.split_once('/')
    {
        return join_slug(owner, repo);
    }

    // URL-like: owner and repo are the last two non-empty path segments.
    let mut segments = stripped.rsplit('/').filter(|s| !s.is_empty());
    let repo = segments.next()?;
    let owner = segments.next()?;
    if owner.contains(':') || owner.contains('@') {
        return None;
    }
    join_slug(owner, repo)
}

fn join_slug(owner: &str, repo: &str) -> Option<String> {
    if owner.is_empty() || repo.is_empty() {
        return None;
    }
    Some(format!("{owner}/{repo}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::process::Command;
    use tempfile::tempdir;

    fn git_init(dir: &Path) {
        let status = Command::new("git")
            .arg("-C")
            .arg(dir)
            .args(["init", "--quiet", "-b", "main"])
            .status()
            .unwrap();
        assert!(status.success(), "git init failed");
    }

    fn git_set_origin(dir: &Path, url: &str) {
        let status = Command::new("git")
            .arg("-C")
            .arg(dir)
            .args(["remote", "add", "origin", url])
            .status()
            .unwrap();
        assert!(status.success(), "git remote add failed");
    }

    #[test]
    fn parse_ssh_scp_style() {
        assert_eq!(
            parse_owner_repo("git@github.com:sjcarpenter/hippo.git"),
            Some("sjcarpenter/hippo".to_string())
        );
    }

    #[test]
    fn parse_https_with_suffix() {
        assert_eq!(
            parse_owner_repo("https://github.com/sjcarpenter/hippo.git"),
            Some("sjcarpenter/hippo".to_string())
        );
    }

    #[test]
    fn parse_https_no_suffix() {
        assert_eq!(
            parse_owner_repo("https://github.com/sjcarpenter/hippo"),
            Some("sjcarpenter/hippo".to_string())
        );
    }

    #[test]
    fn parse_ssh_url_style() {
        assert_eq!(
            parse_owner_repo("ssh://git@github.com/sjcarpenter/hippo.git"),
            Some("sjcarpenter/hippo".to_string())
        );
    }

    #[test]
    fn parse_trailing_slash_tolerated() {
        assert_eq!(
            parse_owner_repo("https://github.com/sjcarpenter/hippo/"),
            Some("sjcarpenter/hippo".to_string())
        );
    }

    #[test]
    fn parse_rejects_single_segment() {
        assert_eq!(parse_owner_repo("hippo.git"), None);
    }

    #[test]
    fn parse_rejects_empty() {
        assert_eq!(parse_owner_repo(""), None);
        assert_eq!(parse_owner_repo("   "), None);
    }

    #[test]
    fn derive_uses_origin_when_configured() {
        let tmp = tempdir().unwrap();
        git_init(tmp.path());
        git_set_origin(tmp.path(), "git@github.com:sjcarpenter/hippo.git");

        assert_eq!(
            derive_git_repo(tmp.path()),
            Some("sjcarpenter/hippo".to_string())
        );
    }

    #[test]
    fn derive_falls_back_to_toplevel_basename() {
        let tmp = tempdir().unwrap();
        let repo_root = tmp.path().join("my-local-repo");
        std::fs::create_dir(&repo_root).unwrap();
        git_init(&repo_root);

        assert_eq!(derive_git_repo(&repo_root), Some("my-local-repo".into()));
    }

    #[test]
    fn derive_returns_none_outside_worktree() {
        let tmp = tempdir().unwrap();
        assert_eq!(derive_git_repo(tmp.path()), None);
    }
}
