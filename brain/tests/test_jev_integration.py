"""Runtime wiring, configuration parity, and background worker ownership."""

import asyncio
import sqlite3
from unittest.mock import AsyncMock

import pytest

from hippo_brain import _load_runtime_settings, classification, rag, retrieval
from hippo_brain import mcp as mcp_module
from hippo_brain.server import BrainServer


@pytest.fixture(autouse=True)
def reset_settings():
    old = retrieval.get_tuning()
    yield
    retrieval._active_tuning = old
    classification.configure(False)


@pytest.mark.asyncio
async def test_rag_dispatch_preserves_inputs_and_capture_when_reranking_disabled(monkeypatch):
    client = AsyncMock()
    client.embed.return_value = [[0.0] * 768]
    client.chat.return_value = "answer"
    result = retrieval.SearchResult("node", 0.8, "summary", "detail", None, [], "", "", 1)
    monkeypatch.setattr(rag, "retrieval_search", lambda *a, **k: [result])
    captured = []
    monkeypatch.setattr(rag, "capture_query", lambda *a, **k: captured.append((a, k)))
    reranker = AsyncMock(return_value=[result])
    monkeypatch.setattr(rag, "rerank_results", reranker)
    with sqlite3.connect(":memory:") as conn:
        retrieval.configure({})
        await rag.ask("q", client, conn, "local", "embedding", skip_preflight=True)
        assert len(captured) == 1 and captured[0][0][1] == [result]
        reranker.assert_not_awaited()
        jev = object()
        retrieval.configure({"rerank": True, "rerank_backend": "jev", "rerank_adaptive": True})
        await rag.ask("q", client, conn, "local", "embedding", skip_preflight=True, jev_client=jev)
        args = reranker.await_args
        assert args.args[3] == [result]
        assert args.kwargs["jev_client"] is jev and args.kwargs["conn"] is conn
        assert args.kwargs["backend"] == "jev" and args.kwargs["adaptive"] is True


def test_http_and_mcp_read_identical_query_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = tmp_path / ".config/hippo/config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(
        '[retrieval]\nrerank=true\nrerank_backend="jev"\nrerank_recipe="single-v1"\ndecision_deadline_ms=700\n[classification]\nenabled=true\n'
    )
    http = _load_runtime_settings()
    mcp = mcp_module._load_config()
    assert http["retrieval"] == mcp["retrieval"]
    assert http["classification"]["enabled"] is True
    assert (
        http["classification"] == mcp["classification"]
    )  # MCP reads the recipe, starts no worker.


@pytest.mark.asyncio
async def test_wide_query_keeps_tail_but_only_ranks_thirty(monkeypatch):
    from dataclasses import replace

    client = AsyncMock()
    client.embed.return_value = [[0.0] * 768]
    client.chat.return_value = "answer"
    base = retrieval.SearchResult("node", 0.8, "summary", "detail", None, [], "", "", 1)
    results = [replace(base, uuid=f"node-{i}") for i in range(50)]
    monkeypatch.setattr(rag, "retrieval_search", lambda *a, **k: results)
    reranker = AsyncMock(return_value=list(reversed(results[:30])))
    monkeypatch.setattr(rag, "rerank_results", reranker)
    retrieval.configure({"rerank": True, "rerank_backend": "jev"})
    diagnostics = {}
    with sqlite3.connect(":memory:") as conn:
        response = await rag.ask(
            "q",
            client,
            conn,
            "local",
            "embedding",
            limit=50,
            skip_preflight=True,
            decision_diagnostics=diagnostics,
        )
    assert reranker.await_args.args[3] == results[:30]
    assert reranker.await_args.args[4] == 30
    assert diagnostics["unranked_tail_count"] == 20
    assert [row["uuid"] for row in response["sources"]] == [
        row.uuid for row in list(reversed(results[:30])) + results[30:]
    ]


