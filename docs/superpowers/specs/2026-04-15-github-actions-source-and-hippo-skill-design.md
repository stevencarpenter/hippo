# GitHub Actions Source and Hippo Brain Skill Design

## Context

Hippo captures shell activity, Claude Code sessions, and Firefox browsing. What it cannot observe today is whether the work actually succeeded: a `git push` leaves the local environment, CI runs on GitHub, and the outcome never makes it back into the knowledge base. The user closes the session, reopens it tomorrow, and Claude Code has no memory of what passed, what failed, or what lessons were learned.

This design adds GitHub Actions as a fourth data source, narrowly scoped to **workflow-run outcomes** (runs, jobs, annotations, failure log excerpts). It then closes the loop by shipping a default Claude Code skill — `using-hippo-brain` — that teaches Claude when and how to consult hippo so that pushed changes, CI outcomes, and repeated mistakes become part of cross-session working memory.

The deliberate v1 scope excludes PRs, issues, reviews, and comments. Those threads are largely captured today via the Firefox source when you read them in-app. Workflow outcomes are the only GitHub signal that is both high-value and genuinely invisible to every other hippo source.

## Design Decisions

- **Ingestion shape**: scheduled poller, not webhooks. No public endpoint, no Cloudflare Tunnel dependency, and GitHub events are not latency-sensitive for a knowledge base.
- **Polling topology**: two-tier. A slow background poller (5–15 min) for all watched repos, plus a `sha_watchlist` that tightens to 30–60s polling for SHAs the user has just pushed, with a ~20 min expiry.
- **Scope**: `workflow_runs`, their `workflow_jobs`, structured `workflow_annotations`, and truncated `workflow_log_excerpts` for failed steps only. No full-log storage.
- **Annotation parsing**: annotations are parsed into `(tool, rule_id, path, start_line, message)` tuples so retrieval can filter by tool or rule, not grep a blob.
- **Lesson synthesis**: a new `lesson` knowledge-node type is produced by the brain enrichment when a failure → fix pattern is detected on the same branch within a SHA window. Lessons are clustered by `(repo, tool, rule_id, path_prefix)`; single-occurrence failures are stored as raw events but do not promote to lessons until a second occurrence is seen.
- **MCP surface**: two structured tools added (`get_ci_status`, `get_lessons`) alongside existing prose-oriented `ask`. Agent consumers prefer structured; human users can continue to use `ask`.
- **Claude integration**: a default skill (`using-hippo-brain`) ships inside the hippo repo, installed into `~/.claude/skills/` by `mise run install`. A minimal `PostToolUse` hook on `git push` registers the SHA in the watchlist. An optional `Notification` hook handles CI terminal failures when no session is active.
- **Machine gating**: personal-mac only in v1. Work machines have separate credentials and policies.
- **Distribution**: skill ships in-repo so its instructions stay versioned with the MCP tools it references.

## Architecture

