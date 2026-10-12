"""HIPO-7: synthetic capture and derivation only, never production stores/inference."""

import json
import os
import runpy
import socket
import sqlite3
import struct
import subprocess
import tempfile
import time
import uuid
import weakref
from pathlib import Path

import pytest

from hippo_brain.auto_memory import (
    build_memory_enrichment_prompt,
    claim_pending_memories,
    ingest_memory_file,
    write_memory_knowledge_node,
)
from hippo_brain.browser_enrichment import (
    build_browser_enrichment_prompt,
    write_browser_knowledge_node,
)
from hippo_brain.claude_sessions import (
    SessionSegment,
    build_claude_enrichment_prompt,
    insert_segment,
    write_claude_knowledge_node,
)
from hippo_brain.client import MockInferenceClient
from hippo_brain.embeddings import embed_knowledge_node
from hippo_brain.enrichment import build_enrichment_prompt, write_knowledge_node
from hippo_brain.models import EnrichmentResult
from hippo_brain.opencode_sessions import (
    build_opencode_enrichment_prompt,
    write_opencode_knowledge_node,
)
from hippo_brain.redaction import index_state, index_text, redact
from hippo_brain.server import BrainServer
from hippo_brain.vector_store import EMBED_DIM, insert_vectors, knn_search, open_conn
from hippo_brain.workflow_enrichment import enrich_one_async

ROOT = Path(__file__).resolve().parents[2]
SPAN = "ghp_HIPCANARY_927483"


def result():
    return EnrichmentResult(
        summary=SPAN,
        intent="testing",
        outcome="success",
        embed_text=SPAN,
        entities={"tools": [SPAN]},
        tags=[SPAN],
        key_decisions=[SPAN],
        design_decisions=[{"considered": SPAN, "chosen": "safe", "reason": SPAN}],
    )


@pytest.mark.parametrize(
    "span,placeholder",
    [
        (SPAN, "<github-token>"),
        ("ghp_", "<github-token>"),
        ("AKIAlowercase_suffix", "<aws-key>"),
        ("550e8400-e29b-41d4-a716-446655440000", "<uuid>"),
        ("abc1234", "<git-sha>"),
        ("5f3a9c2e1b8d7f6a4c3e2d1b9a8f7e6d5c4b3a2e", "<git-sha>"),
        ("3a4b5c6d7e8f9012a3b4c5d6e7f8091a2b3c4d5e6f708192a3b4c5d6e7f80912", "<hash>"),
        ("dGhpc2lzbm90YWp3dGp1c3RhYmFzZTY0c3RyaW5n", "<base64>"),
        ("eyJshort.lookalike", "<jwt>"),
    ],
)
def test_derived_exclusion_keeps_capture_preserve_rules(span, placeholder):
    assert redact(span) == span
    assert index_text(span) == placeholder
    assert index_text(index_text(span)) == placeholder
    if placeholder != "<base64>":
        for root in ("src/foo-bar", "/Users/foo-bar"):
            assert index_text(f"{root}/{span}") == f"{root}/{placeholder}"


@pytest.mark.parametrize(
    "path",
    [
        "/private/var/folders/v8/y545g92s5gd7tk27zk4nmhjm0000gn/T/pytest-of-synthetic/MEMORY.md",
        "/Users/Developer/Projects2026/OrdinaryFolder/Memory",
        "/Users/foo-bar/VeryLongComponentName2",
        "/Users/foo.bar/VeryLongComponentName2",
    ],
)
def test_absolute_paths_are_not_base64_blobs(path):
    assert index_text(path) == path
    assert index_text(f"file {path}") == f"file {path}"
    assert index_state({path: path}) == {path: path}
    assert json.loads(index_text(json.dumps({path: path}))) == {path: path}
    assert index_state({"source_path": path}) == {"source_path": path}
    assert index_text("dGhpc2lzbm90YWp3dGp1c3RhYmFzZTY0c3RyaW5n/Ab9+") == "<base64>"


