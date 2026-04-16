//! GitHub REST client for the gh-poll subcommand.

use anyhow::{Context, Result, bail};
use reqwest::{Client, StatusCode, header};
use serde::Deserialize;
use std::time::Duration;

pub const DEFAULT_PER_PAGE: u32 = 20;

#[derive(Debug, Clone, serde::Serialize, Deserialize)]
pub struct WorkflowRun {
    pub id: i64,
    pub head_sha: String,
    pub head_branch: Option<String>,
    pub status: String,
    pub conclusion: Option<String>,
    pub event: String,
    pub html_url: String,
    pub run_started_at: Option<String>,
    pub updated_at: Option<String>,
    pub actor: Option<Actor>,
}

#[derive(Debug, Clone, serde::Serialize, Deserialize)]
pub struct Actor {
    pub login: String,
}

#[derive(Debug, Clone, serde::Serialize, Deserialize)]
pub struct Job {
    pub id: i64,
    pub name: String,
    pub status: String,
    pub conclusion: Option<String>,
    pub started_at: Option<String>,
    pub completed_at: Option<String>,
    pub runner_name: Option<String>,
    pub check_run_url: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct Annotation {
    pub annotation_level: String,
    pub message: String,
    pub path: Option<String>,
    pub start_line: Option<i64>,
}

#[derive(Debug, Default, Clone)]
pub struct ListRunsQuery {
    pub per_page: Option<u32>,
    pub created_since: Option<String>, // ISO8601
}

pub struct GhApi {
    base_url: String,
    token: String,
    http: Client,
}

impl GhApi {
    pub fn new(base_url: String, token: String) -> Self {
        let base_url = base_url.trim_end_matches('/').to_string();
        let http = Client::builder()
            .user_agent(concat!("hippo-gh-poll/", env!("CARGO_PKG_VERSION")))
            .timeout(Duration::from_secs(30))
            .build()
            .expect("reqwest client");
        Self {
            base_url,
            token,
            http,
        }
    }

    async fn get_json<T: for<'de> Deserialize<'de>>(&self, url: &str) -> Result<T> {
        const MAX_RETRIES: u8 = 8;
        let mut attempts: u8 = 0;
        loop {
            let resp = self
                .http
                .get(url)
                .header(header::AUTHORIZATION, format!("Bearer {}", self.token))
                .header(header::ACCEPT, "application/vnd.github+json")
                .header("X-GitHub-Api-Version", "2022-11-28")
                .send()
                .await?;

            let status = resp.status();
            let headers = resp.headers().clone();

            // Secondary rate limit: 429 or 403 with retry-after header.
            let has_retry_after = headers.get("retry-after").is_some();
            let is_secondary_rate_limit = status == StatusCode::TOO_MANY_REQUESTS
                || (status == StatusCode::FORBIDDEN && has_retry_after);

            // Primary rate limit: 403 with x-ratelimit-remaining: 0.
            let is_primary_rate_limit = status == StatusCode::FORBIDDEN
                && !has_retry_after
                && headers
                    .get("x-ratelimit-remaining")
                    .and_then(|v| v.to_str().ok())
                    .and_then(|v| v.parse::<u64>().ok())
                    == Some(0);

            if is_secondary_rate_limit || is_primary_rate_limit {
                attempts += 1;
                if attempts > MAX_RETRIES {
                    bail!("GitHub rate-limited after {MAX_RETRIES} retries: {status}");
                }
                let wait = if let Some(retry_after) = headers
                    .get("retry-after")
                    .and_then(|v| v.to_str().ok())
                    .and_then(|v| v.parse::<u64>().ok())
                {
                    retry_after
                } else if let Some(reset) = headers
                    .get("x-ratelimit-reset")
                    .and_then(|v| v.to_str().ok())
                    .and_then(|v| v.parse::<u64>().ok())
                {
                    // x-ratelimit-reset is a Unix timestamp; sleep until then + 1s buffer.
                    let now = std::time::SystemTime::now()
                        .duration_since(std::time::UNIX_EPOCH)
                        .unwrap_or_default()
                        .as_secs();
                    reset.saturating_sub(now).max(1) + 1
                } else {
                    60
                };
                // Cap wait to 5 minutes to avoid unbounded sleeps from clock skew.
                let capped = wait.min(300);
                tokio::time::sleep(Duration::from_secs(capped)).await;
                continue;
            }

            if !status.is_success() {
                let body = resp.text().await.unwrap_or_default();
                bail!("GitHub API {status}: {body}");
            }
            return resp.json::<T>().await.context("parse GitHub response");
        }
    }

    pub async fn list_runs(&self, repo: &str, q: &ListRunsQuery) -> Result<Vec<WorkflowRun>> {
        #[derive(Deserialize)]
        struct Envelope {
            workflow_runs: Vec<WorkflowRun>,
        }

        let per_page = q.per_page.unwrap_or(DEFAULT_PER_PAGE);
        let mut url = format!(
            "{}/repos/{repo}/actions/runs?per_page={per_page}",
            self.base_url
        );
        if let Some(ref created) = q.created_since {
            url.push_str(&format!("&created=%3E={created}"));
        }
        let env: Envelope = self.get_json(&url).await?;
        Ok(env.workflow_runs)
    }

    pub async fn list_jobs(&self, repo: &str, run_id: i64) -> Result<Vec<Job>> {
        #[derive(Deserialize)]
        struct Envelope {
            jobs: Vec<Job>,
        }
        let url = format!("{}/repos/{repo}/actions/runs/{run_id}/jobs", self.base_url);
        let env: Envelope = self.get_json(&url).await?;
        Ok(env.jobs)
    }

    pub async fn get_annotations(&self, check_run_url: &str) -> Result<Vec<Annotation>> {
        // check_run_url looks like:
        //   https://api.github.com/repos/{owner}/{repo}/check-runs/{id}
        let url = format!("{check_run_url}/annotations");
        self.get_json(&url).await
    }

    /// Download the tail of a job's plain-text log.
    ///
    /// Uses the per-job endpoint (`/actions/jobs/{id}/logs`) which returns a
    /// redirect to a plain-text log file. (The per-*run* endpoint returns a ZIP
    /// archive — we intentionally use per-job to avoid decompression overhead.)
    pub async fn get_log_tail(
        &self,
        repo: &str,
        job_id: i64,
        max_bytes: usize,
    ) -> Result<(String, bool)> {
        let url = format!("{}/repos/{repo}/actions/jobs/{job_id}/logs", self.base_url);
        let resp = self
            .http
            .get(&url)
            .header(header::AUTHORIZATION, format!("Bearer {}", self.token))
            .send()
            .await?;
        let status = resp.status();
        if !status.is_success() {
            let body = resp.text().await.unwrap_or_default();
            bail!("GitHub log API {status}: {body}");
        }
        let bytes = resp.bytes().await?;

        // Defensive: per-job endpoint should return plain text, but bail
        // gracefully if we get a ZIP archive (PK\x03\x04 magic bytes).
        if bytes.len() >= 4 && bytes[..4] == [0x50, 0x4b, 0x03, 0x04] {
            bail!("GitHub log API returned a ZIP archive for job {job_id}; expected plain text");
        }

        if bytes.len() <= max_bytes {
            return Ok((String::from_utf8_lossy(&bytes).into_owned(), false));
        }
        let tail = &bytes[bytes.len() - max_bytes..];
        Ok((String::from_utf8_lossy(tail).into_owned(), true))
    }
}
