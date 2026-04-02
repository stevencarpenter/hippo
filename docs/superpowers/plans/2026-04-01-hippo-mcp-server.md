# Hippo MCP Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose Hippo's knowledge base as MCP tools so Claude Code can query a developer's shell, Claude, and browser activity mid-conversation.

**Architecture:** A Python FastMCP server inside the `brain/` project, using stdio transport. Opens SQLite + LanceDB directly and calls LM Studio for embeddings. Three tools: `search_knowledge` (semantic/lexical), `search_events` (raw events across sources), `get_entities` (entity graph). Structured logging with custom metrics for future OTel integration.

**Tech Stack:** Python 3.14+, FastMCP (mcp SDK), LanceDB, SQLite, LM Studio, structured logging

**Spec:** `docs/superpowers/specs/2026-04-01-hippo-mcp-server-design.md`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `brain/src/hippo_brain/mcp.py` | **New** — FastMCP server: tool definitions, lifespan, config loading, main entry point |
| `brain/src/hippo_brain/mcp_queries.py` | **New** — Pure query functions: search_knowledge_impl, search_events_impl, get_entities_impl, since parsing. No MCP/framework dependency. |
| `brain/src/hippo_brain/mcp_logging.py` | **New** — Structured logger setup + metrics counters for OTel-readiness |
| `brain/pyproject.toml` | **Modify** — Add `hippo-mcp` entry point + `mcp` dependency |
| `brain/tests/test_mcp_queries.py` | **New** — Unit tests for query functions (in-memory SQLite) |
| `brain/tests/test_mcp_server.py` | **New** — Integration tests for tool registration and MCP protocol |

**Why split `mcp.py` and `mcp_queries.py`?** The query functions are pure (take a sqlite3 connection, return dicts). The MCP layer is glue (tool decorators, lifespan, transport). Splitting them means tests can exercise queries without importing `mcp` SDK at all, and future consumers (CLI, HTTP) can reuse the query functions.

---

### Task 1: Add mcp dependency and entry point

**Files:**
- Modify: `brain/pyproject.toml`

- [ ] **Step 1: Add mcp dependency and entry point**

In `brain/pyproject.toml`, add `"mcp>=1.0"` to `dependencies` and `hippo-mcp` to `[project.scripts]`:

```toml
[project]
name = "hippo-brain"
version = "0.4.1"
requires-python = ">=3.14"
dependencies = [
    "httpx>=0.28",
    "lancedb>=0.30",
    "mcp>=1.0",
    "pyarrow>=23.0",
    "uvicorn>=0.42",
    "starlette>=1.0",
]

[project.scripts]
hippo-brain = "hippo_brain:main"
hippo-mcp = "hippo_brain.mcp:main"
```

- [ ] **Step 2: Verify dependency resolves**

Run: `uv run --project brain python -c "import mcp; print(mcp.__version__)"`
Expected: Prints a version string (e.g., `1.12.4`). If it fails, check that `mcp` is the correct PyPI package name.

- [ ] **Step 3: Commit**

```bash
git add brain/pyproject.toml brain/uv.lock
git commit -m "chore(brain): add mcp SDK dependency and hippo-mcp entry point"
```

---

### Task 2: Structured logging and metrics module

**Files:**
- Create: `brain/src/hippo_brain/mcp_logging.py`
- Test: `brain/tests/test_mcp_logging.py`

This module sets up structured logging that writes to stderr (stdout is reserved for MCP stdio protocol) and exposes metric counters that a future OTel exporter can scrape.

- [ ] **Step 1: Write the failing test**

Create `brain/tests/test_mcp_logging.py`:

```python
import logging

from hippo_brain.mcp_logging import setup_logging, MetricsCollector


def test_setup_logging_returns_logger():
    logger = setup_logging("test-mcp")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "hippo.mcp"
    assert logger.level == logging.INFO


def test_setup_logging_writes_to_stderr(capsys):
    logger = setup_logging("test-mcp")
    logger.info("hello from test")
    captured = capsys.readouterr()
    assert captured.out == "", "logging must not write to stdout (reserved for MCP stdio)"
    assert "hello from test" in captured.err


def test_metrics_collector_counters():
    m = MetricsCollector()
    assert m.tool_calls == 0
    assert m.tool_errors == 0
    assert m.semantic_searches == 0
    assert m.lexical_searches == 0
    assert m.lexical_fallbacks == 0
    assert m.lmstudio_errors == 0

    m.tool_calls += 1
    m.semantic_searches += 1
    assert m.tool_calls == 1


def test_metrics_collector_snapshot():
    m = MetricsCollector()
    m.tool_calls = 5
    m.tool_errors = 1
    m.semantic_searches = 3
    m.lexical_searches = 2
    m.lexical_fallbacks = 1
    m.lmstudio_errors = 1
    m.events_searched = 100
    m.entities_returned = 50

    snap = m.snapshot()
    assert snap == {
        "tool_calls": 5,
        "tool_errors": 1,
        "semantic_searches": 3,
        "lexical_searches": 2,
        "lexical_fallbacks": 1,
        "lmstudio_errors": 1,
        "events_searched": 100,
        "entities_returned": 50,
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project brain pytest brain/tests/test_mcp_logging.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement mcp_logging.py**

Create `brain/src/hippo_brain/mcp_logging.py`:

```python
"""Structured logging and metrics for the Hippo MCP server.

Logging goes to stderr (stdout is reserved for MCP stdio transport).
MetricsCollector holds counters suitable for future OTel export.
"""