@pytest.mark.parametrize(
    "path",
    [
        "src/Foo2/LongComponentNameHere",
        "src/foo-bar/VeryLongComponentName2",
        "src/foo.bar/VeryLongComponentName2",
        "src/LongComponentNameHere2.py",
        "brain/Foo2/LongComponentNameHere",
        "crates/Foo2/LongComponentNameHere",
    ],
)
def test_relative_repository_paths_are_not_base64_blobs(path):
    assert redact(path) == path
    assert index_state({path: path}) == {path: path}
    assert json.loads(index_text(json.dumps({path: path}))) == {path: path}
    assert index_text(path) == path
    assert index_text(f"file {path}") == f"file {path}"
    assert index_state({"source_path": path}) == {"source_path": path}
    blob = "dGhpc2lzbm90YWp3dGp1c3RhYmFzZTY0c3RyaW5n/Ab9+"
    assert index_text(blob) == "<base64>"
    assert index_state({"summary": blob}) == {"summary": "<base64>"}
    assert index_text(f"{path} {SPAN}") == f"{path} <github-token>"


@pytest.mark.parametrize("identifier", ["1234567", "12345678", "1" * 40])
def test_decimal_identifiers_remain_searchable(identifier):
    text = f"error code {identifier}"
    assert redact(text) == text
    assert index_text(text) == text
    assert index_state({"summary": text, "identifier": identifier}) == {
        "summary": text,
        "identifier": identifier,
    }
    assert index_text("1" * 64) == "<hash>"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("abc1234", "<git-sha>"),
        ("ABC1234", "<git-sha>"),
        ("f" + "1" * 39, "<git-sha>"),
        ("F" + "1" * 39, "<git-sha>"),
        ("abc123", "abc123"),
        ("f" + "1" * 40, "f" + "1" * 40),
        ("zabc1234z", "zabc1234z"),
        ("1234567g", "1234567g"),
    ],
)
def test_short_sha_letter_requirement_keeps_case_length_and_boundaries(text, expected):
    assert redact(text) == text
    assert index_text(text) == expected
    assert index_state({"summary": text}) == {"summary": expected}


async def test_vector_only_handle_excludes_both_inputs(tmp_path):
    conn = open_conn(tmp_path / "vectors.db")
    client = MockInferenceClient()
    try:
        await embed_knowledge_node(
            client,
            conn,
            {"id": 1, "embed_text": SPAN, "commands_raw": SPAN},
            embed_model="synthetic",
        )
        assert [call["texts"] for call in client.embed_calls] == [
            ["<github-token>"],
            ["<github-token>"],
        ]
        assert conn.execute("SELECT COUNT(*) FROM knowledge_vectors").fetchone()[0] == 1
    finally:
        conn.close()


async def test_node_cleanup_does_not_hide_unrelated_sql_errors(tmp_path):
    conn = open_conn(tmp_path / "invalid-nodes.db")
    conn.execute("CREATE TABLE knowledge_nodes(id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO knowledge_nodes VALUES(1)")
    conn.commit()
    try:
        with pytest.raises(sqlite3.OperationalError, match="no such column: content"):
            await embed_knowledge_node(
                MockInferenceClient(),
                conn,
                {"id": 1, "embed_text": SPAN},
                embed_model="synthetic",
            )
        assert conn.execute("SELECT COUNT(*) FROM knowledge_vectors").fetchone()[0] == 0
        assert conn.execute("SELECT id FROM knowledge_nodes").fetchone()[0] == 1
    finally:
        conn.close()


def test_existing_credential_redaction_and_structured_keys_are_not_weakened():
    token = "ghp_" + "a" * 36
    assert redact(token) == index_text(token) == "[REDACTED]"
    data = {SPAN: {"Authorization": "Bearer synthetic-sensitive-value", "safe": SPAN}}
    assert index_state(data) == {
        "<github-token>": {"Authorization": "[REDACTED]", "safe": "<github-token>"}
    }
    # Escaped JSON must not bypass the FTS content arm.
    encoded = json.dumps(data).replace("ghp_H", r"ghp_\u0048")
    assert json.loads(index_text(encoded)) == index_state(data)
    assert SPAN in json.dumps(data)  # the source object was not mutated
    assert (
        index_text("cargo test v1.2.3 ~/.ssh/id_ed25519") == "cargo test v1.2.3 ~/.ssh/id_ed25519"
    )


