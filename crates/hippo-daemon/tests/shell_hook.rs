use std::fs;
use std::process::Command;

use tempfile::tempdir;

#[test]
fn test_git_probes_use_captured_cwd() {
    let temp = tempdir().unwrap();
    let old_repo = temp.path().join("old-repo");
    let new_repo = temp.path().join("new-repo");
    fs::create_dir_all(&old_repo).unwrap();
    fs::create_dir_all(&new_repo).unwrap();

    let hook_path = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("../../shell/hippo.zsh")
        .canonicalize()
        .unwrap();

    let script = format!(
        r#"
set -e
git() {{
  case "$*" in
    *"--is-inside-work-tree"*) return 0 ;;
    *"-C {old_repo}"*"--abbrev-ref HEAD"*) print -r -- old-branch ;;
    *"-C {old_repo}"*"--short HEAD"*) print -r -- old-commit ;;
    *"--abbrev-ref HEAD"*) print -r -- new-branch ;;
    *"--short HEAD"*) print -r -- new-commit ;;
    *"status --porcelain"*) return 0 ;;
  esac
}}
hippo() {{
  return 0
}}
cd "{old_repo}"
source "{hook_path}"
_HIPPO_CMD='cd "{new_repo}"'
_HIPPO_CWD="{old_repo}"
_HIPPO_START="${{EPOCHREALTIME:-0}}"
cd "{new_repo}"
_hippo_precmd
print -r -- "$_HIPPO_GIT_BRANCH"
    "#,
        old_repo = old_repo.display(),
        new_repo = new_repo.display(),
        hook_path = hook_path.display(),
    );

    let zsh = std::env::var("SHELL").unwrap_or_else(|_| "/opt/homebrew/bin/zsh".to_string());
    let output = Command::new(zsh).arg("-lc").arg(script).output().unwrap();
    assert!(
        output.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );

    let branch = String::from_utf8(output.stdout).unwrap();
    assert_eq!(branch.trim(), "old-branch");
}