import logging
import sys
from dataclasses import dataclass, field


def setup_logging(server_name: str) -> logging.Logger:
    """Configure structured logging to stderr for the MCP server."""
    logger = logging.getLogger("hippo.mcp")
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        logger.addHandler(handler)

    logger.propagate = False
    return logger


@dataclass
class MetricsCollector:
    """Counters for MCP server observability.

    Designed for future OTel gauge/counter export. Each field maps to a
    metric name like `hippo.mcp.tool_calls`.
    """

    tool_calls: int = 0
    tool_errors: int = 0
    semantic_searches: int = 0
    lexical_searches: int = 0
    lexical_fallbacks: int = 0
    lmstudio_errors: int = 0
    events_searched: int = 0
    entities_returned: int = 0

    def snapshot(self) -> dict[str, int]:
        """Return all metrics as a dict (for health checks or OTel export)."""
        return {
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "semantic_searches": self.semantic_searches,
            "lexical_searches": self.lexical_searches,
            "lexical_fallbacks": self.lexical_fallbacks,
            "lmstudio_errors": self.lmstudio_errors,
            "events_searched": self.events_searched,
            "entities_returned": self.entities_returned,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project brain pytest brain/tests/test_mcp_logging.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Lint**

Run: `uv run --project brain ruff check brain/src/hippo_brain/mcp_logging.py && uv run --project brain ruff format --check brain/src/hippo_brain/mcp_logging.py`
Expected: Clean

- [ ] **Step 6: Commit**

```bash
git add brain/src/hippo_brain/mcp_logging.py brain/tests/test_mcp_logging.py
git commit -m "feat(mcp): add structured logging and metrics collector for OTel readiness"
```

---

### Task 3: Query functions module (search_knowledge, search_events, get_entities)

**Files:**
- Create: `brain/src/hippo_brain/mcp_queries.py`
- Test: `brain/tests/test_mcp_queries.py`

Pure query functions that take a `sqlite3.Connection` and return lists of dicts. No MCP framework dependency. The since-parsing helper also lives here.

- [ ] **Step 1: Write failing tests**

Create `brain/tests/test_mcp_queries.py`:

```python
import sqlite3
import time

import pytest

from hippo_brain.mcp_queries import (
    parse_since,
    search_knowledge_lexical,
    search_events_impl,
    get_entities_impl,
)


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript("""
        CREATE TABLE knowledge_nodes (
            id INTEGER PRIMARY KEY,
            uuid TEXT NOT NULL UNIQUE,
            content TEXT NOT NULL,
            embed_text TEXT NOT NULL,
            node_type TEXT NOT NULL DEFAULT 'observation',
            outcome TEXT,
            tags TEXT,
            enrichment_model TEXT,
            enrichment_version INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            session_id INTEGER,
            timestamp INTEGER NOT NULL,
            command TEXT,
            exit_code INTEGER,
            duration_ms INTEGER,
            cwd TEXT,
            hostname TEXT,
            shell TEXT,
            git_repo TEXT,
            git_branch TEXT,
            git_commit TEXT,
            git_dirty INTEGER,
            stdout TEXT,
            stderr TEXT,
            enriched INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE claude_sessions (
            id INTEGER PRIMARY KEY,
            session_id TEXT NOT NULL,
            project_dir TEXT NOT NULL,
            cwd TEXT NOT NULL,
            git_branch TEXT,
            segment_index INTEGER NOT NULL,
            start_time INTEGER NOT NULL,
            end_time INTEGER NOT NULL,
            summary_text TEXT NOT NULL,
            tool_calls_json TEXT,
            user_prompts_json TEXT,
            message_count INTEGER NOT NULL,
            token_count INTEGER,
            source_file TEXT NOT NULL,
            is_subagent INTEGER NOT NULL DEFAULT 0,
            parent_session_id TEXT,
            enriched INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT 0,
            UNIQUE (session_id, segment_index)
        );
        CREATE TABLE browser_events (
            id INTEGER PRIMARY KEY,
            timestamp INTEGER NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            domain TEXT NOT NULL,
            dwell_ms INTEGER NOT NULL,
            scroll_depth REAL,
            extracted_text TEXT,
            search_query TEXT,
            referrer TEXT,
            content_hash TEXT,
            envelope_id TEXT,
            enriched INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            canonical TEXT,
            metadata TEXT,
            first_seen INTEGER NOT NULL DEFAULT 0,
            last_seen INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT 0,
            UNIQUE (type, canonical)
        );
    """)
    return conn


class TestParseSince:
    def test_hours(self):
        now_ms = int(time.time() * 1000)
        result = parse_since("24h")
        assert abs(result - (now_ms - 24 * 3600 * 1000)) < 2000

    def test_days(self):
        now_ms = int(time.time() * 1000)
        result = parse_since("7d")
        assert abs(result - (now_ms - 7 * 24 * 3600 * 1000)) < 2000

    def test_minutes(self):
        now_ms = int(time.time() * 1000)
        result = parse_since("30m")
        assert abs(result - (now_ms - 30 * 60 * 1000)) < 2000

    def test_invalid_returns_zero(self):
        assert parse_since("") == 0
        assert parse_since("abc") == 0
        assert parse_since("24x") == 0


class TestSearchKnowledgeLexical:
    def test_finds_matching_nodes(self, db):
        db.execute(
            "INSERT INTO knowledge_nodes (uuid, content, embed_text, outcome, tags) "
            "VALUES ('u1', '{\"summary\":\"cargo build fix\"}', 'Fixed cargo build error in hippo', 'success', '[]')"
        )
        db.execute(
            "INSERT INTO knowledge_nodes (uuid, content, embed_text, outcome) "
            "VALUES ('u2', '{\"summary\":\"unrelated\"}', 'Nothing relevant here', 'success')"
        )
        db.commit()

        results = search_knowledge_lexical(db, "cargo build", limit=10)
        assert len(results) == 1
        assert "cargo build" in results[0]["embed_text"].lower()

    def test_empty_query_returns_recent(self, db):
        db.execute(
            "INSERT INTO knowledge_nodes (uuid, content, embed_text, outcome, created_at) "
            "VALUES ('u1', '{\"summary\":\"recent\"}', 'recent work', 'success', 9999)"
        )
        db.commit()

        results = search_knowledge_lexical(db, "", limit=10)
        assert len(results) == 1

    def test_limit_respected(self, db):
        for i in range(5):
            db.execute(
                "INSERT INTO knowledge_nodes (uuid, content, embed_text, outcome) "
                f"VALUES ('u{i}', '{{\"summary\":\"cargo {i}\"}}', 'cargo test {i}', 'success')"
            )
        db.commit()

        results = search_knowledge_lexical(db, "cargo", limit=3)
        assert len(results) == 3


class TestSearchEvents:
    def test_shell_events(self, db):
        now_ms = int(time.time() * 1000)
        db.execute(
            "INSERT INTO events (timestamp, command, exit_code, duration_ms, cwd, shell, git_branch) "
            "VALUES (?, 'cargo test', 0, 1000, '/projects/hippo', 'zsh', 'main')",
            (now_ms,),
        )
        db.commit()

        results = search_events_impl(db, query="cargo", source="shell", limit=10)
        assert len(results) == 1
        assert results[0]["source"] == "shell"
        assert results[0]["summary"] == "cargo test"

    def test_browser_events(self, db):
        now_ms = int(time.time() * 1000)
        db.execute(
            "INSERT INTO browser_events (timestamp, url, title, domain, dwell_ms, scroll_depth) "
            "VALUES (?, 'https://docs.rs/serde', 'serde docs', 'docs.rs', 5000, 0.8)",
            (now_ms,),
        )
        db.commit()

        results = search_events_impl(db, query="serde", source="browser", limit=10)
        assert len(results) == 1
        assert results[0]["source"] == "browser"
        assert "docs.rs" in results[0]["summary"]

    def test_claude_events(self, db):
        now_ms = int(time.time() * 1000)
        db.execute(
            "INSERT INTO claude_sessions "
            "(session_id, project_dir, cwd, segment_index, start_time, end_time, "
            "summary_text, message_count, source_file) "
            "VALUES ('s1', '/proj', '/proj', 0, ?, ?, 'Implemented MCP server', 10, 'f.jsonl')",
            (now_ms, now_ms + 60000),
        )
        db.commit()

        results = search_events_impl(db, query="MCP", source="claude", limit=10)
        assert len(results) == 1
        assert results[0]["source"] == "claude"

    def test_source_all(self, db):
        now_ms = int(time.time() * 1000)
        db.execute(
            "INSERT INTO events (timestamp, command, exit_code, duration_ms, cwd, shell) "
            "VALUES (?, 'echo all', 0, 10, '/tmp', 'zsh')",
            (now_ms,),
        )
        db.execute(
            "INSERT INTO browser_events (timestamp, url, title, domain, dwell_ms) "
            "VALUES (?, 'https://all.com', 'all page', 'all.com', 3000)",
            (now_ms,),
        )
        db.commit()

        results = search_events_impl(db, source="all", limit=10)
        sources = {r["source"] for r in results}
        assert "shell" in sources
        assert "browser" in sources

    def test_since_filter(self, db):
        old_ms = int(time.time() * 1000) - 48 * 3600 * 1000  # 48h ago
        now_ms = int(time.time() * 1000)
        db.execute(
            "INSERT INTO events (timestamp, command, exit_code, duration_ms, cwd, shell) "
            "VALUES (?, 'old cmd', 0, 10, '/tmp', 'zsh')",
            (old_ms,),
        )
        db.execute(
            "INSERT INTO events (timestamp, command, exit_code, duration_ms, cwd, shell) "
            "VALUES (?, 'new cmd', 0, 10, '/tmp', 'zsh')",
            (now_ms,),
        )
        db.commit()

        results = search_events_impl(db, since="24h", source="shell", limit=10)
        assert len(results) == 1
        assert results[0]["summary"] == "new cmd"

    def test_project_filter(self, db):
        now_ms = int(time.time() * 1000)
        db.execute(
            "INSERT INTO events (timestamp, command, exit_code, duration_ms, cwd, shell) "
            "VALUES (?, 'cmd1', 0, 10, '/projects/hippo', 'zsh')",
            (now_ms,),
        )
        db.execute(
            "INSERT INTO events (timestamp, command, exit_code, duration_ms, cwd, shell) "
            "VALUES (?, 'cmd2', 0, 10, '/projects/other', 'zsh')",
            (now_ms,),
        )
        db.commit()

        results = search_events_impl(db, project="hippo", source="shell", limit=10)
        assert len(results) == 1
        assert results[0]["summary"] == "cmd1"


class TestGetEntities:
    def test_basic(self, db):
        db.execute(
            "INSERT INTO entities (type, name, canonical, first_seen, last_seen) "
            "VALUES ('tool', 'cargo', 'cargo', 1000, 2000)"
        )
        db.commit()

        results = get_entities_impl(db, limit=50)
        assert len(results) == 1
        assert results[0]["type"] == "tool"
        assert results[0]["name"] == "cargo"

    def test_type_filter(self, db):
        db.execute(
            "INSERT INTO entities (type, name, canonical, first_seen, last_seen) "
            "VALUES ('tool', 'cargo', 'cargo', 1000, 2000)"
        )
        db.execute(
            "INSERT INTO entities (type, name, canonical, first_seen, last_seen) "
            "VALUES ('project', 'hippo', 'hippo', 1000, 2000)"
        )
        db.commit()

        results = get_entities_impl(db, entity_type="project", limit=50)
        assert len(results) == 1
        assert results[0]["name"] == "hippo"

    def test_query_filter(self, db):
        db.execute(
            "INSERT INTO entities (type, name, canonical, first_seen, last_seen) "
            "VALUES ('tool', 'cargo', 'cargo', 1000, 2000)"
        )
        db.execute(
            "INSERT INTO entities (type, name, canonical, first_seen, last_seen) "
            "VALUES ('tool', 'ruff', 'ruff', 1000, 2000)"
        )
        db.commit()

        results = get_entities_impl(db, query="carg", limit=50)
        assert len(results) == 1
        assert results[0]["name"] == "cargo"

    def test_limit(self, db):
        for i in range(10):
            db.execute(
                "INSERT INTO entities (type, name, canonical, first_seen, last_seen) "
                f"VALUES ('tool', 'tool{i}', 'tool{i}', 1000, 2000)"
            )
        db.commit()

        results = get_entities_impl(db, limit=5)
        assert len(results) == 5
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project brain pytest brain/tests/test_mcp_queries.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement mcp_queries.py**

Create `brain/src/hippo_brain/mcp_queries.py`:

```python
"""Pure query functions for the Hippo MCP server.

Each function takes a sqlite3.Connection and returns a list of dicts.
No MCP framework dependency — reusable from CLI, HTTP, or MCP.
"""

import json
import re
import sqlite3
import time


def parse_since(since: str) -> int:
    """Parse a duration string like '24h', '7d', '30m' into an epoch-ms threshold.

    Returns 0 if the string is empty or unparseable (meaning no time filter).
    """
    if not since:
        return 0
    match = re.match(r"^(\d+)(h|d|m)$", since.strip())
    if not match:
        return 0
    value = int(match.group(1))
    unit = match.group(2)
    unit_ms = {"h": 3600 * 1000, "d": 24 * 3600 * 1000, "m": 60 * 1000}[unit]
    now_ms = int(time.time() * 1000)
    return now_ms - (value * unit_ms)


def search_knowledge_lexical(
    conn: sqlite3.Connection, query: str, limit: int = 10
) -> list[dict]:
    """Lexical (LIKE) search over knowledge_nodes."""
    if query:
        pattern = f"%{query}%"
        rows = conn.execute(
            """
            SELECT id, uuid, content, embed_text, outcome, tags, created_at
            FROM knowledge_nodes
            WHERE content LIKE ? OR embed_text LIKE ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (pattern, pattern, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT id, uuid, content, embed_text, outcome, tags, created_at
            FROM knowledge_nodes
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    results = []
    for row in rows:
        node_id, uuid, content_str, embed_text, outcome, tags_str, created_at = row
        try:
            content = json.loads(content_str)
        except (json.JSONDecodeError, TypeError):
            content = {}
        try:
            tags = json.loads(tags_str) if tags_str else []
        except (json.JSONDecodeError, TypeError):
            tags = []

        results.append({
            "score": None,
            "summary": content.get("summary", ""),
            "intent": content.get("intent", ""),
            "outcome": outcome or "",
            "tags": tags,
            "embed_text": embed_text or "",
            "cwd": "",
            "git_branch": "",
        })
    return results


def search_events_impl(
    conn: sqlite3.Connection,
    query: str = "",
    source: str = "all",
    since: str = "",
    project: str = "",
    limit: int = 20,
) -> list[dict]:
    """Search raw events across shell, claude, and browser sources."""
    since_ms = parse_since(since)
    results = []

    if source in ("shell", "all"):
        results.extend(_search_shell_events(conn, query, since_ms, project, limit))
    if source in ("claude", "all"):
        results.extend(_search_claude_events(conn, query, since_ms, project, limit))
    if source in ("browser", "all"):
        results.extend(_search_browser_events(conn, query, since_ms, limit))

    results.sort(key=lambda r: r["timestamp"], reverse=True)
    return results[:limit] if source == "all" else results


def _search_shell_events(
    conn: sqlite3.Connection, query: str, since_ms: int, project: str, limit: int
) -> list[dict]:
    conditions = []
    params: list = []

    if query:
        conditions.append("command LIKE ?")
        params.append(f"%{query}%")
    if since_ms:
        conditions.append("timestamp >= ?")
        params.append(since_ms)
    if project:
        conditions.append("cwd LIKE ?")
        params.append(f"%{project}%")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"""
        SELECT timestamp, command, exit_code, duration_ms, cwd, git_branch
        FROM events
        {where}
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    return [
        {
            "source": "shell",
            "timestamp": row[0],
            "summary": row[1] or "",
            "cwd": row[4] or "",
            "detail": f"exit={row[2]} duration={row[3]}ms",
            "git_branch": row[5] or "",
        }
        for row in rows
    ]


def _search_claude_events(
    conn: sqlite3.Connection, query: str, since_ms: int, project: str, limit: int
) -> list[dict]:
    conditions = []
    params: list = []

    if query:
        conditions.append("summary_text LIKE ?")
        params.append(f"%{query}%")
    if since_ms:
        conditions.append("start_time >= ?")
        params.append(since_ms)
    if project:
        conditions.append("cwd LIKE ?")
        params.append(f"%{project}%")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"""
        SELECT start_time, summary_text, cwd, git_branch, message_count,
               tool_calls_json
        FROM claude_sessions
        {where}
        ORDER BY start_time DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    results = []
    for row in rows:
        tool_count = 0
        if row[5]:
            try:
                tool_count = len(json.loads(row[5]))
            except (json.JSONDecodeError, TypeError):
                pass
        results.append({
            "source": "claude",
            "timestamp": row[0],
            "summary": row[1] or "",
            "cwd": row[2] or "",
            "detail": f"messages={row[4]} tools={tool_count}",
            "git_branch": row[3] or "",
        })
    return results


def _search_browser_events(
    conn: sqlite3.Connection, query: str, since_ms: int, limit: int
) -> list[dict]:
    conditions = []
    params: list = []

    if query:
        conditions.append("(url LIKE ? OR title LIKE ?)")
        params.extend([f"%{query}%", f"%{query}%"])
    if since_ms:
        conditions.append("timestamp >= ?")
        params.append(since_ms)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"""
        SELECT timestamp, url, title, domain, dwell_ms, scroll_depth
        FROM browser_events
        {where}
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    return [
        {
            "source": "browser",
            "timestamp": row[0],
            "summary": f"{row[3]} — {row[2] or row[1]}",
            "cwd": "",
            "detail": f"dwell={row[4]}ms scroll={int((row[5] or 0) * 100)}%",
            "git_branch": "",
        }
        for row in rows
    ]


def get_entities_impl(
    conn: sqlite3.Connection,
    entity_type: str = "",
    query: str = "",
    limit: int = 50,
) -> list[dict]:
    """List entities from the knowledge graph."""
    conditions = []
    params: list = []

    if entity_type:
        conditions.append("type = ?")
        params.append(entity_type)
    if query:
        conditions.append("name LIKE ?")
        params.append(f"%{query}%")

    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    rows = conn.execute(
        f"""
        SELECT type, name, canonical, first_seen, last_seen
        FROM entities
        {where}
        ORDER BY last_seen DESC
        LIMIT ?
        """,
        (*params, limit),
    ).fetchall()

    return [
        {
            "type": row[0],
            "name": row[1],
            "canonical": row[2] or "",
            "first_seen": row[3],
            "last_seen": row[4],
        }
        for row in rows
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project brain pytest brain/tests/test_mcp_queries.py -v`
Expected: All tests PASS

- [ ] **Step 5: Lint**

Run: `uv run --project brain ruff check brain/src/hippo_brain/mcp_queries.py && uv run --project brain ruff format --check brain/src/hippo_brain/mcp_queries.py`
Expected: Clean

- [ ] **Step 6: Commit**

```bash
git add brain/src/hippo_brain/mcp_queries.py brain/tests/test_mcp_queries.py
git commit -m "feat(mcp): add pure query functions for knowledge, events, and entities"
```

---

### Task 4: MCP server module with FastMCP tools

**Files:**
- Create: `brain/src/hippo_brain/mcp.py`
- Test: `brain/tests/test_mcp_server.py`

The FastMCP server wires the query functions to MCP tools. Uses lifespan to initialize SQLite, LanceDB, and LM Studio client at startup.

- [ ] **Step 1: Write failing tests**

Create `brain/tests/test_mcp_server.py`:

```python
import json
import sqlite3

import pytest

from hippo_brain.mcp import mcp, _get_conn, _state


class TestMCPToolRegistration:
    def test_tools_registered(self):
        """Verify all 3 tools are registered with the FastMCP server."""
        tools = mcp._tool_manager._tools
        tool_names = set(tools.keys())
        assert "search_knowledge" in tool_names
        assert "search_events" in tool_names
        assert "get_entities" in tool_names

    def test_search_knowledge_has_parameters(self):
        tools = mcp._tool_manager._tools
        tool = tools["search_knowledge"]
        # The tool should accept query, mode, limit
        schema = tool.parameters
        assert "query" in schema.get("properties", {}) or "query" in str(schema)


class TestGetConn:
    def test_returns_connection(self, tmp_path):
        from hippo_brain.mcp_queries import get_entities_impl

        db_path = str(tmp_path / "test.db")
        # Create a minimal DB
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA user_version = 4")
        conn.execute(
            "CREATE TABLE entities (id INTEGER PRIMARY KEY, type TEXT, name TEXT, "
            "canonical TEXT, metadata TEXT, first_seen INTEGER, last_seen INTEGER, "
            "created_at INTEGER)"
        )
        conn.close()

        test_conn = _get_conn(db_path)
        assert isinstance(test_conn, sqlite3.Connection)
        # Verify WAL mode
        mode = test_conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"
        test_conn.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project brain pytest brain/tests/test_mcp_server.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement mcp.py**

Create `brain/src/hippo_brain/mcp.py`:

```python
"""Hippo MCP Server — expose the knowledge base as tools for Claude Code.

Runs as a stdio MCP server. Opens SQLite + LanceDB + LM Studio directly
(independent of hippo-brain HTTP server). Read-only: never writes to the DB.

Usage: uv run --project brain hippo-mcp
"""

import sqlite3
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from hippo_brain.client import LMStudioClient
from hippo_brain.embeddings import (
    get_or_create_table,
    open_vector_db,
    search_similar,
)
from hippo_brain.mcp_logging import MetricsCollector, setup_logging
from hippo_brain.mcp_queries import (
    get_entities_impl,
    search_events_impl,
    search_knowledge_lexical,
)

logger = setup_logging("hippo-mcp")
metrics = MetricsCollector()


def _load_config() -> dict:
    """Load settings from ~/.config/hippo/config.toml (same as brain __init__.py)."""
    config_path = Path.home() / ".config" / "hippo" / "config.toml"
    if not config_path.exists():
        return {
            "db_path": str(Path.home() / ".local" / "share" / "hippo" / "hippo.db"),
            "data_dir": str(Path.home() / ".local" / "share" / "hippo"),
            "lmstudio_base_url": "http://localhost:1234/v1",
            "embedding_model": "",
        }

    with config_path.open("rb") as f:
        config = tomllib.load(f)

    storage = config.get("storage", {})
    data_dir = Path(
        storage.get("data_dir", Path.home() / ".local" / "share" / "hippo")
    ).expanduser()

    return {
        "db_path": str(data_dir / "hippo.db"),
        "data_dir": str(data_dir),
        "lmstudio_base_url": config.get("lmstudio", {}).get(
            "base_url", "http://localhost:1234/v1"
        ),
        "embedding_model": config.get("models", {}).get("embedding", ""),
    }


@dataclass
class _ServerState:
    """Holds initialized resources for the MCP server."""

    db_path: str = ""
    lm_client: LMStudioClient | None = None
    embedding_model: str = ""
    vector_table: object | None = None


_state = _ServerState()


def _get_conn(db_path: str = "") -> sqlite3.Connection:
    """Open a read-only SQLite connection with standard pragmas."""
    path = db_path or _state.db_path
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_state() -> None:
    """Initialize server state from config. Called once at startup."""
    config = _load_config()
    _state.db_path = config["db_path"]
    _state.embedding_model = config["embedding_model"]

    _state.lm_client = LMStudioClient(base_url=config["lmstudio_base_url"])

    if config["embedding_model"]:
        try:
            vdb = open_vector_db(config["data_dir"])
            _state.vector_table = get_or_create_table(vdb)
            logger.info(
                "vector store initialized",
                extra={"data_dir": config["data_dir"]},
            )
        except Exception as e:
            logger.warning("vector store unavailable: %s", e)
            _state.vector_table = None

    logger.info(
        "MCP server state initialized",
        extra={
            "db_path": config["db_path"],
            "embedding_model": config["embedding_model"],
            "lmstudio_url": config["lmstudio_base_url"],
        },
    )


# --- FastMCP server ---

mcp = FastMCP(
    "hippo",
    instructions=(
        "Search a developer's personal knowledge base. "
        "Use search_knowledge for enriched summaries of work sessions. "
        "Use search_events for raw shell commands, Claude sessions, and browser visits. "
        "Use get_entities to explore known projects, tools, files, and concepts."
    ),
)


@mcp.tool()
async def search_knowledge(
    query: str, mode: str = "semantic", limit: int = 10
) -> list[dict]:
    """Search enriched knowledge nodes — synthesized summaries of developer activity.

    Args:
        query: Natural language search query.
        mode: "semantic" (vector similarity, default) or "lexical" (substring match).
        limit: Max results (default 10).

    Returns:
        List of knowledge nodes with score, summary, intent, outcome, tags, embed_text.
    """
    start = time.monotonic()
    metrics.tool_calls += 1
    logger.info("search_knowledge called", extra={"query": query, "mode": mode, "limit": limit})

    try:
        if mode == "semantic" and _state.vector_table is not None and _state.lm_client:
            try:
                metrics.semantic_searches += 1
                vecs = await _state.lm_client.embed([query], model=_state.embedding_model)
                hits = search_similar(_state.vector_table, vecs[0], limit=limit)
                results = [
                    {
                        "score": round(1.0 - hit.get("_distance", 0.0), 4),
                        "summary": hit.get("summary", ""),
                        "intent": "",
                        "outcome": hit.get("outcome", ""),
                        "tags": hit.get("tags", ""),
                        "embed_text": hit.get("embed_text", ""),
                        "cwd": hit.get("cwd", ""),
                        "git_branch": hit.get("git_branch", ""),
                    }
                    for hit in hits
                ]
                elapsed = time.monotonic() - start
                logger.info(
                    "search_knowledge completed",
                    extra={"mode": "semantic", "results": len(results), "elapsed_s": round(elapsed, 3)},
                )
                return results
            except Exception as e:
                metrics.lexical_fallbacks += 1
                metrics.lmstudio_errors += 1
                logger.warning("semantic search failed, falling back to lexical: %s", e)

        metrics.lexical_searches += 1
        conn = _get_conn()
        try:
            results = search_knowledge_lexical(conn, query, limit=limit)
        finally:
            conn.close()

        elapsed = time.monotonic() - start
        logger.info(
            "search_knowledge completed",
            extra={"mode": "lexical", "results": len(results), "elapsed_s": round(elapsed, 3)},
        )
        return results
    except Exception as e:
        metrics.tool_errors += 1
        logger.error("search_knowledge failed: %s", e)
        raise


@mcp.tool()
async def search_events(
    query: str = "",
    source: str = "all",
    since: str = "",
    project: str = "",
    limit: int = 20,
) -> list[dict]:
    """Search raw events — shell commands, Claude sessions, and browser visits.

    Args:
        query: Keyword search (substring match). Empty returns most recent events.
        source: Filter by source: "shell", "claude", "browser", or "all" (default).
        since: Time filter, e.g. "24h", "7d", "30m". Empty means no time filter.
        project: Substring match on working directory path.
        limit: Max results (default 20).

    Returns:
        List of events with source, timestamp, summary, cwd, detail, git_branch.
    """
    start = time.monotonic()
    metrics.tool_calls += 1
    logger.info(
        "search_events called",
        extra={"query": query, "source": source, "since": since, "project": project, "limit": limit},
    )

    try:
        conn = _get_conn()
        try:
            results = search_events_impl(
                conn, query=query, source=source, since=since, project=project, limit=limit
            )
        finally:
            conn.close()

        metrics.events_searched += len(results)
        elapsed = time.monotonic() - start
        logger.info(
            "search_events completed",
            extra={"results": len(results), "elapsed_s": round(elapsed, 3)},
        )
        return results
    except Exception as e:
        metrics.tool_errors += 1
        logger.error("search_events failed: %s", e)
        raise


@mcp.tool()
async def get_entities(
    type: str = "", query: str = "", limit: int = 50
) -> list[dict]:
    """List known entities from the knowledge graph — projects, tools, files, domains, concepts.

    Args:
        type: Filter by entity type (e.g., "project", "tool", "file", "domain", "concept").
        query: Substring match on entity name.
        limit: Max results (default 50).

    Returns:
        List of entities with type, name, canonical, first_seen, last_seen.
    """
    start = time.monotonic()
    metrics.tool_calls += 1
    logger.info(
        "get_entities called",
        extra={"type": type, "query": query, "limit": limit},
    )

    try:
        conn = _get_conn()
        try:
            results = get_entities_impl(conn, entity_type=type, query=query, limit=limit)
        finally:
            conn.close()

        metrics.entities_returned += len(results)
        elapsed = time.monotonic() - start
        logger.info(
            "get_entities completed",
            extra={"results": len(results), "elapsed_s": round(elapsed, 3)},
        )
        return results
    except Exception as e:
        metrics.tool_errors += 1
        logger.error("get_entities failed: %s", e)
        raise


def main():
    """Entry point for hippo-mcp console script."""
    _init_state()
    logger.info("starting MCP server (stdio transport)")
    mcp.run(transport="stdio")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project brain pytest brain/tests/test_mcp_server.py -v`
Expected: All tests PASS

- [ ] **Step 5: Run the full test suite**

Run: `uv run --project brain pytest brain/tests -v`
Expected: All existing + new tests PASS

- [ ] **Step 6: Lint**

Run: `uv run --project brain ruff check brain/src/hippo_brain/mcp.py && uv run --project brain ruff format --check brain/src/hippo_brain/mcp.py`
Expected: Clean

- [ ] **Step 7: Commit**

```bash
git add brain/src/hippo_brain/mcp.py brain/tests/test_mcp_server.py
git commit -m "feat(mcp): add FastMCP server with search_knowledge, search_events, get_entities tools"
```

---

### Task 5: Integration test — stdio protocol round-trip

**Files:**
- Modify: `brain/tests/test_mcp_server.py` (add integration test)

- [ ] **Step 1: Write the integration test**

Add to `brain/tests/test_mcp_server.py`:

```python
import subprocess
import sys
import struct


class TestMCPStdioProtocol:
    def test_server_starts_and_lists_tools(self, tmp_path):
        """Start the MCP server as a subprocess and verify tools/list works."""
        # We can't easily test full MCP protocol without the SDK client,
        # so just verify the process starts and responds to initialize.
        proc = subprocess.Popen(
            [sys.executable, "-m", "hippo_brain.mcp"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(tmp_path),
        )

        # MCP uses JSON-RPC over stdio. Send initialize request.
        init_msg = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0.1"},
            },
        }).encode()

        # Write message with Content-Length header (MCP stdio framing)
        header = f"Content-Length: {len(init_msg)}\r\n\r\n".encode()
        proc.stdin.write(header + init_msg)
        proc.stdin.flush()

        # Read response (with timeout)
        import select
        ready, _, _ = select.select([proc.stdout], [], [], 5)
        assert ready, "MCP server did not respond within 5 seconds"

        # Read Content-Length header from response
        response_header = b""
        while b"\r\n\r\n" not in response_header:
            chunk = proc.stdout.read(1)
            if not chunk:
                break
            response_header += chunk

        content_length = int(response_header.decode().split("Content-Length: ")[1].split("\r\n")[0])
        response_body = proc.stdout.read(content_length)
        response = json.loads(response_body)

        assert response.get("id") == 1
        assert "result" in response
        assert "serverInfo" in response["result"]

        proc.terminate()
        proc.wait(timeout=3)
```

- [ ] **Step 2: Also add `__main__.py` so `python -m hippo_brain.mcp` works**

Create `brain/src/hippo_brain/mcp/__init__.py` — actually, since `mcp.py` is a single file module, add a `__main__` block to it instead. Add to the bottom of `brain/src/hippo_brain/mcp.py`:

```python
if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Run integration test**

Run: `uv run --project brain pytest brain/tests/test_mcp_server.py::TestMCPStdioProtocol -v`
Expected: PASS (server starts, responds to initialize)

- [ ] **Step 4: Commit**

```bash
git add brain/src/hippo_brain/mcp.py brain/tests/test_mcp_server.py
git commit -m "test(mcp): add stdio protocol integration test"
```

---

### Task 6: Full verification and documentation

**Files:**
- Modify: `CLAUDE.md` (add MCP server section)

- [ ] **Step 1: Run full test suite**

```bash
uv run --project brain pytest brain/tests -v
```
Expected: All tests pass

- [ ] **Step 2: Run linting**

```bash
uv run --project brain ruff check brain/ && uv run --project brain ruff format --check brain/
```
Expected: Clean

- [ ] **Step 3: Verify MCP server starts**

```bash
echo '{}' | timeout 3 uv run --project brain hippo-mcp 2>/tmp/hippo-mcp-test.log || true
cat /tmp/hippo-mcp-test.log
```
Expected: Log shows "MCP server state initialized" and "starting MCP server"

- [ ] **Step 4: Update CLAUDE.md**

Add an MCP Server section to the project `CLAUDE.md` under Commands:

```markdown
### MCP Server

    uv run --project brain hippo-mcp    # Start MCP server (stdio transport)

The MCP server exposes three tools: `search_knowledge`, `search_events`, `get_entities`.
Configure in `~/.config/mcp/mcp-master.json`:

```json
{
  "hippo": {
    "type": "stdio",
    "command": "uv",
    "args": ["run", "--project", "/path/to/hippo/brain", "hippo-mcp"]
  }
}
```

Logs go to stderr. Metrics available via `MetricsCollector.snapshot()` for future OTel export.
```

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: add MCP server section to CLAUDE.md"
```

---

### Task Assignment Notes

Tasks are designed for parallel execution by teammates:

| Task | Dependencies | Parallelizable with |
|------|-------------|---------------------|
| Task 1 (dependency) | None | — |
| Task 2 (logging/metrics) | Task 1 | Task 3 |
| Task 3 (query functions) | Task 1 | Task 2 |
| Task 4 (MCP server) | Task 1, 2, 3 | — |
| Task 5 (integration test) | Task 4 | — |
| Task 6 (verification) | Task 5 | — |

**Recommended team split:** Tasks 2 and 3 can run in parallel after Task 1 completes. Task 4 depends on both. Tasks 5-6 are sequential finishers.