@pytest.fixture
def index_db(tmp_db, tmp_path):
    original, _ = tmp_db
    path = tmp_path / "hippo.db"
    copied = sqlite3.connect(path)
    original.backup(copied)
    copied.close()
    conn = open_conn(path)
    try:
        yield conn, path
    finally:
        conn.close()


def segment():
    return SessionSegment(
        session_id="synthetic-index-capture",
        project_dir="/synthetic",
        cwd="/synthetic",
        git_branch="main",
        segment_index=0,
        start_time=1,
        end_time=2,
        user_prompts=[SPAN],
        tool_calls=[{"name": "Bash", "summary": "echo " + SPAN}],
        message_count=5,
        source_file="/synthetic/session.jsonl",
    )


async def test_all_node_writers_and_prompt_builders_exclude_spans(index_db, tmp_path):
    conn, path = index_db
    conn.execute(
        "INSERT INTO sessions(id,start_time,shell,hostname,username) VALUES(1,1,'zsh','test','test')"
    )
    conn.execute(
        "INSERT INTO events(id,session_id,timestamp,command,duration_ms,cwd,hostname,shell) "
        "VALUES(1,1,1,?,1,'/synthetic','test','zsh')",
        ("echo " + SPAN,),
    )
    conn.execute(
        "INSERT INTO browser_events(id,timestamp,url,domain,dwell_ms,title) "
        "VALUES(1,1,'https://example.test','example.test',10000,?)",
        (SPAN,),
    )
    conn.commit()
    seg = segment()
    sid = insert_segment(conn, seg)
    assert (
        SPAN
        in conn.execute("SELECT summary_text FROM agentic_sessions WHERE id=?", (sid,)).fetchone()[
            0
        ]
    )
    source = tmp_path / "MEMORY.md"
    source.write_text("# Synthetic\n\n" + SPAN)
    memory = ingest_memory_file(conn, source, repository="synthetic", now_ms=1)
    claim = claim_pending_memories(conn, worker_id="synthetic", now_ms=2)[0]
    prompts = [
        build_enrichment_prompt([{"command": SPAN}]),
        build_claude_enrichment_prompt([seg]),
        build_opencode_enrichment_prompt([{"summary_text": SPAN}]),
        build_browser_enrichment_prompt([{"title": SPAN}]),
        build_memory_enrichment_prompt(claim),
    ]
    assert all(SPAN not in prompt and "<github-token>" in prompt for prompt in prompts)
    for writer, ids in [
        (write_knowledge_node, [1]),
        (write_browser_knowledge_node, [1]),
        (write_claude_knowledge_node, [sid]),
        (write_opencode_knowledge_node, [sid]),
    ]:
        original = result()
        original.intent = writer.__name__  # distinct content avoids cross-source dedup
        nid = writer(conn, original, ids, "synthetic")
        assert nid is not None
        assert SPAN == original.summary
        assert all(
            SPAN not in field
            for field in conn.execute(
                "SELECT summary,embed_text,content FROM knowledge_fts WHERE rowid=?", (nid,)
            ).fetchone()
        )
    nid = write_memory_knowledge_node(conn, result(), memory.revision_id, "synthetic", now_ms=3)
    assert (
        SPAN
        not in conn.execute("SELECT content FROM knowledge_fts WHERE rowid=?", (nid,)).fetchone()[0]
    )
    assert source.read_text().endswith(SPAN)
    conn.execute(
        "INSERT INTO workflow_runs(id,repo,head_sha,event,status,conclusion,html_url,actor,raw_json,first_seen_at,last_seen_at) "
        "VALUES(1,'synthetic',?,'push','completed','success','https://example.test','test','{}',1,1)",
        ("abc1234",),
    )
    conn.execute(
        "INSERT INTO workflow_jobs(id,run_id,name,status,raw_json) VALUES(1,1,'test','completed','{}')"
    )
    conn.execute(
        "INSERT INTO workflow_annotations(job_id,level,message) VALUES(1,'failure',?)", (SPAN,)
    )
    conn.commit()
    client = MockInferenceClient()
    client.CANNED_RESPONSE = json.dumps(vars(result()))
    enriched = await enrich_one_async(str(path), 1, client, "synthetic")
    assert enriched is not None
    assert SPAN not in client.chat_calls[0]["messages"][1]["content"]
    assert (
        SPAN
        not in conn.execute(
            "SELECT content FROM knowledge_fts WHERE rowid=?", (enriched[0],)
        ).fetchone()[0]
    )
    assert SPAN in conn.execute("SELECT command FROM events WHERE id=1").fetchone()[0]