```
                      ┌──────────────────────────────────┐
                      │  GitHub REST API                 │
                      │  /repos/{r}/actions/runs         │
                      │  /actions/runs/{id}/jobs         │
                      │  /check-runs/{id}/annotations    │
                      │  /actions/jobs/{id}/logs (302)   │
                      └───────────────┬──────────────────┘
                                      │ PAT auth
                                      │
┌────────────────────────────┐        │          ┌────────────────────────────┐
│  shell capture             │        │          │  hippo gh-poll (Rust)      │
│  PostToolUse hook on       │        │          │  • background 5–15 min     │
│  'git push' (Claude Code)  │        │          │  • tight 30–60s for        │
│  → register watchlist      │        │          │    watched SHAs            │
│    entry via daemon        │        │          │  • dedup by last_seen      │
└──────────────┬─────────────┘        │          │  • parse annotations       │
               │                      │          │  • fetch log tails on fail │
               │                      │          │  • emit events to daemon   │
               ▼                      │          └──────────────┬─────────────┘
┌──────────────────────────────────┐  │                         │
│  hippo-daemon (Rust)             │◄─┘                         │
│  • sha_watchlist table           │◄────────────────────────────┘
│  • workflow_* event insertion    │
│  • enrichment queue population   │
└──────────────┬───────────────────┘
               │ SQLite (WAL)
               ▼
┌──────────────────────────────────┐
│  hippo-brain (Python)            │
│  • enrich workflow_runs          │
│  • correlate with shell + claude │
│    sessions via head_sha + ±15m  │
│  • synthesize "change outcome"   │
│    knowledge nodes               │
│  • cluster repeated failures     │
│    → lessons                     │
└──────────────┬───────────────────┘
               │
               ▼
┌──────────────────────────────────┐
│  hippo MCP (Python)              │
│  • get_ci_status(repo, sha)      │
│  • get_lessons(repo?, path?, …)  │
│  • existing: ask, search_*, …    │
└──────────────┬───────────────────┘
               │ stdio
               ▼
┌──────────────────────────────────┐
│  Claude Code                     │
│  • using-hippo-brain skill       │
│    (shipped in hippo repo,       │
│    installed globally)           │
│  • optional Notification hook    │
│    for terminal CI failure when  │
│    no session is active          │
└──────────────────────────────────┘
```

## Components

### 1. `hippo gh-poll` — Rust subcommand

A new subcommand on the existing `hippo` CLI, executed by a launchd job on a 5-minute timer. Internal loop:

1. Load watched repos from `config.toml` `[github]` section and PAT from environment (`HIPPO_GITHUB_TOKEN`, sourced from the existing encrypted env).
2. Read `sha_watchlist` entries whose `expires_at > now`. Poll those first, at 30–60s cadence if the poller itself is invoked more frequently.
3. For each watched repo, call `GET /repos/{r}/actions/runs?per_page=20&created>={last_seen_at}`.
4. For each new or updated run:
   - Upsert into `workflow_runs`.
   - If `status == 'completed'` and not yet decomposed, fetch jobs + annotations; upsert into `workflow_jobs`, `workflow_annotations`.
   - If any job `conclusion in ('failure', 'cancelled')`, fetch `jobs/{id}/logs`, capture the last ~50 KB (configurable), upsert into `workflow_log_excerpts`.
   - Insert a row into `workflow_enrichment_queue`.
5. On terminal status for a watchlist SHA, delete the watchlist entry (success) or mark the entry as "terminal-failure" to enable the notification hook.

Rate-limit awareness: single authenticated token gets 5000 req/hr. Worst case (10 repos × 12 polls/hr × 3 API calls) is ~360 req/hr, well within budget. Backoff on `429`/`403 rate limit` respects the `x-ratelimit-reset` header.

Failure mode: poll errors are logged to stderr and counted by the metrics module; they do not block the main hippo daemon.

### 2. Schema additions (SQLite schema v5)

New tables in `crates/hippo-core/src/storage.rs`:

