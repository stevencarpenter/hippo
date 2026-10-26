//! `hippo eval export-bench-cases`: write private blind-review bench cases
//! from accepted findings and from escapes with a known fix commit.
//!
//! A case replays the defect as a change: `base` holds the fixed file and
//! `head` holds the defective file, so the trap is an added (`+`) row, the
//! same convention as the public cases in the agents repository's
//! `skills/blind-review/bench/cases/`. Every written text passes through the
//! ingest redaction engines. The output never goes inside a git checkout.

use std::collections::BTreeMap;
use std::path::Path;
use std::process::{Command, Stdio};

use anyhow::Context;
use hippo_core::redaction::RedactionEngine;
use rusqlite::Connection;
use serde_json::{Value as Json, json};

use crate::agent_eval::line_hash;

/// Cases written and cases skipped per reason.
#[derive(Debug, Default, PartialEq, Eq)]
pub struct ExportSummary {
    pub written: usize,
    pub skipped: BTreeMap<&'static str, usize>,
}

/// A finding or escape that has the revisions a case needs.
struct Candidate {
    case_id: String,
    description: String,
    repo: String,
    path: String,
    line: i64,
    /// Findings carry the raw-line hash; escapes have none.
    line_hash: Option<String>,
    defective_rev: String,
    fixed_rev: String,
    claim: String,
    severity: Option<String>,
    category: String,
}

/// Effective verdict per finding (design section 3.4), plus the latest
/// automatic `likely-addressed` commit. A commit cited on the accepted
/// verdict itself (`review_log.py mark --commit`) takes precedence.
const FINDINGS_SQL: &str = "
WITH eff AS (
    SELECT finding_id, verdict, commit_sha FROM (
        SELECT v.*, ROW_NUMBER() OVER (
            PARTITION BY finding_id
            ORDER BY (auto = 0) DESC, decided_at DESC, verdict_id DESC
        ) AS rn
        FROM eval_verdicts v
    ) WHERE rn = 1
), fix AS (
    SELECT finding_id, commit_sha FROM (
        SELECT finding_id, commit_sha, ROW_NUMBER() OVER (
            PARTITION BY finding_id ORDER BY decided_at DESC, verdict_id DESC
        ) AS rn
        FROM eval_verdicts
        WHERE auto = 1 AND verdict = 'likely-addressed' AND commit_sha IS NOT NULL
    ) WHERE rn = 1
)
SELECT f.finding_id, f.repo, f.path, f.side, f.line, f.line_hash, f.severity,
       f.category, f.claim, r.head_sha, COALESCE(eff.commit_sha, fix.commit_sha)
FROM eval_findings f
JOIN eff ON eff.finding_id = f.finding_id AND eff.verdict = 'accepted'
JOIN eval_review_runs r ON r.run_id = f.run_id
LEFT JOIN fix ON fix.finding_id = f.finding_id
WHERE f.logged_at >= ?1 AND (?2 IS NULL OR f.repo = ?2)
ORDER BY f.finding_id";

const ESCAPES_SQL: &str = "
SELECT escape_id, repo, path, line, fix_commit, note
FROM eval_escapes
WHERE recorded_at >= ?1 AND (?2 IS NULL OR repo = ?2)
ORDER BY escape_id";

/// Write one `<case id>.json` per exportable finding or escape into `out`.
/// `since_ms` bounds `logged_at` (findings) and `recorded_at` (escapes).
pub fn export_bench_cases(
    conn: &Connection,
    out: &Path,
    redaction: &[RedactionEngine],
    since_ms: i64,
    repo: Option<&str>,
) -> anyhow::Result<ExportSummary> {
    refuse_inside_checkout(out)?;
    hippo_core::storage::ensure_private_dir(out)?;
    let mut summary = ExportSummary::default();
    let skip = |summary: &mut ExportSummary, reason| {
        *summary.skipped.entry(reason).or_default() += 1;
    };

    let mut candidates = Vec::new();
    let mut stmt = conn.prepare(FINDINGS_SQL)?;
    let mut rows = stmt.query(rusqlite::params![since_ms, repo])?;
    while let Some(row) = rows.next()? {
        let id: String = row.get(0)?;
        let side: String = row.get(3)?;
        let hash: Option<String> = row.get(5)?;
        let commit: Option<String> = row.get(10)?;
        let reason = match (side.as_str(), &hash, &commit) {
            // The flagged line is absent from the defective file.
            ("-", _, _) => Some("removed_line_finding"),
            (_, None, _) => Some("anchor_not_found"),
            (_, _, None) => Some("no_linked_commit"),
            _ => None,
        };
        if let Some(reason) = reason {
            skip(&mut summary, reason);
            continue;
        }
        candidates.push(Candidate {
            case_id: format!("f-{id}"),
            description: format!(
                "Accepted finding {id}: the change reintroduces the flagged line."
            ),
            repo: row.get(1)?,
            path: row.get(2)?,
            line: row.get(4)?,
            line_hash: hash,
            defective_rev: row.get(9)?,
            fixed_rev: commit.unwrap_or_default(),
            claim: row.get::<_, Option<String>>(8)?.unwrap_or_default(),
            severity: row.get(6)?,
            category: row.get(7)?,
        });
    }
    let mut stmt = conn.prepare(ESCAPES_SQL)?;
    let mut rows = stmt.query(rusqlite::params![since_ms, repo])?;
    while let Some(row) = rows.next()? {
        let id: String = row.get(0)?;
        let Some(fix) = row.get::<_, Option<String>>(4)? else {
            skip(&mut summary, "no_linked_commit");
            continue;
        };
        candidates.push(Candidate {
            case_id: format!("e-{id}"),
            description: format!("Escaped defect {id}: the change reverts its fix."),
            repo: row.get(1)?,
            path: row.get(2)?,
            line: row.get(3)?,
            line_hash: None,
            defective_rev: format!("{fix}^"),
            fixed_rev: fix,
            claim: row.get(5)?,
            severity: None,
            category: "unknown".into(),
        });
    }

    for candidate in &candidates {
        match build_case(candidate, redaction) {
            Ok(case) => {
                let path = out.join(format!("{}.json", candidate.case_id));
                std::fs::write(&path, serde_json::to_string_pretty(&case)? + "\n")
                    .with_context(|| format!("writing {}", path.display()))?;
                summary.written += 1;
            }
            Err(reason) => skip(&mut summary, reason),
        }
    }
    Ok(summary)
}

