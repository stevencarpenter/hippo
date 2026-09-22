"""Classification publication is revision-safe and preserves source-owned data."""

import asyncio
import fcntl
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import sqlite_vec

from hippo_brain import classification as c
from hippo_brain import jev
from hippo_brain.auto_memory import ingest_memory_file, write_memory_knowledge_node
from hippo_brain.browser_enrichment import write_browser_knowledge_node
from hippo_brain.claude_sessions import write_claude_knowledge_node
from hippo_brain.enrichment import upsert_entities, write_knowledge_node
from hippo_brain.models import EnrichmentResult
from hippo_brain.opencode_sessions import write_opencode_knowledge_node
from hippo_brain.schema_version import require_accepted_schema
from hippo_brain.vector_store import ensure_vec_table
from hippo_brain.workflow_enrichment import enrich_one_async


@pytest.fixture(autouse=True)
def reset_configuration():
    c.configure(False)
    yield
    c.configure(False)


def node(conn, name="node"):
    with conn:
        node_id = conn.execute(
            "INSERT INTO knowledge_nodes(uuid,content,embed_text,tags) VALUES (?,?,?,?)",
            (
                name,
                '{"summary":"SQLite schema migration","tags":["original"]}',
                "database migration",
                '["original"]',
            ),
        ).lastrowid
    return node_id


def response(probability=0.9):
    return {
        "model": "1.13.0",
        "answers": {
            topic: {"type": "noul", "noul": probability if topic == "database-storage" else 0.1}
            for topic in c.default_recipe().topics
        },
    }


def claimed(conn, node_id, now=100):
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        c.enqueue_node(conn, node_id, enabled=True, now_ms=now)
        return c.claim(conn, now_ms=now)[0]


def publish(conn, work, result=None, now=101):
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        return c.apply_result(conn, work, result or response(), now_ms=now)


def test_apply_preserves_node_fts_and_foreign_entities(tmp_db):
    conn, _ = tmp_db
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    ensure_vec_table(conn)
    nid = node(conn)
    with conn:
        conn.execute(
            "INSERT INTO knowledge_vectors(knowledge_node_id,vec_knowledge,vec_command) VALUES (?,?,?)",
            (
                nid,
                sqlite_vec.serialize_float32([0.1] * 768),
                sqlite_vec.serialize_float32([0.2] * 768),
            ),
        )
    vectors = conn.execute("SELECT * FROM knowledge_vectors").fetchall()
    original = conn.execute("SELECT * FROM knowledge_nodes").fetchall()
    fts = conn.execute("SELECT rowid,* FROM knowledge_fts").fetchall()
    with conn:
        eid = conn.execute(
            "INSERT INTO entities(type,name,canonical) VALUES ('concept','original','original')"
        ).lastrowid
        conn.execute("INSERT INTO knowledge_node_entities VALUES (?,?)", (nid, eid))
    work = claimed(conn, nid)
    assert publish(conn, work)
    assert c.current_topics(conn, [nid]) == {nid: {"database-storage": 0.9}}
    assert conn.execute("SELECT * FROM knowledge_nodes").fetchall() == original
    assert conn.execute("SELECT rowid,* FROM knowledge_fts").fetchall() == fts
    assert conn.execute("SELECT * FROM knowledge_vectors").fetchall() == vectors
    assert conn.execute(
        "SELECT 1 FROM knowledge_node_entities WHERE entity_id=?", (eid,)
    ).fetchone()
    assert not publish(conn, work)