```sql
CREATE TABLE workflow_runs (
    id              INTEGER PRIMARY KEY,  -- GitHub run id
    repo            TEXT NOT NULL,
    head_sha        TEXT NOT NULL,
    head_branch     TEXT,
    event           TEXT NOT NULL,        -- 'push', 'pull_request', ...
    status          TEXT NOT NULL,        -- queued | in_progress | completed
    conclusion      TEXT,                 -- success | failure | cancelled | ...
    started_at      INTEGER,              -- epoch ms
    completed_at    INTEGER,
    html_url        TEXT NOT NULL,
    actor           TEXT,
    raw_json        TEXT NOT NULL,
    first_seen_at   INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL
);
CREATE INDEX idx_workflow_runs_sha ON workflow_runs(head_sha);
CREATE INDEX idx_workflow_runs_repo_started ON workflow_runs(repo, started_at);

CREATE TABLE workflow_jobs (
    id              INTEGER PRIMARY KEY,  -- GitHub job id
    run_id          INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL,
    conclusion      TEXT,
    started_at      INTEGER,
    completed_at    INTEGER,
    runner_name     TEXT,
    raw_json        TEXT NOT NULL
);
CREATE INDEX idx_workflow_jobs_run ON workflow_jobs(run_id);

CREATE TABLE workflow_annotations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES workflow_jobs(id) ON DELETE CASCADE,
    level           TEXT NOT NULL,        -- notice | warning | failure
    tool            TEXT,                 -- parsed: 'ruff', 'pytest', 'cargo', 'mypy'
    rule_id         TEXT,                 -- parsed: 'F401', 'E0308', ...
    path            TEXT,
    start_line      INTEGER,
    message         TEXT NOT NULL
);
CREATE INDEX idx_workflow_annotations_job ON workflow_annotations(job_id);
CREATE INDEX idx_workflow_annotations_tool_rule ON workflow_annotations(tool, rule_id);

CREATE TABLE workflow_log_excerpts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES workflow_jobs(id) ON DELETE CASCADE,
    step_name       TEXT,
    excerpt         TEXT NOT NULL,        -- truncated to config.toml max_bytes
    truncated       INTEGER NOT NULL      -- 0/1
);

CREATE TABLE sha_watchlist (
    sha             TEXT NOT NULL,
    repo            TEXT NOT NULL,
    created_at      INTEGER NOT NULL,
    expires_at      INTEGER NOT NULL,
    terminal_status TEXT,                 -- NULL until CI resolves
    PRIMARY KEY (sha, repo)
);
CREATE INDEX idx_sha_watchlist_expires ON sha_watchlist(expires_at);

CREATE TABLE workflow_enrichment_queue (
    run_id          INTEGER PRIMARY KEY REFERENCES workflow_runs(id) ON DELETE CASCADE,
    enqueued_at     INTEGER NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT
);

CREATE TABLE lessons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo            TEXT NOT NULL,
    tool            TEXT,
    rule_id         TEXT,
    path_prefix     TEXT,
    summary         TEXT NOT NULL,        -- LLM-synthesized
    fix_hint        TEXT,                 -- LLM-synthesized
    occurrences     INTEGER NOT NULL DEFAULT 1,
    first_seen_at   INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    UNIQUE(repo, tool, rule_id, path_prefix)
);

-- Join tables to the knowledge graph
CREATE TABLE knowledge_node_workflow_runs (
    node_id         INTEGER NOT NULL,
    run_id          INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    PRIMARY KEY (node_id, run_id)
);

CREATE TABLE knowledge_node_lessons (
    node_id         INTEGER NOT NULL,
    lesson_id       INTEGER NOT NULL REFERENCES lessons(id) ON DELETE CASCADE,
    PRIMARY KEY (node_id, lesson_id)
);
```

Migration path: a straightforward additive v4→v5 migration. No existing table is altered; no data re-indexed.

### 3. Annotation parser

Given a raw annotation body, produce `(tool, rule_id)` when possible. Implemented as a small pipeline of regexes in `crates/hippo-core`:

| Pattern | Extracted |
|---|---|
| `^(?P<rule>[EWF]\d{3,4})\b` in ruff step context | `tool=ruff`, `rule=$rule` |
| `error\[(?P<rule>E\d{4})\]` in cargo output | `tool=cargo`, `rule=$rule` |
| `error: .* \[(?P<rule>[a-z-]+)\]` in mypy/pyright context | `tool=mypy` or `pyright`, `rule=$rule` |
| `assert` or `FAILED .*::test_` in pytest output | `tool=pytest`, `rule=None` |

Job name is a strong prior — a job named `ruff` or `clippy` biases the parser. When no parser matches, the annotation is stored with `tool=NULL`; retrieval by tool simply skips it.

### 4. Brain enrichment — change outcome and lesson synthesis

Extend `brain/src/hippo_brain/enrichment.py` (or add `workflow_enrichment.py` following the `browser_enrichment.py` pattern) with two new responsibilities:

