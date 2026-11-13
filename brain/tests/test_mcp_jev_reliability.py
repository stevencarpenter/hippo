"""MCP rerank reliability: decision deadlines under load and the Jev key path."""

import asyncio
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import hippo_brain.mcp as mcp_module
from hippo_brain import rag, retrieval


@pytest.fixture(autouse=True)
def _restore_tuning():
    yield
    retrieval.configure({})


@pytest.fixture(autouse=True)
def dispatched(monkeypatch):
    """Record every Jev HTTP dispatch and keep these tests off the network."""
    sent = []

    async def post(self, url, **kwargs):
        sent.append(kwargs["headers"]["Authorization"])
        raise httpx.ConnectError("no network in tests")

    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    return sent


def _results(count=3):
    return [
        retrieval.SearchResult(f"node-{i}", 0.8, f"summary {i}", "detail", None, [], "", "", 1)
        for i in range(count)
    ]


class QuickJev:
    """Answers every question with full relevance after a short awaited delay."""

    model = "1.13.0"

    def __init__(self, delay=0.02):
        self.delay = delay
        self.calls = 0

    async def assess(self, state, questions, *, timeout_seconds=None):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return {
            "model": "1.13.0",
            "answers": {
                key: {"type": "noul", "noul": 1.0}
                if question["type"] == "noul"
                else {
                    "type": "score",
                    "score": 3,
                    "confidence": 0.9,
                    "probabilities": {"0": 0.0, "1": 0.0, "2": 0.0, "3": 1.0},
                }
                for key, question in questions.items()
            },
            "usage": {"input_tokens": 10, "output_tokens": 1},
        }

    async def aclose(self):
        pass


def _blocking_search(seconds):
    def search(*_args, **_kwargs):
        time.sleep(seconds)  # stands in for synchronous SQLite work on the event loop
        return _results()

    return search


async def test_sync_work_in_other_tool_calls_cannot_starve_a_rerank_deadline(monkeypatch):
    retrieval.configure({"rerank": True, "rerank_backend": "jev", "decision_deadline_ms": 150})
    monkeypatch.setattr(retrieval, "search", _blocking_search(0.2))
    jev = QuickJev()
    monkeypatch.setattr(mcp_module._state, "jev_client", jev)

    outcomes = await asyncio.gather(
        *(
            mcp_module._search_context(
                None, "q", None, retrieval.Filters(), mode="lexical", limit=3
            )
            for _ in range(4)
        )
    )

    assert [diagnostics["fallback_reason"] for _, diagnostics in outcomes] == [None] * 4
    assert jev.calls == 4


async def test_ask_rerank_deadline_survives_blocking_sibling_calls(tmp_db, monkeypatch):
    _, path = tmp_db
    retrieval.configure({"rerank": True, "rerank_backend": "jev", "decision_deadline_ms": 150})
    monkeypatch.setattr(rag, "retrieval_search", _blocking_search(0.2))
    decisions = []
    monkeypatch.setattr(rag, "capture_decision", lambda trace, **_: decisions.append(trace))
    inference = AsyncMock()
    inference.embed.return_value = [[0.0] * 768]
    inference.chat.return_value = "answer"
    for name, value in (
        ("db_path", str(path)),
        ("inference_client", inference),
        ("vector_table", object()),
        ("query_model", "query"),
        ("embedding_model", "embed"),
        ("classification_enabled", False),
        ("jev_client", QuickJev()),
    ):
        monkeypatch.setattr(mcp_module._state, name, value)

    await asyncio.gather(*(mcp_module.ask(question="q", limit=3) for _ in range(3)))

    assert [trace["fallback_reason"] for trace in decisions] == [None] * 3


