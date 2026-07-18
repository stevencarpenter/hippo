from __future__ import annotations

import sqlite3

import pytest

import hippo_brain.enrichment_sources as es
from hippo_brain.server import BrainServer


@pytest.fixture
def server(tmp_db, tmp_path):
    conn, db_path = tmp_db
    srv = BrainServer(
        db_path=str(db_path),
        data_dir=str(tmp_path),
        enrichment_model="mock-model",
        embedding_model="text-embedding-mock",
    )
    try:
        yield conn, srv
    finally:
        if srv._vector_db is not None:
            srv._vector_db.close()


def test_claim_all_sources_propagates_shell_structural_error(server, monkeypatch):
    """A structural failure claiming the PRIMARY shell source must propagate (AP-11).

    The enrichment loop relies on the exception to record last_error and back off;
    swallowing it to [] silently stops enriching shell while health stays green.
    """
    conn, srv = server

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated shell schema drift")

    monkeypatch.setattr(es, "claim_pending_events_by_session", _boom)

    with pytest.raises(sqlite3.OperationalError, match="schema drift"):
        es.claim_all_sources(conn, srv)


def test_claim_all_sources_swallows_non_shell_claim_error(server, monkeypatch):
    """Non-shell sources keep their deliberate swallow-and-continue behavior."""
    conn, srv = server

    def _boom(*_args, **_kwargs):
        raise sqlite3.OperationalError("simulated browser claim failure")

    monkeypatch.setattr(es, "claim_pending_browser_events", _boom)

    claims = es.claim_all_sources(conn, srv)  # must not raise

    assert claims["browser"] == []
    # Other sources still produce (empty) claim lists rather than being skipped.
    assert "shell" in claims and "claude-auto-memory" in claims
