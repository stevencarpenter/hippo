from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hippo_brain.auto_memory import ingest_memory_file, write_memory_knowledge_node
from hippo_brain.auto_memory_categories import (
    category_from_filename,
    list_document_categories,
    list_document_links,
    replace_model_categories,
)
from hippo_brain.models import EnrichmentResult
from hippo_brain.mcp_queries import search_knowledge_lexical
from hippo_brain.retrieval import Filters, _apply_filters


@pytest.fixture
def conn(tmp_db):
    connection, _path = tmp_db
    yield connection


def test_category_from_filename_handles_prefixes_and_index() -> None:
    assert category_from_filename("MEMORY.md") == "index"
    assert category_from_filename("feedback_candor.md") == "feedback"
    assert category_from_filename("project_architecture.md") == "project"
    assert category_from_filename("reference_api.md") == "reference"
    assert category_from_filename("user_role.md") == "user"
    assert category_from_filename("debugging.md") is None


def test_ingest_assigns_filename_category_and_resolves_index_links(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    fixture_dir = (
        Path(__file__).parent.parent
        / "src"
        / "hippo_brain"
        / "_fixtures"
        / "auto_memory_spike"
        / "source"
    )
    for name in (
        "MEMORY.md",
        "project_architecture.md",
        "debugging.md",
        "workflow.md",
        "feedback_candor.md",
    ):
        target = tmp_path / name
        target.write_text((fixture_dir / name).read_text(encoding="utf-8"))

    for name in (
        "MEMORY.md",
        "project_architecture.md",
        "debugging.md",
        "workflow.md",
        "feedback_candor.md",
    ):
        ingest_memory_file(conn, tmp_path / name, repository="hippo", now_ms=1000)

    index_id = conn.execute(
        "SELECT id FROM memory_documents WHERE logical_path = 'MEMORY.md'"
    ).fetchone()[0]
    categories = list_document_categories(conn, int(index_id))
    assert any(c.category == "index" and c.source == "filename" for c in categories)

    links = list_document_links(conn, int(index_id))
    resolved = [link for link in links if link["resolution"] == "resolved"]
    assert {link["target_logical_path"] for link in resolved} == {
        "project_architecture.md",
        "debugging.md",
        "workflow.md",
        "feedback_candor.md",
    }


def test_unresolved_link_resolves_when_target_file_is_ingested_later(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    index = tmp_path / "MEMORY.md"
    index.write_text("- [Debug](debugging.md)\n")
    ingest_memory_file(conn, index, repository="hippo", now_ms=1000)
    index_id = conn.execute(
        "SELECT id FROM memory_documents WHERE logical_path = 'MEMORY.md'"
    ).fetchone()[0]
    assert list_document_links(conn, int(index_id))[0]["resolution"] == "unresolved"

    (tmp_path / "debugging.md").write_text("# Debug\n\nnotes\n")
    ingest_memory_file(conn, tmp_path / "debugging.md", repository="hippo", now_ms=2000)

    links = list_document_links(conn, int(index_id))
    assert links[0]["resolution"] == "resolved"


def test_model_categories_replace_on_enrichment_but_filename_persists(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    source = tmp_path / "feedback_style.md"
    source.write_text("# Feedback\n\nPrefer integration tests.\n")
    ingested = ingest_memory_file(conn, source, repository="hippo", now_ms=1000)
    replace_model_categories(
        conn,
        ingested.document_id,
        ["project"],
        model_name="mock-model",
        now_ms=2000,
    )
    categories = list_document_categories(conn, ingested.document_id)
    assert {c.category for c in categories} == {"feedback", "project"}
    assert sum(1 for c in categories if c.source == "filename") == 1
    assert sum(1 for c in categories if c.source == "model") == 1

    replace_model_categories(
        conn, ingested.document_id, ["reference"], model_name="mock-model", now_ms=3000
    )
    categories = list_document_categories(conn, ingested.document_id)
    model_categories = [c.category for c in categories if c.source == "model"]
    assert model_categories == ["reference"]
    assert any(c.category == "feedback" and c.source == "filename" for c in categories)


def test_category_filter_returns_matching_memory_nodes(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    feedback = tmp_path / "feedback_style.md"
    feedback.write_text("# Style\n\nBe direct.\n")
    project = tmp_path / "project_notes.md"
    project.write_text("# Notes\n\nUse cargo.\n")

    feedback_ingest = ingest_memory_file(conn, feedback, repository="hippo", now_ms=1000)
    project_ingest = ingest_memory_file(conn, project, repository="hippo", now_ms=1000)

    result = EnrichmentResult(
        summary="Style guidance.",
        intent="feedback",
        outcome="success",
        tags=["style"],
        embed_text="feedback style guidance",
    )
    feedback_node = write_memory_knowledge_node(
        conn, result, feedback_ingest.revision_id, "mock-model", now_ms=2000
    )
    project_result = EnrichmentResult(
        summary="Build notes.",
        intent="project",
        outcome="success",
        tags=["build"],
        embed_text="cargo build notes",
    )
    project_node = write_memory_knowledge_node(
        conn, project_result, project_ingest.revision_id, "mock-model", now_ms=2000
    )
    assert feedback_node is not None and project_node is not None

    kept = _apply_filters(conn, [feedback_node, project_node], Filters(memory_category="feedback"))
    assert kept == {feedback_node}

    lexical = search_knowledge_lexical(
        conn,
        "style",
        source="claude-auto-memory",
        project="hippo",
        category="feedback",
    )
    assert len(lexical) == 1
    assert lexical[0]["memory_categories"]
    assert lexical[0]["memory_categories"][0]["source"] == "filename"


def test_external_link_is_not_invented(conn: sqlite3.Connection, tmp_path: Path) -> None:
    index = tmp_path / "MEMORY.md"
    index.write_text("- [Docs](https://example.com/docs)\n")
    ingest_memory_file(conn, index, repository="hippo", now_ms=1000)
    index_id = conn.execute(
        "SELECT id FROM memory_documents WHERE logical_path = 'MEMORY.md'"
    ).fetchone()[0]
    links = list_document_links(conn, int(index_id))
    assert len(links) == 1
    assert links[0]["resolution"] == "external"
    assert links[0]["target_document_id"] is None


def test_self_link_in_index_is_marked_circular(conn: sqlite3.Connection, tmp_path: Path) -> None:
    index = tmp_path / "MEMORY.md"
    index.write_text("- [Self](MEMORY.md)\n")
    ingest_memory_file(conn, index, repository="hippo", now_ms=1000)
    index_id = conn.execute(
        "SELECT id FROM memory_documents WHERE logical_path = 'MEMORY.md'"
    ).fetchone()[0]
    links = list_document_links(conn, int(index_id))
    assert len(links) == 1
    assert links[0]["resolution"] == "circular"
