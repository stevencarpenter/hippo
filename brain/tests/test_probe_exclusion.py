"""Integration tests for probe_tag exclusion filters (AP-6: probe contamination).

Verifies that every user-facing query on the three event tables
(events / claude_sessions / browser_events) respects the
  AND probe_tag IS NULL
belt-and-braces guard so synthetic probe rows never leak into
production result sets, enrichment queues, or project listings.

Reference: docs/capture/anti-patterns.md AP-6.
"""

import sqlite3
import time

import pytest

from hippo_brain.browser_enrichment import (
    claim_pending_browser_events,
    get_correlated_browser_events,
)
from hippo_brain.claude_sessions import claim_pending_claude_segments
from hippo_brain.enrichment import claim_pending_events_by_session
from hippo_brain.mcp_queries import (
    _search_browser_events,
    _search_claude_events,
    _search_shell_events,
    list_projects_impl,
    search_events_impl,
)
from tests.conftest import SCHEMA_PATH


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db():
    """In-memory SQLite with full hippo schema."""
    schema = SCHEMA_PATH.read_text()
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(schema)
    conn.commit()
    yield conn
    conn.close()


# ---------------------------------------------------------------------------
# Helpers — insert probe and real rows
# ---------------------------------------------------------------------------

_NOW = int(time.time() * 1000)
_OLD = _NOW - 300_000  # 5 minutes ago (past the stale_secs threshold)


def _insert_session(conn, session_id: int, ts: int = _OLD) -> None:
    conn.execute(
        "INSERT INTO sessions (id, start_time, shell, hostname, username) "
        "VALUES (?, ?, 'zsh', 'host', 'u')",
        (session_id, ts),
    )


def _insert_shell_event(
    conn,
    event_id: int,
    session_id: int,
    command: str,
    cwd: str,
    probe_tag: str | None = None,
    ts: int = _OLD,
) -> None:
    conn.execute(
        """INSERT INTO events (id, session_id, timestamp, command, exit_code,
                               duration_ms, cwd, hostname, shell, probe_tag)
           VALUES (?, ?, ?, ?, 0, 500, ?, 'host', 'zsh', ?)""",
        (event_id, session_id, ts, command, cwd, probe_tag),
    )
    conn.execute("INSERT INTO enrichment_queue (event_id) VALUES (?)", (event_id,))


