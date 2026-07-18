from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hippo_brain.auto_memory import (
    ingest_memory_file,
    mark_memory_enrichment_failed,
    write_memory_knowledge_node,
)
from hippo_brain.auto_memory_lifecycle import RevisionRetention
from hippo_brain.auto_memory_reconcile import (
    ReconcileConfig,
    reconcile_source,
    reconcile_sources,
    wait_for_stable_file,
)
from hippo_brain.models import EnrichmentResult


@pytest.fixture
def conn(tmp_db):
    connection, _path = tmp_db
    yield connection


def _publish(conn: sqlite3.Connection, revision_id: int, summary: str) -> int:
    result = EnrichmentResult(
        summary=summary,
        intent="memory",
        outcome="success",
        tags=["memory"],
        embed_text=summary,
    )
    node_id = write_memory_knowledge_node(
        conn, result, revision_id, "mock-model", now_ms=revision_id * 1000
    )
    assert node_id is not None
    return node_id


def test_wait_for_stable_file_requires_quiet_window(tmp_path: Path) -> None:
    source = tmp_path / "MEMORY.md"
    source.write_text("# One\n\nbody\n")
    clock = {"now": 0}

    def clock_ms() -> int:
        return clock["now"]

    def sleep(_seconds: float) -> None:
        clock["now"] += 100

    config = ReconcileConfig(stable_idle_ms=250, stable_sample_ms=50, stable_timeout_ms=2000)
    assert wait_for_stable_file(source, config, clock_ms=clock_ms, sleep=sleep) is True


def test_reconcile_skips_unstable_file(conn: sqlite3.Connection, tmp_path: Path) -> None:
    source = tmp_path / "MEMORY.md"
    source.write_text("# Draft\n\nfirst\n")
    clock = {"now": 0}

    def clock_ms() -> int:
        return clock["now"]

    def sleep(_seconds: float) -> None:
        clock["now"] += 100
        source.write_text(f"# Draft\n\nrewrite at {clock['now']}\n")

    config = ReconcileConfig(stable_idle_ms=500, stable_sample_ms=50, stable_timeout_ms=300)
    result = reconcile_source(
        conn,
        {"path": str(source), "repository": "hippo", "logical_path": "MEMORY.md"},
        retention=RevisionRetention(),
        reconcile=config,
        clock_ms=clock_ms,
        sleep=sleep,
    )
    assert result.outcome == "unstable"
    assert result.changed is False
    assert conn.execute("SELECT COUNT(*) FROM memory_documents").fetchone()[0] == 0


def test_reconcile_unchanged_hash_is_noop(conn: sqlite3.Connection, tmp_path: Path) -> None:
    source = tmp_path / "MEMORY.md"
    source.write_text("# Stable\n\nsame body\n")
    ingest_memory_file(conn, source, repository="hippo", now_ms=1000)

    result = reconcile_source(
        conn,
        {"path": str(source), "repository": "hippo", "logical_path": "MEMORY.md"},
        retention=RevisionRetention(),
        reconcile=ReconcileConfig(stable_idle_ms=1, stable_sample_ms=1, stable_timeout_ms=100),
        require_stable=False,
        now_ms=2000,
    )
    assert result.outcome == "unchanged"
    assert result.changed is False


def test_reconcile_summary_reports_pending_and_failed(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    source = tmp_path / "MEMORY.md"
    source.write_text("# Alpha\n\none\n")
    first = ingest_memory_file(conn, source, repository="hippo", now_ms=1000)
    _publish(conn, first.revision_id, "alpha projection")

    source.write_text("# Alpha\n\ntwo\n")
    second = ingest_memory_file(conn, source, repository="hippo", now_ms=2000)
    mark_memory_enrichment_failed(
        conn, second.revision_id, "simulated enrichment failure", now_ms=3000
    )

    summary = reconcile_sources(
        conn,
        [{"path": str(source), "repository": "hippo", "logical_path": "MEMORY.md"}],
        require_stable=False,
        now_ms=4000,
    )
    assert summary["changed"] == 0
    assert summary["pending_enrichment"] >= 1
    assert summary["sources"][0]["projection_status"] == "stale"
    assert summary["sources"][0]["pending_enrichment"] >= 1


def test_reconcile_sources_respects_absence_confirm_polls(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    source = tmp_path / "MEMORY.md"
    source.write_text("# Gone soon\n\nbody\n")
    ingest_memory_file(conn, source, repository="hippo", now_ms=1000)
    source.unlink()
    configured = [{"path": str(source), "repository": "hippo", "logical_path": "MEMORY.md"}]
    retention = RevisionRetention(absence_confirm_polls=2)

    first = reconcile_sources(
        conn, configured, retention=retention, require_stable=False, now_ms=2000
    )
    assert first["sources"][0]["outcome"] == "unavailable"

    second = reconcile_sources(
        conn, configured, retention=retention, require_stable=False, now_ms=3000
    )
    assert second["sources"][0]["outcome"] == "tombstoned"


def test_reconcile_changed_content_increments_summary(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    source = tmp_path / "MEMORY.md"
    source.write_text("# Gamma\n\nfirst\n")

    first_summary = reconcile_sources(
        conn,
        [{"path": str(source), "repository": "hippo", "logical_path": "MEMORY.md"}],
        require_stable=False,
        now_ms=1000,
    )
    assert first_summary["changed"] == 1
    assert first_summary["pending_enrichment"] == 1

    source.write_text("# Gamma\n\nsecond\n")
    second_summary = reconcile_sources(
        conn,
        [{"path": str(source), "repository": "hippo", "logical_path": "MEMORY.md"}],
        require_stable=False,
        now_ms=2000,
    )
    assert second_summary["changed"] == 1
    assert second_summary["pending_enrichment"] >= 1
    assert conn.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0] == 2