@pytest.mark.asyncio
async def test_classification_pause_query_priority_and_shutdown(tmp_db):
    _, path = tmp_db
    server = BrainServer(db_path=str(path), poll_interval_secs=0)
    worker = AsyncMock()
    worker.process_batch.return_value = {"claimed": 0}
    server._classification_worker = worker
    server._paused = True
    task = asyncio.create_task(server._classification_loop())
    server._classification_task = task
    await asyncio.sleep(0.02)
    worker.process_batch.assert_not_awaited()
    server._paused = False
    server._query_inflight = 1
    await asyncio.sleep(0.11)
    worker.process_batch.assert_not_awaited()
    server._query_inflight = 0
    await asyncio.sleep(0.11)
    worker.process_batch.assert_awaited()
    jev = AsyncMock()
    server.jev_client = jev
    await server.stop_enrichment()
    assert not server.classification_status["running"] and task.done()
    jev.aclose.assert_awaited_once()
    server.close()


@pytest.mark.asyncio
async def test_mcp_lifespan_closes_only_its_decision_client(monkeypatch):
    client = AsyncMock()
    monkeypatch.setattr(mcp_module._state, "jev_client", client)
    async with mcp_module._mcp_lifespan(None):
        assert mcp_module._state.jev_client is client
    client.aclose.assert_awaited_once()
    assert mcp_module._state.jev_client is None


@pytest.mark.asyncio
async def test_failed_worker_does_not_skip_shutdown_cleanup(tmp_db):
    _, path = tmp_db
    server = BrainServer(db_path=str(path))

    async def failed():
        raise RuntimeError("worker failed")

    server._enrichment_task = asyncio.create_task(failed())
    server._classification_task = asyncio.create_task(asyncio.sleep(60))
    pending = server._classification_task
    jev = AsyncMock()
    server.jev_client = jev
    await asyncio.sleep(0)
    with pytest.raises(RuntimeError, match="worker failed"):
        await server.stop_enrichment()
    assert pending.done() and server._enrichment_task is None and server.jev_client is None
    jev.aclose.assert_awaited_once()
    server.close()


@pytest.mark.asyncio
async def test_http_probe_origin_is_explicit_not_inferred_from_output_cap(tmp_db, monkeypatch):
    from hippo_brain import server as server_module

    _, path = tmp_db
    server = BrainServer(db_path=str(path), embedding_model="embed", query_model="query")
    server._vector_table = object()
    ask = AsyncMock(return_value={"answer": "ok", "sources": []})
    monkeypatch.setattr(server_module, "rag_ask", ask)
    request = AsyncMock()
    request.json.return_value = {"question": "q", "max_tokens": 200}
    for headers, origin in (({}, "http"), ({"x-hippo-query-origin": "probe"}, "probe")):
        request.headers = headers
        await server.ask(request)
        assert ask.await_args.kwargs["capture_origin"] == origin
    server.close()


@pytest.mark.asyncio
async def test_mcp_retrieval_captures_filters_and_exposes_topics(tmp_db, monkeypatch):
    _, path = tmp_db
    monkeypatch.setattr(mcp_module._state, "db_path", str(path))
    monkeypatch.setattr(mcp_module._state, "inference_client", None)
    result = retrieval.SearchResult("node", 0.8, "summary", "detail", None, [], "", "", 1)
    result.controlled_topics = {"database-storage": 0.95}
    monkeypatch.setattr(retrieval, "search", lambda *a, **kw: [result])
    captures = []
    monkeypatch.setattr(mcp_module, "capture_query", lambda *a, **kw: captures.append((a, kw)))
    rows = await mcp_module._retrieve_filtered(
        query="database",
        mode="lexical",
        limit=5,
        project="hippo",
        since="",
        source="shell",
        branch="jev",
    )
    assert rows[0]["controlled_topics"] == {"database-storage": 0.95}
    assert captures[0][1]["origin"] == "mcp"
    assert captures[0][1]["filters"].project == "hippo"


@pytest.mark.asyncio
async def test_missing_classification_schema_does_not_prevent_service_start(tmp_path, monkeypatch):
    path = tmp_path / "old.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=24")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    server = BrainServer(db_path=str(path), classification_enabled=True)
    server.jev_client = AsyncMock()

    async def idle():
        await asyncio.Event().wait()

    for name in ("_enrichment_loop", "_reaper_loop", "_embed_reaper_loop"):
        monkeypatch.setattr(server, name, idle)
    server.start_enrichment()
    assert server._classification_task is None
    assert server.classification_status["last_error"] == "classification schema unavailable"
    await server.stop_enrichment()
    server.close()