async def test_reembed_and_orphan_reaper_clean_both_embedding_arms(index_db):
    conn, path = index_db
    conn.execute(
        "INSERT INTO sessions(id,start_time,shell,hostname,username) VALUES(1,1,'zsh','test','test')"
    )
    conn.execute(
        "INSERT INTO events(id,session_id,timestamp,command,duration_ms,cwd,hostname,shell) "
        "VALUES(1,1,1,?,1,'/synthetic','test','zsh')",
        (SPAN,),
    )
    conn.execute(
        "INSERT INTO knowledge_nodes(id,uuid,content,embed_text,created_at) VALUES(1,'synthetic',?,?,1)",
        (json.dumps({"summary": SPAN}), SPAN),
    )
    conn.execute("INSERT INTO knowledge_node_events VALUES(1,1)")
    conn.commit()
    server = BrainServer(db_path=str(path), embedding_model="synthetic", embed_orphan_stale_secs=0)
    server.client = MockInferenceClient()
    try:
        await server._embed_reaper_tick()
        assert [call["texts"] for call in server.client.embed_calls] == [
            ["<github-token>"],
            ["<github-token>"],
        ]
        assert (
            SPAN
            not in conn.execute("SELECT content FROM knowledge_fts WHERE rowid=1").fetchone()[0]
        )
        # Re-embed existing vectors with raw source commands and historical derived text.
        conn.execute(
            "UPDATE knowledge_nodes SET content=?,embed_text=? WHERE id=1",
            (json.dumps({"summary": SPAN}), SPAN),
        )
        conn.commit()
        client = MockInferenceClient()
        reembed = runpy.run_path(str(ROOT / "scripts/hippo-re-embed.py"))["re_embed_nodes"]
        assert await reembed(server._vector_table, client, "synthetic") == 1
        assert [call["texts"] for call in client.embed_calls] == [
            ["<github-token>"],
            ["<github-token>"],
        ]
        assert (
            SPAN
            not in conn.execute("SELECT content FROM knowledge_fts WHERE rowid=1").fetchone()[0]
        )
        assert conn.execute("SELECT command FROM events WHERE id=1").fetchone()[0] == SPAN
    finally:
        server.close()


async def test_ingestion_prompt_exclusion_preserves_source(index_db, monkeypatch):
    conn, path = index_db
    sid = insert_segment(conn, segment())
    source_query = (
        "SELECT summary_text, user_prompts_json, tool_calls_json FROM agentic_sessions WHERE id=?"
    )
    source = conn.execute(source_query, (sid,)).fetchone()
    client = MockInferenceClient()
    client.CANNED_RESPONSE = json.dumps(vars(result()))
    monkeypatch.setattr("hippo_brain.client.InferenceClient", lambda **kwargs: client)
    enrich = runpy.run_path(str(ROOT / "scripts/hippo-ingest-claude.py"))["run_enrichment"]
    await enrich({"models": {"enrichment": "synthetic"}}, "", path, path.parent)
    assert len(client.chat_calls) == 1
    prompt = client.chat_calls[0]["messages"][1]["content"]
    assert SPAN not in prompt
    assert "<github-token>" in prompt
    assert conn.execute(source_query, (sid,)).fetchone() == source
    assert conn.execute("SELECT COUNT(*) FROM knowledge_nodes").fetchone()[0] == 1


