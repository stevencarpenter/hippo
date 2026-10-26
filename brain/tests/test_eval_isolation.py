"""Agent-eval rows never reach enrichment, embedding, or outbound flows (AP-14).

Design: docs/superpowers/specs/2026-10-25-agent-eval-schema-design.md, sections
5.3 and 5.5. The regex guard is the enforcing control: no brain module names an
``eval_*`` table except the explicit allowlist.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import hippo_brain.enrichment_sources as es
from hippo_brain.server import BrainServer

REPO = Path(__file__).resolve().parents[2]
BRAIN_SRC = REPO / "brain" / "src" / "hippo_brain"
# Re-embedding and maintenance scripts sit on the same derived-index boundary.
SCRIPT_DIRS = (REPO / "scripts", REPO / "brain" / "scripts")
AGENT_EVAL_SCHEMA = REPO / "crates" / "hippo-core" / "src" / "schema" / "agent_eval.sql"
FIXTURE = REPO / "crates" / "hippo-daemon" / "tests" / "fixtures" / "agent_eval" / "all_types.jsonl"

EVAL_TABLE = re.compile(r"\beval_(review_runs|findings|verdicts|escapes|bench_runs)\b")
# Modules allowed to name the eval tables, relative to brain/src/hippo_brain.
# HIPO-67 (scorecard queries) adds its module here.
ALLOWLIST = frozenset({"schema_version.py", "eval_linkage.py"})

TABLES = {
    "review_run": ("eval_review_runs", "run_id"),
    "finding": ("eval_findings", "finding_id"),
    "verdict": ("eval_verdicts", "verdict_id"),
    "escape": ("eval_escapes", "escape_id"),
    "bench_run": ("eval_bench_runs", "bench_run_id"),
}
RENAMED = {
    "commit": "commit_sha",
    "model_ids": "model_ids_json",
    "scores": "scores_json",
    "per_case": "per_case_json",
}


def test_no_script_names_an_eval_table():
    scripts = sorted(
        path for root in SCRIPT_DIRS for path in root.rglob("*") if path.suffix in {".py", ".sh"}
    )
    assert len(scripts) > 5
    offenders = [
        str(path.relative_to(REPO))
        for path in scripts
        if EVAL_TABLE.search(path.read_text(encoding="utf-8", errors="replace"))
    ]
    assert offenders == [], f"eval_* tables named in scripts: {offenders}"


def test_no_brain_module_names_an_eval_table_outside_the_allowlist():
    modules = sorted(BRAIN_SRC.rglob("*.py"))
    assert len(modules) > 20
    offenders = [
        str(path.relative_to(BRAIN_SRC))
        for path in modules
        if str(path.relative_to(BRAIN_SRC)) not in ALLOWLIST
        and EVAL_TABLE.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == [], f"eval_* tables named outside the allowlist: {offenders}"


def test_guard_pattern_names_the_five_tables_exactly():
    assert EVAL_TABLE.search("SELECT * FROM eval_findings")
    assert EVAL_TABLE.search("eval_bench_runs")
    assert not EVAL_TABLE.search("eval_qa_version")
    assert not EVAL_TABLE.search("eval_findings_extra")


def test_enrichment_sources_are_exactly_the_six_known_sources():
    names = tuple(source.name for source in es.ENRICHMENT_SOURCES)
    assert names == ("shell", "claude", "browser", "workflow", "opencode", "claude-auto-memory")


def _load_fixture(conn, now_ms: int) -> int:
    conn.executescript(AGENT_EVAL_SCHEMA.read_text())
    lines = [line for line in FIXTURE.read_text().splitlines() if line.strip()]
    for line in lines:
        record = json.loads(line)
        table, key = TABLES[record.pop("type")]
        del record["schema_version"]
        row = {key: record.pop("id"), "ingested_at": now_ms}
        if table in ("eval_findings", "eval_verdicts"):
            row["payload_expiry"] = now_ms + 7_776_000_000
        for name, value in record.items():
            if isinstance(value, bool):
                value = int(value)
            elif isinstance(value, (dict, list)):
                value = json.dumps(value)
            row[RENAMED.get(name, name)] = value
        columns = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO {table} ({columns}) VALUES ({marks})", tuple(row.values()))
    conn.commit()
    return len(lines)


@pytest.fixture
def server(tmp_db, tmp_path):
    conn, db_path = tmp_db
    srv = BrainServer(
        db_path=str(db_path),
        data_dir=str(tmp_path),
        enrichment_model="mock-model",
        embedding_model="text-embedding-mock",
    )
    try:
        yield conn, srv
    finally:
        if srv._vector_db is not None:
            srv._vector_db.close()


def test_no_enrichment_claim_returns_an_ingested_eval_record(server):
    conn, srv = server
    assert _load_fixture(conn, 1_792_990_000_000) == 5
    stored = sum(
        conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table, _ in TABLES.values()
    )
    assert stored == 5

    for source in es.ENRICHMENT_SOURCES:
        assert source.claim(conn, srv) == [], source.name

    assert conn.execute("SELECT count(*) FROM knowledge_nodes").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM knowledge_fts").fetchone()[0] == 0
    queues = [
        name
        for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE '%enrichment_queue'"
        )
    ]
    assert len(queues) >= 5
    for queue in queues:
        assert conn.execute(f"SELECT count(*) FROM {queue}").fetchone()[0] == 0, queue
