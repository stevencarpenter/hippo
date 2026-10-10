use hippo_core::storage;
use std::process::Command;

#[test]
fn cli_requires_confirmation_and_sweeps_only_the_synthetic_configured_store() {
    let root = tempfile::tempdir().unwrap();
    let config_root = root.path().join("config");
    let data_root = root.path().join("data");
    std::fs::create_dir_all(config_root.join("hippo")).unwrap();
    std::fs::write(
        config_root.join("hippo/config.toml"),
        "[telemetry]\nenabled=false\n",
    )
    .unwrap();
    let path = data_root.join("hippo/hippo.db");
    let mut cmd = Command::new(env!("CARGO_BIN_EXE_hippo"));
    cmd.env("HOME", root.path())
        .env("XDG_CONFIG_HOME", &config_root)
        .env("XDG_DATA_HOME", &data_root)
        .arg("gc-event-payloads");
    let refused = cmd.output().unwrap();
    assert!(!refused.status.success());
    assert!(String::from_utf8_lossy(&refused.stderr).contains("--confirm"));
    assert!(!path.exists());

    let conn = storage::open_db(&path).unwrap();
    conn.execute_batch(
        "INSERT INTO sessions(id,start_time,shell,hostname,username) VALUES(1,0,'zsh','test','test');
         INSERT INTO events(session_id,timestamp,command,stdout,duration_ms,cwd,hostname,shell,created_at)
         VALUES(1,0,'synthetic command','synthetic payload',0,'/tmp/test','test','zsh',1);",
    ).unwrap();
    let applied = cmd.arg("--confirm").output().unwrap();
    assert!(
        applied.status.success(),
        "{}",
        String::from_utf8_lossy(&applied.stderr)
    );
    let stdout = String::from_utf8_lossy(&applied.stdout);
    assert!(stdout.contains("Cleared payloads from 1 retained event rows"));
    assert!(stdout.contains("WAL checkpoint completed"));
    assert_eq!(
        conn.query_row("SELECT command,stdout FROM events", [], |r| Ok((
            r.get::<_, String>(0)?,
            r.get::<_, Option<String>>(1)?
        )))
        .unwrap(),
        ("synthetic command".into(), None)
    );
    assert_eq!(
        conn.query_row("SELECT what FROM deletion_receipts", [], |r| r
            .get::<_, String>(0))
            .unwrap(),
        "events/1/stdout"
    );
}