async def test_empty_reembed_records_model_for_the_first_new_node(index_db):
    from hippo_brain.vector_store import get_stored_embed_model, record_embed_model

    conn, _ = index_db
    record_embed_model(conn, "model-a")
    conn.commit()
    client = MockInferenceClient()
    reembed = runpy.run_path(str(ROOT / "scripts/hippo-re-embed.py"))["re_embed_nodes"]
    assert await reembed(conn, client, "model-b") == 0
    assert get_stored_embed_model(conn) == "model-b"
    assert client.embed_calls == []
    conn.execute(
        "INSERT INTO knowledge_nodes(id,uuid,content,embed_text) VALUES(1,'first','{}','safe')"
    )
    conn.commit()
    await embed_knowledge_node(client, conn, {"id": 1, "embed_text": "safe"}, embed_model="model-b")
    assert conn.execute("SELECT COUNT(*) FROM knowledge_vectors").fetchone()[0] == 1


async def test_reembed_releases_staged_vectors_before_next_node(index_db, monkeypatch):
    conn, _ = index_db
    for nid in (1, 2, 3):
        conn.execute(
            "INSERT INTO knowledge_nodes(id,uuid,content,embed_text) VALUES(?,?,?,?)",
            (nid, f"bounded-{nid}", "{}", f"safe node {nid}"),
        )
    conn.commit()
    references = []
    staged_files = []

    class TrackedVector(list):
        pass

    class BoundedClient(MockInferenceClient):
        async def embed(self, texts, model=""):
            assert not conn.in_transaction
            vectors = [TrackedVector(vector) for vector in await super().embed(texts, model=model)]
            references.extend(weakref.ref(vector) for vector in vectors)
            assert sum(ref() is not None for ref in references) <= 2
            return vectors

    def staged_file():
        stream = tempfile.TemporaryFile()
        staged_files.append(stream)
        return stream

    reembed = runpy.run_path(str(ROOT / "scripts/hippo-re-embed.py"))["re_embed_nodes"]
    monkeypatch.setitem(reembed.__globals__, "TemporaryFile", staged_file)
    client = BoundedClient()
    assert await reembed(conn, client, "model-b") == 3
    assert len(client.embed_calls) == 6
    assert all(ref() is None for ref in references)
    assert len(staged_files) == 1 and staged_files[0].closed
    rows = conn.execute("SELECT * FROM knowledge_vectors ORDER BY knowledge_node_id").fetchall()
    assert len(rows) == 3
    for nid, knowledge, command in rows:
        expected = MockInferenceClient._deterministic_vector(f"safe node {nid}", EMBED_DIM)
        blob = struct.pack(f"<{EMBED_DIM}f", *expected)
        assert knowledge == blob and command == blob


async def test_reembed_allows_another_writer_during_inference(index_db):
    conn, path = index_db
    conn.execute(
        "INSERT INTO knowledge_nodes(id,uuid,content,embed_text) VALUES(1,'lock','{}','safe')"
    )
    conn.commit()
    writer = sqlite3.connect(path, timeout=0)

    class ConcurrentClient(MockInferenceClient):
        async def embed(self, texts, model=""):
            assert not conn.in_transaction
            writer.execute("UPDATE knowledge_nodes SET outcome='concurrent' WHERE id=1")
            writer.commit()
            return await super().embed(texts, model=model)

    reembed = runpy.run_path(str(ROOT / "scripts/hippo-re-embed.py"))["re_embed_nodes"]
    try:
        client = ConcurrentClient()
        assert await reembed(conn, client, "model-b") == 1
        assert len(client.embed_calls) == 2
        assert conn.execute("SELECT outcome FROM knowledge_nodes").fetchone()[0] == "concurrent"
        assert not conn.in_transaction
    finally:
        writer.close()


async def test_reembed_refuses_inputs_changed_during_inference(index_db):
    from hippo_brain.vector_store import get_stored_embed_model, record_embed_model

    conn, path = index_db
    conn.execute(
        "INSERT INTO knowledge_nodes(id,uuid,content,embed_text) VALUES(1,'changed','{}','safe')"
    )
    insert_vectors(conn, 1, [0.1] * EMBED_DIM, [0.2] * EMBED_DIM)
    record_embed_model(conn, "model-a")
    conn.commit()
    old_vectors = conn.execute("SELECT * FROM knowledge_vectors").fetchall()
    writer = sqlite3.connect(path, timeout=0)

    class ChangedInputClient(MockInferenceClient):
        async def embed(self, texts, model=""):
            writer.execute("UPDATE knowledge_nodes SET embed_text='changed' WHERE id=1")
            writer.commit()
            return await super().embed(texts, model=model)

    reembed = runpy.run_path(str(ROOT / "scripts/hippo-re-embed.py"))["re_embed_nodes"]
    try:
        with pytest.raises(RuntimeError, match="corpus changed during inference"):
            await reembed(conn, ChangedInputClient(), "model-b")
        assert conn.execute("SELECT * FROM knowledge_vectors").fetchall() == old_vectors
        assert get_stored_embed_model(conn) == "model-a"
        assert conn.execute("SELECT embed_text FROM knowledge_nodes").fetchone()[0] == "changed"
        assert not conn.in_transaction
    finally:
        writer.close()