def _start_mcp_with_failing_key_lookup(tmp_path, monkeypatch, lookups):
    """Initialise MCP state with a configured reference whose lookup times out."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("TYPESAFE_API_KEY", "stale-inherited-key")
    config = tmp_path / ".config/hippo/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(
        '[retrieval]\nrerank=true\nrerank_backend="jev"\n'
        '[brain]\ntypesafe_api_key_op_ref="op://vault/item/field"\n'
    )
    monkeypatch.setattr(mcp_module.shutil, "which", lambda _: "/usr/local/bin/op")

    def read_key(command, **_kwargs):
        lookups.append(command)
        if len(lookups) == 1:
            raise subprocess.TimeoutExpired(command, 10)
        return subprocess.CompletedProcess(command, 0, "hippo-key\n", "")

    monkeypatch.setattr(mcp_module.subprocess, "run", read_key)
    monkeypatch.setattr(mcp_module, "_state", SimpleNamespace())

    def no_vector_db(_):
        raise OSError("vector store not needed")

    monkeypatch.setattr(mcp_module, "open_vector_db", no_vector_db)
    mcp_module._init_state()


async def test_failed_key_lookup_never_dispatches_the_inherited_key(
    tmp_path, tmp_db, monkeypatch, dispatched
):
    _, path = tmp_db
    lookups = []
    _start_mcp_with_failing_key_lookup(tmp_path, monkeypatch, lookups)
    monkeypatch.setattr(mcp_module._state, "jev_retry_at", float("inf"), raising=False)
    monkeypatch.setattr(retrieval, "search", lambda *a, **k: _results())
    monkeypatch.setattr(rag, "retrieval_search", lambda *a, **k: _results())
    decisions = []
    monkeypatch.setattr(rag, "capture_decision", lambda trace, **_: decisions.append(trace))

    _, diagnostics = await mcp_module._search_context(
        None, "q", None, retrieval.Filters(), mode="lexical", limit=3
    )

    inference = AsyncMock()
    inference.embed.return_value = [[0.0] * 768]
    inference.chat.return_value = "answer"
    mcp_module._state.db_path = str(path)
    mcp_module._state.inference_client = inference
    mcp_module._state.vector_table = object()
    mcp_module._state.query_model = "query"
    await mcp_module.ask(question="q", limit=3)

    assert dispatched == []
    assert diagnostics["fallback_reason"] == "JevUnavailable"
    assert (diagnostics["fallback_cause"], diagnostics["effect"]) == ("no_credentials", None)
    assert [trace["fallback_reason"] for trace in decisions] == ["JevUnavailable"]


async def test_failed_key_lookup_is_retried_without_blocking_the_decision(
    tmp_path, monkeypatch, dispatched
):
    lookups = []
    _start_mcp_with_failing_key_lookup(tmp_path, monkeypatch, lookups)
    assert mcp_module._state.jev_client is None
    monkeypatch.setattr(retrieval, "search", lambda *a, **k: _results())
    mcp_module._state.jev_retry_at = 0.0

    _, first = await mcp_module._search_context(
        None, "q", None, retrieval.Filters(), mode="lexical", limit=3
    )
    for _ in range(200):
        if mcp_module._state.jev_client is not None:
            break
        await asyncio.sleep(0.005)

    assert first["fallback_reason"] == "JevUnavailable" and dispatched == []
    assert len(lookups) == 2
    assert mcp_module._state.jev_client._api_key == "hippo-key"
    await mcp_module._close_jev_client()


async def test_adaptive_rerank_reads_the_retrieval_connection_from_the_decision_loop(
    tmp_db, monkeypatch
):
    _, path = tmp_db
    # Only multi-v1 refines, and refinement is what reads the connection.
    retrieval.configure(
        {
            "rerank": True,
            "rerank_backend": "jev",
            "rerank_adaptive": True,
            "rerank_recipe": "multi-v1",
        }
    )
    monkeypatch.setattr(retrieval, "search", lambda *a, **k: _results())
    monkeypatch.setattr(mcp_module._state, "db_path", str(path))
    monkeypatch.setattr(mcp_module._state, "jev_client", QuickJev())
    conn = mcp_module._open_retrieval_conn()
    try:
        _, diagnostics = await mcp_module._search_context(
            conn, "q", None, retrieval.Filters(), mode="lexical", limit=3
        )
    finally:
        conn.close()

    assert diagnostics["fallback_reason"] is None


async def test_hits_carry_jev_relevance_only_when_jev_ranked_them(tmp_db, monkeypatch):
    _, path = tmp_db
    retrieval.configure({"rerank": True, "rerank_backend": "jev"})
    monkeypatch.setattr(retrieval, "search", lambda *a, **k: _results())
    monkeypatch.setattr(mcp_module._state, "db_path", str(path))
    monkeypatch.setattr(mcp_module._state, "inference_client", None)
    query = dict(query="q", mode="lexical", limit=3, project="", since="", source="", branch="")

    monkeypatch.setattr(mcp_module._state, "jev_client", QuickJev())
    ranked = await mcp_module._retrieve_filtered(**query)

    class Failing(QuickJev):
        async def assess(self, state, questions, *, timeout_seconds=None):
            raise TimeoutError

    monkeypatch.setattr(mcp_module._state, "jev_client", Failing())
    fallback = await mcp_module._retrieve_filtered(**query)

    assert [hit["relevance"] for hit in ranked] == [1.0, 1.0, 1.0]
    assert all("relevance" not in hit for hit in fallback)
