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

    // Find zsh explicitly — $SHELL may be bash on CI even when zsh is installed
    let zsh = ["/bin/zsh", "/usr/bin/zsh", "/opt/homebrew/bin/zsh"]
        .iter()
        .find(|p| std::path::Path::new(p).exists())
        .expect("zsh not found — install with: apt-get install zsh")
        .to_string();
    let output = Command::new(zsh).arg("-lc").arg(script).output().unwrap();
    assert!(
        output.status.success(),
        "stderr: {}",
        String::from_utf8_lossy(&output.stderr)
    );

    let branch = String::from_utf8(output.stdout).unwrap();
    assert_eq!(branch.trim(), "old-branch");
}

#[test]
fn clean_zsh_loads_clock_and_sends_numeric_duration() {
    let temp = tempdir().unwrap();
    let hook = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../shell/hippo.zsh");
    let output = Command::new("zsh")
        .args(["-f", "-c"])
        .arg(
            r#"
source "$HOOK"
_HIPPO_OUTPUT_FILE="$SCRATCH"
git() { return 1; }
hippo() { print -rl -- "$@" > "$CAPTURE_ARGS"; }
_hippo_preexec 'echo safe'
_hippo_precmd
for i in {1..100}; do
    [[ -s "$CAPTURE_ARGS" ]] && break
    sleep 0.01
done
[[ -s "$CAPTURE_ARGS" ]]
"#,
        )
        .env("HOOK", hook)
        .env("SCRATCH", temp.path().join("output"))
        .env("CAPTURE_ARGS", temp.path().join("args"))
        .current_dir(temp.path())
        .output()
        .unwrap();
    assert!(output.status.success(), "{:?}", output);
    let args = fs::read_to_string(temp.path().join("args")).unwrap();
    let args: Vec<_> = args.lines().collect();
    let duration = args.iter().position(|s| *s == "--duration-ms").unwrap();
    assert!(args[duration + 1].parse::<u64>().is_ok(), "{args:?}");
    assert!(String::from_utf8_lossy(&output.stderr).is_empty());
}