@pytest.mark.parametrize("fail_call", [3, 4])
async def test_reembed_model_switch_is_atomic(index_db, fail_call):
    from hippo_brain.vector_store import (
        EmbedDriftError,
        check_embed_model_drift,
        get_stored_embed_model,
        record_embed_model,
    )

    conn, path = index_db
    for nid in (1, 2):
        conn.execute(
            "INSERT INTO knowledge_nodes(id,uuid,content,embed_text,created_at) VALUES(?,?,?,?,1)",
            (nid, str(nid), json.dumps({"summary": SPAN}), SPAN),
        )
        insert_vectors(conn, nid, [0.1] * EMBED_DIM, [0.2] * EMBED_DIM)
    record_embed_model(conn, "model-a")
    conn.commit()
    old_vectors = conn.execute(
        "SELECT * FROM knowledge_vectors ORDER BY knowledge_node_id"
    ).fetchall()
    old_nodes = conn.execute("SELECT * FROM knowledge_nodes ORDER BY id").fetchall()
    old_fts = conn.execute("SELECT * FROM knowledge_fts ORDER BY rowid").fetchall()
    reader = open_conn(path)

    class MigrationClient(MockInferenceClient):
        async def embed(self, texts, model=""):
            assert get_stored_embed_model(reader) == "model-a"
            assert (
                reader.execute(
                    "SELECT * FROM knowledge_vectors ORDER BY knowledge_node_id"
                ).fetchall()
                == old_vectors
            )
            if len(self.embed_calls) + 1 == fail_call:
                raise RuntimeError("synthetic inference failure")
            return await super().embed(texts, model=model)

    reembed = runpy.run_path(str(ROOT / "scripts/hippo-re-embed.py"))["re_embed_nodes"]
    try:
        with pytest.raises(RuntimeError, match="synthetic inference failure"):
            await reembed(conn, MigrationClient(), "model-b", "command-b")
        assert not conn.in_transaction
        assert (
            conn.execute("SELECT * FROM knowledge_vectors ORDER BY knowledge_node_id").fetchall()
            == old_vectors
        )
        assert conn.execute("SELECT * FROM knowledge_nodes ORDER BY id").fetchall() == old_nodes
        assert conn.execute("SELECT * FROM knowledge_fts ORDER BY rowid").fetchall() == old_fts
        with pytest.raises(EmbedDriftError):
            check_embed_model_drift(conn, "model-b")
        client = MockInferenceClient()
        assert await reembed(conn, client, "model-b", "command-b") == 2
        assert get_stored_embed_model(reader) == "model-b"
        rows = reader.execute(
            "SELECT * FROM knowledge_vectors ORDER BY knowledge_node_id"
        ).fetchall()
        assert len(rows) == 2
        assert all(new[1] != old[1] and new[2] != old[2] for new, old in zip(rows, old_vectors))
        assert [call["model"] for call in client.embed_calls] == [
            "model-b",
            "command-b",
            "model-b",
            "command-b",
        ]
    finally:
        reader.close()