def test_reserved_source_concepts_never_become_classifier_owned(tmp_db):
    conn, _ = tmp_db
    nid = node(conn)
    names = [c.NAMESPACE + "database-storage", c.SOURCE_ESCAPE + c.NAMESPACE + "database-storage"]
    with conn:
        upsert_entities(conn, nid, {"errors": names}, {"errors": "concept"}, 100)
    source_links = conn.execute(
        "SELECT e.id,e.name,e.canonical,e.metadata FROM entities e JOIN knowledge_node_entities ne ON ne.entity_id=e.id WHERE ne.knowledge_node_id=? ORDER BY e.id",
        (nid,),
    ).fetchall()
    assert [r[1] for r in source_links] == names
    assert len({r[2] for r in source_links}) == 2
    assert all(r[2].startswith(c.SOURCE_ESCAPE) and r[3] is None for r in source_links)
    assert publish(conn, claimed(conn, nid))
    with conn:
        conn.execute("UPDATE knowledge_nodes SET embed_text='new input' WHERE id=?", (nid,))
        c.enqueue_node(conn, nid, enabled=False)
    assert (
        conn.execute(
            "SELECT e.id,e.name,e.canonical,e.metadata FROM entities e JOIN knowledge_node_entities ne ON ne.entity_id=e.id WHERE ne.knowledge_node_id=? ORDER BY e.id",
            (nid,),
        ).fetchall()
        == source_links
    )


def test_existing_namespace_collision_abstains_without_modifying_source(tmp_db):
    conn, _ = tmp_db
    nid = node(conn)
    with conn:
        eid = conn.execute(
            "INSERT INTO entities(type,name,canonical,metadata) VALUES ('concept',?,?,?)",
            ("original", c.NAMESPACE + "database-storage", '{"source":"legacy"}'),
        ).lastrowid
        conn.execute("INSERT INTO knowledge_node_entities VALUES (?,?)", (nid, eid))
    original = conn.execute("SELECT * FROM entities").fetchall()
    work = claimed(conn, nid)
    assert conn.execute("SELECT * FROM entities").fetchall() == original
    with pytest.raises(c.TopicOwnershipError):
        publish(conn, work)
    with conn:
        c.enqueue_node(conn, nid, enabled=False)
    assert conn.execute("SELECT * FROM entities").fetchall() == original
    assert conn.execute("SELECT * FROM knowledge_node_entities").fetchall() == [(nid, eid)]
    assert c.current_topics(conn, [nid]) == {}


def test_publication_cap_thresholds_and_redaction(tmp_db):
    conn, _ = tmp_db
    nid = node(conn)
    secret = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    with conn:
        conn.execute(
            "UPDATE knowledge_nodes SET content=? WHERE id=?",
            (json.dumps({"summary": "x" * 1190 + secret}), nid),
        )
    recipe = replace(
        c.default_recipe(),
        max_topics=2,
        thresholds={"database-storage": 0.85},
        publish_topics=("database-storage", "observability", "source-control"),
    )
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        c.enqueue_node(conn, nid, enabled=True, recipe=recipe, now_ms=100)
        work = c.claim(conn, recipe=recipe, now_ms=100)[0]
    assert "ghp_" not in work.state["summary"]
    result = response()
    result["answers"]["observability"]["noul"] = 0.9
    result["answers"]["source-control"]["noul"] = 0.9
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        assert c.apply_result(conn, work, result, recipe=recipe, now_ms=101)
    assert c.current_topics(conn, [nid], recipe) == {
        nid: {"observability": 0.9, "source-control": 0.9}
    }


def test_source_publication_rolls_back_if_enqueue_fails(tmp_db):
    conn, _ = tmp_db
    c.configure(True)
    conn.executescript(
        "CREATE TRIGGER fail_classification BEFORE INSERT ON knowledge_node_classifications BEGIN SELECT RAISE(ABORT,'classification failure'); END;"
    )
    with pytest.raises(sqlite3.IntegrityError, match="classification failure"):
        write_knowledge_node(conn, EnrichmentResult("summary", "intent", "success"), [], "local")
    assert conn.execute("SELECT count(*) FROM knowledge_nodes").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM knowledge_fts").fetchone()[0] == 0


