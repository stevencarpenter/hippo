//! `hippo ingest agent-eval <file.jsonl>`: typed import of agent-evaluation
//! records into the `eval_*` tables.
//!
//! Contract: `docs/superpowers/specs/2026-10-25-agent-eval-schema-design.md`
//! section 4. This is not a capture source: it writes no `source_health` row,
//! enqueues nothing for enrichment, and writes no fallback file. The
//! producer's ledger is the durable copy, and a rerun on it is idempotent.

use std::collections::{BTreeSet, HashMap};
use std::path::Path;

use hippo_core::redaction::RedactionEngine;
use hippo_core::storage::EVENT_PAYLOAD_RETENTION_MS;
use rusqlite::types::Value;
use rusqlite::{Connection, OptionalExtension};
use serde_json::{Map, Value as Json};
use sha2::{Digest, Sha256};

const MAX_LINE_BYTES: usize = 1 << 20;
const BATCH_LINES: usize = 100;
const NOTE_CAP: usize = 4000;
const LINE_TEXT_CAP: usize = 1000;
const CATEGORY_CAP: usize = 64;
const LEVELS: &[&str] = &["high", "medium", "low"];
const VERDICTS: &[&str] = &[
    "accepted",
    "false-positive",
    "not-useful",
    "duplicate",
    "likely-addressed",
];
/// Values the `agentic_sessions.harness` CHECK accepts.
const HARNESSES: &[&str] = &["claude-code", "opencode", "codex", "cursor", "pi"];

/// Per-file outcome. `rejections` holds `(line number, reason)` in file order.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct IngestSummary {
    pub written: usize,
    pub skipped: usize,
    pub rejections: Vec<(usize, String)>,
}

impl IngestSummary {
    /// Exit status 0 requires at least one written or skipped line.
    pub fn accepted_any(&self) -> bool {
        self.written + self.skipped >= 1
    }
}

