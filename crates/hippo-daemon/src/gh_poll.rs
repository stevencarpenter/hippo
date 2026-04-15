//! Orchestrates a single poll pass over watched repos.

use anyhow::{Context, Result};
use chrono::Utc;
use hippo_core::storage::{open_db, watchlist, workflow_store};
use std::path::Path;

use crate::gh_api::{GhApi, ListRunsQuery};

#[derive(Debug, Clone, Default)]
pub struct PollConfig {
    pub watched_repos: Vec<String>,
    pub log_excerpt_max_bytes: usize,
}

fn parse_ts(s: Option<&str>) -> Option<i64> {
    s.and_then(|v| chrono::DateTime::parse_from_rfc3339(v).ok())
        .map(|dt| dt.timestamp_millis())
}

pub async fn run_once(api: &GhApi, db_path: &Path, cfg: &PollConfig) -> Result<()> {
    let conn = open_db(db_path)?;
    let now = Utc::now().timestamp_millis();

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
            let raw = serde_json::to_string(&run).unwrap_or_default();
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
                    let _ = watchlist::mark_terminal(&conn, &run.head_sha, repo, concl)?;
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
                    let job_raw = serde_json::to_string(job).unwrap_or_default();
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
                        for a in annotations {
                            workflow_store::insert_annotation(
                                &conn,
                                job.id,
                                &job.name,
                                &a.annotation_level,
                                &a.message,
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
                        match api.get_log_tail(repo, job.id, cfg.log_excerpt_max_bytes).await {
                            Ok((excerpt, truncated)) => {
                                workflow_store::insert_log_excerpt(
                                    &conn, job.id, None, &excerpt, truncated,
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

    Ok(())
}
