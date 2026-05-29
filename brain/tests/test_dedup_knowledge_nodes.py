"""Tests for brain/scripts/dedup-knowledge-nodes.py.

The script's filename uses a hyphen, so it can't be imported via the standard
`import` statement — we load it through `importlib.util` to exercise `run()`.

The `tmp_db` fixture uses a plain sqlite3 connection (schema.sql intentionally
omits the vec0 `knowledge_vectors` table), so these tests exercise the
vec0-absent path; the vector delete itself is covered by the integration suite.
"""

from __future__ import annotations

import importlib.util
import uuid
from pathlib import Path

SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "dedup-knowledge-nodes.py"


def _load():
    spec = importlib.util.spec_from_file_location("dedup_knowledge_nodes", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


dedup = _load()


def _node(conn, content, embed_text, node_type="observation") -> int:
    """Insert a knowledge node; return its id."""
    cur = conn.execute(
        "INSERT INTO knowledge_nodes (uuid, content, embed_text, node_type) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), content, embed_text, node_type),
    )
    conn.commit()
    return cur.lastrowid


def _session(conn, session_id, harness="claude-code", segment_index=0) -> int:
    """Insert a minimal valid agentic_sessions row; return its id."""
    cur = conn.execute(
        "INSERT INTO agentic_sessions "
        "(session_id, harness, segment_index, project_dir, cwd, summary_text, "
        " start_time, end_time) VALUES (?, ?, ?, '/p', '/p', 's', 0, 0)",
        (session_id, harness, segment_index),
    )
    conn.commit()
    return cur.lastrowid


def _entity(conn, name) -> int:
    cur = conn.execute(
        "INSERT INTO entities (type, name, canonical, first_seen, last_seen, created_at) "
        "VALUES ('tool', ?, ?, 0, 0, 0)",
        (name, name),
    )
    conn.commit()
    return cur.lastrowid


def _link_agentic(conn, node_id, session_id):
    conn.execute(
        "INSERT INTO knowledge_node_agentic_sessions (knowledge_node_id, agentic_session_id) "
        "VALUES (?, ?)",
        (node_id, session_id),
    )
    conn.commit()


def _link_entity(conn, node_id, entity_id):
    conn.execute(
        "INSERT INTO knowledge_node_entities (knowledge_node_id, entity_id) VALUES (?, ?)",
        (node_id, entity_id),
    )
    conn.commit()


def _count(conn, table) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ── link-table discovery (the 8th-table guard) ───────────────────────────────


def test_link_table_discovery_finds_all_and_excludes_node_table(tmp_db):
    conn, _ = tmp_db
    tables = dedup._link_tables(conn)
    # Every discovered table is a real link table with a knowledge_node_id col.
    assert "knowledge_nodes" not in tables
    assert tables["knowledge_node_agentic_sessions"] == "agentic_session_id"
    assert tables["knowledge_node_entities"] == "entity_id"
    assert tables["knowledge_node_claude_sessions"] == "claude_session_id"
    # All keycols are the single non-node column.
    for t, keycol in tables.items():
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({t})")]
        assert "knowledge_node_id" in cols
        assert keycol != "knowledge_node_id"


# ── core dedup behavior ──────────────────────────────────────────────────────


def test_identical_nodes_collapse_to_min_id_survivor(tmp_db):
    conn, _ = tmp_db
    first = _node(conn, '{"summary":"x"}', "embed x")
    second = _node(conn, '{"summary":"x"}', "embed x")
    third = _node(conn, '{"summary":"x"}', "embed x")

    stats = dedup.run(conn, dry_run=False)

    assert stats["losers"] == 2
    assert stats["deleted"] == 2
    remaining = [r[0] for r in conn.execute("SELECT id FROM knowledge_nodes")]
    assert remaining == [first]  # earliest id survives
    assert second not in remaining and third not in remaining


def test_different_embed_text_is_not_merged(tmp_db):
    """Same content + node_type but different embed_text ⇒ different vector ⇒
    NOT retrieval-equivalent ⇒ must NOT be collapsed."""
    conn, _ = tmp_db
    _node(conn, '{"summary":"x"}', "embed A")
    _node(conn, '{"summary":"x"}', "embed B")

    stats = dedup.run(conn, dry_run=False)

    assert stats["losers"] == 0
    assert _count(conn, "knowledge_nodes") == 2


def test_different_node_type_is_not_merged(tmp_db):
    conn, _ = tmp_db
    _node(conn, '{"summary":"x"}', "embed x", node_type="observation")
    _node(conn, '{"summary":"x"}', "embed x", node_type="change_outcome")

    stats = dedup.run(conn, dry_run=False)
    assert stats["losers"] == 0
    assert _count(conn, "knowledge_nodes") == 2


def test_multi_link_union_repoints_all_associations(tmp_db):
    """Loser's session + entity links must move onto the survivor (union)."""
    conn, _ = tmp_db
    sess_a = _session(conn, "sess-a")
    sess_b = _session(conn, "sess-b")
    ent = _entity(conn, "cargo")

    survivor = _node(conn, '{"summary":"y"}', "embed y")
    loser = _node(conn, '{"summary":"y"}', "embed y")
    _link_agentic(conn, survivor, sess_a)
    _link_agentic(conn, loser, sess_b)
    _link_entity(conn, loser, ent)

    dedup.run(conn, dry_run=False)

    # Loser gone, survivor now carries BOTH sessions + the entity.
    assert _count(conn, "knowledge_nodes") == 1
    survivor_sessions = {
        r[0]
        for r in conn.execute(
            "SELECT agentic_session_id FROM knowledge_node_agentic_sessions "
            "WHERE knowledge_node_id = ?",
            (survivor,),
        )
    }
    assert survivor_sessions == {sess_a, sess_b}
    survivor_entities = {
        r[0]
        for r in conn.execute(
            "SELECT entity_id FROM knowledge_node_entities WHERE knowledge_node_id = ?",
            (survivor,),
        )
    }
    assert survivor_entities == {ent}
    # No dangling edges to the deleted loser.
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM knowledge_node_agentic_sessions WHERE knowledge_node_id = ?",
            (loser,),
        ).fetchone()[0]
        == 0
    )


