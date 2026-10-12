use std::fs;
use std::io::Write;
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use tempfile::tempdir;

#[test]
fn capture_preserves_caller_options_and_function_behavior() {
    let temp = tempdir().unwrap();
    let hook = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../shell/hippo.zsh");
    let output = Command::new("zsh")
        .args(["-f", "-c"])
        .arg(
            r#"
source "$HOOK"
probe() {
    local sample='one two'
    local -a words=( $sample )
    print -r -- "words=${#words} noclobber=$options[noclobber]"
    print replacement > "$EXISTING"
    print -r -- "overwrite=$?"
    false | true
}
for enabled in on off; do
    if [[ $enabled == on ]]; then
        setopt pipefail shwordsplit noclobber
    else
        unsetopt pipefail shwordsplit noclobber
    fi
    unsetopt multios
    print original >| "$EXISTING"
    probe >| "$BASELINE"; expected=$?
    print original >| "$EXISTING"
    hippo_capture probe; actual=$?
    [[ $actual == $expected ]] || exit 10
    [[ $options[pipefail] == $enabled && $options[shwordsplit] == $enabled && $options[noclobber] == $enabled && $options[multios] == off ]] || exit 11
    cmp "$BASELINE" "$_HIPPO_CAPTURE_DIR/stdout" || exit 12
    if [[ $enabled == on ]]; then
        [[ $actual == 1 && $(< "$EXISTING") == original ]] || exit 13
    else
        [[ $actual == 0 && $(< "$EXISTING") == replacement ]] || exit 14
    fi
    _hippo_capture_cleanup
done
"#,
        )
        .env("HOOK", hook)
        .env("TMPDIR", temp.path())
        .env("EXISTING", temp.path().join("existing"))
        .env("BASELINE", temp.path().join("baseline"))
        .current_dir(temp.path())
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
}

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
fn capture_paths_survive_relative_tmpdir_and_directory_changes() {
    let temp = tempdir().unwrap();
    let hook = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../shell/hippo.zsh");
    for kind in ["relative", "absolute"] {
        let root = temp.path().join(kind);
        for dir in ["start/tmp", "next", "home"] {
            fs::create_dir_all(root.join(dir)).unwrap();
        }
        let output = Command::new("zsh")
            .args(["-d", "-f", "-c"])
            .arg(
                r#"
source "$HOOK"
_HIPPO_OUTPUT_FILE="$ROOT/legacy"
git() { return 1; }
hippo() {
    local out err event_exit
    while (( $# )); do
        case "$1" in
            --stdout-file) out="$2"; shift ;;
            --stderr-file) err="$2"; shift ;;
            --exit) event_exit="$2"; shift ;;
        esac
        shift
    done
    local result=0
    cp "$out" "$ROOT/sent-out" || result=1
    cp "$err" "$ROOT/sent-err" || result=1
    print -r -- "$result $event_exit" > "$ROOT/sent-status"
}
cd "$ROOT/start"
if [[ "$KIND" == absolute ]]; then
    TMPDIR="$ROOT/start/tmp"
else
    TMPDIR=tmp
fi
hop() { print before; cd "$ROOT/next"; print after >&2; return 17; }
_hippo_preexec 'hippo_capture hop'
hippo_capture hop
result=$?
saved="$_HIPPO_CAPTURE_DIR"
return_status() { return "$1"; }
return_status "$result"
_hippo_precmd
for i in {1..100}; do
    [[ -s "$ROOT/sent-status" ]] && break
    sleep 0.01
done
[[ $(< "$ROOT/sent-status") == '0 17' ]] || exit 10
[[ $(< "$ROOT/sent-out") == before && $(< "$ROOT/sent-err") == after ]] || exit 11
[[ "$saved" == /* ]] || exit 12
for i in {1..100}; do
    [[ ! -d "$saved" ]] && break
    sleep 0.01
done
[[ ! -d "$saved" ]]
"#,
            )
            .env("HOOK", &hook)
            .env("ROOT", &root)
            .env("KIND", kind)
            .env("HOME", root.join("home"))
            .env("ZDOTDIR", root.join("home"))
            .output()
            .unwrap();
        assert!(output.status.success(), "{kind}: {output:?}");
        assert!(String::from_utf8_lossy(&output.stdout).contains("before\n"));
        assert!(String::from_utf8_lossy(&output.stderr).contains("after\n"));
    }
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

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn opt_in_hook_sends_independent_streams_and_preserves_legacy_events() {
    use hippo_core::events::EventPayload;
    use hippo_core::protocol::DaemonRequest;
    use hippo_core::redaction::RedactionEngine;
    use hippo_core::storage;

    // Keep the socket path short so config cannot select the shared /tmp fallback.
    let temp = tempfile::Builder::new()
        .prefix("hippo-stream-")
        .tempdir_in("/tmp")
        .unwrap();
    let data = temp.path().join("data/hippo");
    let config = temp.path().join("config/hippo");
    let bin = temp.path().join("bin");
    let capture_tmp = temp.path().join("capture-tmp");
    for dir in [&data, &config, &bin, &capture_tmp] {
        fs::create_dir_all(dir).unwrap();
    }
    fs::write(
        config.join("config.toml"),
        "[daemon]\noutput_head_lines=1\noutput_tail_lines=1\n",
    )
    .unwrap();
    std::os::unix::fs::symlink(env!("CARGO_BIN_EXE_hippo"), bin.join("hippo")).unwrap();
    let listener = tokio::net::UnixListener::bind(data.join("daemon.sock")).unwrap();
    let receiver = tokio::spawn(async move {
        let mut events = Vec::new();
        for _ in 0..8 {
            let (mut stream, _) = tokio::time::timeout(Duration::from_secs(10), listener.accept())
                .await
                .unwrap()
                .unwrap();
            let json = tokio::time::timeout(
                Duration::from_secs(5),
                hippo_daemon::framing::read_frame(&mut stream),
            )
            .await
            .unwrap()
            .unwrap()
            .unwrap();
            let DaemonRequest::IngestEvent(event) = serde_json::from_slice(&json).unwrap() else {
                panic!("expected an event");
            };
            events.push(event);
        }
        events
    });
    let hook = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../shell/hippo.zsh");
    let mut shell = Command::new("zsh")
        .args(["-dfi"])
        .env("HOOK", hook)
        .env("SCRATCH", temp.path().join("legacy-output"))
        .env("HOME", temp.path().join("home"))
        .env("ZDOTDIR", temp.path().join("home"))
        .env("XDG_CONFIG_HOME", temp.path().join("config"))
        .env("XDG_DATA_HOME", temp.path().join("data"))
        .env("TMPDIR", &capture_tmp)
        .env("HIPPO_OTEL_ENABLED", "false")
        .env(
            "PATH",
            format!("{}:{}", bin.display(), std::env::var("PATH").unwrap()),
        )
        .current_dir(temp.path())
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    shell
        .stdin
        .take()
        .unwrap()
        .write_all(
            br#"source "$HOOK"; _HIPPO_OUTPUT_FILE="$SCRATCH"; git() { return 1; }; PROMPT=''; RPROMPT=''; emit() { printf 'password=out-synthetic\nout-middle-1\nout-middle-2\nout-last\n'; printf 'password=err-synthetic\nerr-middle\nerr-last\n' >&2; return 7; }
printf 'ordinary-out\n'; printf 'ordinary-err\n' >&2; (exit 9)
hippo_capture emit
verbose() { print 'out-first'; print 'err-first' >&2; repeat 200000 { print 'out-middle'; print 'err-middle' >&2; }; printf 'out-final'; printf 'err-final' >&2; return 8; }; hippo_capture verbose
printf 'legacy\n' > "$_HIPPO_OUTPUT_FILE"; (exit 3)
mktemp() { print attempted >> "$TMPDIR/mktemp-called"; command mktemp "$@"; }; forbidden() { print executed >> "$TMPDIR/command-ran"; }; hippo_capture forbidden & job=$!; wait "$job"; result=$?; (exit "$result")
value=$(hippo_capture forbidden); result=$?; (exit "$result")
hippo_capture forbidden | cat; result=${pipestatus[1]}; (exit "$result")
(hippo_capture forbidden)
exit 0
"#,
        )
        .unwrap();
    let visible = shell.wait_with_output().unwrap();
    assert!(visible.status.success(), "{visible:?}");
    let stdout = String::from_utf8_lossy(&visible.stdout);
    let stderr = String::from_utf8_lossy(&visible.stderr);
    assert!(stdout.contains("ordinary-out\n"), "{stdout}");
    assert!(
        stdout.contains("out-middle-1\nout-middle-2\nout-last\n"),
        "{stdout}"
    );
    assert!(stderr.contains("ordinary-err\n"), "{stderr}");
    assert!(stderr.contains("err-middle\nerr-last\n"), "{stderr}");

    let events = receiver.await.unwrap();
    let shells: Vec<_> = events
        .iter()
        .map(|e| match &e.payload {
            EventPayload::Shell(shell) => shell,
            _ => panic!("expected shell payload"),
        })
        .collect();
    let rejected: Vec<_> = shells.iter().filter(|e| e.exit_code == 2).collect();
    assert_eq!(rejected.len(), 4, "{shells:?}");
    for event in rejected {
        assert!(event.stdout.is_none() && event.stderr.is_none());
    }
    assert!(!capture_tmp.join("mktemp-called").exists());
    assert!(!capture_tmp.join("command-ran").exists());
    assert_eq!(stderr.matches("only foreground commands").count(), 4);
    let ordinary = shells.iter().find(|e| e.exit_code == 9).unwrap();
    assert!(ordinary.stdout.is_none() && ordinary.stderr.is_none());
    let legacy = shells.iter().find(|e| e.exit_code == 3).unwrap();
    assert_eq!(legacy.stdout.as_ref().unwrap().content, "legacy");
    assert!(!legacy.stdout.as_ref().unwrap().truncated);
    assert!(legacy.stderr.is_none());
    let verbose = shells.iter().find(|e| e.exit_code == 8).unwrap();
    for (stream, prefix) in [(&verbose.stdout, "out"), (&verbose.stderr, "err")] {
        let stream = stream.as_ref().unwrap();
        assert_eq!(
            stream.content,
            format!("{prefix}-first\n... (200000 lines omitted) ...\n{prefix}-final")
        );
        assert!(stream.truncated);
        assert_eq!(stream.original_bytes, 10 + 200000 * 11 + 9);
    }
    let captured = shells.iter().find(|e| e.exit_code == 7).unwrap();
    let out = captured.stdout.as_ref().unwrap();
    let err = captured.stderr.as_ref().unwrap();
    assert_eq!(
        out.content,
        "password=out-synthetic\n... (2 lines omitted) ...\nout-last\n"
    );
    assert_eq!(
        err.content,
        "password=err-synthetic\n... (1 lines omitted) ...\nerr-last\n"
    );
    assert!(out.truncated && err.truncated);
    assert_eq!(
        out.original_bytes,
        "password=out-synthetic\nout-middle-1\nout-middle-2\nout-last\n".len()
    );
    assert_eq!(
        err.original_bytes,
        "password=err-synthetic\nerr-middle\nerr-last\n".len()
    );

    // Exercise the same shared redaction and storage interfaces as daemon flush.
    let (redacted, _) = hippo_daemon::redact_shell_event(captured, &RedactionEngine::builtin());
    let conn = storage::open_db(&temp.path().join("captured.db")).unwrap();
    let sid = storage::upsert_session(&conn, "streams", "test", "zsh", "test").unwrap();
    let eid = storage::insert_event(&conn, sid, &redacted, redacted.redaction_count, None).unwrap();
    let stored: (String, String, i32, i32) = conn
        .query_row(
            "SELECT stdout, stderr, stdout_truncated, stderr_truncated FROM events WHERE id=?1",
            [eid],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
        )
        .unwrap();
    assert!(!stored.0.contains("out-synthetic"));
    assert!(!stored.1.contains("err-synthetic"));
    assert!(stored.0.contains("out-last") && stored.1.contains("err-last"));
    assert_eq!((stored.2, stored.3), (1, 1));
    assert!(redacted.redaction_count >= 2);

    let deadline = Instant::now() + Duration::from_secs(5);
    while fs::read_dir(&capture_tmp).unwrap().next().is_some() {
        assert!(Instant::now() < deadline, "capture files were not removed");
        std::thread::sleep(Duration::from_millis(10));
    }
}