def _insert_browser_event(
    conn,
    event_id: int,
    url: str,
    domain: str,
    probe_tag: str | None = None,
    ts: int = _OLD,
    dwell_ms: int = 10_000,
    scroll_depth: float = 0.5,
) -> None:
    conn.execute(
        """INSERT INTO browser_events
               (id, timestamp, url, title, domain, dwell_ms, scroll_depth, probe_tag)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (event_id, ts, url, "Title", domain, dwell_ms, scroll_depth, probe_tag),
    )
    conn.execute(
        "INSERT INTO browser_enrichment_queue (browser_event_id) VALUES (?)",
        (event_id,),
    )


def _insert_claude_session(
    conn,
    seg_id: int,
    cwd: str,
    probe_tag: str | None = None,
    ts: int = _OLD,
    message_count: int = 10,
) -> None:
    conn.execute(
        """INSERT INTO claude_sessions
               (id, session_id, project_dir, cwd, segment_index, start_time,
                end_time, summary_text, tool_calls_json, user_prompts_json,
                message_count, source_file, created_at, probe_tag)
           VALUES (?, ?, ?, ?, 0, ?, ?, ?, '[]', '[]', ?, '/tmp/s.jsonl', ?, ?)""",
        (
            seg_id,
            f"sess-{seg_id}",
            cwd,
            cwd,
            ts,
            ts + 3_600_000,
            f"Worked on {cwd}",
            message_count,
            ts,
            probe_tag,
        ),
    )
    conn.execute(
        "INSERT INTO claude_enrichment_queue (claude_session_id, created_at) VALUES (?, ?)",
        (seg_id, ts),
    )


# ---------------------------------------------------------------------------
# Tests — enrichment queue claim functions
# ---------------------------------------------------------------------------


class TestShellEnrichmentExcludesProbes:
    """claim_pending_events_by_session must skip probe-tagged shell events."""

    def test_probe_event_not_claimed(self, db):
        _insert_session(db, session_id=1)
        _insert_session(db, session_id=2)

        # Probe event in session 1
        _insert_shell_event(db, 1, 1, "__hippo_probe__", "/probe", probe_tag="probe-v1")
        # Real event in session 2
        _insert_shell_event(db, 2, 2, "cargo test", "/project", probe_tag=None)
        db.commit()

        chunks = claim_pending_events_by_session(
            db, max_per_chunk=10, worker_id="test", stale_secs=1
        )

        claimed_commands = [e["command"] for chunk in chunks for e in chunk]
        assert "__hippo_probe__" not in claimed_commands, "probe event must not be claimed"
        assert "cargo test" in claimed_commands, "real event must be claimed"

    def test_all_probe_session_yields_no_chunks(self, db):
        _insert_session(db, session_id=1)
        for i in range(3):
            _insert_shell_event(db, i + 1, 1, f"__probe_{i}__", "/probe", probe_tag="probe-v1")
        db.commit()

        chunks = claim_pending_events_by_session(
            db, max_per_chunk=10, worker_id="test", stale_secs=1
        )
        assert chunks == [], "all-probe session must yield no chunks"


class TestBrowserEnrichmentExcludesProbes:
    """claim_pending_browser_events must skip probe-tagged browser events."""

    def test_probe_browser_event_not_claimed(self, db):
        _insert_browser_event(
            db, 1, "https://probe.hippo.local/probe", "probe.hippo.local", probe_tag="probe-v1"
        )
        _insert_browser_event(db, 2, "https://docs.rs/tokio", "docs.rs", probe_tag=None)
        db.commit()

        chunks = claim_pending_browser_events(db, worker_id="test", stale_secs=1)

        claimed_urls = [e["url"] for chunk in chunks for e in chunk]
        assert "https://probe.hippo.local/probe" not in claimed_urls, (
            "probe browser event must not be claimed"
        )
        assert "https://docs.rs/tokio" in claimed_urls, "real browser event must be claimed"

    def test_all_probe_browser_yields_no_chunks(self, db):
        for i in range(3):
            _insert_browser_event(
                db,
                i + 1,
                f"https://probe.hippo.local/{i}",
                "probe.hippo.local",
                probe_tag="probe-v1",
            )
        db.commit()

        chunks = claim_pending_browser_events(db, worker_id="test", stale_secs=1)
        assert chunks == [], "all-probe browser batch must yield no chunks"


class TestClaudeEnrichmentExcludesProbes:
    """claim_pending_claude_segments must skip probe-tagged claude sessions."""

    def test_probe_claude_session_not_claimed(self, db):
        _insert_claude_session(db, 1, "/probe-cwd", probe_tag="probe-session-v1")
        _insert_claude_session(db, 2, "/real-project", probe_tag=None, message_count=8)
        db.commit()

        batches = claim_pending_claude_segments(db, "test-worker")

        claimed_cwds = [s["cwd"] for batch in batches for s in batch]
        assert "/probe-cwd" not in claimed_cwds, "probe claude session must not be claimed"
        assert "/real-project" in claimed_cwds, "real claude session must be claimed"

    def test_all_probe_claude_yields_no_batches(self, db):
        for i in range(3):
            _insert_claude_session(
                db, i + 1, f"/probe-{i}", probe_tag="probe-session-v1", message_count=5
            )
        db.commit()

        batches = claim_pending_claude_segments(db, "test-worker")
        assert batches == [], "all-probe claude sessions must yield no batches"


# ---------------------------------------------------------------------------
# Tests — correlated browser events
# ---------------------------------------------------------------------------


class TestGetCorrelatedBrowserEventsExcludesProbes:
    """get_correlated_browser_events must not return probe-tagged events."""

    def test_probe_browser_excluded_from_correlation(self, db):
        mid = _NOW - 30_000  # within the default ±5 min window
        _insert_browser_event(
            db,
            1,
            "https://probe.hippo.local/x",
            "probe.hippo.local",
            probe_tag="p",
            ts=mid,
            dwell_ms=50,
        )
        _insert_browser_event(
            db, 2, "https://docs.rs/anyhow", "docs.rs", probe_tag=None, ts=mid, dwell_ms=8_000
        )
        db.commit()

        events = get_correlated_browser_events(db, _NOW - 60_000, _NOW)
        urls = [e["url"] for e in events]
        assert "https://probe.hippo.local/x" not in urls, (
            "probe browser events must not appear in shell correlation context"
        )
        assert "https://docs.rs/anyhow" in urls


# ---------------------------------------------------------------------------
# Tests — MCP / user-facing query functions
# ---------------------------------------------------------------------------


class TestMcpSearchExcludesProbes:
    """MCP search functions must not surface probe rows."""

    def test_search_shell_events_excludes_probes(self, db):
        _insert_session(db, 1)
        _insert_shell_event(db, 1, 1, "__hippo_probe__", "/probe", probe_tag="probe-v1")
        _insert_shell_event(db, 2, 1, "cargo test", "/real", probe_tag=None)
        db.commit()

        results = _search_shell_events(db, query="", since_ms=0, project="", branch="", limit=50)
        commands = [r["summary"] for r in results]
        assert "__hippo_probe__" not in commands
        assert "cargo test" in commands

    def test_search_claude_events_excludes_probes(self, db):
        _insert_claude_session(db, 1, "/probe-dir", probe_tag="probe-v1", message_count=5)
        _insert_claude_session(db, 2, "/real-project", probe_tag=None, message_count=7)
        db.commit()

        results = _search_claude_events(db, query="", since_ms=0, project="", branch="", limit=50)
        cwds = [r["cwd"] for r in results]
        assert "/probe-dir" not in cwds
        assert "/real-project" in cwds

    def test_search_browser_events_excludes_probes(self, db):
        _insert_browser_event(
            db, 1, "https://probe.hippo.local/x", "probe.hippo.local", probe_tag="probe-v1"
        )
        _insert_browser_event(db, 2, "https://docs.rs/x", "docs.rs", probe_tag=None)
        db.commit()

        results = _search_browser_events(db, query="", since_ms=0, limit=50)
        # summary format is "{domain} — {title}"
        summaries = [r["summary"] for r in results]
        assert not any("probe.hippo.local" in s for s in summaries), (
            "probe browser events must not appear in search results"
        )
        assert any("docs.rs" in s for s in summaries), "real browser event must appear"

    def test_search_events_combined_excludes_probes(self, db):
        """search_events_impl aggregates all three sources — all must be filtered."""
        _insert_session(db, 1)
        _insert_shell_event(db, 1, 1, "__hippo_probe__", "/probe", probe_tag="probe-v1")
        _insert_shell_event(db, 2, 1, "cargo build", "/real", probe_tag=None)
        _insert_browser_event(
            db, 1, "https://probe.hippo.local/p", "probe.hippo.local", probe_tag="probe-v1"
        )
        _insert_browser_event(db, 2, "https://docs.rs/p", "docs.rs", probe_tag=None)
        db.commit()

        # search_events_impl returns list[dict] directly
        results = search_events_impl(db, query="")
        all_summaries = [r["summary"] for r in results]
        assert not any("__hippo_probe__" in s for s in all_summaries)
        assert not any("probe.hippo.local" in s for s in all_summaries)


class TestListProjectsExcludesProbes:
    """list_projects_impl must not surface directories from probe events."""

    def test_probe_project_dirs_excluded(self, db):
        _insert_session(db, 1)
        _insert_session(db, 2)

        # Probe event in a unique probe-only directory
        _insert_shell_event(db, 1, 1, "__hippo_probe__", "/probe-only-dir", probe_tag="probe-v1")
        # Real event in a distinct real directory
        _insert_shell_event(db, 2, 2, "cargo test", "/my-real-project", probe_tag=None)

        # Probe claude session
        _insert_claude_session(db, 1, "/probe-claude-dir", probe_tag="probe-session-v1")
        # Real claude session
        _insert_claude_session(db, 2, "/real-claude-dir", probe_tag=None, message_count=5)
        db.commit()

        projects = list_projects_impl(db)
        cwds = [p["cwd_root"] for p in projects]

        assert "/probe-only-dir" not in cwds, "probe shell event cwd must not appear in projects"
        assert "/probe-claude-dir" not in cwds, (
            "probe claude session dir must not appear in projects"
        )
        assert "/my-real-project" in cwds, "real shell event project must appear"
        assert "/real-claude-dir" in cwds, "real claude project dir must appear"