def test_session_coverage_preserved(tmp_db):
    """Every session that referenced ANY copy still references a live node."""
    conn, _ = tmp_db
    sess_b = _session(conn, "only-on-loser")
    survivor = _node(conn, '{"summary":"z"}', "embed z")
    loser = _node(conn, '{"summary":"z"}', "embed z")
    _link_agentic(conn, loser, sess_b)  # the ONLY copy for sess_b is the loser

    dedup.run(conn, dry_run=False)

    nodes_for_b = conn.execute(
        "SELECT knowledge_node_id FROM knowledge_node_agentic_sessions "
        "WHERE agentic_session_id = ?",
        (sess_b,),
    ).fetchall()
    assert [r[0] for r in nodes_for_b] == [survivor]


def test_existing_survivor_edge_deduped_by_union(tmp_db):
    """If both survivor and loser link the same entity, the union must not
    create a duplicate edge (composite PK / INSERT OR IGNORE)."""
    conn, _ = tmp_db
    ent = _entity(conn, "shared")
    survivor = _node(conn, '{"summary":"q"}', "embed q")
    loser = _node(conn, '{"summary":"q"}', "embed q")
    _link_entity(conn, survivor, ent)
    _link_entity(conn, loser, ent)

    dedup.run(conn, dry_run=False)

    edges = conn.execute(
        "SELECT COUNT(*) FROM knowledge_node_entities WHERE knowledge_node_id = ?",
        (survivor,),
    ).fetchone()[0]
    assert edges == 1


def test_zero_link_duplicates_collapse(tmp_db):
    """Identical nodes with no links at all still dedup, FK stays clean."""
    conn, _ = tmp_db
    _node(conn, '{"summary":"orphan"}', "embed orphan")
    _node(conn, '{"summary":"orphan"}', "embed orphan")

    stats = dedup.run(conn, dry_run=False)

    assert stats["deleted"] == 1
    assert _count(conn, "knowledge_nodes") == 1
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_dry_run_changes_nothing(tmp_db):
    conn, _ = tmp_db
    _node(conn, '{"summary":"d"}', "embed d")
    _node(conn, '{"summary":"d"}', "embed d")

    stats = dedup.run(conn, dry_run=True)

    assert stats["losers"] == 1
    assert stats["deleted"] == 0
    assert _count(conn, "knowledge_nodes") == 2


def test_rerun_is_idempotent(tmp_db):
    conn, _ = tmp_db
    _node(conn, '{"summary":"i"}', "embed i")
    _node(conn, '{"summary":"i"}', "embed i")
    _node(conn, '{"summary":"i"}', "embed i")

    first = dedup.run(conn, dry_run=False)
    assert first["deleted"] == 2
    second = dedup.run(conn, dry_run=False)
    assert second["deleted"] == 0
    assert _count(conn, "knowledge_nodes") == 1


def test_foreign_keys_intact_after_complex_run(tmp_db):
    conn, _ = tmp_db
    sessions = [_session(conn, f"s{i}") for i in range(3)]
    ents = [_entity(conn, f"e{i}") for i in range(2)]
    # group 1: three identical, fanned across sessions + entities
    g1 = [_node(conn, '{"summary":"g1"}', "embed g1") for _ in range(3)]
    for i, n in enumerate(g1):
        _link_agentic(conn, n, sessions[i])
        _link_entity(conn, n, ents[i % 2])
    # group 2: two identical, no links
    [_node(conn, '{"summary":"g2"}', "embed g2") for _ in range(2)]
    # singleton
    _node(conn, '{"summary":"solo"}', "embed solo")

    stats = dedup.run(conn, dry_run=False)

    assert stats["deleted"] == 3  # 2 from g1 + 1 from g2
    assert _count(conn, "knowledge_nodes") == 3  # g1 survivor + g2 survivor + solo
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    # g1 survivor carries all three sessions.
    survivor_g1 = min(g1)
    sess = {
        r[0]
        for r in conn.execute(
            "SELECT agentic_session_id FROM knowledge_node_agentic_sessions "
            "WHERE knowledge_node_id = ?",
            (survivor_g1,),
        )
    }
    assert sess == set(sessions)
