"""Topic retrieval respects source policy and only reads current memberships."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, replace

import pytest

from hippo_brain import classification
from hippo_brain.retrieval import DEFAULT_TUNING, Filters, Tuning, _query_topics, configure, search
from tests.retrieval_fixtures import FakeBackend


TOPIC = "knowledge-retrieval"
NOW = 1_700_000_000_000
TOPIC_TUNING = Tuning(topic_retrieval=True, recency_half_life_days=0, command_weight=0)


def add_node(
    conn: sqlite3.Connection,
    node_id: int,
    topics: tuple[str, ...] = (TOPIC,),
    *,
    project: str = "/hippo",
    branch: str = "main",
    created_at: int = NOW,
    probability: float = 0.9,
    excluded: bool = False,
) -> None:
    conn.execute(
        "INSERT INTO knowledge_nodes(id,uuid,content,embed_text,tags,created_at) VALUES(?,?,?,?,?,?)",
        (
            node_id,
            f"uuid-{node_id:03}",
            json.dumps({"summary": "Vector search"}),
            "Search relevant knowledge",
            '["original"]',
            created_at,
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO sessions(id,start_time,shell,hostname,username) "
        "VALUES(1,0,'zsh','host','user')"
    )
    conn.execute(
        "INSERT INTO events(id,session_id,timestamp,command,duration_ms,cwd,hostname,shell,"
        "git_branch,probe_tag) VALUES(?,1,?,'search',1,?,'host','zsh',?,?)",
        (node_id, created_at, project, branch, "probe" if excluded else None),
    )
    conn.execute("INSERT INTO knowledge_node_events VALUES(?,?)", (node_id, node_id))
    classification.enqueue_node(conn, node_id, enabled=True, now_ms=NOW)
    work = classification.claim(conn, limit=1, now_ms=NOW)[0]
    recipe = classification.default_recipe()
    response = {
        "model": recipe.model_id,
        "answers": {
            topic: {"type": "noul", "noul": probability if topic in topics else 0.01}
            for topic in recipe.topics
        },
    }
    assert classification.apply_result(conn, work, response, now_ms=NOW)


def run_search(conn: sqlite3.Connection, **kwargs):
    return search(
        conn,
        "vector search",
        [1.0, 0.0],
        backend=kwargs.pop("backend", FakeBackend()),
        tuning=kwargs.pop("tuning", TOPIC_TUNING),
        topic_ids=kwargs.pop("topic_ids", [TOPIC]),
        now_ms=NOW,
        **kwargs,
    )


def test_disabled_path_does_not_read_classification_and_preserves_baseline(tmp_db, monkeypatch):
    conn, _ = tmp_db
    add_node(conn, 1)
    add_node(conn, 2)
    baseline = run_search(conn, backend=FakeBackend(knn=[(1, 0.2)]), tuning=DEFAULT_TUNING)
    monkeypatch.setattr(classification, "current_topics", lambda *args, **kwargs: pytest.fail())
    actual = run_search(
        conn,
        backend=FakeBackend(knn=[(1, 0.2)]),
        tuning=replace(TOPIC_TUNING, topic_retrieval=False),
    )
    assert [asdict(result) for result in actual] == [asdict(result) for result in baseline]
    assert actual[0].controlled_topics == {}


def test_topic_channel_recovers_missing_node_without_modifying_tags_or_sources(tmp_db):
    conn, _ = tmp_db
    add_node(conn, 1, topics=())
    add_node(conn, 2)
    before = conn.execute("SELECT * FROM knowledge_nodes ORDER BY id").fetchall()
    diagnostics = {}
    results = run_search(conn, backend=FakeBackend(knn=[(1, 0.2)]), diagnostics=diagnostics)
    assert [result.uuid for result in results] == ["uuid-001", "uuid-002"]
    assert results[1].controlled_topics == {TOPIC: 0.9}
    assert results[1].tags == ["original"]
    assert results[1].linked_event_ids == [2]
    assert diagnostics["topic_added_node_ids"] == [2]
    assert diagnostics["topic_selected_node_ids"] == [2]
    assert conn.execute("SELECT * FROM knowledge_nodes ORDER BY id").fetchall() == before


@pytest.mark.parametrize(
    "change",
    [
        "UPDATE knowledge_node_classifications SET status='pending' WHERE node_id=1",
        "UPDATE knowledge_node_classifications SET requested_revision=2 WHERE node_id=1",
        "UPDATE knowledge_node_classifications SET recipe_hash='old' WHERE node_id=1",
        "UPDATE knowledge_node_classifications SET applied_input_hash='old' WHERE node_id=1",
        "UPDATE knowledge_nodes SET embed_text='changed before writer notification' WHERE id=1",
        "UPDATE knowledge_nodes SET uuid='different' WHERE id=1",
        "DELETE FROM knowledge_node_entities WHERE knowledge_node_id=1",
    ],
)
def test_stale_or_unpublished_membership_is_never_admitted(tmp_db, change):
    conn, _ = tmp_db
    add_node(conn, 1)
    conn.execute(change)
    assert run_search(conn) == []


@pytest.mark.parametrize(
    "filters, expected",
    [
        (Filters(project="hippo"), {"uuid-001", "uuid-003", "uuid-004"}),
        (Filters(branch="main"), {"uuid-001", "uuid-002", "uuid-004"}),
        (Filters(since_ms=NOW), {"uuid-001", "uuid-002", "uuid-003"}),
        (Filters(source="browser"), set()),
        (Filters(source="shell", project="hippo", branch="main", since_ms=NOW), {"uuid-001"}),
    ],
)
def test_topic_channel_uses_existing_filters_before_and_after_fusion(tmp_db, filters, expected):
    conn, _ = tmp_db
    add_node(conn, 1)
    add_node(conn, 2, project="/different")
    add_node(conn, 3, branch="feature")
    add_node(conn, 4, created_at=NOW - 1000)
    add_node(conn, 5, excluded=True)
    # Include every ID in the ordinary arm too, exercising the post-union filter.
    results = run_search(
        conn, filters=filters, backend=FakeBackend(knn=[(i, 0.1) for i in range(1, 6)])
    )
    assert {result.uuid for result in results} == expected


def test_excluded_nodes_do_not_consume_the_twenty_per_topic_budget(tmp_db):
    conn, _ = tmp_db
    for node_id in range(1, 26):
        add_node(conn, node_id, excluded=True, probability=1.0)
    add_node(conn, 26)
    results = run_search(conn)
    assert [result.uuid for result in results] == ["uuid-026"]


def test_entity_filter_and_baseline_displacement_are_reported(tmp_db):
    conn, _ = tmp_db
    add_node(conn, 1, topics=())
    add_node(conn, 2)
    diagnostics = {}
    results = run_search(
        conn,
        backend=FakeBackend(knn=[(1, 0.1), (2, 0.2)]),
        limit=1,
        diagnostics=diagnostics,
    )
    assert [result.uuid for result in results] == ["uuid-002"]
    assert diagnostics["topic_displaced_node_ids"] == [1]
    assert diagnostics["topic_added_node_ids"] == []
    filtered = run_search(conn, filters=Filters(entity=classification.NAMESPACE + TOPIC))
    assert [result.uuid for result in filtered] == ["uuid-002"]
    assert run_search(conn, filters=Filters(entity="unrelated")) == []


def test_topic_caps_uuid_ties_and_explicit_widened_experiment(tmp_db):
    conn, _ = tmp_db
    topics = (TOPIC, "database-storage", "observability")
    for node_id in range(1, 64):
        add_node(conn, node_id, topics=(topics[(node_id - 1) // 21],))
    diagnostics = {}
    online = run_search(
        conn, topic_ids=[*topics, "testing-validation"], limit=100, diagnostics=diagnostics
    )
    widened = run_search(conn, topic_ids=topics, limit=100, topic_pool_limit=60)
    assert len(online) == 30
    assert len(widened) == 40
    assert diagnostics["query_topics"] == list(topics)
    assert diagnostics["topic_candidates_by_topic"] == dict.fromkeys(topics, 20)
    assert len(diagnostics["topic_added_node_ids"]) == 40
    assert [result.uuid for result in widened] == sorted(result.uuid for result in widened)


def test_missing_classification_schema_keeps_original_retrieval(tmp_db):
    conn, _ = tmp_db
    add_node(conn, 1)
    conn.execute("DROP TABLE knowledge_node_classifications")
    diagnostics = {}
    results = run_search(conn, backend=FakeBackend(knn=[(1, 0.1)]), diagnostics=diagnostics)
    assert [result.uuid for result in results] == ["uuid-001"]
    assert results[0].controlled_topics == {}
    assert diagnostics["topic_unavailable"] == "classification_schema"


def test_aliases_match_reviewed_phrases_not_substrings_and_deduplicate_topics():
    topics = {
        TOPIC: {"aliases": ["vector search", "reranking"]},
        "database-storage": {"aliases": ["sql"]},
    }
    assert _query_topics("SQL with VECTOR SEARCH and reranking", None, topics) == [
        "database-storage",
        TOPIC,
    ]
    assert _query_topics("nosql and vector searching", None, topics) == []
    assert _query_topics("", [TOPIC, TOPIC, "unknown", "database-storage"], topics) == [
        TOPIC,
        "database-storage",
    ]
    assert _query_topics("vector search", [], topics) == []


def test_config_preserves_local_default_and_clamps_budget():
    try:
        assert configure(None) == DEFAULT_TUNING
        configured = configure(
            {
                "rerank_backend": "jev",
                "rerank_recipe": "single-v1",
                "rerank_adaptive": True,
                "topic_retrieval": True,
                "decision_deadline_ms": 9999,
            }
        )
        assert configured.rerank_backend == "jev"
        assert configured.rerank_recipe == "single-v1"
        assert configured.rerank_adaptive and configured.topic_retrieval
        assert configured.decision_deadline_ms == 2000
        invalid = configure(
            {"rerank_backend": "typo", "rerank_recipe": {}, "decision_deadline_ms": float("nan")}
        )
        assert invalid.rerank_backend == "local"
        assert invalid.rerank_recipe == "multi-v1"
        assert invalid.decision_deadline_ms == 2000
    finally:
        configure(None)


def test_explicit_clock_changes_recency_without_mutating_global_time(tmp_db):
    conn, _ = tmp_db
    add_node(conn, 1, created_at=NOW - 86_400_000)
    add_node(conn, 2, created_at=NOW)
    tuning = Tuning(recency_half_life_days=1, recency_floor=0, mmr_lambda=1)
    backend = FakeBackend(knn=[(1, 0.0), (2, 1.0)])
    early = search(
        conn, "", [1.0], mode="semantic", tuning=tuning, backend=backend, now_ms=NOW - 86_400_000
    )
    late = search(conn, "", [1.0], mode="semantic", tuning=tuning, backend=backend, now_ms=NOW)
    assert early[0].score == 1.0
    assert [result.score for result in late] == [0.5, 0.5]
