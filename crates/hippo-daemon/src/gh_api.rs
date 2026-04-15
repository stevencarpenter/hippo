//! GitHub REST client for the gh-poll subcommand.

use anyhow::{Context, Result, bail};
use reqwest::{Client, StatusCode, header};
use serde::Deserialize;
use std::time::Duration;

#[derive(Debug, Clone, Deserialize)]
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

#[derive(Debug, Clone, Deserialize)]
pub struct Actor {
    pub login: String,
}

#[derive(Debug, Clone, Deserialize)]
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
        let http = Client::builder()
            .user_agent(concat!("hippo-gh-poll/", env!("CARGO_PKG_VERSION")))
            .timeout(Duration::from_secs(30))
            .build()
            .expect("reqwest client");
        Self { base_url, token, http }
    }

    async fn get_json<T: for<'de> Deserialize<'de>>(&self, url: &str) -> Result<T> {
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
            if status == StatusCode::TOO_MANY_REQUESTS
                || (status == StatusCode::FORBIDDEN
                    && resp
                        .headers()
                        .get("x-ratelimit-remaining")
                        .and_then(|v| v.to_str().ok())
                        == Some("0"))
            {
                let wait = resp
                    .headers()
                    .get("retry-after")
                    .and_then(|v| v.to_str().ok())
                    .and_then(|v| v.parse::<u64>().ok())
                    .unwrap_or(60);
                tokio::time::sleep(Duration::from_secs(wait)).await;
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

        let per_page = q.per_page.unwrap_or(20);
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
        let url = format!(
            "{}/repos/{repo}/actions/runs/{run_id}/jobs",
            self.base_url
        );
        let env: Envelope = self.get_json(&url).await?;
        Ok(env.jobs)
    }

    pub async fn get_annotations(&self, check_run_url: &str) -> Result<Vec<Annotation>> {
        // check_run_url looks like:
        //   https://api.github.com/repos/{owner}/{repo}/check-runs/{id}
        let url = format!("{check_run_url}/annotations");
        self.get_json(&url).await
    }

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
        let bytes = resp.bytes().await?;
        if bytes.len() <= max_bytes {
            return Ok((String::from_utf8_lossy(&bytes).to_string(), false));
        }
        let tail = &bytes[bytes.len() - max_bytes..];
        Ok((String::from_utf8_lossy(tail).to_string(), true))
    }
}
