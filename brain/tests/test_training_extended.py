"""Extended tests for hippo_brain.training — since_ms filter, split ratios, edge cases."""

import json
import tempfile
import time
from pathlib import Path

from hippo_brain.training import export_training_data


def _seed_many(conn, count: int, base_ts: int | None = None):
    """Insert `count` knowledge nodes with linked events, each with distinct timestamps."""
    now_ms = base_ts or int(time.time() * 1000)

    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (now_ms,),
    )

    for i in range(1, count + 1):
        ts = now_ms + i * 1000
        # Insert event
        conn.execute(
            "INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms, "
            "cwd, hostname, shell, git_branch) "
            "VALUES (?, 1, ?, ?, 0, ?, '/project', 'laptop', 'zsh', 'main')",
            (i, ts, f"cmd-{i}", 500 + i),
        )

        # Insert knowledge node
        content = json.dumps(
            {"summary": f"Summary for node {i}", "intent": "testing", "outcome": "success"}
        )
        conn.execute(
            "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, outcome, tags, "
            "enrichment_model, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 'success', '[\"test\"]', 'model', ?, ?)",
            (i, f"uuid-{i}", content, f"embed text {i}", ts, ts),
        )

        # Link event to knowledge node
        conn.execute(
            "INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (?, ?)",
            (i, i),
        )

    conn.commit()


def test_export_with_since_ms_filter(tmp_db):
    """Only nodes created at or after since_ms should be exported."""
    conn, _ = tmp_db
    base_ts = 1_000_000_000_000  # far past
    _seed_many(conn, 5, base_ts=base_ts)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Filter: only nodes created at or after the 4th node's timestamp
        cutoff = base_ts + 4 * 1000
        stats = export_training_data(conn, tmpdir, since_ms=cutoff)
        assert stats["total"] == 2  # nodes 4 and 5


def test_export_since_ms_no_matches(tmp_db):
    """since_ms far in the future should yield zero results."""
    conn, _ = tmp_db
    _seed_many(conn, 3)

    with tempfile.TemporaryDirectory() as tmpdir:
        stats = export_training_data(conn, tmpdir, since_ms=99_999_999_999_999)
        assert stats == {"total": 0, "train": 0, "valid": 0, "test": 0}


def test_80_10_10_split_with_enough_examples(tmp_db):
    """With 20 examples, verify approximate 80/10/10 split."""
    conn, _ = tmp_db
    _seed_many(conn, 20)

    with tempfile.TemporaryDirectory() as tmpdir:
        stats = export_training_data(conn, tmpdir)
        assert stats["total"] == 20
        assert stats["train"] == 16  # int(20 * 0.8) = 16
        assert stats["valid"] == 2  # int(20 * 0.1) = 2
        assert stats["test"] == 2  # remainder

        # Verify all three files exist and have correct line counts
        for split, expected_count in [("train", 16), ("valid", 2), ("test", 2)]:
            path = Path(tmpdir) / f"{split}.jsonl"
            assert path.exists()
            lines = path.read_text().strip().split("\n")
            assert len(lines) == expected_count


def test_split_with_10_examples(tmp_db):
    """With 10 examples, verify split math works correctly."""
    conn, _ = tmp_db
    _seed_many(conn, 10)

    with tempfile.TemporaryDirectory() as tmpdir:
        stats = export_training_data(conn, tmpdir)
        assert stats["total"] == 10
        assert stats["train"] == 8
        assert stats["valid"] == 1
        assert stats["test"] == 1


def test_min_events_filter(tmp_db):
    """Nodes with fewer events than min_events should be excluded."""
    conn, _ = tmp_db
    _seed_many(conn, 3)

    with tempfile.TemporaryDirectory() as tmpdir:
        # Each node has exactly 1 event, so min_events=2 excludes all
        stats = export_training_data(conn, tmpdir, min_events=2)
        assert stats["total"] == 0


def test_export_non_json_content_falls_back_to_embed_text(tmp_db):
    """When content is not valid JSON, assistant message falls back to embed_text."""
    conn, _ = tmp_db
    now_ms = int(time.time() * 1000)

    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (now_ms,),
    )
    conn.execute(
        "INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms, "
        "cwd, hostname, shell) VALUES (1, 1, ?, 'make build', 0, 1000, '/project', 'laptop', 'zsh')",
        (now_ms,),
    )
    # Content is NOT valid JSON — raw text
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, outcome, tags, "
        "enrichment_model, created_at, updated_at) "
        "VALUES (1, 'uuid-x', 'not json', 'the embed fallback text', 'success', '[]', 'model', ?, ?)",
        (now_ms, now_ms),
    )
    conn.execute("INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (1, 1)")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        stats = export_training_data(conn, tmpdir)
        assert stats["total"] == 1

        train_path = Path(tmpdir) / "train.jsonl"
        line = train_path.read_text().strip()
        data = json.loads(line)
        # Should fall back to embed_text since content is not JSON
        assert data["messages"][2]["content"] == "the embed fallback text"


def test_export_skips_failure_outcome(tmp_db):
    """Nodes with outcome='failure' should not be exported."""
    conn, _ = tmp_db
    now_ms = int(time.time() * 1000)

    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (now_ms,),
    )
    conn.execute(
        "INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms, "
        "cwd, hostname, shell) VALUES (1, 1, ?, 'cmd', 1, 500, '/p', 'laptop', 'zsh')",
        (now_ms,),
    )
    conn.execute(
        "INSERT INTO knowledge_nodes (id, uuid, content, embed_text, outcome, tags, "
        "enrichment_model, created_at, updated_at) "
        "VALUES (1, 'uuid-fail', '{}', 'text', 'failure', '[]', 'model', ?, ?)",
        (now_ms, now_ms),
    )
    conn.execute("INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (1, 1)")
    conn.commit()

    with tempfile.TemporaryDirectory() as tmpdir:
        stats = export_training_data(conn, tmpdir)
        assert stats["total"] == 0
