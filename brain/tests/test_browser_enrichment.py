"""Tests for browser enrichment module."""

import sqlite3
import time

import pytest

from hippo_brain.browser_enrichment import (
    _chunk_by_time_gap,
    build_browser_enrichment_prompt,
    claim_pending_browser_events,
    format_browser_context_for_shell_prompt,
    get_correlated_browser_events,
    mark_browser_queue_failed,
    write_browser_knowledge_node,
)
from hippo_brain.models import EnrichmentResult
from tests.conftest import SCHEMA_PATH


@pytest.fixture
def db():
    """In-memory SQLite with the full hippo schema."""
    schema = SCHEMA_PATH.read_text()
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(schema)
    conn.commit()
    yield conn
    conn.close()


def _insert_browser_event(
    conn,
    event_id,
    timestamp,
    url="https://example.com",
    title="Example",
    domain="example.com",
    dwell_ms=5000,
    scroll_depth=0.5,
    extracted_text=None,
    search_query=None,
):
    """Helper to insert a browser event and its queue entry."""
    conn.execute(
        """INSERT INTO browser_events (id, timestamp, url, title, domain, dwell_ms,
                                       scroll_depth, extracted_text, search_query)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event_id,
            timestamp,
            url,
            title,
            domain,
            dwell_ms,
            scroll_depth,
            extracted_text,
            search_query,
        ),
    )
    conn.execute(
        "INSERT INTO browser_enrichment_queue (browser_event_id) VALUES (?)",
        (event_id,),
    )
    conn.commit()


class TestClaimPendingBrowserEvents:
    def test_claim_pending_browser_events(self, db):
        """Insert 2 old browser events, claim them, verify 1 chunk with 2 events."""
        old_ts = int(time.time() * 1000) - 120_000  # 2 minutes ago
        _insert_browser_event(
            db, 1, old_ts, url="https://docs.rs/anyhow", title="anyhow docs", domain="docs.rs"
        )
        _insert_browser_event(
            db,
            2,
            old_ts + 1000,
            url="https://docs.rs/thiserror",
            title="thiserror docs",
            domain="docs.rs",
        )

        chunks = claim_pending_browser_events(db, "test-worker", stale_secs=60)
        assert len(chunks) == 1
        assert len(chunks[0]) == 2
        assert chunks[0][0]["id"] == 1
        assert chunks[0][1]["id"] == 2

    def test_claim_skips_fresh_events(self, db):
        """Insert event with current timestamp, claim returns empty."""
        now_ms = int(time.time() * 1000)
        _insert_browser_event(db, 1, now_ms)

        chunks = claim_pending_browser_events(db, "test-worker", stale_secs=60)
        assert chunks == []

    def test_claim_skips_low_engagement_events(self, db):
        """Events with low scroll and no search query are marked skipped."""
        now_ms = int(time.time() * 1000)
        stale_ts = now_ms - 120_000

        # Low scroll, no search query — should be skipped
        _insert_browser_event(
            db,
            1,
            stale_ts,
            url="https://so.com/q/1",
            domain="so.com",
            dwell_ms=5000,
            scroll_depth=0.05,
        )
        # Good scroll — should be kept
        _insert_browser_event(
            db,
            2,
            stale_ts + 1000,
            url="https://so.com/q/2",
            domain="so.com",
            dwell_ms=8000,
            scroll_depth=0.80,
        )
        # Low scroll but has search query — should be kept
        _insert_browser_event(
            db,
            3,
            stale_ts + 2000,
            url="https://so.com/q/3",
            domain="so.com",
            dwell_ms=4000,
            scroll_depth=0.05,
            search_query="rust help",
        )

        chunks = claim_pending_browser_events(db, "test-worker", stale_secs=60)
        all_events = [e for chunk in chunks for e in chunk]
        assert len(all_events) == 2
        urls = [e["url"] for e in all_events]
        assert "https://so.com/q/2" in urls
        assert "https://so.com/q/3" in urls

        # Verify the skipped event's queue status and error message
        row = db.execute(
            "SELECT status, error_message FROM browser_enrichment_queue WHERE browser_event_id = 1"
        ).fetchone()
        assert row[0] == "skipped"
        assert row[1] is not None and len(row[1]) > 0, (
            "error_message must be set for skipped low-engagement events"
        )
        assert "scroll" in row[1], f"expected 'scroll' in error_message, got: {row[1]!r}"

    def test_long_dwell_bypasses_scroll_filter(self, db):
        """Events with low scroll but dwell >= long_dwell_bypass_ms are kept."""
        stale_ts = int(time.time() * 1000) - 120_000

        # Low scroll, no query, but 3-minute dwell — should be kept
        _insert_browser_event(
            db,
            1,
            stale_ts,
            url="https://github.com/sjcarpenter/hippo/pull/99",
            domain="github.com",
            dwell_ms=180_000,
            scroll_depth=0.05,
        )
        # Low scroll, no query, short dwell — should be skipped
        _insert_browser_event(
            db,
            2,
            stale_ts + 1000,
            url="https://github.com/sjcarpenter/hippo/issues/1",
            domain="github.com",
            dwell_ms=5000,
            scroll_depth=0.05,
        )

        chunks = claim_pending_browser_events(
            db, "test-worker", stale_secs=60, long_dwell_bypass_ms=120_000
        )
        all_events = [e for chunk in chunks for e in chunk]
        assert len(all_events) == 1
        assert all_events[0]["id"] == 1

        row = db.execute(
            "SELECT status, error_message FROM browser_enrichment_queue WHERE browser_event_id = 2"
        ).fetchone()
        assert row[0] == "skipped"
        assert row[1] is not None and "dwell=" in row[1], (
            f"error_message must contain 'dwell=' for low-dwell skips, got: {row[1]!r}"
        )

    def test_claim_splits_chunks_on_time_gap(self, db):
        """Events separated by >5 min gap should be in different chunks."""
        old_ts = int(time.time() * 1000) - 600_000  # 10 minutes ago
        _insert_browser_event(db, 1, old_ts)
        _insert_browser_event(db, 2, old_ts + 400_000)  # 6.7 min later — new chunk

        chunks = claim_pending_browser_events(db, "test-worker", stale_secs=60)
        assert len(chunks) == 2
        assert len(chunks[0]) == 1
        assert len(chunks[1]) == 1


class TestChunkByTimeGap:
    def test_single_chunk(self):
        events = [{"timestamp": 1000}, {"timestamp": 2000}, {"timestamp": 3000}]
        chunks = _chunk_by_time_gap(events, gap_ms=300_000)
        assert len(chunks) == 1
        assert len(chunks[0]) == 3

    def test_splits_on_gap(self):
        events = [{"timestamp": 1000}, {"timestamp": 2000}, {"timestamp": 500_000}]
        chunks = _chunk_by_time_gap(events, gap_ms=300_000)
        assert len(chunks) == 2
        assert len(chunks[0]) == 2
        assert len(chunks[1]) == 1

    def test_empty_list(self):
        chunks = _chunk_by_time_gap([], gap_ms=300_000)
        assert chunks == []


class TestBuildBrowserEnrichmentPrompt:
    def test_contains_domain_search_query_dwell(self):
        events = [
            {
                "url": "https://stackoverflow.com/questions/123",
                "title": "Rust Display trait implementation",
                "domain": "stackoverflow.com",
                "dwell_ms": 45000,
                "scroll_depth": 0.85,
                "search_query": "rust Display trait",
                "extracted_text": "Some content about Display trait...",
            }
        ]
        prompt = build_browser_enrichment_prompt(events)
        assert "stackoverflow.com" in prompt
        assert "rust Display trait" in prompt
        assert "45.0s" in prompt
        assert "85%" in prompt
        assert "Rust Display trait implementation" in prompt

    def test_truncates_long_content(self):
        events = [
            {
                "url": "https://example.com",
                "title": "Test",
                "domain": "example.com",
                "dwell_ms": 1000,
                "scroll_depth": None,
                "search_query": None,
                "extracted_text": "x" * 5000,
            }
        ]
        prompt = build_browser_enrichment_prompt(events)
        # The content excerpt should be truncated to 2000 chars
        assert len(prompt) < 5000


class TestGetCorrelatedBrowserEvents:
    def test_returns_nearby_events(self, db):
        """Events within window are returned, far events are not."""
        session_start = 1_000_000
        session_end = 2_000_000
        window_ms = 300_000

        # Nearby event (within window before session start)
        _insert_browser_event(
            db, 1, session_start - 100_000, url="https://nearby.com", title="Nearby"
        )
        # Nearby event (within session)
        _insert_browser_event(db, 2, 1_500_000, url="https://during.com", title="During")
        # Far event (way before)
        _insert_browser_event(db, 3, session_start - 1_000_000, url="https://far.com", title="Far")
        # Far event (way after)
        _insert_browser_event(
            db, 4, session_end + 1_000_000, url="https://future.com", title="Future"
        )

        events = get_correlated_browser_events(db, session_start, session_end, window_ms)
        urls = [e["url"] for e in events]
        assert "https://nearby.com" in urls
        assert "https://during.com" in urls
        assert "https://far.com" not in urls
        assert "https://future.com" not in urls

    def test_empty_when_no_events(self, db):
        events = get_correlated_browser_events(db, 1_000_000, 2_000_000)
        assert events == []


class TestFormatBrowserContextForShellPrompt:
    def test_formats_context(self):
        events = [
            {
                "domain": "stackoverflow.com",
                "title": "Rust Display trait implementation",
                "dwell_ms": 45000,
                "scroll_depth": 0.85,
                "search_query": "rust Display trait",
            },
            {
                "domain": "docs.rs",
                "title": "std::fmt - Rust",
                "dwell_ms": 20000,
                "scroll_depth": 0.5,
                "search_query": None,
            },
        ]
        text = format_browser_context_for_shell_prompt(events)
        assert "Browser Activity (concurrent):" in text
        assert 'stackoverflow.com - "Rust Display trait implementation"' in text
        assert "45.0s" in text
        assert "85% scroll" in text
        assert 'Search query: "rust Display trait"' in text
        assert 'docs.rs - "std::fmt - Rust"' in text

    def test_empty_events(self):
        text = format_browser_context_for_shell_prompt([])
        assert text == ""


class TestWriteBrowserKnowledgeNode:
    def test_write_and_verify(self, db):
        """Insert event, write knowledge node, verify junction and queue status."""
        old_ts = int(time.time() * 1000) - 120_000
        _insert_browser_event(
            db, 1, old_ts, url="https://docs.rs/serde", title="serde docs", domain="docs.rs"
        )

        result = EnrichmentResult(
            summary="Researched serde serialization patterns",
            intent="research",
            outcome="success",
            entities={
                "projects": ["hippo"],
                "tools": ["serde", "rust"],
                "files": [],
                "services": ["docs.rs"],
                "errors": [],
            },
            tags=["rust", "serialization", "serde"],
            embed_text="Researched serde derive macros and custom serialization for Rust structs",
            key_decisions=["Use serde derive instead of manual impl"],
            problems_encountered=[],
        )

        node_id = write_browser_knowledge_node(db, result, [1], "test-model")
        assert node_id > 0

        # Verify knowledge node exists
        row = db.execute(
            "SELECT embed_text FROM knowledge_nodes WHERE id = ?", (node_id,)
        ).fetchone()
        assert row[0] == "Researched serde derive macros and custom serialization for Rust structs"

        # Verify junction table link
        link = db.execute(
            "SELECT browser_event_id FROM knowledge_node_browser_events WHERE knowledge_node_id = ?",
            (node_id,),
        ).fetchone()
        assert link[0] == 1

        # Verify browser event marked enriched
        enriched = db.execute("SELECT enriched FROM browser_events WHERE id = 1").fetchone()[0]
        assert enriched == 1

        # Verify queue marked done
        status = db.execute(
            "SELECT status FROM browser_enrichment_queue WHERE browser_event_id = 1"
        ).fetchone()[0]
        assert status == "done"

        # Verify entities created
        entity = db.execute(
            "SELECT name FROM entities WHERE type = 'project' AND canonical = 'hippo'"
        ).fetchone()
        assert entity is not None

    def test_domain_entities_created(self, db):
        """Domains in enrichment result are stored as entity type 'domain'."""
        old_ts = int(time.time() * 1000) - 120_000
        _insert_browser_event(
            db,
            1,
            old_ts,
            url="https://docs.rs/serde",
            domain="docs.rs",
        )

        result = EnrichmentResult(
            summary="Browsed docs.rs and stackoverflow",
            intent="research",
            outcome="success",
            entities={
                "projects": [],
                "tools": [],
                "files": [],
                "services": [],
                "errors": [],
                "domains": ["docs.rs", "stackoverflow.com"],
            },
            tags=["rust"],
            embed_text="Visited docs.rs and stackoverflow for Rust research",
        )

        node_id = write_browser_knowledge_node(db, result, [1], "test-model")
        assert node_id > 0

        # Verify domain entities created with correct type
        domains = db.execute(
            "SELECT name FROM entities WHERE type = 'domain' ORDER BY name"
        ).fetchall()
        assert len(domains) == 2
        domain_names = [d[0] for d in domains]
        assert "docs.rs" in domain_names
        assert "stackoverflow.com" in domain_names

        # Verify linked to knowledge node
        linked = db.execute(
            "SELECT COUNT(*) FROM knowledge_node_entities WHERE knowledge_node_id = ?",
            (node_id,),
        ).fetchone()[0]
        assert linked == 2

    def test_rollback_on_failure(self, db):
        """Verify transaction rolls back on error, leaving no partial state."""
        old_ts = int(time.time() * 1000) - 120_000
        _insert_browser_event(db, 1, old_ts)

        result = EnrichmentResult(
            summary="Test",
            intent="research",
            outcome="success",
            entities={"projects": [], "tools": [], "files": [], "services": [], "errors": []},
            tags=[],
            embed_text="test embed",
        )

        # Attempt to write with a nonexistent event_id to trigger foreign key error
        with pytest.raises(Exception):
            write_browser_knowledge_node(db, result, [1, 999], "test-model")

        # No partial state
        assert db.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM knowledge_node_browser_events").fetchone()[0] == 0


class TestMarkBrowserQueueFailed:
    def test_retry_then_fail(self, db):
        old_ts = int(time.time() * 1000) - 120_000
        _insert_browser_event(db, 1, old_ts)

        # Override max_retries to 2 for faster test
        db.execute("UPDATE browser_enrichment_queue SET max_retries = 2 WHERE browser_event_id = 1")
        db.commit()

        # First failure — stays pending
        mark_browser_queue_failed(db, [1], "timeout")
        row = db.execute(
            "SELECT status, retry_count, error_message FROM browser_enrichment_queue WHERE browser_event_id = 1"
        ).fetchone()
        assert row[0] == "pending"
        assert row[1] == 1
        assert row[2] == "timeout"

        # Second failure — becomes failed (retry_count >= max_retries)
        mark_browser_queue_failed(db, [1], "timeout again")
        row = db.execute(
            "SELECT status, retry_count FROM browser_enrichment_queue WHERE browser_event_id = 1"
        ).fetchone()
        assert row[0] == "failed"
        assert row[1] == 2


class TestBrowserDwellFilter:
    def test_short_dwell_events_are_skipped(self, db):
        stale_ts = int(time.time() * 1000) - 120_000
        _insert_browser_event(
            db,
            1,
            stale_ts,
            url="https://example.com/blip",
            dwell_ms=500,
            scroll_depth=0.9,
        )
        _insert_browser_event(
            db,
            2,
            stale_ts + 1000,
            url="https://example.com/read",
            dwell_ms=4000,
            scroll_depth=0.9,
        )

        chunks = claim_pending_browser_events(db, "test-worker", stale_secs=60)
        all_events = [e for chunk in chunks for e in chunk]
        assert [e["id"] for e in all_events] == [2]

        row = db.execute(
            "SELECT status, error_message FROM browser_enrichment_queue WHERE browser_event_id = 1"
        ).fetchone()
        assert row[0] == "skipped"
        assert "dwell_ms=500" in row[1]
