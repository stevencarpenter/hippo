//! Agent-eval canary (design section 5.5): a synthetic GitHub token in free
//! text is stored redacted, never reaches a derived index or an enrichment
//! queue, never reaches the database bytes, and a rerun skips every line.

use std::path::{Path, PathBuf};
use std::process::{Command, Output};

use hippo_core::storage;
use rusqlite::Connection;

const FIXTURES: &str = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/agent_eval");

struct Env {
    _root: tempfile::TempDir,
    root: PathBuf,
}

impl Env {
    fn new() -> Self {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().to_path_buf();
        std::fs::create_dir_all(root.join("config/hippo")).unwrap();
        std::fs::write(
            root.join("config/hippo/config.toml"),
            "[telemetry]\nenabled=false\n",
        )
        .unwrap();
        Self { _root: dir, root }
    }

    fn db_path(&self) -> PathBuf {
        self.root.join("data/hippo/hippo.db")
    }

    fn ingest(&self, file: &Path) -> Output {
        Command::new(env!("CARGO_BIN_EXE_hippo"))
            .env("HOME", &self.root)
            .env("XDG_CONFIG_HOME", self.root.join("config"))
            .env("XDG_DATA_HOME", self.root.join("data"))
            .args(["ingest", "agent-eval"])
            .arg(file)
            .output()
            .unwrap()
    }
}

fn stdout(out: &Output) -> String {
    String::from_utf8_lossy(&out.stdout).trim().to_owned()
}

fn assert_ingest(out: &Output, expected: &str) {
    assert!(
        out.status.success(),
        "exit {:?}; stderr: {}",
        out.status.code(),
        String::from_utf8_lossy(&out.stderr)
    );
    assert_eq!(stdout(out), expected);
}

/// Row counts of every derived-index and enrichment-queue table.
fn derived_counts(conn: &Connection) -> Vec<(String, i64)> {
    let tables: Vec<String> = conn
        .prepare(
            "SELECT name FROM sqlite_master WHERE type = 'table'
             AND (name LIKE '%enrichment_queue' OR name IN ('knowledge_nodes', 'knowledge_fts'))
             ORDER BY name",
        )
        .unwrap()
        .query_map([], |r| r.get(0))
        .unwrap()
        .collect::<rusqlite::Result<_>>()
        .unwrap();
    assert!(tables.len() >= 7, "expected queue tables, got {tables:?}");
    tables
        .into_iter()
        .map(|t| {
            let n = conn
                .query_row(&format!("SELECT count(*) FROM \"{t}\""), [], |r| r.get(0))
                .unwrap();
            (t, n)
        })
        .collect()
}

#[test]
fn canary_token_is_redacted_hashed_raw_and_never_indexed() {
    let env = Env::new();
    // Assembled at runtime so no token-shaped literal sits in the repository.
    let token = format!(
        "ghp_{}",
        ["Zq7Xk2Lm9P", "w4Rt8Vb3Nc", "6Hd1Jf5Gs0", "Ya2Ue4"].concat()
    );
    assert_eq!(token.len(), 40);
    let raw_line = format!("    let auth = \"{token}\";  ");
    let sha = "0a1b2c3d4e5f60718293a4b5c6d7e8f901234567";
    let records = [
        serde_json::json!({"schema_version":1,"type":"review_run","id":"run-canary",
            "agent":"blind-review","mode":"mechanical","repo":"/home/dev/example-repo",
            "base":"main","base_sha":sha,"head_sha":sha,"input_sha256":"00ff",
            "started_at":1792900000000_i64,"finished_at":1792900042000_i64}),
        serde_json::json!({"schema_version":1,"type":"finding","id":"finding-canary",
            "run_id":"run-canary","repo":"/home/dev/example-repo","path":"src/auth.rs",
            "side":"+","line":3,"line_text":raw_line,"severity":"high",
            "confidence":"high","category":"security",
            "claim":format!("Hardcoded credential {token} in source."),
            "logged_at":1792900043000_i64}),
        serde_json::json!({"schema_version":1,"type":"verdict","id":"verdict-canary",
            "finding_id":"finding-canary","verdict":"accepted",
            "note":format!("Rotated {token}."),"auto":false,"decided_at":1792900044000_i64}),
    ];
    let file = env.root.join("canary.jsonl");
    std::fs::write(
        &file,
        records.iter().map(|r| format!("{r}\n")).collect::<String>(),
    )
    .unwrap();

    let conn = storage::open_db(&env.db_path()).unwrap();
    let before = derived_counts(&conn);

    assert_ingest(&env.ingest(&file), "written 3, skipped 0, rejected 0");

    let (claim, line_text, line_hash, note): (String, String, String, String) = conn
        .query_row(
            "SELECT f.claim, f.line_text, f.line_hash, v.note
             FROM eval_findings f JOIN eval_verdicts v USING (finding_id)
             WHERE f.finding_id = 'finding-canary'",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .unwrap();
    for stored in [&claim, &line_text, &note] {
        assert!(stored.contains("[REDACTED]"), "{stored}");
        assert!(!stored.contains(&token), "{stored}");
    }
    assert_eq!(line_hash, hippo_daemon::agent_eval::line_hash(&raw_line));
    assert_ne!(line_hash, hippo_daemon::agent_eval::line_hash(&line_text));

    assert_eq!(derived_counts(&conn), before);
    let fts_hits: i64 = conn
        .query_row(
            "SELECT count(*) FROM knowledge_fts WHERE knowledge_fts MATCH 'ghp*'",
            [],
            |r| r.get(0),
        )
        .unwrap();
    assert_eq!(fts_hits, 0);

    let busy: i64 = conn
        .query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |r| r.get(0))
        .unwrap();
    assert_eq!(busy, 0);
    for path in [env.db_path(), env.db_path().with_extension("db-wal")] {
        if let Ok(bytes) = std::fs::read(&path) {
            assert!(
                !bytes.windows(token.len()).any(|w| w == token.as_bytes()),
                "token bytes found in {}",
                path.display()
            );
        }
    }

    assert_ingest(&env.ingest(&file), "written 0, skipped 3, rejected 0");
    assert_eq!(derived_counts(&conn), before);
}

