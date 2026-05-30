"""Tests for write-time content dedup in the workflow + browser enrichers.

These writers' duplication is *cross-source* — distinct runs/visits producing
byte-identical enrichment. `find_identical_node` lets the writer link the new
source key onto the existing node instead of minting a duplicate (returns None
so the server skips embedding). See AP-13.

The workflow path (`enrich_one_async`) is async and LLM-driven, so it is driven
with a fake inference client returning a canned JSON reply.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hippo_brain import vector_store
from hippo_brain.browser_enrichment import write_browser_knowledge_node
from hippo_brain.claude_sessions import find_identical_node
from hippo_brain.models import EnrichmentResult
from hippo_brain.workflow_enrichment import enrich_one_async

_SCHEMA = Path(__file__).resolve().parents[2] / "crates" / "hippo-core" / "src" / "schema.sql"


def _make_db(tmp_path) -> Path:
    """Create a schema'd vec0 DB file; return its path."""
    conn = vector_store.open_conn(tmp_path / "hippo.db")
    conn.executescript(_SCHEMA.read_text())
    conn.commit()
    conn.close()
    return tmp_path / "hippo.db"


def _node_count(conn, node_type) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM knowledge_nodes WHERE node_type = ?", (node_type,)
    ).fetchone()[0]


# ── find_identical_node ──────────────────────────────────────────────────────


def test_find_identical_node_matches_full_triple(tmp_db):
    conn, _ = tmp_db
    conn.execute(
        "INSERT INTO knowledge_nodes (uuid, content, embed_text, node_type) "
        "VALUES ('u1', '{\"s\":1}', 'e', 'change_outcome')"
    )
    conn.commit()
    assert find_identical_node(conn, '{"s":1}', "e", "change_outcome") is not None
    # different embed_text / node_type / content => no match
    assert find_identical_node(conn, '{"s":1}', "OTHER", "change_outcome") is None
    assert find_identical_node(conn, '{"s":1}', "e", "observation") is None
    assert find_identical_node(conn, '{"s":2}', "e", "change_outcome") is None


# ── workflow (enrich_one_async, async + fake inference) ───────────────────────

_WF_REPLY = json.dumps(
    {
        "summary": "Created a GitHub release for hippo v0.20.4",
        "intent": "release",
        "outcome": "success",
        "entities": {"projects": ["hippo"], "tools": [], "files": [], "services": [], "errors": []},
        "tags": ["release"],
        "embed_text": "github release hippo v0.20.4",
    }
)


class _FakeInference:
    """Minimal stand-in for InferenceClient: chat() returns a canned reply."""

    def __init__(self, reply: str):
        self._reply = reply

    async def chat(self, *, messages, model):  # noqa: ARG002
        return self._reply


def _seed_run(db_path: Path, run_id: int) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO workflow_runs "
        "(id, repo, head_sha, head_branch, event, status, conclusion, started_at, "
        " completed_at, html_url, actor, raw_json, first_seen_at, last_seen_at, enriched) "
        "VALUES (?, 'o/r', 'sha', 'main', 'push', 'completed', 'success', 1000, 2000, "
        "        'http://x', 'me', '{}', 1000, 2000, 0)",
        (run_id,),
    )
    conn.commit()
    conn.close()


async def test_workflow_distinct_runs_same_content_collapse(tmp_path):
    db_path = _make_db(tmp_path)
    _seed_run(db_path, 1)
    _seed_run(db_path, 2)
    inf = _FakeInference(_WF_REPLY)

    r1 = await enrich_one_async(str(db_path), 1, inf, "m")
    r2 = await enrich_one_async(str(db_path), 2, inf, "m")

    assert r1 is not None  # (node_id, node_dict)
    assert r2 is None  # second run linked onto the existing node, no new mint

    conn = sqlite3.connect(str(db_path))
    assert _node_count(conn, "change_outcome") == 1
    runs = {r[0] for r in conn.execute("SELECT run_id FROM knowledge_node_workflow_runs")}
    assert runs == {1, 2}
    conn.close()


async def test_workflow_different_content_mints_new_node(tmp_path):
    db_path = _make_db(tmp_path)
    _seed_run(db_path, 1)
    _seed_run(db_path, 2)
    r1 = await enrich_one_async(str(db_path), 1, _FakeInference(_WF_REPLY), "m")
    other = json.dumps(
        {
            "summary": "Tests failed on macos",
            "intent": "ci",
            "outcome": "failure",
            "entities": {"projects": [], "tools": [], "files": [], "services": [], "errors": []},
            "tags": [],
            "embed_text": "ci failure macos cargo test",
        }
    )
    r2 = await enrich_one_async(str(db_path), 2, _FakeInference(other), "m")

    assert r1 is not None and r2 is not None
    conn = sqlite3.connect(str(db_path))
    assert _node_count(conn, "change_outcome") == 2
    conn.close()


# ── browser (write_browser_knowledge_node) ───────────────────────────────────

_BR_RESULT = EnrichmentResult(
    summary="Read the sqlite-vec docs",
    intent="research",
    outcome="success",
    embed_text="sqlite-vec docs vec0",
)


def _seed_browser_event(conn, eid) -> int:
    conn.execute(
        "INSERT INTO browser_events (id, url, title, domain, timestamp, created_at) "
        "VALUES (?, 'https://x', 't', 'x', 0, 0)",
        (eid,),
    )
    conn.commit()
    return eid


def test_browser_identical_content_collapses(tmp_path):
    conn = vector_store.open_conn(tmp_path / "hippo.db")
    conn.executescript(_SCHEMA.read_text())
    conn.commit()
    e1 = _seed_browser_event(conn, 1)
    e2 = _seed_browser_event(conn, 2)

    n1 = write_browser_knowledge_node(conn, _BR_RESULT, [e1], "m")
    n2 = write_browser_knowledge_node(conn, _BR_RESULT, [e2], "m")

    assert n1 is not None
    assert n2 is None  # identical content => linked onto existing node
    assert _node_count(conn, "observation") == 1
    events = {
        r[0]
        for r in conn.execute(
            "SELECT browser_event_id FROM knowledge_node_browser_events WHERE knowledge_node_id = ?",
            (n1,),
        )
    }
    assert events == {1, 2}
    conn.close()


def test_browser_different_content_mints_new_node(tmp_path):
    conn = vector_store.open_conn(tmp_path / "hippo.db")
    conn.executescript(_SCHEMA.read_text())
    conn.commit()
    e1 = _seed_browser_event(conn, 1)
    e2 = _seed_browser_event(conn, 2)

    write_browser_knowledge_node(conn, _BR_RESULT, [e1], "m")
    other = EnrichmentResult(
        summary="Read the rust book", intent="research", outcome="success", embed_text="rust book"
    )
    n2 = write_browser_knowledge_node(conn, other, [e2], "m")

    assert n2 is not None
    assert _node_count(conn, "observation") == 2
    conn.close()
