//! `hippo agents report`: per-agent scorecards from the brain (HIPO-67).
//!
//! The brain owns the aggregate SQL (`hippo_brain.scorecard_queries`) and serves
//! it at `GET /agents/scorecard`, the same data as the MCP tool
//! `get_agent_scorecard`. This module fetches that JSON and renders one table
//! per agent. Field definitions: `docs/mcp-reference.md#get_agent_scorecard`.

use std::fmt::Write as _;
use std::time::Duration;

use anyhow::{Context, Result, bail};
use serde_json::Value;

/// Optional filters, sent as query parameters when present.
#[derive(Debug, Default)]
pub struct ReportFilters<'a> {
    pub agent: Option<&'a str>,
    pub since: Option<&'a str>,
    pub project: Option<&'a str>,
    pub model: Option<&'a str>,
}

/// Fetch scorecards from the brain at `base_url` (for example `http://localhost:9175`).
pub async fn fetch(base_url: &str, filters: &ReportFilters<'_>) -> Result<Value> {
    let params = [
        ("agent", filters.agent),
        ("since", filters.since),
        ("project", filters.project),
        ("model", filters.model),
    ];
    let url = url::Url::parse_with_params(
        &format!("{base_url}/agents/scorecard"),
        params
            .iter()
            .filter_map(|(key, value)| value.map(|v| (*key, v))),
    )
    .context("invalid brain URL")?;
    let resp = reqwest::Client::new()
        .get(url.clone())
        .timeout(Duration::from_secs(30))
        .send()
        .await
        .with_context(|| {
            format!("brain server unavailable at {url}; start it with `mise run start`")
        })?;
    let status = resp.status();
    let text = resp.text().await.context("reading the brain response")?;
    if status == reqwest::StatusCode::NOT_FOUND {
        bail!("brain returned {status}: the running brain predates /agents/scorecard; restart it");
    }
    if !status.is_success() {
        let error = serde_json::from_str::<Value>(&text)
            .ok()
            .and_then(|body| body.get("error")?.as_str().map(str::to_string))
            .unwrap_or_else(|| text.trim().chars().take(200).collect());
        bail!("brain returned {status}: {error}");
    }
    serde_json::from_str(&text).context("brain returned a non-JSON response")
}

fn int(value: &Value, pointer: &str) -> i64 {
    value.pointer(pointer).and_then(Value::as_i64).unwrap_or(0)
}

fn seconds(value: &Value, pointer: &str) -> String {
    value
        .pointer(pointer)
        .and_then(Value::as_f64)
        .map_or_else(|| "n/a".to_string(), |ms| format!("{:.1}s", ms / 1000.0))
}

fn date(ms: i64) -> String {
    chrono::DateTime::from_timestamp_millis(ms)
        .map(|dt| dt.format("%Y-%m-%d").to_string())
        .unwrap_or_default()
}

/// `key count` pairs from a JSON object of counts, comma-separated.
fn tally(value: &Value, pointer: &str) -> String {
    let pairs: Vec<String> = value
        .pointer(pointer)
        .and_then(Value::as_object)
        .into_iter()
        .flatten()
        .map(|(key, count)| format!("{key} {}", count.as_i64().unwrap_or(0)))
        .collect();
    if pairs.is_empty() {
        "none".to_string()
    } else {
        pairs.join(", ")
    }
}

fn tokens(value: &Value, pointer: &str) -> String {
    [
        ("input", "input_tokens"),
        ("output", "output_tokens"),
        ("cache read", "cache_read_tokens"),
        ("cache creation", "cache_creation_tokens"),
        ("total", "token_count"),
    ]
    .iter()
    .filter_map(|(label, key)| {
        value
            .pointer(&format!("{pointer}/{key}"))
            .and_then(Value::as_i64)
            .map(|n| format!("{label} {n}"))
    })
    .collect::<Vec<_>>()
    .join(", ")
}

fn number(value: &Value) -> String {
    match value {
        Value::Number(n) => n.to_string(),
        other => other.to_string(),
    }
}