@pytest.mark.parametrize("fail_publication", [False, True])
async def test_reembed_cleans_legacy_tags_entities_and_orphans_atomically(
    index_db, fail_publication
):
    from hippo_brain.classification import NAMESPACE, OWNER_METADATA
    from hippo_brain.embeddings import search_similar
    from hippo_brain.vector_store import get_stored_embed_model, record_embed_model

    conn, _ = index_db
    for nid in (1, 2):
        conn.execute(
            "INSERT INTO knowledge_nodes(id,uuid,content,embed_text,tags) VALUES(?,?,?,?,?)",
            (
                nid,
                str(nid),
                json.dumps({"summary": SPAN, "entities": {"tools": [SPAN]}}),
                SPAN,
                json.dumps([SPAN, "safe"]),
            ),
        )
        insert_vectors(conn, nid, [0.1] * EMBED_DIM, [0.2] * EMBED_DIM)
    for eid, kind, name, canonical, metadata in [
        (1, "tool", SPAN, SPAN, None),
        (2, "tool", "cargo", "cargo", None),
        (3, "concept", NAMESPACE + "python", NAMESPACE + "python", OWNER_METADATA),
        (4, "tool", SPAN + "_ORPHAN", SPAN + "_ORPHAN", None),
        (5, "tool", SPAN, "<github-token>", None),
    ]:
        conn.execute(
            "INSERT INTO entities(id,type,name,canonical,metadata) VALUES(?,?,?,?,?)",
            (eid, kind, name, canonical, metadata),
        )
    conn.executemany(
        "INSERT INTO knowledge_node_entities VALUES(?,?)", [(1, 1), (2, 1), (1, 2), (2, 2), (1, 3)]
    )
    record_embed_model(conn, "model-a")
    if fail_publication:
        conn.execute(
            "CREATE TRIGGER fail_cleanup BEFORE UPDATE ON knowledge_nodes WHEN NEW.id=2 "
            "BEGIN SELECT RAISE(ABORT,'synthetic cleanup failure'); END"
        )
    conn.commit()
    tables = (
        "knowledge_nodes",
        "knowledge_vectors",
        "knowledge_fts",
        "entities",
        "knowledge_node_entities",
        "embed_model_meta",
    )
    before = {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in tables}
    reembed = runpy.run_path(str(ROOT / "scripts/hippo-re-embed.py"))["re_embed_nodes"]
    if fail_publication:
        with pytest.raises(sqlite3.IntegrityError, match="synthetic cleanup failure"):
            await reembed(conn, MockInferenceClient(), "model-b")
        assert {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in tables} == before
        assert not conn.in_transaction
        return
    assert await reembed(conn, MockInferenceClient(), "model-b") == 2
    assert get_stored_embed_model(conn) == "model-b"
    assert all(SPAN not in tags for (tags,) in conn.execute("SELECT tags FROM knowledge_nodes"))
    assert all(
        SPAN not in value
        for row in conn.execute("SELECT content,embed_text FROM knowledge_fts")
        for value in row
    )
    names = conn.execute("SELECT name,canonical FROM entities").fetchall()
    assert all(SPAN not in str(row) for row in names)
    assert ("<github-token>", "<github-token>") in names
    assert conn.execute("SELECT name FROM entities WHERE id=2").fetchone()[0] == "cargo"
    assert conn.execute("SELECT metadata FROM entities WHERE id=3").fetchone()[0] == OWNER_METADATA
    links = conn.execute(
        "SELECT knowledge_node_id,entity_id FROM knowledge_node_entities"
    ).fetchall()
    assert (1, 2) in links and (2, 2) in links and (1, 3) in links
    assert (1, 1) not in links and (2, 1) not in links
    assert all(
        SPAN not in json.dumps(hit["tags"]) for hit in search_similar(conn, [0.1] * EMBED_DIM)
    )


def ingest_and_shutdown(socket_path, envelope):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(5)
        peer.connect(str(socket_path))
        # IngestEvent is fire-and-forget. Ordered frames on one connection make
        # Shutdown acknowledge only after this synthetic event was accepted.
        for body in ({"type": "IngestEvent", "data": envelope}, {"type": "Shutdown"}):
            payload = json.dumps(body).encode()
            peer.sendall(struct.pack(">I", len(payload)) + payload)
        with peer.makefile("rb") as reader:
            size = struct.unpack(">I", reader.read(4))[0]
            return json.loads(reader.read(size))