#[test]
fn all_five_record_types_ingest_once_and_rerun_skips_every_line() {
    let env = Env::new();
    let file = Path::new(FIXTURES).join("all_types.jsonl");
    let lines = std::fs::read_to_string(&file)
        .unwrap()
        .lines()
        .filter(|l| !l.trim().is_empty())
        .count();
    assert_eq!(lines, 5);

    assert_ingest(&env.ingest(&file), "written 5, skipped 0, rejected 0");
    assert_ingest(&env.ingest(&file), "written 0, skipped 5, rejected 0");

    let conn = storage::open_db(&env.db_path()).unwrap();
    for table in [
        "eval_review_runs",
        "eval_findings",
        "eval_verdicts",
        "eval_escapes",
        "eval_bench_runs",
    ] {
        let n: i64 = conn
            .query_row(&format!("SELECT count(*) FROM {table}"), [], |r| r.get(0))
            .unwrap();
        assert_eq!(n, 1, "{table}");
    }
    let (commit, auto, expiry_gap): (String, i64, i64) = conn
        .query_row(
            "SELECT commit_sha, auto, payload_expiry - ingested_at FROM eval_verdicts",
            [],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .unwrap();
    assert_eq!(commit, "1122334455667788990011223344556677889900");
    assert_eq!(auto, 0);
    assert_eq!(expiry_gap, storage::EVENT_PAYLOAD_RETENTION_MS);
    let (hash, identity): (String, Option<String>) = conn
        .query_row(
            "SELECT line_hash, repo_identity FROM eval_findings",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )
        .unwrap();
    assert_eq!(
        hash,
        hippo_daemon::agent_eval::line_hash("    if user.is_admin or True:")
    );
    // The fixture repo path does not exist, so no identity derives.
    assert_eq!(identity, None);
}

#[test]
fn exit_code_is_one_when_nothing_is_accepted() {
    let env = Env::new();
    let missing = env.ingest(&env.root.join("absent.jsonl"));
    assert_eq!(missing.status.code(), Some(1));

    let blank = env.root.join("blank.jsonl");
    std::fs::write(&blank, "\n   \n").unwrap();
    let out = env.ingest(&blank);
    assert_eq!(out.status.code(), Some(1));
    assert_eq!(stdout(&out), "written 0, skipped 0, rejected 0");

    let bad = env.root.join("bad.jsonl");
    std::fs::write(&bad, "not json\n\n[1]\n").unwrap();
    let out = env.ingest(&bad);
    assert_eq!(out.status.code(), Some(1));
    assert_eq!(stdout(&out), "written 0, skipped 0, rejected 2");
    let stderr = String::from_utf8_lossy(&out.stderr);
    assert!(
        stderr.contains("line 1: rejected: invalid_json"),
        "{stderr}"
    );
    assert!(
        stderr.contains("line 3: rejected: not_an_object"),
        "{stderr}"
    );
}