fn render_card(out: &mut String, card: &Value, low_sample_min: i64) {
    let agent = card.get("agent").and_then(Value::as_str).unwrap_or("");
    let _ = writeln!(out, "== {agent} ==");

    let _ = writeln!(
        out,
        "  invocations        {} (subagent {})",
        int(card, "/sessions/invocations"),
        int(card, "/sessions/subagent_invocations")
    );
    let _ = writeln!(
        out,
        "  sessions           {} distinct, {} projects",
        int(card, "/sessions/distinct_sessions"),
        int(card, "/sessions/distinct_projects")
    );
    let _ = writeln!(
        out,
        "  duration           median {}, p90 {} (n={}, {} from session facts)",
        seconds(card, "/sessions/duration_ms/median"),
        seconds(card, "/sessions/duration_ms/p90"),
        int(card, "/sessions/duration_ms/denominator"),
        int(card, "/sessions/duration_ms/from_facts")
    );
    let _ = writeln!(
        out,
        "  tokens             {}",
        tokens(card, "/sessions/tokens")
    );
    let cost = card
        .pointer("/sessions/cost_usd/total")
        .and_then(Value::as_f64)
        .map_or_else(|| "n/a".to_string(), |usd| format!("${usd:.4}"));
    let _ = writeln!(
        out,
        "  cost               {cost} (n={} sessions with a cost)",
        int(card, "/sessions/cost_usd/denominator")
    );

    let _ = writeln!(
        out,
        "  review runs        {} (failed {}), units {} (failed {})",
        int(card, "/review_runs/runs"),
        int(card, "/review_runs/failed_runs"),
        int(card, "/review_runs/units_total"),
        int(card, "/review_runs/units_failed")
    );
    let _ = writeln!(
        out,
        "  reviewer tokens    {}",
        tokens(card, "/review_runs/tokens")
    );
    let _ = writeln!(
        out,
        "  findings           {} (judged {}, open {})",
        int(card, "/findings/total"),
        int(card, "/findings/judged"),
        int(card, "/findings/open")
    );
    let _ = writeln!(
        out,
        "  by severity        {}",
        tally(card, "/findings/by_severity")
    );
    let _ = writeln!(
        out,
        "  by category        {}",
        tally(card, "/findings/by_category")
    );
    let _ = writeln!(out, "  verdict            rate     count/judged");
    for name in [
        "accepted",
        "false-positive",
        "not-useful",
        "likely-addressed",
    ] {
        let base = format!("/findings/verdict_rates/{name}");
        let rate = card
            .pointer(&format!("{base}/rate"))
            .and_then(Value::as_f64)
            .map_or_else(|| "n/a".to_string(), |r| format!("{:.1}%", r * 100.0));
        let low = card
            .pointer(&format!("{base}/low_sample"))
            .and_then(Value::as_bool)
            .unwrap_or(true);
        let marker = if low {
            format!("  low sample (<{low_sample_min} judged)")
        } else {
            String::new()
        };
        let _ = writeln!(
            out,
            "    {name:<16} {rate:<8} {}/{}{marker}",
            int(card, &format!("{base}/count")),
            int(card, &format!("{base}/denominator"))
        );
    }
    let _ = writeln!(
        out,
        "  likely-addressed   {} with an inferred fix commit",
        int(card, "/findings/likely_addressed_inferred")
    );
    let _ = writeln!(out, "  escapes            {}", int(card, "/escapes"));

    let benches = card
        .get("benchmarks")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or_default();
    if benches.is_empty() {
        let _ = writeln!(out, "  benchmarks         none");
    }
    for bench in benches {
        let name = bench.get("bench").and_then(Value::as_str).unwrap_or("");
        let run = bench
            .get("bench_run_id")
            .and_then(Value::as_str)
            .unwrap_or("");
        let previous = bench
            .pointer("/previous/bench_run_id")
            .and_then(Value::as_str)
            .map_or_else(
                || "no earlier run with the same cases".to_string(),
                |id| format!("vs {id}"),
            );
        let _ = writeln!(
            out,
            "  benchmark          {name} {run} ({}, {previous})",
            date(int(bench, "/recorded_at"))
        );
        let trend = bench.get("trend");
        for (key, score) in bench
            .get("scores")
            .and_then(Value::as_object)
            .into_iter()
            .flatten()
        {
            let delta = trend
                .and_then(|t| t.get(key))
                .and_then(Value::as_f64)
                .map(|d| format!(" ({d:+})"))
                .unwrap_or_default();
            let _ = writeln!(out, "    {key:<24} {}{delta}", number(score));
        }
    }
}