**Change outcome node.** For each workflow run enqueued for enrichment:

1. Look up any shell event matching `git push` with the run's `head_sha` within ±15 min of `started_at`.
2. Look up the Claude session active at that time (if any) and the files it touched.
3. Look up any browser events in the same window (often includes PR page visits post-push).
4. Build an enrichment prompt that combines run conclusion, top annotations (limit: 10), the diff files, and session context.
5. Write a knowledge node summarizing the change outcome, with edges to: the push event, the session, the workflow run, and any browser events.

**Lesson synthesis.** Runs on a per-repo schedule (hourly is sufficient):

1. Query recent failed annotations grouped by `(repo, tool, rule_id, path_prefix)` where `path_prefix` is the top two path segments.
2. For each cluster with `count >= 2` in the last N days (configurable, default 30), ensure a matching row in `lessons`. If new, synthesize `summary` + `fix_hint` via the configured LLM using the most recent example annotation plus the diff of the fix commit when available.
3. Update `occurrences` and `last_seen_at` on each poll. Do not create a lesson for a single-occurrence failure.

This clustering is what prevents the lesson store from becoming a landfill. Single-failure noise stays as raw events; only patterns graduate to lessons.

### 5. MCP tool additions

In `brain/src/hippo_brain/mcp.py` + `mcp_queries.py`, add two tools.

**`get_ci_status(repo: str, sha: str | None = None, branch: str | None = None) -> CIStatus`**

- If `sha` provided, returns the latest run for that SHA.
- If `branch` provided without SHA, returns the latest run for that branch.
- Returns a structured `CIStatus` with: conclusion, top 10 annotations (with tool/rule/path/line), failed-job names, duration, html_url.
- Machine-friendly JSON; no prose synthesis.

**`get_lessons(repo: str | None = None, path: str | None = None, tool: str | None = None, limit: int = 10) -> list[Lesson]`**

- Filters by any combination of repo, path prefix, tool.
- Ordered by `(occurrences DESC, last_seen_at DESC)` — frequency over recency.
- Returns: summary, fix hint, occurrences, last-seen timestamp, example annotation path/line.

Both tools return small, bounded payloads suitable for agent context windows. `ask` remains the prose-synthesis path for human queries.

### 6. `using-hippo-brain` Claude Code skill

New tree: `extension/claude-skill/using-hippo-brain/SKILL.md`. Parallels the existing `extension/firefox/` placement for external integrations.

**Contents.** The skill's frontmatter `description` is the load-bearing part — it determines when Claude proactively invokes it. The body teaches Claude:

- When to query hippo and when not to (explicit "yes"/"ok"/"proceed" responses do not warrant a query)
- The in-flight SHA mental model — after `git push`, the SHA is in flight until CI reaches terminal state; check once when the user re-engages after a pause
- How to pick between structured tools (`get_ci_status`, `get_lessons`) and prose synthesis (`ask`)
- How to surface CI failures: propose a fix with the failure context; do not bury the result
- How to handle session boundaries: at SessionStart, the skill is available but does not auto-query — it waits for context that warrants a query

Draft of the skill file is included verbatim in Appendix A to fix the contract. The exact wording will be iterated, but the behavioral shape is fixed.

**Installation.** `mise run install` creates a symlink from `$XDG_DATA_HOME/.claude/skills/using-hippo-brain/` (resolving to `~/.claude/skills/using-hippo-brain/`) to the in-repo source. Symlink rather than copy so edits are live during development.

**`hippo doctor`** gains a check: skill is installed at the expected path and points at the repo copy for the installed version. Mismatch (e.g., stale link from an older hippo install) surfaces as a warning.

### 7. Claude Code hooks (minimal)

Two hooks, both deterministic and narrow.

**`PostToolUse` on Bash `git push`.** Parses the pushed SHA (from `git rev-parse HEAD` invoked via a helper, not from push output parsing which is brittle) and POSTs a watchlist entry to the hippo daemon's Unix socket. Fire-and-forget; no blocking.