@pytest.fixture
def captured_event():
    # Canonical mise run test builds this binary in its Rust integration phase.
    binary = ROOT / "target/debug/hippo"
    if not binary.exists():
        pytest.skip("Rust capture integration requires mise run build before Python-only tests")
    with tempfile.TemporaryDirectory(prefix="hipidx-", dir="/tmp") as directory:
        root = Path(directory)
        os.chmod(root, 0o700)
        config_dir = root / "config/hippo"
        config_dir.mkdir(parents=True)
        data = root / "data/hippo"
        (config_dir / "config.toml").write_text(
            f'[storage]\ndata_dir = "{data}"\n[daemon]\nflush_interval_ms = 10\n'
            "[brain]\nport = 0\n[telemetry]\nenabled = false\n"
        )
        env = {
            **os.environ,
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_DATA_HOME": str(root / "data"),
        }
        with (root / "capture.log").open("wb") as output:
            daemon = subprocess.Popen(
                [str(binary), "daemon", "run", "--bench"], env=env, stdout=output, stderr=output
            )
            socket_path = data / "daemon.sock"
            try:
                deadline = time.monotonic() + 10
                while not socket_path.exists():
                    assert daemon.poll() is None, (root / "capture.log").read_text()
                    assert time.monotonic() < deadline, "isolated capture daemon startup timed out"
                    time.sleep(0.01)
                envelope = {
                    "envelope_id": str(uuid.uuid4()),
                    "producer_version": 1,
                    "timestamp": "2026-10-08T00:00:00Z",
                    "payload": {
                        "type": "Shell",
                        "data": {
                            "session_id": str(uuid.uuid4()),
                            "command": "cargo test " + SPAN,
                            "exit_code": 0,
                            "duration_ms": 1,
                            "cwd": str(root),
                            "hostname": "synthetic",
                            "shell": "Zsh",
                            "stdout": None,
                            "stderr": None,
                            "env_snapshot": {},
                            "git_state": None,
                            "redaction_count": 0,
                        },
                    },
                }
                assert ingest_and_shutdown(socket_path, envelope)["type"] == "Ack"
                assert daemon.wait(timeout=10) == 0
                conn = open_conn(data / "hippo.db")
                conn.row_factory = sqlite3.Row
                try:
                    event = dict(conn.execute("SELECT * FROM events").fetchone())
                    assert event["command"] == "cargo test " + SPAN
                    yield conn, data / "hippo.db", event
                finally:
                    conn.close()
            finally:
                if daemon.poll() is None:
                    daemon.terminate()
                    daemon.wait(timeout=10)


async def test_real_synthetic_capture_to_fts_and_vector_probe(captured_event):
    """Proves exclusion in both inputs and a controlled KNN probe, not inversion resistance."""
    conn, path, event = captured_event
    client = MockInferenceClient()
    client.CANNED_RESPONSE = json.dumps(
        vars(result())
    )  # hostile model output reintroduces the span
    server = BrainServer(db_path=str(path), embedding_model="synthetic")
    server.client = client
    try:
        await server._enrich_shell_batches([[event]], conn)
        assert SPAN not in client.chat_calls[0]["messages"][1]["content"]
        assert all(SPAN not in str(call["texts"]) for call in client.embed_calls)
        (nid,) = conn.execute("SELECT id FROM knowledge_nodes").fetchone()
        assert all(
            SPAN not in field
            for field in conn.execute(
                "SELECT summary,embed_text,content FROM knowledge_fts WHERE rowid=?", (nid,)
            ).fetchone()
        )
        # A raw-vector positive control shows this probe detects an exact unfiltered signal.
        raw_vec = MockInferenceClient._deterministic_vector(SPAN, EMBED_DIM)
        conn.execute(
            "INSERT INTO knowledge_nodes(id,uuid,content,embed_text) VALUES(99,'control','{}','control')"
        )
        insert_vectors(conn, 99, raw_vec, raw_vec)
        conn.commit()
        for arm in ("vec_knowledge", "vec_command"):
            hits = knn_search(conn, raw_vec, column=arm, limit=2)
            assert hits[0]["knowledge_node_id"] == 99
            assert hits[0]["distance"] == pytest.approx(0, abs=1e-6)
            assert next(hit for hit in hits if hit["knowledge_node_id"] == nid)["distance"] > 0.1
        assert conn.execute("SELECT command FROM events").fetchone()[0] == "cargo test " + SPAN
    finally:
        server.close()