def test_disabled_update_invalidates_and_unchanged_rebuild_restores_links(tmp_db):
    conn, _ = tmp_db
    nid = node(conn)
    assert publish(conn, claimed(conn, nid))
    with conn:
        conn.execute("DELETE FROM knowledge_node_entities WHERE knowledge_node_id=?", (nid,))
        assert not c.enqueue_node(conn, nid, enabled=False)
    assert c.current_topics(conn, [nid]) == {nid: {"database-storage": 0.9}}
    with conn:
        conn.execute("UPDATE knowledge_nodes SET tags='[\"different\"]' WHERE id=?", (nid,))
        before = conn.total_changes
        assert not c.enqueue_node(conn, nid, enabled=True)
        assert conn.total_changes == before
    with conn:
        conn.execute("UPDATE knowledge_nodes SET embed_text='changed' WHERE id=?", (nid,))
        assert not c.enqueue_node(conn, nid, enabled=False)
    assert c.current_topics(conn, [nid]) == {}
    assert conn.execute(
        "SELECT status,requested_revision FROM knowledge_node_classifications"
    ).fetchone() == ("skipped", 2)
    assert conn.execute("SELECT count(*) FROM knowledge_node_entities").fetchone()[0] == 0
    with conn:
        assert c.enqueue_node(conn, nid, enabled=True)


@pytest.mark.parametrize("change", ["content", "uuid", "recipe", "lease", "delete"])
def test_late_result_never_publishes(tmp_db, change):
    conn, _ = tmp_db
    nid = node(conn)
    work = claimed(conn, nid)
    with conn:
        if change == "content":
            conn.execute("UPDATE knowledge_nodes SET embed_text='new' WHERE id=?", (nid,))
        elif change == "uuid":
            conn.execute("UPDATE knowledge_nodes SET uuid='replacement' WHERE id=?", (nid,))
        elif change == "recipe":
            c.enqueue_node(
                conn, nid, enabled=True, recipe=replace(c.default_recipe(), taxonomy_version="v2")
            )
        elif change == "lease":
            conn.execute("UPDATE knowledge_node_classifications SET lease_token='reclaimed'")
        else:
            conn.execute("DELETE FROM knowledge_nodes WHERE id=?", (nid,))
    assert not publish(conn, work)
    assert c.current_topics(conn, [nid]) == {}


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.1, 1.1, True, "0.9"])
def test_invalid_probability_writes_nothing(tmp_db, bad):
    conn, _ = tmp_db
    nid = node(conn)
    work = claimed(conn, nid)
    with pytest.raises(ValueError):
        publish(conn, work, response(bad))
    assert conn.execute("SELECT count(*) FROM knowledge_node_entities").fetchone()[0] == 0
    assert (
        conn.execute("SELECT status FROM knowledge_node_classifications").fetchone()[0]
        == "processing"
    )


def test_expired_lease_reclaimed_but_old_response_rejected(tmp_db):
    conn, _ = tmp_db
    nid = node(conn)
    old = claimed(conn, nid)
    assert not publish(conn, old, now=100 + c.LEASE_MS)
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        fresh = c.claim(conn, now_ms=100 + c.LEASE_MS)[0]
    assert fresh.attempts == 2
    assert not publish(conn, old, now=101 + c.LEASE_MS)
    assert publish(conn, fresh, now=101 + c.LEASE_MS)


def test_partial_and_model_drift_fail_complete_result(tmp_db):
    conn, _ = tmp_db
    work = claimed(conn, node(conn))
    partial = response()
    partial["answers"].pop("observability")
    with pytest.raises(ValueError):
        publish(conn, work, partial)
    wrong = response() | {"model": "latest"}
    with pytest.raises(ValueError):
        publish(conn, work, wrong)
    assert publish(conn, work, response(0.1))
    assert c.current_topics(conn, [work.node_id]) == {work.node_id: {}}


