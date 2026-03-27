"""Extended enrichment tests — edge cases for git_repo field and empty entities."""

import time

from hippo_brain.enrichment import (
    build_enrichment_prompt,
    claim_pending_events,
    write_knowledge_node,
)
from hippo_brain.models import EnrichmentResult


def test_build_enrichment_prompt_with_git_repo():
    """Line 41: events with git_repo field should include it in the prompt."""
    events = [
        {
            "command": "git push origin main",
            "exit_code": 0,
            "duration_ms": 1200,
            "cwd": "/projects/hippo",
            "git_branch": "main",
            "git_commit": None,
            "git_repo": "hippo",
        }
    ]
    prompt = build_enrichment_prompt(events)
    assert "git_repo: hippo" in prompt


def test_build_enrichment_prompt_without_optional_git_fields():
    """Events missing git_branch, git_commit, git_repo should not include those lines."""
    events = [
        {
            "command": "ls -la",
            "exit_code": 0,
            "duration_ms": 10,
            "cwd": "/tmp",
        }
    ]
    prompt = build_enrichment_prompt(events)
    assert "git_branch" not in prompt
    assert "git_commit" not in prompt
    assert "git_repo" not in prompt


def test_claim_pending_events_empty_queue(tmp_db):
    """claim_pending_events on empty queue returns empty list."""
    conn, _ = tmp_db
    result = claim_pending_events(conn, batch_size=10, worker_id="worker")
    assert result == []


def test_write_knowledge_node_empty_entities(tmp_db):
    """Line 88: result.entities as empty dict — no entities inserted."""
    conn, _ = tmp_db
    now_ms = int(time.time() * 1000)

    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (now_ms,),
    )
    conn.execute(
        "INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms, "
        "cwd, hostname, shell) VALUES (1, 1, ?, 'echo hello', 0, 50, '/tmp', 'laptop', 'zsh')",
        (now_ms,),
    )
    conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (1)")
    conn.commit()

    result = EnrichmentResult(
        summary="Printed hello",
        intent="misc",
        outcome="success",
        entities={},  # empty dict — line 88 path
        relationships=[],
        tags=[],
        embed_text="echo hello",
    )
    node_id = write_knowledge_node(conn, result, [1], "test-model")
    assert node_id > 0

    # No entities should have been created
    count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert count == 0


def test_write_knowledge_node_entities_not_dict(tmp_db):
    """If result.entities is not a dict (e.g. a list), no crash."""
    conn, _ = tmp_db
    now_ms = int(time.time() * 1000)

    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (now_ms,),
    )
    conn.execute(
        "INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms, "
        "cwd, hostname, shell) VALUES (1, 1, ?, 'echo hello', 0, 50, '/tmp', 'laptop', 'zsh')",
        (now_ms,),
    )
    conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (1)")
    conn.commit()

    result = EnrichmentResult(
        summary="test",
        intent="misc",
        outcome="success",
        entities=["not", "a", "dict"],  # type: ignore
        relationships=[],
        tags=[],
        embed_text="test",
    )
    node_id = write_knowledge_node(conn, result, [1], "test-model")
    assert node_id > 0
    count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert count == 0
