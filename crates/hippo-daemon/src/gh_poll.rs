//! Orchestrates a single poll pass over watched repos.

use anyhow::{Context, Result};
use chrono::Utc;
use hippo_core::redaction::RedactionEngine;
use hippo_core::storage::{open_db, watchlist, workflow_store};
use std::path::{Path, PathBuf};

use crate::gh_api::{GhApi, ListRunsQuery};

#[derive(Debug, Clone, Default)]
pub struct PollConfig {
    pub watched_repos: Vec<String>,
    pub log_excerpt_max_bytes: usize,
    /// Path to `redact.toml`. `None` → builtin patterns only.
    pub redact_config_path: Option<PathBuf>,
}

/// Resolve a GitHub token the same way the gh-poll launchd wrapper does:
/// process env first, then `~/.config/zsh/.env` (chezmoi-deployed), then
/// `gh auth token`. Used by install-time validation, the `hippo doctor`
/// check, and the gh-poll runtime — all three agree so the user doesn't hit
/// "doctor reports missing but launchd finds it" divergences.
///
/// Returns `None` only when none of those three sources yields a non-empty
/// token.
pub fn resolve_github_token(token_env: &str) -> Option<String> {
    if let Ok(v) = std::env::var(token_env)
        && !v.is_empty()
    {
        return Some(v);
    }
    if let Some(home) = dirs::home_dir() {
        let env_file = home.join(".config/zsh/.env");
        if let Some(v) = read_var_from_env_file(&env_file, token_env) {
            return Some(v);
        }
    }
    let output = std::process::Command::new("gh")
        .args(["auth", "token"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let token = String::from_utf8_lossy(&output.stdout).trim().to_string();
    (!token.is_empty()).then_some(token)
}

/// Minimal parser for the subset of bash assignment syntax that the wrapper
/// would resolve via `set -a; source FILE; set +a`. Handles:
///   - `KEY=value` and `export KEY=value`
///   - blank lines and `# comments`
///   - optional single or double quotes around the value
///
/// Does not expand `$VAR` references or handle heredocs — if the user's env
/// file uses those for their token, they can set the variable in the process
/// env directly, which is the first resolver path.
fn read_var_from_env_file(path: &Path, var_name: &str) -> Option<String> {
    let content = std::fs::read_to_string(path).ok()?;
    for line in content.lines() {
        let trimmed = line.trim();
        if trimmed.is_empty() || trimmed.starts_with('#') {
            continue;
        }
        let assignment = trimmed.strip_prefix("export ").unwrap_or(trimmed);
        let Some((key, value)) = assignment.split_once('=') else {
            continue;
        };
        if key.trim() != var_name {
            continue;
        }
        let value = value.trim();
        let unquoted = match (value.chars().next(), value.chars().last()) {
            (Some('"'), Some('"')) if value.len() >= 2 => &value[1..value.len() - 1],
            (Some('\''), Some('\'')) if value.len() >= 2 => &value[1..value.len() - 1],
            _ => value,
        };
        if !unquoted.is_empty() {
            return Some(unquoted.to_string());
        }
    }
    None
}

fn parse_ts(s: Option<&str>) -> Option<i64> {
    s.and_then(|v| chrono::DateTime::parse_from_rfc3339(v).ok())
        .map(|dt| dt.timestamp_millis())
}

pub async fn run_once(api: &GhApi, db_path: &Path, cfg: &PollConfig) -> Result<()> {
    let conn = open_db(db_path)?;
    let now = Utc::now().timestamp_millis();

    let redactor = match &cfg.redact_config_path {
        Some(path) => match RedactionEngine::from_config_path(path) {
            Ok(engine) => engine,
            Err(e) => {
                eprintln!(
                    "warn: failed to load redact config {}: {e}. Using builtin patterns.",
                    path.display()
                );
                RedactionEngine::builtin()
            }
        },
        None => RedactionEngine::builtin(),
    };

    for repo in &cfg.watched_repos {
        let runs = api
            .list_runs(
                repo,
                &ListRunsQuery {
                    per_page: Some(20),
                    ..Default::default()
                },
            )
            .await
            .with_context(|| format!("list_runs for {repo}"))?;

        for run in runs {
            let actor = run.actor.as_ref().map(|a| a.login.as_str());
            let raw = redactor
                .redact(&serde_json::to_string(&run).unwrap_or_default())
                .text;
            workflow_store::upsert_run(
                &conn,
                &workflow_store::RunRow {
                    id: run.id,
                    repo,
                    head_sha: &run.head_sha,
                    head_branch: run.head_branch.as_deref(),
                    event: &run.event,
                    status: &run.status,
                    conclusion: run.conclusion.as_deref(),
                    started_at: parse_ts(run.run_started_at.as_deref()),
                    completed_at: parse_ts(run.updated_at.as_deref()),
                    html_url: &run.html_url,
                    actor,
                    raw_json: &raw,
                },
                now,
            )?;

            if run.status == "completed" {
                if let Some(concl) = run.conclusion.as_deref() {
                    match concl {
                        "failure" | "cancelled" => {
                            let _ = watchlist::mark_terminal(&conn, &run.head_sha, repo, concl)?;
                        }
                        _ => {
                            // Success rows don't need notification — delete immediately.
                            let _ = conn.execute(
                                "DELETE FROM sha_watchlist WHERE sha = ?1 AND repo = ?2",
                                rusqlite::params![&run.head_sha, repo],
                            );
                        }
                    }
                }

                // Skip drill-down if this run was already enriched (fully processed).
                let already_enriched: bool = conn
                    .query_row(
                        "SELECT enriched FROM workflow_runs WHERE id = ?1",
                        [run.id],
                        |r| r.get(0),
                    )
                    .unwrap_or(false);
                if already_enriched {
                    continue;
                }

                let jobs = match api.list_jobs(repo, run.id).await {
                    Ok(jobs) => jobs,
                    Err(e) => {
                        // TODO: replace with structured logging
                        eprintln!("warn: list_jobs failed for {repo} run {}: {e}", run.id);
                        continue; // skip this run, try the next
                    }
                };
                for job in &jobs {
                    let job_raw = redactor
                        .redact(&serde_json::to_string(job).unwrap_or_default())
                        .text;
                    workflow_store::upsert_job(
                        &conn,
                        &workflow_store::JobRow {
                            id: job.id,
                            run_id: run.id,
                            name: &job.name,
                            status: &job.status,
                            conclusion: job.conclusion.as_deref(),
                            started_at: parse_ts(job.started_at.as_deref()),
                            completed_at: parse_ts(job.completed_at.as_deref()),
                            runner_name: job.runner_name.as_deref(),
                            raw_json: &job_raw,
                        },
                    )?;

                    if let Some(cru) = &job.check_run_url {
                        // TODO: replace with structured logging
                        let annotations = match api.get_annotations(cru).await {
                            Ok(a) => a,
                            Err(e) => {
                                eprintln!("warn: get_annotations failed for {cru}: {e}");
                                Vec::new()
                            }
                        };
                        // Clear stale annotations from a previous poll pass before re-inserting.
                        conn.execute(
                            "DELETE FROM workflow_annotations WHERE job_id = ?1",
                            [job.id],
                        )?;
                        for a in annotations {
                            let redacted_message = redactor.redact(&a.message).text;
                            workflow_store::insert_annotation(
                                &conn,
                                job.id,
                                &job.name,
                                &a.annotation_level,
                                &redacted_message,
                                a.path.as_deref(),
                                a.start_line,
                            )?;
                        }
                    }

                    if matches!(
                        job.conclusion.as_deref(),
                        Some("failure") | Some("cancelled")
                    ) {
                        // TODO: replace with structured logging
                        match api
                            .get_log_tail(repo, job.id, cfg.log_excerpt_max_bytes)
                            .await
                        {
                            Ok((excerpt, truncated)) => {
                                let redacted_excerpt = redactor.redact(&excerpt).text;
                                // Clear stale excerpts from a previous poll pass before re-inserting.
                                conn.execute(
                                    "DELETE FROM workflow_log_excerpts WHERE job_id = ?1",
                                    [job.id],
                                )?;
                                workflow_store::insert_log_excerpt(
                                    &conn,
                                    job.id,
                                    None,
                                    &redacted_excerpt,
                                    truncated,
                                )?;
                            }
                            Err(e) => eprintln!(
                                "warn: get_log_tail failed for {repo} job {}: {e}",
                                job.id
                            ),
                        }
                    }
                }

                workflow_store::enqueue_enrichment(&conn, run.id, now)?;
            }
        }
    }

    // Housekeeping: remove fully-processed, expired watchlist entries.
    let _ = watchlist::cleanup_expired(&conn, now);

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn resolve_prefers_env_over_gh() {
        // SAFETY: process env mutation. Unique var name isolates from parallel tests.
        let var = "HIPPO_GH_POLL_RESOLVER_TEST_ENV_WINS";
        unsafe {
            std::env::set_var(var, "env-value");
        }
        let got = resolve_github_token(var);
        unsafe {
            std::env::remove_var(var);
        }
        assert_eq!(got.as_deref(), Some("env-value"));
    }

    #[test]
    fn resolve_treats_empty_env_as_unset() {
        // If the env var is set but empty, the resolver must fall through to
        // `gh auth token` rather than returning "". This ensures an empty
        // export (e.g. `export HIPPO_GITHUB_TOKEN=` in a profile) doesn't
        // block the fallback.
        let var = "HIPPO_GH_POLL_RESOLVER_TEST_EMPTY_ENV";
        unsafe {
            std::env::set_var(var, "");
        }
        let got = resolve_github_token(var);
        unsafe {
            std::env::remove_var(var);
        }
        // Either None (no gh / not logged in in test env) or Some("...") if
        // this is a dev machine with `gh auth login` — both prove the empty
        // env was skipped, which is what we're verifying.
        assert!(got.as_deref() != Some(""));
    }

    #[test]
    fn env_file_parses_bare_assignment() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(".env");
        std::fs::write(&path, "FOO=bar\nBAZ=qux\n").unwrap();
        assert_eq!(read_var_from_env_file(&path, "FOO").as_deref(), Some("bar"));
        assert_eq!(read_var_from_env_file(&path, "BAZ").as_deref(), Some("qux"));
        assert_eq!(read_var_from_env_file(&path, "MISSING"), None);
    }

    #[test]
    fn env_file_parses_export_and_quotes() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(".env");
        std::fs::write(
            &path,
            "# a comment\n\nexport DQ=\"double quoted\"\nexport SQ='single quoted'\nPLAIN=plain\n",
        )
        .unwrap();
        assert_eq!(
            read_var_from_env_file(&path, "DQ").as_deref(),
            Some("double quoted")
        );
        assert_eq!(
            read_var_from_env_file(&path, "SQ").as_deref(),
            Some("single quoted")
        );
        assert_eq!(
            read_var_from_env_file(&path, "PLAIN").as_deref(),
            Some("plain")
        );
    }

    #[test]
    fn env_file_skips_empty_values_and_missing_file() {
        // Missing file → None (not an error path; file is optional).
        assert_eq!(
            read_var_from_env_file(Path::new("/nonexistent/path/.env"), "X"),
            None
        );

        // Empty assignment is treated as unset so we fall through to the next
        // resolver source, matching `if [ -z "$VAR" ]` in the wrapper.
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join(".env");
        std::fs::write(&path, "EMPTY=\n").unwrap();
        assert_eq!(read_var_from_env_file(&path, "EMPTY"), None);
    }
}