def test_backfill_idempotent_malformed_skipped_and_recipe_invalidates(tmp_db):
    conn, _ = tmp_db
    first = node(conn)
    second = node(conn, "malformed")
    with conn:
        conn.execute("UPDATE knowledge_nodes SET content='[]' WHERE id=?", (second,))
        batch = c.backfill(conn, limit=1)
        assert batch == {"after_id": first, "scanned": 1, "queued": 1}
        assert c.backfill(conn, limit=1)["queued"] == 0
        assert c.backfill(conn, after_id=first)["queued"] == 0
    assert (
        conn.execute(
            "SELECT status FROM knowledge_node_classifications WHERE node_id=?", (second,)
        ).fetchone()[0]
        == "skipped"
    )
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        work = c.claim(conn, now_ms=10**15)[0]
    assert publish(conn, work, now=10**15 + 1)
    assert (
        c.current_topics(conn, [first], replace(c.default_recipe(), taxonomy_version="changed"))
        == {}
    )


def test_missing_schema_is_optional_for_ordinary_queries(tmp_db):
    conn, _ = tmp_db
    nid = node(conn)
    conn.execute("DROP TABLE knowledge_node_classifications")
    conn.execute("PRAGMA user_version=24")
    require_accepted_schema(conn)
    assert not c.schema_ready(conn)
    assert not c.enqueue_node(conn, nid, enabled=True)
    assert c.current_topics(conn, [nid]) == {}


def test_configured_recipe_is_shared_by_writer_and_reader(tmp_db, tmp_path):
    conn, _ = tmp_db
    path = tmp_path / "recipe.json"
    path.write_text(
        json.dumps(
            {"thresholds": {"database-storage": 0.95}, "publish_topics": ["database-storage"]}
        )
    )
    recipe = c.load_recipe(path)
    c.configure(True, recipe)
    nid = node(conn)
    assert publish(conn, claimed(conn, nid))
    assert c.default_recipe().recipe_hash == recipe.recipe_hash
    assert c.current_topics(conn, [nid]) == {nid: {}}
    assert c.load_recipe().thresholds == {}
    path.write_text('{"model_id":"latest"}')
    with pytest.raises(ValueError, match="pinned"):
        c.load_recipe(path)


async def test_worker_does_not_hold_transaction_during_inference_and_cas_rejects_change(tmp_db):
    conn, path = tmp_db
    nid = node(conn)
    with conn:
        c.enqueue_node(conn, nid, enabled=True)

    class Client:
        async def assess(self, *args, **kwargs):
            # A separate writer must acquire its write lock while inference awaits.
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE knowledge_nodes SET embed_text='new evidence' WHERE id=?", (nid,)
                )
                c.enqueue_node(conn, nid, enabled=True)
            await asyncio.sleep(0)
            return response()

    counts = await c.ClassificationWorker(path, Client()).process_batch()
    assert counts["stale"] == 1
    assert c.current_topics(conn, [nid]) == {}
    assert conn.execute(
        "SELECT status,attempts FROM knowledge_node_classifications"
    ).fetchone() == ("pending", 0)
    path.resolve().with_name(path.name + ".classification-claims.lock").unlink(missing_ok=True)


async def test_worker_retry_cancellation_and_terminal_validation(tmp_db):
    conn, path = tmp_db
    nid = node(conn)
    with conn:
        c.enqueue_node(conn, nid, enabled=True)

    class Client:
        error = httpx.ReadTimeout("source text must not enter stored errors")

        async def assess(self, *args, **kwargs):
            raise self.error

    client = Client()
    worker = c.ClassificationWorker(path, client)
    assert (await worker.process_batch())["retry"] == 1
    assert (await worker.process_batch())["claimed"] == 0
    assert (
        conn.execute("SELECT error FROM knowledge_node_classifications").fetchone()[0]
        == "ReadTimeout"
    )
    with conn:
        conn.execute("UPDATE knowledge_node_classifications SET next_attempt_at=0")
    client.error = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await worker.process_batch()
    with conn:
        conn.execute("UPDATE knowledge_node_classifications SET next_attempt_at=0")
    client.error = ValueError("invalid")
    assert (await worker.process_batch())["failed"] == 1
    assert worker.status()["counts"] == {"failed": 1}


