"""Shared in-memory schema and fake vector backend for retrieval unit tests."""

from __future__ import annotations

from dataclasses import dataclass, field

TRUST_EVAL_SCHEMA = """
CREATE TABLE knowledge_nodes (
    id INTEGER PRIMARY KEY,
    uuid TEXT NOT NULL,
    content TEXT NOT NULL,
    embed_text TEXT NOT NULL,
    node_type TEXT NOT NULL DEFAULT 'observation',
    outcome TEXT,
    tags TEXT,
    created_at INTEGER NOT NULL
);
CREATE TABLE events (
    id INTEGER PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    cwd TEXT NOT NULL,
    git_repo TEXT,
    git_branch TEXT,
    probe_tag TEXT
);
CREATE TABLE knowledge_node_events (
    knowledge_node_id INTEGER NOT NULL,
    event_id INTEGER NOT NULL,
    PRIMARY KEY (knowledge_node_id, event_id)
);
CREATE TABLE agentic_sessions (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL DEFAULT '',
    harness TEXT NOT NULL DEFAULT 'claude-code',
    segment_index INTEGER NOT NULL DEFAULT 0,
    start_time INTEGER NOT NULL,
    end_time INTEGER NOT NULL,
    cwd TEXT NOT NULL,
    project_dir TEXT,
    git_branch TEXT,
    summary_text TEXT NOT NULL DEFAULT 'session work',
    message_count INTEGER NOT NULL DEFAULT 5,
    source_file TEXT NOT NULL DEFAULT '/proj/session.jsonl',
    probe_tag TEXT
);
CREATE TABLE knowledge_node_agentic_sessions (
    knowledge_node_id INTEGER NOT NULL,
    agentic_session_id INTEGER NOT NULL,
    PRIMARY KEY (knowledge_node_id, agentic_session_id)
);
CREATE TABLE browser_events (
    id INTEGER PRIMARY KEY,
    timestamp INTEGER NOT NULL,
    probe_tag TEXT
);
CREATE TABLE workflow_runs (
    id INTEGER PRIMARY KEY,
    repo TEXT,
    head_sha TEXT,
    status TEXT NOT NULL DEFAULT 'completed',
    conclusion TEXT DEFAULT 'success',
    started_at INTEGER
);
CREATE TABLE knowledge_node_browser_events (
    knowledge_node_id INTEGER NOT NULL,
    browser_event_id INTEGER NOT NULL,
    PRIMARY KEY (knowledge_node_id, browser_event_id)
);
CREATE TABLE entities (
    id INTEGER PRIMARY KEY,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    canonical TEXT
);
CREATE TABLE knowledge_node_entities (
    knowledge_node_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    PRIMARY KEY (knowledge_node_id, entity_id)
);
CREATE TABLE knowledge_node_workflow_runs (
    knowledge_node_id INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    PRIMARY KEY (knowledge_node_id, run_id)
);
"""


@dataclass
class FakeBackend:
    """Injectable backend matching :mod:`hippo_brain.vector_store` shape."""

    knn: list[tuple[int, float]] = field(default_factory=list)
    fts: list[tuple[int, float]] = field(default_factory=list)

    def knn_search(self, _conn, _query_vec, column="vec_knowledge", limit=10):
        assert column in {"vec_knowledge", "vec_command"}
        return [
            {
                "knowledge_node_id": nid,
                "distance": dist,
                "score": max(0.0, 1.0 - dist / 2.0),
            }
            for nid, dist in self.knn[:limit]
        ]

    def fts_search(self, _conn, _query, limit=10):
        return [
            {
                "knowledge_node_id": nid,
                "bm25": bm25,
                "score": 1.0 / (1.0 + abs(bm25)),
            }
            for nid, bm25 in self.fts[:limit]
        ]
