-- v29 (agent evaluation records, HIPO-64). Idempotent; shared by the fresh
-- schema and the v28→v29 migration. Written only by `hippo ingest agent-eval`
-- and the payload GC. No brain enrichment, embedding, FTS, or classification
-- path reads these tables (docs/capture/anti-patterns.md AP-14).
-- DDL source: docs/superpowers/specs/2026-10-25-agent-eval-schema-design.md.
CREATE TABLE IF NOT EXISTS eval_review_runs (
    run_id                TEXT    PRIMARY KEY,
    agent                 TEXT    NOT NULL,
    mode                  TEXT    NOT NULL,
    requested_model       TEXT,
    model_id              TEXT    NOT NULL DEFAULT '',
    effort                TEXT,
    cli_version           TEXT    NOT NULL DEFAULT '',
    repo                  TEXT    NOT NULL,
    repo_identity         TEXT,
    base                  TEXT    NOT NULL,
    base_sha              TEXT    NOT NULL,
    head_sha              TEXT    NOT NULL,
    input_sha256          TEXT    NOT NULL,
    units_total           INTEGER NOT NULL DEFAULT 0,
    units_failed          INTEGER NOT NULL DEFAULT 0,
    input_tokens          INTEGER NOT NULL DEFAULT 0,
    output_tokens         INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
    cache_creation_tokens INTEGER NOT NULL DEFAULT 0,
    session_id            TEXT,
    harness               TEXT,
    started_at            INTEGER NOT NULL,
    finished_at           INTEGER NOT NULL,
    ingested_at           INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_review_runs_agent_started_at
    ON eval_review_runs (agent, started_at);
CREATE INDEX IF NOT EXISTS idx_eval_review_runs_session_id_harness
    ON eval_review_runs (session_id, harness) WHERE session_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_eval_review_runs_repo_identity_started_at
    ON eval_review_runs (repo_identity, started_at);

CREATE TABLE IF NOT EXISTS eval_findings (
    finding_id     TEXT    PRIMARY KEY,
    run_id         TEXT    NOT NULL REFERENCES eval_review_runs (run_id),
    repo           TEXT    NOT NULL,
    repo_identity  TEXT,
    path           TEXT    NOT NULL,
    side           TEXT    NOT NULL CHECK (side IN ('+', '-')),
    line           INTEGER NOT NULL,
    line_text      TEXT,
    line_hash      TEXT,
    severity       TEXT    NOT NULL,
    confidence     TEXT    NOT NULL,
    category       TEXT    NOT NULL DEFAULT 'unknown',
    claim          TEXT,
    logged_at      INTEGER NOT NULL,
    ingested_at    INTEGER NOT NULL,
    payload_expiry INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_findings_run_id
    ON eval_findings (run_id);
CREATE INDEX IF NOT EXISTS idx_eval_findings_repo_path
    ON eval_findings (repo, path);
CREATE INDEX IF NOT EXISTS idx_eval_findings_payload_expiry
    ON eval_findings (payload_expiry)
    WHERE line_text IS NOT NULL OR claim IS NOT NULL;

-- Append-only. The only permitted UPDATE sets `note` to NULL at payload expiry.
CREATE TABLE IF NOT EXISTS eval_verdicts (
    verdict_id     TEXT    PRIMARY KEY,
    finding_id     TEXT    NOT NULL REFERENCES eval_findings (finding_id),
    verdict        TEXT    NOT NULL,
    commit_sha     TEXT,
    note           TEXT,
    auto           INTEGER NOT NULL CHECK (auto IN (0, 1)),
    decided_at     INTEGER NOT NULL,
    ingested_at    INTEGER NOT NULL,
    payload_expiry INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_verdicts_finding_id_decided_at
    ON eval_verdicts (finding_id, decided_at);
CREATE INDEX IF NOT EXISTS idx_eval_verdicts_commit_sha
    ON eval_verdicts (commit_sha) WHERE commit_sha IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_eval_verdicts_payload_expiry
    ON eval_verdicts (payload_expiry) WHERE note IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS eval_verdicts_no_delete
BEFORE DELETE ON eval_verdicts BEGIN
    SELECT RAISE(ABORT, 'eval_verdicts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS eval_verdicts_no_update
BEFORE UPDATE OF verdict_id, finding_id, verdict, commit_sha, auto, decided_at,
    ingested_at, payload_expiry
ON eval_verdicts BEGIN
    SELECT RAISE(ABORT, 'eval_verdicts are append-only');
END;
CREATE TRIGGER IF NOT EXISTS eval_verdicts_note_clear_only
BEFORE UPDATE OF note ON eval_verdicts
WHEN NEW.note IS NOT NULL BEGIN
    SELECT RAISE(ABORT, 'eval_verdicts.note can only be cleared');
END;
CREATE TRIGGER IF NOT EXISTS eval_verdicts_no_replace
BEFORE INSERT ON eval_verdicts
WHEN EXISTS (
    SELECT 1 FROM eval_verdicts
    -- NEW is SQLite's trigger row, not a FROM table.
    WHERE verdict_id = NEW.verdict_id -- noqa: RF01,RF03
) BEGIN
    SELECT RAISE(ABORT, 'eval_verdicts are append-only');
END;

-- Escape notes never expire, so the table has no payload_expiry.
CREATE TABLE IF NOT EXISTS eval_escapes (
    escape_id      TEXT    PRIMARY KEY,
    repo           TEXT    NOT NULL,
    repo_identity  TEXT,
    path           TEXT    NOT NULL,
    line           INTEGER NOT NULL,
    fix_commit     TEXT,
    note           TEXT    NOT NULL,
    recorded_at    INTEGER NOT NULL,
    ingested_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_escapes_repo_path
    ON eval_escapes (repo, path);

CREATE TABLE IF NOT EXISTS eval_bench_runs (
    bench_run_id   TEXT    PRIMARY KEY,
    bench          TEXT    NOT NULL,
    recorded_at    INTEGER NOT NULL,
    repo_commit    TEXT    NOT NULL,
    cli_version    TEXT    NOT NULL DEFAULT '',
    model          TEXT    NOT NULL,
    model_ids_json TEXT    NOT NULL DEFAULT '[]',
    effort         TEXT,
    reps           INTEGER NOT NULL,
    cases          INTEGER NOT NULL,
    cases_sha256   TEXT    NOT NULL,
    brief_sha256   TEXT    NOT NULL,
    failed_reviews INTEGER NOT NULL DEFAULT 0,
    tokens         INTEGER NOT NULL DEFAULT 0,
    scores_json    TEXT    NOT NULL,
    per_case_json  TEXT    NOT NULL DEFAULT '[]',
    ingested_at    INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_bench_runs_bench_recorded_at
    ON eval_bench_runs (bench, recorded_at);