/// `lower_hex(sha256(utf8(normalize(line))))`, where `normalize` drops one
/// trailing `\r` and then ASCII spaces and tabs at both ends. ASCII only, so
/// Python producers (`str.strip(" \t")`) compute the same hash.
pub fn line_hash(line: &str) -> String {
    let line = line.strip_suffix('\r').unwrap_or(line);
    let normalized = line.trim_matches([' ', '\t']);
    Sha256::digest(normalized.as_bytes())
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

#[derive(Clone, Copy)]
enum Compare {
    /// Stored value must equal the record value.
    Exact,
    /// Both sides parse as JSON and compare with numbers as `f64`.
    Json,
    /// Redacted or hippo-derived: never compared.
    Never,
}

/// One validated record, ready to insert. `cols[0]` is the primary key.
struct Prepared {
    table: &'static str,
    parent: Option<(&'static str, &'static str, String)>,
    cols: Vec<(&'static str, Value, Compare)>,
}

enum Outcome {
    Written,
    Skipped,
    Rejected(&'static str),
}

/// Ingest JSONL `input`. Redaction applies every engine in order. A failed
/// batch transaction counts its lines as `db_error` and processing continues.
pub fn ingest(
    conn: &mut Connection,
    input: &[u8],
    redaction: &[RedactionEngine],
    now_ms: i64,
) -> IngestSummary {
    let mut summary = IngestSummary::default();
    let mut identities: HashMap<String, Option<String>> = HashMap::new();
    let mut unknown_keys = BTreeSet::new();
    let lines: Vec<(usize, &[u8])> = input
        .split(|&b| b == b'\n')
        .enumerate()
        .map(|(i, line)| (i + 1, line))
        .filter(|(_, line)| !line.trim_ascii().is_empty())
        .collect();

    for batch in lines.chunks(BATCH_LINES) {
        // Validation, hashing, redaction, and git lookups run before the
        // write lock is taken.
        let mut ready = Vec::with_capacity(batch.len());
        for &(n, raw) in batch {
            match prepare_line(raw, redaction, now_ms, &mut identities, &mut unknown_keys) {
                Ok(record) => ready.push((n, record)),
                Err(reason) => summary.rejections.push((n, reason)),
            }
        }
        match write_batch(conn, &ready) {
            Ok(outcomes) => {
                for ((n, _), outcome) in ready.iter().zip(outcomes) {
                    match outcome {
                        Outcome::Written => summary.written += 1,
                        Outcome::Skipped => summary.skipped += 1,
                        Outcome::Rejected(reason) => {
                            summary.rejections.push((*n, reason.to_string()))
                        }
                    }
                }
            }
            Err(err) => {
                tracing::warn!(error = %err, lines = ready.len(), "agent-eval batch failed");
                eprintln!("agent-eval batch failed: {err:#}");
                summary
                    .rejections
                    .extend(ready.iter().map(|(n, _)| (*n, "db_error".to_string())));
            }
        }
    }
    summary.rejections.sort_by_key(|(n, _)| *n);
    if !unknown_keys.is_empty() {
        let keys: Vec<&str> = unknown_keys.iter().map(String::as_str).collect();
        tracing::warn!(keys = ?keys, "agent-eval: ignored keys not defined for their record type");
    }
    summary
}

fn write_batch(conn: &mut Connection, ready: &[(usize, Prepared)]) -> anyhow::Result<Vec<Outcome>> {
    if ready.is_empty() {
        return Ok(Vec::new());
    }
    // `open_db` makes this BEGIN IMMEDIATE, so select-then-insert is race-free.
    let tx = conn.transaction()?;
    let outcomes = ready
        .iter()
        .map(|(_, record)| write_record(&tx, record))
        .collect::<anyhow::Result<Vec<_>>>()?;
    tx.commit()?;
    Ok(outcomes)
}

fn write_record(conn: &Connection, record: &Prepared) -> anyhow::Result<Outcome> {
    let (id_col, id, _) = &record.cols[0];
    let compared: Vec<_> = record
        .cols
        .iter()
        .filter(|(_, _, cmp)| !matches!(cmp, Compare::Never))
        .collect();
    let select = format!(
        "SELECT {} FROM {} WHERE {id_col} = ?1",
        compared
            .iter()
            .map(|(c, _, _)| *c)
            .collect::<Vec<_>>()
            .join(", "),
        record.table
    );
    let existing: Option<Vec<Value>> = conn
        .query_row(&select, [id], |row| {
            (0..compared.len())
                .map(|i| row.get::<_, Value>(i))
                .collect()
        })
        .optional()?;
    if let Some(stored) = existing {
        let same = compared
            .iter()
            .zip(&stored)
            .all(|((_, new, cmp), old)| match cmp {
                Compare::Json => json_text_eq(old, new),
                _ => old == new,
            });
        return Ok(if same {
            Outcome::Skipped
        } else {
            Outcome::Rejected("id_conflict")
        });
    }
    if let Some((table, key, parent_id)) = &record.parent {
        let found: bool = conn.query_row(
            &format!("SELECT EXISTS(SELECT 1 FROM {table} WHERE {key} = ?1)"),
            [parent_id],
            |row| row.get(0),
        )?;
        if !found {
            return Ok(Outcome::Rejected("unknown_parent"));
        }
    }
    let insert = format!(
        "INSERT INTO {} ({}) VALUES ({})",
        record.table,
        record
            .cols
            .iter()
            .map(|(c, _, _)| *c)
            .collect::<Vec<_>>()
            .join(", "),
        (1..=record.cols.len())
            .map(|i| format!("?{i}"))
            .collect::<Vec<_>>()
            .join(", ")
    );
    conn.execute(
        &insert,
        rusqlite::params_from_iter(record.cols.iter().map(|(_, v, _)| v)),
    )?;
    Ok(Outcome::Written)
}

fn json_text_eq(stored: &Value, new: &Value) -> bool {
    let parse = |v: &Value| match v {
        Value::Text(t) => serde_json::from_str::<Json>(t).ok(),
        _ => None,
    };
    matches!((parse(stored), parse(new)), (Some(a), Some(b)) if json_eq(&a, &b))
}

/// Structural JSON equality with every number compared as `f64` (`1 == 1.0`).
fn json_eq(a: &Json, b: &Json) -> bool {
    match (a, b) {
        (Json::Number(x), Json::Number(y)) => x.as_f64() == y.as_f64(),
        (Json::Array(x), Json::Array(y)) => {
            x.len() == y.len() && x.iter().zip(y).all(|(x, y)| json_eq(x, y))
        }
        (Json::Object(x), Json::Object(y)) => {
            x.len() == y.len()
                && x.iter()
                    .all(|(k, v)| y.get(k).is_some_and(|w| json_eq(v, w)))
        }
        _ => a == b,
    }
}

/// Validated field access. Records every key it reads so the caller can warn
/// about the rest. Errors are rejection reasons.
struct Fields<'a> {
    obj: &'a Map<String, Json>,
    read: Vec<&'static str>,
}

type Field<T> = Result<T, String>;

impl<'a> Fields<'a> {
    /// `None` for an absent key or JSON `null`.
    fn get(&mut self, name: &'static str) -> Option<&'a Json> {
        self.read.push(name);
        self.obj.get(name).filter(|v| !v.is_null())
    }

    fn require(&mut self, name: &'static str) -> Field<&'a Json> {
        self.get(name)
            .ok_or_else(|| format!("missing_field:{name}"))
    }

    fn opt_str(&mut self, name: &'static str) -> Field<Option<String>> {
        self.get(name)
            .map(|v| v.as_str().map(str::to_owned).ok_or_else(|| invalid(name)))
            .transpose()
    }

    fn str(&mut self, name: &'static str) -> Field<String> {
        let v = self.require(name)?;
        v.as_str().map(str::to_owned).ok_or_else(|| invalid(name))
    }

    fn opt_int(&mut self, name: &'static str) -> Field<Option<i64>> {
        self.get(name)
            .map(|v| v.as_i64().ok_or_else(|| invalid(name)))
            .transpose()
    }

    fn int(&mut self, name: &'static str) -> Field<i64> {
        let v = self.require(name)?;
        v.as_i64().ok_or_else(|| invalid(name))
    }

    fn one_of(&mut self, name: &'static str, allowed: &[&str]) -> Field<String> {
        let v = self.str(name)?;
        if allowed.contains(&v.as_str()) {
            Ok(v)
        } else {
            Err(invalid(name))
        }
    }

    fn opt_sha(&mut self, name: &'static str) -> Field<Option<String>> {
        let v = self.opt_str(name)?;
        match v {
            Some(sha) if !is_full_sha(&sha) => Err(invalid(name)),
            v => Ok(v),
        }
    }

    fn sha(&mut self, name: &'static str) -> Field<String> {
        self.opt_sha(name)?
            .ok_or_else(|| format!("missing_field:{name}"))
    }

    /// JSON value of the expected shape, serialized for a `*_json` column.
    fn json(
        &mut self,
        name: &'static str,
        shape: fn(&Json) -> bool,
        default: Option<&str>,
    ) -> Field<String> {
        match (self.get(name), default) {
            (Some(v), _) if shape(v) => Ok(v.to_string()),
            (Some(_), _) => Err(invalid(name)),
            (None, Some(default)) => Ok(default.to_string()),
            (None, None) => Err(format!("missing_field:{name}")),
        }
    }
}

fn invalid(name: &str) -> String {
    format!("invalid_field:{name}")
}

fn is_full_sha(s: &str) -> bool {
    s.len() == 40 && s.bytes().all(|b| matches!(b, b'0'..=b'9' | b'a'..=b'f'))
}

fn is_valid_id(s: &str) -> bool {
    (1..=64).contains(&s.len())
        && s.bytes()
            .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b':' | b'-'))
}

fn text(s: String) -> Value {
    Value::Text(s)
}

fn opt_text(s: Option<String>) -> Value {
    s.map_or(Value::Null, Value::Text)
}

fn cap(s: String, max_chars: usize) -> String {
    match s.char_indices().nth(max_chars) {
        Some((end, _)) => s[..end].to_owned(),
        None => s,
    }
}

/// Redact first, then truncate, so a cut cannot split a credential and hide
/// it from a pattern.
fn redact(raw: &str, engines: &[RedactionEngine], max_chars: usize) -> String {
    let redacted = engines
        .iter()
        .fold(raw.to_owned(), |acc, engine| engine.redact(&acc).text);
    cap(redacted, max_chars)
}

fn prepare_line(
    raw: &[u8],
    redaction: &[RedactionEngine],
    now_ms: i64,
    identities: &mut HashMap<String, Option<String>>,
    unknown_keys: &mut BTreeSet<String>,
) -> Field<Prepared> {
    if raw.len() > MAX_LINE_BYTES {
        return Err("line_too_long".into());
    }
    let json: Json = std::str::from_utf8(raw)
        .ok()
        .and_then(|s| serde_json::from_str(s).ok())
        .ok_or("invalid_json")?;
    let obj = json.as_object().ok_or("not_an_object")?;
    let mut f = Fields {
        obj,
        read: vec!["schema_version", "type"],
    };
    match obj.get("schema_version") {
        None | Some(Json::Null) => return Err("missing_field:schema_version".into()),
        Some(v) if v.as_i64() == Some(1) => {}
        Some(v) if v.is_i64() || v.is_u64() => return Err("unknown_schema_version".into()),
        Some(_) => return Err(invalid("schema_version")),
    }
    let kind = match obj.get("type") {
        None | Some(Json::Null) => return Err("missing_field:type".into()),
        Some(Json::String(kind)) => kind.as_str(),
        Some(_) => return Err(invalid("type")),
    };
    let id = f.str("id")?;
    if !is_valid_id(&id) {
        return Err(invalid("id"));
    }
    let mut identity = |repo: &str| {
        identities
            .entry(repo.to_owned())
            .or_insert_with(|| crate::git_repo::derive_git_repo(Path::new(repo)))
            .clone()
    };
    let expiry = now_ms + EVENT_PAYLOAD_RETENTION_MS;
    use Compare::{Exact, Json as J, Never};
    let prepared = match kind {
        "review_run" => {
            let repo = f.str("repo")?;
            let session_id = f.opt_str("session_id")?;
            let harness = f.opt_str("harness")?;
            if harness.as_deref().is_some_and(|h| !HARNESSES.contains(&h)) {
                return Err(invalid("harness"));
            }
            if session_id.is_some() && harness.is_none() {
                return Err("missing_field:harness".into());
            }
            // Producers send "" for an unknown `model_id` or `cli_version`.
            // Absent, `null`, and "" all store the '' column default, so a
            // rerun compares them as equal.
            Prepared {
                table: "eval_review_runs",
                parent: None,
                cols: vec![
                    ("run_id", text(id), Exact),
                    ("agent", text(f.str("agent")?), Exact),
                    ("mode", text(f.str("mode")?), Exact),
                    (
                        "requested_model",
                        opt_text(f.opt_str("requested_model")?),
                        Exact,
                    ),
                    (
                        "model_id",
                        text(f.opt_str("model_id")?.unwrap_or_default()),
                        Exact,
                    ),
                    ("effort", opt_text(f.opt_str("effort")?), Exact),
                    (
                        "cli_version",
                        text(f.opt_str("cli_version")?.unwrap_or_default()),
                        Exact,
                    ),
                    ("repo_identity", opt_text(identity(&repo)), Never),
                    ("repo", text(repo), Exact),
                    ("base", text(f.str("base")?), Exact),
                    ("base_sha", text(f.sha("base_sha")?), Exact),
                    ("head_sha", text(f.sha("head_sha")?), Exact),
                    ("input_sha256", text(f.str("input_sha256")?), Exact),
                    ("units_total", count(&mut f, "units_total")?, Exact),
                    ("units_failed", count(&mut f, "units_failed")?, Exact),
                    ("input_tokens", count(&mut f, "input_tokens")?, Exact),
                    ("output_tokens", count(&mut f, "output_tokens")?, Exact),
                    (
                        "cache_read_tokens",
                        count(&mut f, "cache_read_tokens")?,
                        Exact,
                    ),
                    (
                        "cache_creation_tokens",
                        count(&mut f, "cache_creation_tokens")?,
                        Exact,
                    ),
                    ("session_id", opt_text(session_id), Exact),
                    ("harness", opt_text(harness), Exact),
                    ("started_at", Value::Integer(f.int("started_at")?), Exact),
                    ("finished_at", Value::Integer(f.int("finished_at")?), Exact),
                    ("ingested_at", Value::Integer(now_ms), Never),
                ],
            }
        }
        "finding" => {
            let run_id = f.str("run_id")?;
            let repo = f.str("repo")?;
            let line_text = f.opt_str("line_text")?;
            // The hash comes from the raw line; any producer hash is ignored.
            let hash = line_text.as_deref().map(line_hash);
            let category = f
                .opt_str("category")?
                .map_or_else(|| "unknown".to_owned(), |c| cap(c, CATEGORY_CAP));
            Prepared {
                table: "eval_findings",
                parent: Some(("eval_review_runs", "run_id", run_id.clone())),
                cols: vec![
                    ("finding_id", text(id), Exact),
                    ("run_id", text(run_id), Exact),
                    ("repo_identity", opt_text(identity(&repo)), Never),
                    ("repo", text(repo), Exact),
                    ("path", text(f.str("path")?), Exact),
                    ("side", text(f.one_of("side", &["+", "-"])?), Exact),
                    ("line", Value::Integer(f.int("line")?), Exact),
                    (
                        "line_text",
                        opt_text(line_text.map(|t| redact(&t, redaction, LINE_TEXT_CAP))),
                        Never,
                    ),
                    ("line_hash", opt_text(hash), Never),
                    ("severity", text(f.one_of("severity", LEVELS)?), Exact),
                    ("confidence", text(f.one_of("confidence", LEVELS)?), Exact),
                    ("category", text(category), Exact),
                    (
                        "claim",
                        text(redact(&f.str("claim")?, redaction, NOTE_CAP)),
                        Never,
                    ),
                    ("logged_at", Value::Integer(f.int("logged_at")?), Exact),
                    ("ingested_at", Value::Integer(now_ms), Never),
                    ("payload_expiry", Value::Integer(expiry), Never),
                ],
            }
        }
        "verdict" => {
            let finding_id = f.str("finding_id")?;
            let auto = f
                .require("auto")?
                .as_bool()
                .ok_or_else(|| invalid("auto"))?;
            Prepared {
                table: "eval_verdicts",
                parent: Some(("eval_findings", "finding_id", finding_id.clone())),
                cols: vec![
                    ("verdict_id", text(id), Exact),
                    ("finding_id", text(finding_id), Exact),
                    ("verdict", text(f.one_of("verdict", VERDICTS)?), Exact),
                    ("commit_sha", opt_text(f.opt_sha("commit")?), Exact),
                    (
                        "note",
                        opt_text(f.opt_str("note")?.map(|n| redact(&n, redaction, NOTE_CAP))),
                        Never,
                    ),
                    ("auto", Value::Integer(i64::from(auto)), Exact),
                    ("decided_at", Value::Integer(f.int("decided_at")?), Exact),
                    ("ingested_at", Value::Integer(now_ms), Never),
                    ("payload_expiry", Value::Integer(expiry), Never),
                ],
            }
        }
        "escape" => {
            let repo = f.str("repo")?;
            Prepared {
                table: "eval_escapes",
                parent: None,
                cols: vec![
                    ("escape_id", text(id), Exact),
                    ("repo_identity", opt_text(identity(&repo)), Never),
                    ("repo", text(repo), Exact),
                    ("path", text(f.str("path")?), Exact),
                    ("line", Value::Integer(f.int("line")?), Exact),
                    ("fix_commit", opt_text(f.opt_sha("fix_commit")?), Exact),
                    (
                        "note",
                        text(redact(&f.str("note")?, redaction, NOTE_CAP)),
                        Never,
                    ),
                    ("recorded_at", Value::Integer(f.int("recorded_at")?), Exact),
                    ("ingested_at", Value::Integer(now_ms), Never),
                ],
            }
        }
        "bench_run" => Prepared {
            table: "eval_bench_runs",
            parent: None,
            cols: vec![
                ("bench_run_id", text(id), Exact),
                ("bench", text(f.str("bench")?), Exact),
                ("recorded_at", Value::Integer(f.int("recorded_at")?), Exact),
                ("repo_commit", text(f.str("repo_commit")?), Exact),
                (
                    "cli_version",
                    text(f.opt_str("cli_version")?.unwrap_or_default()),
                    Exact,
                ),
                ("model", text(f.str("model")?), Exact),
                (
                    "model_ids_json",
                    text(f.json("model_ids", Json::is_array, Some("[]"))?),
                    J,
                ),
                ("effort", opt_text(f.opt_str("effort")?), Exact),
                ("reps", Value::Integer(f.int("reps")?), Exact),
                ("cases", Value::Integer(f.int("cases")?), Exact),
                ("cases_sha256", text(f.str("cases_sha256")?), Exact),
                ("brief_sha256", text(f.str("brief_sha256")?), Exact),
                ("failed_reviews", count(&mut f, "failed_reviews")?, Exact),
                ("tokens", count(&mut f, "tokens")?, Exact),
                (
                    "scores_json",
                    text(f.json("scores", Json::is_object, None)?),
                    J,
                ),
                (
                    "per_case_json",
                    text(f.json("per_case", Json::is_array, Some("[]"))?),
                    J,
                ),
                ("ingested_at", Value::Integer(now_ms), Never),
            ],
        },
        _ => return Err("unknown_type".into()),
    };
    unknown_keys.extend(
        obj.keys()
            .filter(|k| !f.read.contains(&k.as_str()))
            .map(|k| format!("{kind}.{k}")),
    );
    Ok(prepared)
}

/// Optional integer count; an omitted value takes the column default 0.
fn count(f: &mut Fields<'_>, name: &'static str) -> Field<Value> {
    Ok(Value::Integer(f.opt_int(name)?.unwrap_or(0)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn line_hash_matches_the_shared_test_vector() {
        assert_eq!(
            line_hash("    if user.is_admin or True:  \r"),
            "d5474129f47594d84b580d2b1d5d10e2ddd523ce5b0668b5de867807203e676c"
        );
        assert_eq!(
            line_hash("if user.is_admin or True:"),
            line_hash("\t if user.is_admin or True: \t")
        );
    }

    #[test]
    fn json_eq_compares_numbers_by_value() {
        let a: Json = serde_json::from_str(r#"{"r":1,"x":[1,{"y":2.50}]}"#).unwrap();
        let b: Json = serde_json::from_str(r#"{"x":[1.0,{"y":2.5}],"r":1.0}"#).unwrap();
        assert!(json_eq(&a, &b));
        let c: Json = serde_json::from_str(r#"{"r":1,"x":[1,{"y":2.51}]}"#).unwrap();
        assert!(!json_eq(&a, &c));
    }

    #[test]
    fn cap_counts_characters_not_bytes() {
        assert_eq!(cap("ééé".to_owned(), 2), "éé");
        assert_eq!(cap("ab".to_owned(), 2), "ab");
    }

    #[test]
    fn ids_and_shas_follow_the_contract() {
        assert!(is_valid_id("0f4e8a2c-6b1d:x.y_z"));
        assert!(!is_valid_id(""));
        assert!(!is_valid_id(&"a".repeat(65)));
        assert!(!is_valid_id("has space"));
        assert!(is_full_sha(&"a1".repeat(20)));
        assert!(!is_full_sha(&"A1".repeat(20)));
        assert!(!is_full_sha("abc1234"));
    }
}