**Optional `SessionStart` hook for pending CI failures.** The daemon, on observing a watchlist SHA resolve to `failure`/`cancelled`, writes a marker file under `$XDG_DATA_HOME/hippo/pending-notifications/`. When a new Claude Code session starts in the affected repo, a `SessionStart` hook (running `hippo claude-session-context` or similar) reads and clears the marker, injecting a single system message into additional context: "CI failed on SHA X pushed at T; use `get_ci_status` to investigate." This is the safety net for "I closed the session before CI finished" — it is deliberately passive (no desktop notification) and triggers only at session boundaries, not on every tool call.

Hook configurations live in `shell/claude-hooks/` alongside the existing session hook, and the hippo install documentation instructs users to add the JSON stanzas to their `~/.claude/settings.json` (or delivered via `chezmoi` for dotfiles users).

### 8. Configuration (`config.toml`)

```toml
[github]
enabled = false                  # opt-in
poll_interval_secs = 300         # background cadence
tight_poll_interval_secs = 45    # for watchlist SHAs
watchlist_ttl_secs = 1200        # 20 min
log_excerpt_max_bytes = 51200    # 50 KB
watched_repos = [
    "sjcarpenter/hippo",
    # add more
]
token_env = "HIPPO_GITHUB_TOKEN"

[github.lessons]
cluster_window_days = 30
min_occurrences = 2
path_prefix_segments = 2
```

## Privacy and Security

- **Secret redaction.** Annotations, log excerpts, and run JSON are passed through the existing `redaction.rs` pipeline before insertion. GitHub already masks registered secrets in logs, but the hippo redactor applies `redact.toml` patterns as a defense-in-depth layer (emails, tokens, personal identifiers).
- **Token handling.** `HIPPO_GITHUB_TOKEN` is read from the environment only; never stored in SQLite, never logged. Sourced from the existing `dot_config/zsh/encrypted_dot_env`.
- **Scope.** Token needs `repo` + `workflow:read` scopes. Document the minimum scope in the install guide; reject tokens with more scope than necessary at first use.
- **Public vs private repos.** Both supported with the same token. The watched_repos allowlist is the scope boundary — no implicit discovery.
- **Log excerpt hygiene.** Truncate to `log_excerpt_max_bytes`; store `truncated=1` flag so downstream consumers know the full log is not retained.
- **Machine gating.** `[github]` section is only populated on personal-mac machines via chezmoi templating. Work machines do not ingest GitHub Actions data in v1.

## `hippo doctor` Additions

New checks:
- `[github]` enabled: token present, API reachability (`GET /rate_limit`), last successful poll within N × `poll_interval_secs`
- Skill installed at `~/.claude/skills/using-hippo-brain/SKILL.md` and pointing at the current hippo install
- `PostToolUse` hook registered in `~/.claude/settings.json` matching the repo's hook path
- Watchlist health: no entries older than `watchlist_ttl_secs × 2` (indicates the poller is not draining)

## Testing

- **Rust unit tests.** Annotation parser with fixtures for each supported tool. Schema migration v4→v5 round-trip. Watchlist lifecycle (insert, resolve, expire).
- **Rust integration test.** `hippo gh-poll` against a mocked GitHub API using `wiremock`; asserts dedup, pagination, rate-limit handling.
- **Python unit tests.** `get_ci_status`, `get_lessons` query shapes against a fixture DB. Lesson clustering across synthetic annotation streams.
- **Python integration test.** Change-outcome enrichment with co-temporal shell + workflow_run events.
- **Skill behavioral test.** Not automatable end-to-end, but a checklist of prompts to validate manually before merge (e.g., "I just pushed — does Claude check CI?", "I said 'yes' to a trivial question — does Claude avoid spurious queries?").
- **`hippo doctor` smoke test.** CI runs `hippo doctor` and asserts all github-related checks pass in the test environment.

## Out of Scope for v1