/// Render the brain's scorecard response as one table per agent.
pub fn render(body: &Value) -> String {
    let mut out = String::new();
    let since = int(body, "/filters/since_ms");
    let window = if since > 0 {
        format!("since {}", date(since))
    } else {
        "all time".to_string()
    };
    let mut scope = vec![window];
    for key in ["project", "model"] {
        if let Some(v) = body
            .pointer(&format!("/filters/{key}"))
            .and_then(Value::as_str)
            .filter(|v| !v.is_empty())
        {
            scope.push(format!("{key} {v}"));
        }
    }
    let _ = writeln!(out, "Agent scorecards ({})", scope.join(", "));
    let cards = body
        .get("scorecards")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or_default();
    if cards.is_empty() {
        out.push_str("No agent activity matches the filters.\n");
    }
    let low_sample_min = body
        .get("low_sample_min")
        .and_then(Value::as_i64)
        .unwrap_or(5);
    for card in cards {
        out.push('\n');
        render_card(&mut out, card, low_sample_min);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::matchers::{method, path, query_param};
    use wiremock::{Mock, MockServer, ResponseTemplate};

    fn rate(count: i64, denominator: i64, rate: f64, low_sample: bool) -> Value {
        serde_json::json!({"count": count, "denominator": denominator, "rate": rate,
            "low_sample": low_sample})
    }

    fn sample() -> Value {
        serde_json::json!({
            "filters": {"agent": "code-reviewer", "since_ms": 0, "project": "alpha", "model": ""},
            "low_sample_min": 5,
            "scorecards": [{
                "agent": "code-reviewer",
                "sessions": {
                    "invocations": 3, "subagent_invocations": 3,
                    "distinct_sessions": 2, "distinct_projects": 2,
                    "duration_ms": {"median": 50000, "p90": 90000, "denominator": 3,
                        "from_facts": 2},
                    "tokens": {"input_tokens": 315, "output_tokens": 36,
                        "cache_read_tokens": 3005, "cache_creation_tokens": 55,
                        "token_count": 3411},
                    "cost_usd": {"total": 0.75, "denominator": 2}
                },
                "review_runs": {"runs": 2, "failed_runs": 1, "units_total": 7,
                    "units_failed": 1, "tokens": {"input_tokens": 200}},
                "findings": {
                    "total": 7, "open": 1, "judged": 6,
                    "by_severity": {"high": 3, "low": 2, "medium": 2},
                    "by_category": {"security": 3},
                    "by_verdict": {"accepted": 2},
                    "verdict_rates": {
                        "accepted": rate(2, 6, 0.3333, false),
                        "false-positive": rate(1, 6, 0.1667, false),
                        "not-useful": rate(0, 4, 0.0, true),
                        "likely-addressed": rate(2, 6, 0.3333, false)
                    },
                    "likely_addressed_inferred": 1
                },
                "escapes": 2,
                "benchmarks": [{
                    "bench": "code-reviewer-mechanical", "bench_run_id": "b3",
                    "recorded_at": 1792800000000_i64,
                    "scores": {"recall": 0.9, "extra": 0.05, "stable": true},
                    "previous": {"bench_run_id": "b1"},
                    "trend": {"recall": 0.1, "extra": -0.05}
                }]
            }]
        })
    }

    #[test]
    fn render_shows_denominators_low_sample_and_trend() {
        let text = render(&sample());
        assert!(text.starts_with("Agent scorecards (all time, project alpha)\n"));
        assert!(text.contains("== code-reviewer =="));
        assert!(text.contains("invocations        3 (subagent 3)"));
        assert!(text.contains("median 50.0s, p90 90.0s (n=3, 2 from session facts)"));
        assert!(text.contains("cost               $0.7500 (n=2 sessions with a cost)"));
        assert!(text.contains("findings           7 (judged 6, open 1)"));
        assert!(text.contains("by severity        high 3, low 2, medium 2"));
        assert!(text.contains("    accepted         33.3%    2/6\n"));
        assert!(text.contains("    not-useful       0.0%     0/4  low sample (<5 judged)\n"));
        assert!(text.contains("escapes            2"));
        assert!(text.contains("code-reviewer-mechanical b3 (2026-10-24, vs b1)"));
        assert!(text.contains("recall                   0.9 (+0.1)"));
        assert!(text.contains("extra                    0.05 (-0.05)"));
        assert!(text.contains("stable                   true\n"));
    }

    #[test]
    fn render_reports_no_activity() {
        let body = serde_json::json!({"filters": {"since_ms": 1792800000000_i64},
            "scorecards": []});
        assert_eq!(
            render(&body),
            "Agent scorecards (since 2026-10-24)\nNo agent activity matches the filters.\n"
        );
    }

    #[tokio::test]
    async fn fetch_sends_filters_and_returns_the_body() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/agents/scorecard"))
            .and(query_param("agent", "code-reviewer"))
            .and(query_param("since", "30d"))
            .respond_with(ResponseTemplate::new(200).set_body_json(sample()))
            .expect(1)
            .mount(&server)
            .await;
        let filters = ReportFilters {
            agent: Some("code-reviewer"),
            since: Some("30d"),
            ..ReportFilters::default()
        };
        let body = fetch(&server.uri(), &filters).await.unwrap();
        assert_eq!(body, sample());
    }

    #[tokio::test]
    async fn fetch_surfaces_the_brain_error() {
        let server = MockServer::start().await;
        Mock::given(method("GET"))
            .and(path("/agents/scorecard"))
            .respond_with(
                ResponseTemplate::new(400)
                    .set_body_json(serde_json::json!({"error": "since must be <digits><m|h|d>"})),
            )
            .mount(&server)
            .await;
        let filters = ReportFilters {
            since: Some("30 days"),
            ..ReportFilters::default()
        };
        let err = fetch(&server.uri(), &filters).await.unwrap_err();
        assert!(
            err.to_string()
                .contains("400 Bad Request: since must be <digits><m|h|d>"),
            "{err}"
        );
    }

    #[tokio::test]
    async fn fetch_explains_a_brain_without_the_route() {
        let server = MockServer::start().await;
        let err = fetch(&server.uri(), &ReportFilters::default())
            .await
            .unwrap_err();
        assert!(
            err.to_string()
                .contains("404 Not Found: the running brain predates /agents/scorecard"),
            "{err}"
        );
    }

    #[tokio::test]
    async fn fetch_reports_an_unreachable_brain() {
        let err = fetch("http://127.0.0.1:1", &ReportFilters::default())
            .await
            .unwrap_err();
        assert!(
            err.to_string().contains("brain server unavailable"),
            "{err}"
        );
    }
}
