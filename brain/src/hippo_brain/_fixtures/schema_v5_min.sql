-- Minimal v5 schema subset for unit tests.
-- Includes only the tables needed for GitHub Actions query tests.

CREATE TABLE IF NOT EXISTS knowledge_nodes (
    id          INTEGER PRIMARY KEY,
    kind        TEXT,
    title       TEXT,
    body        TEXT,
    created_at  INTEGER
);

CREATE TABLE IF NOT EXISTS workflow_runs (
    id              INTEGER PRIMARY KEY,
    repo            TEXT NOT NULL,
    head_sha        TEXT NOT NULL,
    head_branch     TEXT,
    event           TEXT NOT NULL,
    status          TEXT NOT NULL,
    conclusion      TEXT,
    started_at      INTEGER,
    completed_at    INTEGER,
    html_url        TEXT NOT NULL,
    actor           TEXT,
    raw_json        TEXT NOT NULL,
    first_seen_at   INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    enriched        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS workflow_jobs (
    id              INTEGER PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES workflow_runs(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL,
    conclusion      TEXT,
    started_at      INTEGER,
    completed_at    INTEGER,
    runner_name     TEXT,
    raw_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workflow_annotations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES workflow_jobs(id) ON DELETE CASCADE,
    level           TEXT NOT NULL,
    tool            TEXT,
    rule_id         TEXT,
    path            TEXT,
    start_line      INTEGER,
    message         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS lessons (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    repo            TEXT NOT NULL,
    tool            TEXT NOT NULL DEFAULT '',
    rule_id         TEXT NOT NULL DEFAULT '',
    path_prefix     TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL,
    fix_hint        TEXT,
    occurrences     INTEGER NOT NULL DEFAULT 1,
    first_seen_at   INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    UNIQUE(repo, tool, rule_id, path_prefix)
);
