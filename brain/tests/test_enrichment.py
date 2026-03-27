import time

from hippo_brain.enrichment import (
    build_enrichment_prompt,
    claim_pending_events,
    mark_queue_failed,
    parse_enrichment_response,
    write_knowledge_node,
)
from hippo_brain.models import EnrichmentResult


def test_build_enrichment_prompt():
    events = [
        {
            "command": "cargo test -p hippo-core",
            "exit_code": 0,
            "duration_ms": 3500,
            "cwd": "/Users/dev/projects/hippo",
            "git_branch": "main",
            "git_commit": "abc1234",
        }
    ]
    prompt = build_enrichment_prompt(events)
    assert "cargo test -p hippo-core" in prompt
    assert "/Users/dev/projects/hippo" in prompt
    assert "main" in prompt
    assert "abc1234" in prompt


def test_parse_enrichment_response():
    raw = (
        '{"summary": "Ran tests", "intent": "testing", "outcome": "success", '
        '"entities": {"projects": ["hippo"], "tools": ["cargo"], "files": [], '
        '"services": [], "errors": []}, "relationships": [], '
        '"tags": ["rust"], "embed_text": "cargo test hippo"}'
    )
    result = parse_enrichment_response(raw)
    assert isinstance(result, EnrichmentResult)
    assert result.summary == "Ran tests"
    assert result.outcome == "success"
    assert "hippo" in result.entities["projects"]


def test_parse_enrichment_response_with_code_fences():
    raw = """```json
{"summary": "Built project", "intent": "building", "outcome": "success",
 "entities": {"projects": [], "tools": [], "files": [], "services": [], "errors": []},
 "relationships": [], "tags": [], "embed_text": "build project"}
```"""
    result = parse_enrichment_response(raw)
    assert result.summary == "Built project"
    assert result.intent == "building"


def test_claim_and_write(tmp_db):
    conn, _ = tmp_db
    now_ms = int(time.time() * 1000)

    # Insert session
    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (now_ms,),
    )

    # Insert events
    conn.execute(
        """INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms,
                              cwd, hostname, shell)
           VALUES (1, 1, ?, 'cargo test', 0, 1000, '/project', 'laptop', 'zsh')""",
        (now_ms,),
    )
    conn.execute(
        """INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms,
                              cwd, hostname, shell)
           VALUES (2, 1, ?, 'cargo build', 0, 2000, '/project', 'laptop', 'zsh')""",
        (now_ms,),
    )

    # Insert queue entries
    conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (1)")
    conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (2)")
    conn.commit()

    # Claim events
    events = claim_pending_events(conn, batch_size=10, worker_id="test-worker")
    assert len(events) == 2
    event_ids = [e["id"] for e in events]

    # Write knowledge node
    result = EnrichmentResult(
        summary="Testing and building hippo",
        intent="testing",
        outcome="success",
        entities={"projects": ["hippo"], "tools": ["cargo"], "files": [], "services": [], "errors": []},
        relationships=[],
        tags=["rust", "testing"],
        embed_text="cargo test and build hippo project",
    )
    node_id = write_knowledge_node(conn, result, event_ids, "test-model")
    assert node_id > 0

    # Verify knowledge node content
    row = conn.execute("SELECT content, embed_text FROM knowledge_nodes WHERE id = ?", (node_id,)).fetchone()
    assert "Testing and building hippo" in row[0]
    assert row[1] == "cargo test and build hippo project"

    # Verify entities created
    entities = conn.execute("SELECT type, name FROM entities").fetchall()
    entity_names = [e[1] for e in entities]
    assert "hippo" in entity_names
    assert "cargo" in entity_names

    # Verify events marked enriched
    enriched = conn.execute("SELECT enriched FROM events WHERE id = 1").fetchone()[0]
    assert enriched == 1

    # Verify queue marked done
    status = conn.execute("SELECT status FROM enrichment_queue WHERE event_id = 1").fetchone()[0]
    assert status == "done"


def test_mark_queue_failed(tmp_db):
    conn, _ = tmp_db
    now_ms = int(time.time() * 1000)

    # Insert session + event + queue
    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (now_ms,),
    )
    conn.execute(
        """INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms,
                              cwd, hostname, shell)
           VALUES (1, 1, ?, 'failing cmd', 1, 500, '/project', 'laptop', 'zsh')""",
        (now_ms,),
    )
    conn.execute("INSERT INTO enrichment_queue (event_id, max_retries) VALUES (1, 3)")
    conn.commit()

    # First failure — should stay pending
    mark_queue_failed(conn, [1], "timeout error")
    row = conn.execute("SELECT status, retry_count FROM enrichment_queue WHERE event_id = 1").fetchone()
    assert row[0] == "pending"
    assert row[1] == 1

    # Second failure — still pending
    mark_queue_failed(conn, [1], "timeout error")
    row = conn.execute("SELECT status, retry_count FROM enrichment_queue WHERE event_id = 1").fetchone()
    assert row[0] == "pending"
    assert row[1] == 2

    # Third failure — should be failed (retry_count >= max_retries)
    mark_queue_failed(conn, [1], "timeout error")
    row = conn.execute("SELECT status, retry_count FROM enrichment_queue WHERE event_id = 1").fetchone()
    assert row[0] == "failed"
    assert row[1] == 3