async def test_worker_cooldown_preserves_attempts_and_recovers(tmp_db, monkeypatch):
    conn, path = tmp_db
    now = time.monotonic()
    monkeypatch.setattr(jev, "time", SimpleNamespace(monotonic=lambda: now))
    calls = 0

    async def handler(request):
        nonlocal calls
        calls += 1
        if calls <= 3:
            return httpx.Response(503, request=request)
        return httpx.Response(200, json=response(), request=request)

    for name in ("first", "second"):
        nid = node(conn, name)
        with conn:
            c.enqueue_node(conn, nid, enabled=True)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        client = jev.JevClient("test", client=transport)
        worker = c.ClassificationWorker(path, client)
        for _ in range(3):
            with conn:
                conn.execute("UPDATE knowledge_node_classifications SET next_attempt_at=0")
            assert (await worker.process_batch())["retry"] == 2
        assert calls == 3
        assert conn.execute(
            "SELECT status,attempts FROM knowledge_node_classifications ORDER BY node_id"
        ).fetchall() == [("pending", 2), ("pending", 1)]
        assert (await worker.process_batch())["claimed"] == 0

        now += 31
        with conn:
            conn.execute("UPDATE knowledge_node_classifications SET next_attempt_at=0")
        assert (await worker.process_batch())["ready"] == 2
        assert calls == 5


async def test_worker_records_actual_transport_and_unknown_usage(tmp_db, monkeypatch):
    conn, path = tmp_db
    nid = node(conn)
    with conn:
        c.enqueue_node(conn, nid, enabled=True)
    observed = []
    monkeypatch.setattr(
        c.telemetry,
        "record_decision_metrics",
        lambda record, **kwargs: observed.append((dict(record), kwargs)),
    )

    class Client:
        async def assess(self, *args, **kwargs):
            return response() | {
                "_transport": {
                    "network_calls": 1,
                    "queue_ms": 2.0,
                    "http_ms": 10.0,
                    "payload_bytes": 100,
                }
            }

    assert (await c.ClassificationWorker(path, Client()).process_batch())["ready"] == 1
    record, options = observed[0]
    assert options == {"task": "classification"}
    assert record["transport"]["network_calls"] == 1
    assert record["usage"] is None
    assert record["usage_missing_reason"] == "provider omitted usage"
    assert record["total_ms"] >= record["apply_ms"] >= 0
    with conn:
        conn.execute("UPDATE knowledge_nodes SET embed_text='new' WHERE id=?", (nid,))
        c.enqueue_node(conn, nid, enabled=True)

    class Unavailable:
        async def assess(self, *args, **kwargs):
            error = c.JevUnavailable("cooldown")
            error.jev_transport = {
                "network_calls": 0,
                "queue_ms": 5.0,
                "http_ms": None,
                "payload_bytes": 100,
            }
            raise error

    assert (await c.ClassificationWorker(path, Unavailable()).process_batch())["retry"] == 1
    assert observed[-1][0]["transport"]["network_calls"] == 0
    queued_at, updated_at = conn.execute(
        "SELECT next_attempt_at,updated_at FROM knowledge_node_classifications WHERE node_id=?",
        (nid,),
    ).fetchone()
    assert queued_at - updated_at >= 30_000


