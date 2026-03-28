import json
import tempfile
import time
from pathlib import Path

from hippo_brain.training import export_training_data


def _seed_db(conn):
    """Insert sessions, events, knowledge nodes with linked events."""
    now_ms = int(time.time() * 1000)

    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) VALUES (1, ?, 'zsh', 'laptop', 'user')",
        (now_ms,),
    )

    # Insert events
    for i in range(1, 4):
        conn.execute(
            """INSERT INTO events (id, session_id, timestamp, command, exit_code, duration_ms,
                                   cwd, hostname, shell, git_branch)
               VALUES (?, 1, ?, ?, 0, ?, '/project', 'laptop', 'zsh', 'main')""",
            (i, now_ms + i, f"command-{i}", 1000 + i),
        )

    # Insert knowledge nodes
    content = json.dumps(
        {
            "summary": "Ran project commands successfully",
            "intent": "development",
            "outcome": "success",
        }
    )
    conn.execute(
        """INSERT INTO knowledge_nodes (id, uuid, content, embed_text, outcome, tags,
                                        enrichment_model, created_at, updated_at)
           VALUES (1, 'uuid-1', ?, 'ran project commands', 'success', '["dev"]', 'model', ?, ?)""",
        (content, now_ms, now_ms),
    )

    # Link events to knowledge node
    for i in range(1, 4):
        conn.execute(
            "INSERT INTO knowledge_node_events (knowledge_node_id, event_id) VALUES (1, ?)",
            (i,),
        )

    conn.commit()


def test_export_training_data(tmp_db):
    conn, _ = tmp_db
    _seed_db(conn)

    with tempfile.TemporaryDirectory() as tmpdir:
        stats = export_training_data(conn, tmpdir)

        assert stats["total"] == 1
        assert stats["train"] >= 1

        # Verify JSONL format
        train_path = Path(tmpdir) / "train.jsonl"
        assert train_path.exists()

        with open(train_path) as f:
            for line in f:
                data = json.loads(line)
                assert "messages" in data
                messages = data["messages"]
                assert len(messages) == 3
                assert messages[0]["role"] == "system"
                assert messages[1]["role"] == "user"
                assert messages[2]["role"] == "assistant"
                # User message should contain command text
                assert "command-" in messages[1]["content"]
                # Assistant message should be the summary
                assert "Ran project commands" in messages[2]["content"]


def test_export_empty_db(tmp_db):
    conn, _ = tmp_db

    with tempfile.TemporaryDirectory() as tmpdir:
        stats = export_training_data(conn, tmpdir)
        assert stats == {"total": 0, "train": 0, "valid": 0, "test": 0}
