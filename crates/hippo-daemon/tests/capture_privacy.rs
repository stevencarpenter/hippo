use hippo_core::events::{CapturedOutput, ShellEvent, ShellKind};
use hippo_core::redaction::RedactionEngine;
use std::collections::HashMap;
use std::process::Command;
use tempfile::tempdir;

#[test]
fn shell_redaction_covers_both_outputs_and_preserves_capture_metadata() {
    let pem =
        "private_key=-----BEGIN PRIVATE KEY-----\nZmFrZXNlY3JldA==\n-----END PRIVATE KEY-----";
    let event = ShellEvent {
        session_id: uuid::Uuid::new_v4(),
        command: "echo safe".into(),
        exit_code: 0,
        duration_ms: 1,
        cwd: "/tmp".into(),
        hostname: "test".into(),
        shell: ShellKind::Zsh,
        stdout: Some(CapturedOutput {
            content: "password=synthetic-secret-123".into(),
            truncated: true,
            original_bytes: 1000,
        }),
        stderr: Some(CapturedOutput {
            content: pem.into(),
            truncated: false,
            original_bytes: pem.len(),
        }),
        env_snapshot: HashMap::new(),
        git_state: None,
        redaction_count: 0,
        tool_name: None,
    };
    let (redacted, hits) = hippo_daemon::redact_shell_event(&event, &RedactionEngine::builtin());
    let stdout = redacted.stdout.as_ref().unwrap();
    assert!(!stdout.content.contains("synthetic-secret"));
    assert!(stdout.truncated);
    assert_eq!(stdout.original_bytes, 1000);
    let stderr = redacted.stderr.as_ref().unwrap();
    assert!(!stderr.content.contains("ZmFrZXNlY3JldA=="));
    assert!(!stderr.truncated);
    assert_eq!(stderr.original_bytes, pem.len());
    assert_eq!(redacted.command, "echo safe");
    assert!(redacted.redaction_count >= 2);
    assert_eq!(
        hits.iter().map(|(_, n)| n).sum::<u32>(),
        redacted.redaction_count
    );
    assert!(event.stdout.unwrap().content.contains("synthetic-secret"));
}

#[test]
fn native_host_refuses_invalid_or_unreadable_config_before_capture() {
    for unreadable in [false, true] {
        let temp = tempdir().unwrap();
        let config_dir = temp.path().join("config/hippo");
        std::fs::create_dir_all(&config_dir).unwrap();
        let path = config_dir.join("config.toml");
        if unreadable {
            // A directory yields a read error even when tests run as root.
            std::fs::create_dir(&path).unwrap();
        } else {
            std::fs::write(
                &path,
                "[browser]\nenabled=false\n[daemon]\nsocket_timeout_ms=\"typo\"\n",
            )
            .unwrap();
        }
        let output = Command::new(env!("CARGO_BIN_EXE_hippo"))
            .arg("native-messaging-host")
            .env("HOME", temp.path().join("home"))
            .env("XDG_CONFIG_HOME", temp.path().join("config"))
            .env("XDG_DATA_HOME", temp.path().join("data"))
            .output()
            .unwrap();
        assert!(!output.status.success());
        assert!(output.stdout.is_empty());
        assert!(String::from_utf8_lossy(&output.stderr).contains("config"));
        assert!(!temp.path().join("data").exists());
    }
}

#[test]
fn missing_config_uses_defaults_and_fallback_redacts_captured_output() {
    let temp = tempdir().unwrap();
    let output = Command::new(env!("CARGO_BIN_EXE_hippo"))
        .args([
            "send-event",
            "shell",
            "--cmd",
            "echo safe",
            "--exit",
            "0",
            "--cwd",
        ])
        .arg(temp.path())
        .args([
            "--duration-ms",
            "1",
            "--output",
            r#"{"password":"two word secret","public":"keep"}"#,
        ])
        .env("HOME", temp.path().join("home"))
        .env("XDG_CONFIG_HOME", temp.path().join("config"))
        .env("XDG_DATA_HOME", temp.path().join("data"))
        .env("HIPPO_OTEL_ENABLED", "false")
        .output()
        .unwrap();
    assert!(output.status.success(), "{output:?}");
    let entries =
        hippo_core::storage::list_fallback_files(&temp.path().join("data/hippo/fallback")).unwrap();
    assert_eq!(entries.len(), 1);
    let saved = std::fs::read_to_string(&entries[0]).unwrap();
    assert!(!saved.contains("two word secret"));
    assert!(saved.contains("[REDACTED]"));
    assert!(saved.contains("keep"));
}
