"""Smoke tests for brain/scripts/re-enrich-knowledge-nodes.py.

Hyphenated filename → load via importlib. Tests focus on:
- candidate selection respects enrichment_version filter + source flag
- single-node round-trip (shell + claude) updates content / entities /
  enrichment_version inside one transaction
- already-target-version nodes are NOT re-processed
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from hippo_brain.client import MockLMStudioClient

SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "re-enrich-knowledge-nodes.py"


@pytest.fixture
def conn(tmp_db):
    """tmp_db with Row factory enabled — the script expects dict-able rows."""
    c, _ = tmp_db
    c.row_factory = sqlite3.Row
    return c


def _load_script_module():
    spec = importlib.util.spec_from_file_location("re_enrich_knowledge_nodes", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def re_enrich(monkeypatch):
    """Load the script with deterministic project roots pinned."""
    monkeypatch.setenv("HIPPO_PROJECT_ROOTS", "/Users/test/projects/hippo")
    from hippo_brain.entity_resolver import _cached_fallback_roots

    _cached_fallback_roots.cache_clear()
    return _load_script_module()


def _seed_shell_node(conn, node_id: int, version: int = 1, created_at: int = 1_700_000_000_000):
    """Insert a knowledge node + a linked shell event."""
    node_uuid = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, outcome,
                                     tags, enrichment_model, enrichment_version,
                                     created_at, updated_at)
        VALUES (?, ?, ?, 'old summary', 'observation', 'success',
                '["old"]', 'old-model', ?, ?, ?)
        """,
        (
            node_id,
            node_uuid,
            json.dumps({"summary": "old summary", "entities": {}}),
            version,
            created_at,
            created_at,
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'host', 'user')",
        (created_at,),
    )
    conn.execute(
        """
        INSERT INTO events (session_id, timestamp, command, exit_code, duration_ms,
                            cwd, hostname, shell)
        VALUES (1, ?, 'cargo test', 0, 1000, '/project', 'host', 'zsh')
        """,
        (created_at,),
    )
    event_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (?, ?)",
        (node_id, event_id),
    )
    conn.commit()


def _seed_claude_node(conn, node_id: int, version: int = 1, created_at: int = 1_700_000_000_000):
    """Insert a knowledge node + a linked claude_session segment."""
    node_uuid = str(uuid.uuid4())
    conn.execute(
        """
        INSERT INTO knowledge_nodes (id, uuid, content, embed_text, node_type, outcome,
                                     tags, enrichment_model, enrichment_version,
                                     created_at, updated_at)
        VALUES (?, ?, ?, 'old', 'observation', 'success',
                '["old"]', 'old-model', ?, ?, ?)
        """,
        (
            node_id,
            node_uuid,
            json.dumps({"summary": "old", "entities": {}}),
            version,
            created_at,
            created_at,
        ),
    )
    conn.execute(
        """
        INSERT INTO claude_sessions (session_id, project_dir, cwd, segment_index,
                                     start_time, end_time, summary_text,
                                     tool_calls_json, user_prompts_json,
                                     message_count, source_file, is_subagent)
        VALUES ('sess-x', '/project', '/project', 0, ?, ?,
                'fake summary text', '[]', '[]', 5, '/sess.jsonl', 0)
        """,
        (created_at, created_at + 1000),
    )
    seg_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO knowledge_node_claude_sessions (knowledge_node_id, claude_session_id) "
        "VALUES (?, ?)",
        (node_id, seg_id),
    )
    conn.commit()


def test_candidate_selection_respects_version_filter(conn, re_enrich):
    """Nodes already at TARGET_ENRICHMENT_VERSION must be excluded."""

    _seed_shell_node(conn, node_id=1, version=1)
    _seed_shell_node(conn, node_id=2, version=re_enrich.TARGET_ENRICHMENT_VERSION)

    candidates = re_enrich._select_candidate_nodes(
        conn, source="all", limit=None, newest_first=True
    )
    ids = [c["id"] for c in candidates]
    assert ids == [1], f"only node 1 should need re-enrichment; got {ids}"


def test_candidate_selection_source_filter(conn, re_enrich):

    _seed_shell_node(conn, node_id=1)
    _seed_claude_node(conn, node_id=2)

    shell_only = re_enrich._select_candidate_nodes(
        conn, source="shell", limit=None, newest_first=True
    )
    assert [c["_source"] for c in shell_only] == ["shell"]

    claude_only = re_enrich._select_candidate_nodes(
        conn, source="claude", limit=None, newest_first=True
    )
    assert [c["_source"] for c in claude_only] == ["claude"]


def test_process_node_updates_in_place(conn, re_enrich):
    """Round-trip: re-enriching a shell node updates content + bumps version
    while preserving id / uuid / created_at."""

    _seed_shell_node(conn, node_id=42, created_at=1_700_000_000_000)
    before = conn.execute("SELECT uuid, created_at FROM knowledge_nodes WHERE id = 42").fetchone()
    original_uuid, original_created_at = before["uuid"], before["created_at"]

    candidate = {"id": 42, "uuid": original_uuid, "_source": "shell"}
    client = MockLMStudioClient()

    ok = asyncio.run(
        re_enrich._process_node(
            client, conn, candidate, enrichment_model="m", embed_model="", dry_run=False
        )
    )
    assert ok is True

    after = conn.execute(
        "SELECT uuid, created_at, enrichment_version, content FROM knowledge_nodes WHERE id = 42"
    ).fetchone()
    assert after["uuid"] == original_uuid, "uuid must be preserved"
    assert after["created_at"] == original_created_at, "created_at must be preserved"
    assert after["enrichment_version"] == re_enrich.TARGET_ENRICHMENT_VERSION
    new_content = json.loads(after["content"])
    assert new_content["summary"] == "test command"  # from MockLMStudioClient.CANNED_RESPONSE


def test_dry_run_makes_no_changes(conn, re_enrich):

    _seed_shell_node(conn, node_id=7)
    candidate = {"id": 7, "uuid": "x", "_source": "shell"}
    client = MockLMStudioClient()

    ok = asyncio.run(
        re_enrich._process_node(
            client, conn, candidate, enrichment_model="m", embed_model="", dry_run=True
        )
    )
    assert ok is True

    row = conn.execute(
        "SELECT enrichment_version, content FROM knowledge_nodes WHERE id = 7"
    ).fetchone()
    assert row["enrichment_version"] == 1, "dry-run must not bump version"
    assert json.loads(row["content"])["summary"] == "old summary"
    assert client.chat_calls == [], "dry-run must not call the LLM"


def test_failed_node_keeps_old_version_for_retry(conn, re_enrich, monkeypatch):
    """If the LLM call raises after retries, the node stays at v1 so a
    subsequent run picks it up again."""

    _seed_shell_node(conn, node_id=99)

    class FailingClient(MockLMStudioClient):
        async def chat(self, *args, **kwargs):
            raise RuntimeError("simulated lm-studio outage")

    candidate = {"id": 99, "uuid": "x", "_source": "shell"}
    client = FailingClient()

    ok = asyncio.run(
        re_enrich._process_node(
            client, conn, candidate, enrichment_model="m", embed_model="", dry_run=False
        )
    )
    assert ok is False

    row = conn.execute("SELECT enrichment_version FROM knowledge_nodes WHERE id = 99").fetchone()
    assert row["enrichment_version"] == 1, "failures must NOT bump version"