/// Refuse an `out` inside a git checkout, so a private case cannot be
/// committed by accident. The check runs on the nearest existing ancestor,
/// before the directory is created.
fn refuse_inside_checkout(out: &Path) -> anyhow::Result<()> {
    let absolute = std::path::absolute(out)?;
    let existing = absolute
        .ancestors()
        .find(|p| p.exists())
        .context("no existing ancestor of the output directory")?;
    if git(existing, &["rev-parse", "--is-inside-work-tree"]).is_some() {
        anyhow::bail!(
            "refusing to write private bench cases inside a git checkout: {}",
            absolute.display()
        );
    }
    Ok(())
}

fn build_case(c: &Candidate, redaction: &[RedactionEngine]) -> Result<Json, &'static str> {
    let repo = Path::new(&c.repo);
    if !repo.is_dir() || git(repo, &["rev-parse", "--git-dir"]).is_none() {
        return Err("repo_missing");
    }
    let defective = show(repo, &c.defective_rev, &c.path).ok_or("file_missing_at_revision")?;
    let fixed = show(repo, &c.fixed_rev, &c.path).ok_or("file_missing_at_revision")?;
    let lines: Vec<&str> = defective.split('\n').collect();
    let at = |n: usize| n.checked_sub(1).and_then(|i| lines.get(i)).copied();

    let given = usize::try_from(c.line).unwrap_or(0);
    let anchor = match &c.line_hash {
        None => given,
        // The review may have seen uncommitted edits on top of head_sha, so
        // the flagged line can sit elsewhere in the committed file.
        Some(hash) if at(given).is_some_and(|l| line_hash(l) == *hash) => given,
        Some(hash) => {
            let mut hits = (1..=lines.len()).filter(|&n| line_hash(lines[n - 1]) == *hash);
            match (hits.next(), hits.next()) {
                (Some(n), None) => n,
                _ => return Err("anchor_not_found"),
            }
        }
    };
    let raw_anchor = at(anchor).ok_or("anchor_not_found")?;
    if !added_rows(repo, &c.fixed_rev, &c.defective_rev, &c.path).contains(&anchor) {
        return Err("anchor_not_changed_by_fix");
    }
    let redact = |text: &str| {
        redaction
            .iter()
            .fold(text.to_owned(), |acc, engine| engine.redact(&acc).text)
    };
    if redact(raw_anchor) != raw_anchor {
        return Err("redaction_changed_anchor");
    }
    // Line by line, so a pattern cannot join lines and move the anchor.
    let redact_file = |text: &str| text.split('\n').map(redact).collect::<Vec<_>>().join("\n");

    let mut trap = json!({
        "id": "defect",
        "kind": "bug",
        "category": c.category,
        "path": c.path,
        "anchors": [["+", anchor]],
        "why": redact(&c.claim),
    });
    if let Some(severity) = &c.severity {
        trap["severity"] = json!(severity);
    }
    let file = |text: &str| {
        Json::Object(
            [(c.path.clone(), json!(redact_file(text)))]
                .into_iter()
                .collect(),
        )
    };
    Ok(json!({
        "id": c.case_id,
        "description": c.description,
        "base": file(&fixed),
        "head": file(&defective),
        "traps": [trap],
    }))
}

/// Line numbers of `to` that `git diff from to` adds, using the hunk headers
/// of a zero-context histogram diff, the form the mechanical strip uses.
fn added_rows(repo: &Path, from: &str, to: &str, path: &str) -> Vec<usize> {
    let args = [
        "diff",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--no-renames",
        "--histogram",
        "-U0",
        from,
        to,
        "--",
        path,
    ];
    let Some(diff) = git(repo, &args) else {
        return Vec::new();
    };
    String::from_utf8_lossy(&diff)
        .lines()
        .filter_map(|l| l.strip_prefix("@@ "))
        .filter_map(|l| l.split(' ').find_map(|t| t.strip_prefix('+')))
        .flat_map(|range| {
            let (start, count) = range.split_once(',').unwrap_or((range, "1"));
            let start: usize = start.parse().unwrap_or(0);
            start..start + count.parse::<usize>().unwrap_or(0)
        })
        .collect()
}

/// `git show REV:PATH` as UTF-8 text, or `None` when it is missing or binary.
fn show(repo: &Path, rev: &str, path: &str) -> Option<String> {
    String::from_utf8(git(repo, &["show", &format!("{rev}:{path}")])?).ok()
}

/// Run git in `dir` and return stdout on success. `GIT_DIR` and
/// `GIT_WORK_TREE` are cleared so the caller's environment cannot redirect it.
fn git(dir: &Path, args: &[&str]) -> Option<Vec<u8>> {
    let out = Command::new("git")
        .arg("-C")
        .arg(dir)
        .args(args)
        .env_remove("GIT_DIR")
        .env_remove("GIT_WORK_TREE")
        .stdin(Stdio::null())
        .stderr(Stdio::null())
        .output()
        .ok()?;
    out.status.success().then_some(out.stdout)
}