async def test_query_gate_uses_canonical_database_and_releases_before_inference(tmp_db, tmp_path):
    conn, path = tmp_db
    nid = node(conn)
    with conn:
        c.enqueue_node(conn, nid, enabled=True)
    lock = path.resolve().with_name(path.name + ".classification-claims.lock")
    alias = tmp_path / "database-alias.sqlite"
    alias.symlink_to(path)

    class Client:
        async def assess(self, *args, **kwargs):
            # Exclusive claim lock is already released before HTTP begins.
            descriptor = os.open(lock, os.O_RDWR)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
            return response()

    worker = c.ClassificationWorker(path, Client())
    with c.query_activity(path, enabled=False):
        assert not lock.exists()
    with c.query_activity(path) as first:
        with c.query_activity(alias) as second:
            assert first and second
            inode = lock.stat().st_ino
            assert (await worker.process_batch())["gate_blocked"] == 1
        assert (await worker.process_batch())["claimed"] == 0
    assert (await worker.process_batch())["ready"] == 1
    assert lock.stat().st_ino == inode
    assert lock.stat().st_mode & 0o777 == 0o600
    lock.unlink()


async def test_subprocess_query_lock_suppresses_background_claims(tmp_db):
    conn, path = tmp_db
    nid = node(conn)
    with conn:
        c.enqueue_node(conn, nid, enabled=True)
    lock = path.resolve().with_name(path.name + ".classification-claims.lock")
    code = "import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_CREAT|os.O_RDWR,0o600); fcntl.flock(fd,fcntl.LOCK_SH); print('ready',flush=True); sys.stdin.readline()"
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(lock)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )

    class Client:
        async def assess(self, *args, **kwargs):
            return response()

    worker = c.ClassificationWorker(path, Client())
    try:
        assert process.stdout.readline().strip() == "ready"
        assert (await worker.process_batch())["claimed"] == 0
        process.communicate("\n", timeout=10)
        assert process.returncode == 0
        assert (await worker.process_batch())["ready"] == 1
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=10)
        lock.unlink(missing_ok=True)


def test_sqlite_writer_wait_does_not_hold_query_gate(tmp_db):
    conn, path = tmp_db
    conn.execute("PRAGMA journal_mode=WAL")
    nid = node(conn)
    with conn:
        c.enqueue_node(conn, nid, enabled=True)
    code = """
import asyncio, json, sys
from hippo_brain import classification as c

class Client:
    async def assess(self, *args, **kwargs):
        return {
            "model": "1.13.0",
            "answers": {topic: {"type": "noul", "noul": 0.1} for topic in c.default_recipe().topics},
        }

class Worker(c.ClassificationWorker):
    announced = False

    def _connect(self):
        conn = super()._connect()
        def trace(statement):
            if statement == "BEGIN IMMEDIATE" and not self.announced:
                self.announced = True
                print("waiting", flush=True)
        conn.set_trace_callback(trace)
        return conn

print(json.dumps(asyncio.run(Worker(sys.argv[1], Client()).process_batch())), flush=True)
"""
    conn.execute("BEGIN IMMEDIATE")
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # The child is waiting on our distinct SQLite writer connection.
        assert process.stdout.readline().strip() == "waiting"
        started = time.monotonic()
        with c.query_activity(path) as acquired:
            assert acquired
            assert time.monotonic() - started < 0.5
            assert conn.in_transaction
        conn.rollback()
        output, error = process.communicate(timeout=10)
        assert process.returncode == 0, error
        assert json.loads(output)["ready"] == 1
    finally:
        conn.rollback()
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=10)
        path.resolve().with_name(path.name + ".classification-claims.lock").unlink(missing_ok=True)


