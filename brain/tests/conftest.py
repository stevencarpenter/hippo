import sqlite3
import tempfile
from pathlib import Path

import pytest

SCHEMA_PATH = Path(__file__).parent.parent.parent / "crates" / "hippo-core" / "src" / "schema.sql"


@pytest.fixture
def tmp_db():
    """Create a temporary SQLite database with the hippo schema."""
    schema = SCHEMA_PATH.read_text()
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db_path = Path(tmp.name)

    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(schema)
    conn.commit()

    yield conn, db_path

    conn.close()
    db_path.unlink(missing_ok=True)
    # Clean up WAL/SHM files
    db_path.with_suffix(".db-wal").unlink(missing_ok=True)
    db_path.with_suffix(".db-shm").unlink(missing_ok=True)


@pytest.fixture
def mock_lmstudio_response():
    """Canned LM Studio chat completion response."""
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 1700000000,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": '{"summary": "Ran cargo test", "intent": "testing", "outcome": "success", "entities": {"projects": ["hippo"], "tools": ["cargo"], "files": [], "services": [], "errors": []}, "tags": ["rust", "testing"], "embed_text": "cargo test hippo-core all tests passed"}',
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
    }
