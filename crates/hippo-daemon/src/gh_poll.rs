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