async def test_cancelled_claim_does_not_block_loop_or_dispatch_inference(tmp_db):
    conn, path = tmp_db
    conn.execute("PRAGMA journal_mode=WAL")
    nid = node(conn)
    with conn:
        c.enqueue_node(conn, nid, enabled=True)
    waiting, finished = asyncio.Event(), threading.Event()
    loop = asyncio.get_running_loop()
    calls = []

    class Client:
        async def assess(self, *args, **kwargs):
            calls.append(1)
            return response()

    class Worker(c.ClassificationWorker):
        def _connect(self):
            connection = super()._connect()
            connection.set_trace_callback(
                lambda sql: (
                    loop.call_soon_threadsafe(waiting.set) if sql == "BEGIN IMMEDIATE" else None
                )
            )
            return connection

        def _write_transaction(self, *args):
            try:
                return super()._write_transaction(*args)
            finally:
                finished.set()

    conn.execute("BEGIN IMMEDIATE")
    task = asyncio.create_task(Worker(path, Client()).process_batch())
    try:
        started = time.monotonic()
        await asyncio.wait_for(waiting.wait(), timeout=0.5)
        await asyncio.sleep(0.02)
        assert time.monotonic() - started < 0.5
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        conn.rollback()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        assert await asyncio.to_thread(finished.wait, 1)
    assert calls == []
    assert conn.execute(
        "SELECT status,attempts FROM knowledge_node_classifications"
    ).fetchone() == ("pending", 0)
    path.resolve().with_name(path.name + ".classification-claims.lock").unlink(missing_ok=True)


@pytest.mark.parametrize("invalid_response", [False, True])
async def test_apply_and_failure_waits_leave_loop_responsive(tmp_db, invalid_response):
    conn, path = tmp_db
    conn.execute("PRAGMA journal_mode=WAL")
    nid = node(conn)
    with conn:
        c.enqueue_node(conn, nid, enabled=True)
    held = asyncio.Event()

    class Client:
        async def assess(self, *args, **kwargs):
            # Claim has committed; contend only with publication/failure SQL.
            conn.execute("BEGIN IMMEDIATE")
            held.set()
            if invalid_response:
                raise ValueError("invalid response")
            return response()

    worker = c.ClassificationWorker(path, Client())
    task = asyncio.create_task(worker.process_batch())
    try:
        started = time.monotonic()
        await asyncio.wait_for(held.wait(), timeout=0.5)
        await asyncio.sleep(0.02)
        assert time.monotonic() - started < 0.5
        if not invalid_response:
            # Cancel while apply is blocked. Its owned transaction must never
            # publish after this coroutine has moved to cancellation handling.
            task.cancel()
            await asyncio.sleep(0.02)
        conn.rollback()
        if invalid_response:
            assert (await asyncio.wait_for(task, timeout=1))["failed"] == 1
        else:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1)
    finally:
        conn.rollback()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    assert conn.execute(
        "SELECT status,attempts,applied_revision FROM knowledge_node_classifications"
    ).fetchone() == ("failed" if invalid_response else "pending", 1, None)
    assert c.current_topics(conn, [nid]) == {}
    path.resolve().with_name(path.name + ".classification-claims.lock").unlink(missing_ok=True)


async def test_cancelled_query_releases_shared_gate(tmp_db):
    conn, path = tmp_db
    nid = node(conn)
    with conn:
        c.enqueue_node(conn, nid, enabled=True)
    entered = asyncio.Event()

    async def query():
        with c.query_activity(path):
            entered.set()
            await asyncio.Event().wait()

    class Client:
        async def assess(self, *args, **kwargs):
            return response()

    task = asyncio.create_task(query())
    await entered.wait()
    worker = c.ClassificationWorker(path, Client())
    assert (await worker.process_batch())["gate_blocked"] == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (await worker.process_batch())["ready"] == 1
    path.resolve().with_name(path.name + ".classification-claims.lock").unlink()


async def test_unavailable_gate_fails_open_queries_and_closed_classification(tmp_db, tmp_path):
    conn, path = tmp_db
    nid = node(conn)
    with conn:
        c.enqueue_node(conn, nid, enabled=True)
    lock = path.resolve().with_name(path.name + ".classification-claims.lock")
    target = tmp_path / "unrelated"
    target.write_text("unchanged")
    lock.symlink_to(target)
    before = c.gate_status()
    try:
        with c.query_activity(path) as acquired:
            assert acquired is False
        assert (await c.ClassificationWorker(path, object()).process_batch())["claimed"] == 0
        after = c.gate_status()
        assert after["query"] == before["query"] + 1
        assert after["classification"] == before["classification"] + 1
        assert conn.execute(
            "SELECT status,attempts FROM knowledge_node_classifications"
        ).fetchone() == ("pending", 0)
        assert target.read_text() == "unchanged"
    finally:
        lock.unlink()