- General GitHub events ingestion (PRs, issues, comments, reviews). Considered for v2 once the Actions feedback loop is proven.
- Webhook ingestion. Polling is sufficient.
- Work-machine support. Separate credential and policy story.
- Apple Notes source. Explicitly descoped by the user during brainstorming.
- Cross-repo lesson transfer. Lessons are repo-scoped in v1; generalizing across repos is a future question.
- Auto-proposing fixes via Claude. The skill tells Claude to surface failures; it does not tell Claude to silently push fixes.

## Open Questions

- **Tight-poll invocation cadence.** The launchd timer granularity is per-job. Options: run the poll binary every minute and have it decide internally whether to do a background or tight pass; or run two launchd jobs. Prefer the single-binary approach for simpler observability; revisit if load becomes an issue.
- **Lesson fix-hint quality.** The LLM synthesis for `fix_hint` depends on having the fix diff available, which requires a subsequent green commit. Lessons created from repeated failures without a resolved fix should have `fix_hint = NULL` and note "no resolved fix observed." Worth confirming this is acceptable.
- **Skill description wording.** The frontmatter `description` determines proactive triggering. Initial draft is in Appendix A; expected to iterate based on observed Claude behavior during dogfooding.
- **launchd environment.** `launchctl` loaded jobs do not inherit shell env, so `HIPPO_GITHUB_TOKEN` must be passed via the plist's `EnvironmentVariables` block (populated at install time from the encrypted env) or read from a config file by the poller. The existing hippo launchd plists already solve this pattern; v1 will follow whatever convention they use.
- **Watched-repo discovery.** v1 requires manual `watched_repos` entries in config. A future iteration could auto-populate from git remotes seen in shell capture, but the scope-boundary implications of implicit discovery need their own design pass.

## Appendix A — Draft `SKILL.md`

```markdown
---
name: using-hippo-brain
description: Use when working in a repo with hippo coverage — query past
  CI outcomes for in-flight pushes, retrieve lessons before editing in
  failure-prone areas, and answer retrospective questions about what
  was done. Do not invoke for routine acknowledgments or tiny exchanges.
---

# Using the Hippo Brain

You have access to a persistent local knowledge base via the `hippo` MCP
server. It captures shell activity, prior Claude sessions, browser
history, and CI outcomes from GitHub Actions. Use it as memory across
sessions.

## When to query (and when not to)

| Situation | Action |
|---|---|
| Starting substantive work in a repo for the first time this session | Optional: `get_lessons(repo=<repo>)` for high-frequency patterns |
| Just edited or about to edit a file with a known failure history | `get_lessons(path=<path>)` |
| `git push` happened earlier in this session | Track the SHA mentally; when the user next re-engages or pauses, call `get_ci_status(repo, sha)` once |
| User asks "did it pass" / "what failed" / "what did I do" | `get_ci_status` or `ask` as appropriate |
| User says "yes", "ok", "proceed", "go ahead" | Do nothing. These are flow control, not work boundaries. |
| Routine multi-turn implementation | Do nothing. Don't poll between every edit. |

## In-flight SHA mental model

After `git push origin <branch>`, that SHA is "in flight" until CI
reaches a terminal state (typically 3–10 min). You don't need to poll.
Check once when the user re-engages after a quiet pause, or when
starting a new task. If CI failed, surface the annotations and propose
a fix — don't bury it. If CI passed, no need to mention unless asked.

## Tool selection

- `get_ci_status(repo, sha)` — structured CI outcome. Use for "did it pass."
- `get_lessons(repo?, path?, tool?)` — distilled past mistakes. Use pre-flight.
- `search_knowledge(query)` — semantic retrieval over knowledge nodes.
- `search_events(query)` — raw event timeline.
- `ask(question)` — synthesized prose answer. Use for human-shaped questions.
- `get_entities(...)` — graph exploration.

Prefer the structured tools over `ask` when you know what shape you
want — they are cheaper and machine-friendly. `ask` runs a full RAG
pipeline and returns prose.
```