@pytest.mark.parametrize(
    "writer",
    [
        write_knowledge_node,
        write_browser_knowledge_node,
        write_claude_knowledge_node,
        write_opencode_knowledge_node,
    ],
)
def test_source_writers_enqueue_inside_publication(tmp_db, writer):
    conn, _ = tmp_db
    c.configure(True)
    nid = writer(
        conn,
        EnrichmentResult("SQLite migration", "database", "success", embed_text="sqlite"),
        [],
        "local",
    )
    assert conn.execute(
        "SELECT status FROM knowledge_node_classifications WHERE node_id=?", (nid,)
    ).fetchone() == ("pending",)
    assert not conn.in_transaction


def test_in_place_writer_restores_valid_memberships(tmp_db):
    conn, _ = tmp_db
    result = EnrichmentResult("SQLite migration", "database", "success", embed_text="sqlite")
    c.configure(True)
    nid = write_knowledge_node(conn, result, [], "local")
    work = claimed(conn, nid, now=10**15)
    assert publish(conn, work, now=10**15 + 1)
    script = Path(__file__).parents[1] / "scripts/re-enrich-knowledge-nodes.py"
    spec = importlib.util.spec_from_file_location("reenrich_classification_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module._update_node_in_place(conn, nid, result, "new-local")
    assert c.current_topics(conn, [nid]) == {nid: {"database-storage": 0.9}}


def test_memory_supersession_cascades_classification(tmp_db, tmp_path):
    conn, _ = tmp_db
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    ensure_vec_table(conn)
    c.configure(True)
    source = tmp_path / "MEMORY.md"
    source.write_text("# SQLite\n\nUse transactions.")
    first = ingest_memory_file(conn, source, repository="hippo")
    result = EnrichmentResult("SQLite migration", "database", "success", embed_text="sqlite")
    nid = write_memory_knowledge_node(conn, result, first.revision_id, "local")
    assert publish(conn, claimed(conn, nid, now=10**15), now=10**15 + 1)
    source.write_text("# SQLite\n\nUse short transactions.")
    second = ingest_memory_file(conn, source, repository="hippo")
    replacement = write_memory_knowledge_node(conn, result, second.revision_id, "local")
    assert replacement != nid
    assert conn.execute("SELECT node_id,status FROM knowledge_node_classifications").fetchall() == [
        (replacement, "pending")
    ]
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


async def test_workflow_dedup_invalidates_changed_provenance(tmp_db):
    conn, path = tmp_db
    c.configure(True)
    with conn:
        for run_id in (1, 2):
            conn.execute(
                "INSERT INTO workflow_runs(id,repo,head_sha,head_branch,event,status,conclusion,html_url,raw_json,first_seen_at,last_seen_at,started_at) VALUES (?,'me/repo','sha','main','push','completed','failure','https://example.com','{}',1000,2000,1000)",
                (run_id,),
            )

    class Client:
        async def chat(self, **kwargs):
            return json.dumps(
                {
                    "summary": "SQLite migration",
                    "intent": "database",
                    "outcome": "success",
                    "entities": {},
                    "tags": [],
                    "embed_text": "SQLite schema",
                }
            )

    first = await enrich_one_async(str(path), 1, Client(), "local")
    nid = first[0]
    assert publish(conn, claimed(conn, nid, now=10**15), now=10**15 + 1)
    assert await enrich_one_async(str(path), 2, Client(), "local") is None
    assert conn.execute("SELECT count(*) FROM knowledge_nodes").fetchone()[0] == 1
    assert c.current_topics(conn, [nid]) == {}
    assert conn.execute(
        "SELECT status,requested_revision FROM knowledge_node_classifications"
    ).fetchone() == ("pending", 2)
