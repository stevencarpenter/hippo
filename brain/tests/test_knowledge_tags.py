"""Topic updates preserve source identity and roll back on vector failure."""

import json
import asyncio
from contextlib import closing
from pathlib import Path

import httpx

import pytest

from hippo_brain import vector_store
from hippo_brain.bench import knowledge_tags as tags
from hippo_brain.enrichment import upsert_entities


def test_topic_create_update_is_atomic_and_preserves_unrelated_data(tmp_db):
    _, path = tmp_db
    conn = vector_store.open_conn(path)
    original = {
        "tags": ["original"],
        "content": {"summary": "A node", "tags": ["original"]},
        "embed_text": "Original text",
    }
    try:
        conn.execute(
            "INSERT INTO knowledge_nodes(id,uuid,content,embed_text,tags) VALUES(1,'stable',?,?,?)",
            (json.dumps(original["content"]), original["embed_text"], json.dumps(original["tags"])),
        )
        upsert_entities(conn, 1, {"tools": ["original-tool"]}, {"tools": "tool"}, 1)
        vector_store.insert_vectors(conn, 1, [0.1] * 768, [0.2] * 768)
        conn.execute(
            "INSERT INTO sessions(id,start_time,shell,hostname,username) VALUES(1,0,'zsh','host','user')"
        )
        conn.execute(
            "INSERT INTO events(id,session_id,timestamp,command,duration_ms,cwd,hostname,shell) VALUES(1,1,0,'ls',1,'/tmp','host','zsh')"
        )
        conn.execute("INSERT INTO knowledge_node_events VALUES(1,1)")
        # This first update commits the newly created node and its vectors together.
        tags.update_topics(conn, 1, ["observability"], original, [0.3] * 768)
        command = conn.execute(
            "SELECT vec_command FROM knowledge_vectors WHERE knowledge_node_id=1"
        ).fetchone()[0]
        tags.update_topics(conn, 1, ["database-storage"], original, [0.4] * 768)
        tags.update_topics(conn, 1, ["database-storage"], original, [0.4] * 768)
        row = conn.execute("SELECT uuid,content,tags FROM knowledge_nodes WHERE id=1").fetchone()
        assert row[0] == "stable"
        assert json.loads(row[1])["tags"] == json.loads(row[2]) == ["original", "database-storage"]
        assert conn.execute("SELECT * FROM knowledge_node_events").fetchall() == [(1, 1)]
        linked = {
            r[0]
            for r in conn.execute(
                "SELECT e.name FROM entities e JOIN knowledge_node_entities l ON l.entity_id=e.id"
            )
        }
        assert linked == {"original-tool", tags.PREFIX + "database-storage"}
        assert (
            conn.execute(
                "SELECT vec_command FROM knowledge_vectors WHERE knowledge_node_id=1"
            ).fetchone()[0]
            == command
        )
        assert (
            conn.execute(
                "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH 'observability'"
            ).fetchall()
            == []
        )
        assert conn.execute(
            "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH 'database'"
        ).fetchall() == [(1,)]
        before = conn.execute("SELECT * FROM knowledge_nodes").fetchall()
        vectors = conn.execute("SELECT * FROM knowledge_vectors").fetchall()
        with pytest.raises(ValueError, match="768 finite"):
            tags.update_topics(conn, 1, ["observability"], original, [float("nan")] * 768)
        assert conn.execute("SELECT * FROM knowledge_nodes").fetchall() == before
        # Failure after node/FTS writes must roll those writes back too.
        vector_store.delete_vectors(conn, 1)
        conn.commit()
        with pytest.raises(ValueError, match="missing command vector"):
            tags.update_topics(conn, 1, ["observability"], original, [0.5] * 768)
        assert conn.execute("SELECT * FROM knowledge_nodes").fetchall() == before
        conn.execute("INSERT INTO knowledge_vectors VALUES(?,?,?)", vectors[0])
        conn.commit()
        tags.update_topics(conn, 1, [], original, [0.1] * 768)
        assert (
            conn.execute("SELECT embed_text FROM knowledge_nodes").fetchone()[0]
            == original["embed_text"]
        )
        assert conn.execute(
            "SELECT e.name FROM entities e JOIN knowledge_node_entities l ON l.entity_id=e.id"
        ).fetchall() == [("original-tool",)]
    finally:
        conn.close()


def test_classification_contract_rules_and_external_paths(tmp_path):
    taxonomy = {"observability": {"description": "Monitoring", "terms": ["metrics"]}}
    assert tags.rules("metrics collection", taxonomy) == {"observability": ["metrics"]}
    assert tags.rules("biometrics", taxonomy) == {"observability": []}
    request = tags.request_for({"summary": "Metrics", "detail": ""}, taxonomy)
    assert request["questions"] == tags.ClassificationRecipe(taxonomy).questions
    response = {"answers": {"observability": {"type": "noul", "noul": 0.8}}}
    assert tags.parse(response, taxonomy) == {"observability": 0.8}
    for value in (True, None, float("nan"), 1.01):
        response["answers"]["observability"]["noul"] = value
        with pytest.raises(ValueError):
            tags.parse(response, taxonomy)
    for answers in ({}, {"unexpected": {"type": "noul", "noul": 0.8}}):
        with pytest.raises(ValueError, match="missing or unexpected"):
            tags.parse({"answers": answers}, taxonomy)
    with pytest.raises(ValueError, match="external"):
        tags.workspace(tmp_path)
    assert (
        tags.label_metrics({"a": {"x"}, "b": {"x"}}, {"a": {"x"}, "b": set()})[
            "shared_topic_pair_precision"
        ]
        == 0
    )


def test_shared_topic_links_require_the_same_reference_topic():
    metrics = tags.label_metrics({"a": {"x"}, "b": {"x"}}, {"a": {"y"}, "b": {"y"}})
    assert metrics["shared_topic_pair_precision"] == 1
    assert metrics["shared_topic_link_precision"] == 0


def test_metadata_clone_reuses_current_fingerprint_and_preserves_all_original_data(
    tmp_db, tmp_path
):
    from hippo_brain.classification import default_recipe, node_input

    _, source = tmp_db
    conn = vector_store.open_conn(source)
    conn.execute(
        "INSERT INTO knowledge_nodes(id,uuid,content,embed_text,tags) VALUES(1,'a',?,?,?)",
        (
            json.dumps({"summary": "Metrics collection", "tags": ["original"]}),
            "metrics",
            '["original"]',
        ),
    )
    upsert_entities(conn, 1, {"tools": ["original-tool"]}, {"tools": "tool"}, 1)
    vector_store.insert_vectors(conn, 1, [0.1] * 768, [0.2] * 768)
    conn.commit()
    recipe = default_recipe()
    _, _, fingerprint = node_input(conn, 1)
    original = conn.execute("SELECT * FROM knowledge_nodes").fetchall()
    vectors = conn.execute("SELECT * FROM knowledge_vectors").fetchall()
    conn.close()
    predictions = tmp_path / "predictions.json"
    rows = [
        {
            "node_uuid": "a",
            "input_hash": fingerprint,
            "recipe_hash": recipe.recipe_hash,
            "response": {
                "model": recipe.model_id,
                "answers": {
                    t: {"type": "noul", "noul": 0.95 if t == "observability" else 0.1}
                    for t in recipe.topics
                },
            },
        }
    ]
    predictions.write_text(json.dumps(rows))
    out = tmp_path / "metadata.sqlite"
    result = tags.apply_membership_clone(source, predictions, out)
    assert result["applied"] == 1
    assert all(result["preservation"].values())
    conn = vector_store.open_conn(out)
    try:
        assert conn.execute("SELECT * FROM knowledge_nodes").fetchall() == original
        assert conn.execute("SELECT * FROM knowledge_vectors").fetchall() == vectors
        assert conn.execute(
            "SELECT status,accepted_topics_json FROM knowledge_node_classifications"
        ).fetchone() == ("ready", '["observability"]')
        assert (
            conn.execute(
                "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH 'observability'"
            ).fetchall()
            == []
        )
    finally:
        conn.close()
    rows[0]["input_hash"] = "historical-summary-only-hash"
    predictions.write_text(json.dumps(rows))
    with pytest.raises(ValueError, match="identity/input/recipe"):
        tags.apply_membership_clone(source, predictions, tmp_path / "invalid.sqlite")


def test_nonfinite_api_response_is_recorded_without_aborting_run(tmp_path, monkeypatch):
    import httpx

    root = tmp_path / "experiment"
    root.mkdir()
    monkeypatch.setattr(tags, "decision_root", lambda: tmp_path)
    monkeypatch.setenv("TYPESAFE_API_KEY", "test-token")
    state = {"summary": "Metrics", "detail": ""}
    corpus = [
        {"id": str(i), "node_id": i, "state": state, "input_hash": tags.digest(state)}
        for i in range(2)
    ]
    taxonomy = {"observability": {"description": "Monitoring", "terms": ["metrics"]}}
    tags.write_json(root / "knowledge_topics.json", taxonomy)
    for name, value in {
        "classification-corpus": corpus,
        "classification-label-inputs": corpus,
        "classification-reference": {"labels": [{"id": c["id"], "topics": []} for c in corpus]},
        "classification-protocol": {
            "corpus_hash": tags.digest(corpus),
            "reference_sample_hash": tags.digest(corpus),
            "taxonomy_sha256": tags.sha256(root / "knowledge_topics.json"),
            "primary_threshold": 0.8,
        },
        "rerank-report": {},
    }.items():
        tags.write_json(root / (name + ".json"), value)

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            return httpx.Response(
                200,
                content=b'{"answers":{"observability":{"type":"noul","noul":NaN}}}',
                request=httpx.Request("POST", url),
            )

    monkeypatch.setattr(tags.httpx, "AsyncClient", Client)
    run = asyncio.run(tags.classify(root, 2))
    assert tags.load(run / "completion.json") == {"nodes": 2, "errors": 2}
    assert all(r["status"] == "error" for r in tags.validated_rows(root, run).values())


@pytest.fixture
def current_corpus(tmp_db, tmp_path):
    from hippo_brain.bench import knowledge_corpus
    from hippo_brain.classification import load_recipe, node_input

    _, source = tmp_db
    recipe_path = tmp_path / "recipe.json"
    recipe_path.write_text(
        json.dumps(
            {
                "taxonomy_version": "test-reviewed-v2",
                "thresholds": {"observability": 0.9},
                "publish_topics": ["observability"],
                "max_topics": 1,
            }
        )
    )
    recipe = load_recipe(recipe_path)
    rows = []
    with closing(vector_store.open_conn(source)) as conn:
        conn.execute("INSERT INTO embed_model_meta VALUES(1,'test-embedding')")
        conn.execute(
            "INSERT INTO sessions(id,start_time,shell,hostname,username) VALUES(1,0,'zsh','host','user')"
        )
        for number, uid in enumerate(("first", "second"), 1):
            conn.execute(
                "INSERT INTO knowledge_nodes(id,uuid,content,embed_text,tags) VALUES(?,?,?,?,?)",
                (
                    number,
                    uid,
                    json.dumps({"summary": "Metrics " + uid, "tags": ["original"]}),
                    "Metrics detail " + uid,
                    '["metrics"]',
                ),
            )
            conn.execute(
                "INSERT INTO events(id,session_id,timestamp,command,duration_ms,cwd,hostname,shell) VALUES(?,1,1,'metrics',1,'/tmp','host','zsh')",
                (number,),
            )
            conn.execute("INSERT INTO knowledge_node_events VALUES(?,?)", (number, number))
            vector_store.insert_vectors(conn, number, [number / 10] * 768, [number / 5] * 768)
            upsert_entities(conn, number, {"tools": ["original-tool"]}, {"tools": "tool"}, 1)
            identity = node_input(conn, number)
            probabilities = {t: 0.95 if t == "observability" else 0.1 for t in recipe.topics}
            rows.append(
                {
                    "node_uuid": uid,
                    "input_hash": identity[2],
                    "recipe_hash": recipe.recipe_hash,
                    "backend": "jev",
                    "model": recipe.model_id,
                    "status": "ok",
                    "probabilities": probabilities,
                    "accepted_topics": ["observability"],
                    "response": {
                        "model": "jev-" + recipe.model_id,
                        "answers": {
                            t: {"type": "noul", "noul": p} for t, p in probabilities.items()
                        },
                    },
                }
            )
        conn.commit()
    root = tmp_path / "current-corpus"
    knowledge_corpus.prepare(source, root, node_target=2, query_target=1)
    predictions = tmp_path / "predictions-current.json"
    predictions.write_text(json.dumps(rows))
    return root, predictions, recipe_path


@pytest.mark.parametrize("arm", ["fts", "vectors"])
def test_projection_isolates_channels_and_proves_real_fts_rollback(
    current_corpus, tmp_path, monkeypatch, arm
):
    root, predictions, recipe = current_corpus
    source = root / "baseline.sqlite"
    before_sha = tags.sha256(source)
    calls = []
    original_client = httpx.Client

    def embed(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["model"] == "test-embedding"
        assert "Technical topics: observability" in payload["input"][0]
        return httpx.Response(
            200, json={"model": "test-embedding", "data": [{"index": 0, "embedding": [0.75] * 768}]}
        )

    monkeypatch.setattr(
        tags.httpx,
        "Client",
        lambda **kw: original_client(transport=httpx.MockTransport(embed), **kw),
    )
    out = tmp_path / (arm + ".sqlite")
    result = tags.apply_projection_clone(
        source,
        predictions,
        out,
        arm=arm,
        embedding_model="test-embedding",
        base_url="http://fake/v1",
        recipe_path=recipe,
    )
    assert all(result["preservation"].values())
    assert all(result["rollback"].values())
    assert tags.sha256(source) == before_sha
    assert len(calls) == (2 if arm == "vectors" else 0)
    with (
        closing(tags.open_readonly(source)) as original,
        closing(tags.open_readonly(out)) as projected,
        closing(tags.open_readonly(out.with_suffix(".rollback.sqlite"))) as rollback,
    ):
        original_nodes = original.execute("SELECT * FROM knowledge_nodes ORDER BY id").fetchall()
        original_vectors = original.execute(
            "SELECT * FROM knowledge_vectors ORDER BY knowledge_node_id"
        ).fetchall()
        assert (
            rollback.execute("SELECT * FROM knowledge_nodes ORDER BY id").fetchall()
            == original_nodes
        )
        assert (
            rollback.execute(
                "SELECT * FROM knowledge_vectors ORDER BY knowledge_node_id"
            ).fetchall()
            == original_vectors
        )
        assert (
            rollback.execute(
                "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH 'observability'"
            ).fetchall()
            == []
        )
        matches = projected.execute(
            "SELECT rowid FROM knowledge_fts WHERE knowledge_fts MATCH 'observability'"
        ).fetchall()
        if arm == "fts":
            assert matches == [(1,), (2,)]
            assert (
                projected.execute(
                    "SELECT * FROM knowledge_vectors ORDER BY knowledge_node_id"
                ).fetchall()
                == original_vectors
            )
        else:
            assert matches == []
            assert (
                projected.execute("SELECT * FROM knowledge_nodes ORDER BY id").fetchall()
                == original_nodes
            )
            changed = projected.execute(
                "SELECT * FROM knowledge_vectors ORDER BY knowledge_node_id"
            ).fetchall()
            assert all(
                new[1] != old[1] and new[2] == old[2]
                for new, old in zip(changed, original_vectors, strict=True)
            )


def test_vector_projection_rolls_back_earlier_node_on_publication_failure(
    current_corpus, tmp_path, monkeypatch
):
    root, predictions, recipe = current_corpus
    original_client = httpx.Client
    monkeypatch.setattr(
        tags.httpx,
        "Client",
        lambda **kw: original_client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "model": "test-embedding",
                        "data": [{"index": 0, "embedding": [0.75] * 768}],
                    },
                )
            ),
            **kw,
        ),
    )
    delete = vector_store.delete_vectors

    def fail_second(conn, node_id):
        if node_id == 2:
            raise ValueError("injected second publication failure")
        delete(conn, node_id)

    monkeypatch.setattr(vector_store, "delete_vectors", fail_second)
    out = tmp_path / "failed.sqlite"
    with pytest.raises(ValueError, match="second publication"):
        tags.apply_projection_clone(
            root / "baseline.sqlite",
            predictions,
            out,
            arm="vectors",
            embedding_model="test-embedding",
            base_url="http://fake/v1",
            recipe_path=recipe,
        )
    assert not out.with_suffix(".completion.json").exists()
    with (
        closing(tags.open_readonly(root / "baseline.sqlite")) as original,
        closing(tags.open_readonly(out)) as failed,
    ):
        for table in ("knowledge_nodes", "knowledge_vectors"):
            assert tags._table_hash(original, table) == tags._table_hash(failed, table)


async def test_current_corpus_classifiers_checkpoint_native_and_fresh_local_budget(
    current_corpus, tmp_path, monkeypatch
):
    from hippo_brain.bench.knowledge_corpus import read_records
    from hippo_brain.classification import load_recipe

    root, _, recipe_path = current_corpus
    recipe = load_recipe(recipe_path)
    for backend in ("rules", "stored"):
        out = tmp_path / backend
        first = await tags.classify_corpus(
            root, out, backend=backend, recipe_path=recipe_path, max_requests=0, max_tokens=0
        )
        assert first["complete"] and first["requests"] == first["budget_tokens"] == 0
        assert (
            await tags.classify_corpus(
                root, out, backend=backend, recipe_path=recipe_path, max_requests=0, max_tokens=0
            )
            == first
        )
        assert all(
            r["accepted_topics"] == ["observability"]
            for r in read_records(out / "predictions.jsonl")
        )
        clone = tmp_path / (backend + "-metadata.sqlite")
        projection = tags.apply_membership_clone(
            root / "baseline.sqlite", out / "predictions.jsonl", clone, recipe_path=recipe_path
        )
        assert projection["applied"] == 2
        assert projection["classifier_provenance"] == [(backend, backend)]
    calls = []
    original_client = httpx.AsyncClient

    async def answer(request):
        payload = json.loads(request.content)
        calls.append(payload)
        state = json.loads(payload["messages"][1]["content"])
        assert set(state) == {"state", "topics"}
        assert "source_facts" in state["state"]
        assert payload["model"] == "pinned-local" and payload["max_tokens"] == 512
        return httpx.Response(
            200,
            json={
                "model": "pinned-local",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {t: 0.95 if t == "observability" else 0.1 for t in recipe.topics}
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            },
        )

    monkeypatch.setattr(
        tags.httpx,
        "AsyncClient",
        lambda **kw: original_client(transport=httpx.MockTransport(answer), **kw),
    )
    kwargs = dict(
        backend="local", model="pinned-local", base_url="http://fake/v1", recipe_path=recipe_path
    )
    for label, budget in (("no-calls", {"max_requests": 0}), ("no-tokens", {"max_tokens": 1})):
        result = await tags.classify_corpus(root, tmp_path / label, **kwargs, **budget)
        assert not result["complete"] and result["requests"] == 0
    assert calls == []
    out = tmp_path / "local"
    result = await tags.classify_corpus(root, out, **kwargs, max_requests=1)
    assert result["requests"] == result["terminal_nodes"] == 1 and not result["complete"]
    assert await tags.classify_corpus(root, out, **kwargs, max_requests=1) == result
    assert len(calls) == 1
    row = read_records(out / "predictions.jsonl")[0]
    assert row["usage"] == {"input_tokens": 10, "output_tokens": 20}
    assert row["budget_tokens"] >= len(tags.canonical(calls[0]).encode()) + 512
    assert row["recipe_hash"] == recipe.recipe_hash
    assert tags.load(out / "manifest.json")["recipe"]["taxonomy_version"] == "test-reviewed-v2"
    with pytest.raises(ValueError, match="resume configuration"):
        await tags.classify_corpus(
            root, out, **(kwargs | {"model": "changed-local"}), max_requests=1
        )


@pytest.mark.parametrize("bad", ["model", "probability", "missing-topic", "nonfinite"])
async def test_fresh_local_rejects_invalid_response_and_keeps_unknown_usage(
    current_corpus, tmp_path, monkeypatch, bad
):
    from hippo_brain.bench.knowledge_corpus import read_records
    from hippo_brain.classification import load_recipe

    root, _, recipe = current_corpus
    probabilities = {t: 0.5 for t in load_recipe(recipe).topics}
    if bad == "probability":
        probabilities["observability"] = True
    elif bad == "missing-topic":
        probabilities.pop("observability")
    response = {
        "model": "wrong" if bad == "model" else "local",
        "choices": [{"message": {"content": json.dumps(probabilities)}}],
    }
    if bad == "nonfinite":
        response["usage"] = {"prompt_tokens": float("nan"), "completion_tokens": 1}
    original = httpx.AsyncClient
    monkeypatch.setattr(
        tags.httpx,
        "AsyncClient",
        lambda **kw: original(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, content=json.dumps(response).encode())
            ),
            **kw,
        ),
    )
    out = tmp_path / "invalid-local"
    result = await tags.classify_corpus(
        root,
        out,
        backend="local",
        model="local",
        base_url="http://fake/v1",
        recipe_path=recipe,
        max_requests=1,
    )
    assert result["errors"] == 1 and result["requests"] == 1
    row = read_records(out / "predictions.jsonl")[0]
    assert row["status"] == "error" and row["usage"] is None and row["budget_tokens"] > 0


async def test_cancelled_classifier_reserves_budget_and_never_reissues_request(
    current_corpus, tmp_path, monkeypatch
):
    from hippo_brain.bench.knowledge_corpus import read_records

    root, _, recipe = current_corpus
    calls = []
    original = httpx.AsyncClient

    def cancel(request):
        calls.append(request)
        assert (out / "inflight.json").exists()
        raise asyncio.CancelledError

    monkeypatch.setattr(
        tags.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(cancel), **kw),
    )
    out = tmp_path / "interrupted"
    kwargs = dict(
        backend="local",
        model="local",
        base_url="http://fake/v1",
        max_requests=1,
        recipe_path=recipe,
    )
    with pytest.raises(asyncio.CancelledError):
        await tags.classify_corpus(root, out, **kwargs)
    result = await tags.classify_corpus(root, out, **kwargs)
    assert result["requests"] == result["errors"] == 1 and len(calls) == 1
    assert not result["complete"] and not (out / "inflight.json").exists()
    row = read_records(out / "predictions.jsonl")[0]
    assert row["error"] == "InterruptedRequest" and row["usage"] is None
    assert row["usage_missing_reason"] == "interrupted_request_usage_unknown"
    row["accepted_topics"] = ["tampered"]
    (out / "predictions.jsonl").write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="resume contains"):
        await tags.classify_corpus(root, out, **kwargs)


def test_current_benchmark_cli_exposes_recipe_and_independent_arms(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["knowledge_tags", "--help"])
    with pytest.raises(SystemExit) as result:
        tags.main()
    assert result.value.code == 0
    help_text = capsys.readouterr().out
    for flag in (
        "classify-corpus",
        "--recipe",
        "--max-requests",
        "--max-tokens",
        "--arm {fts,vectors}",
    ):
        assert flag in help_text


async def test_current_jev_corpus_uses_wire_model_and_clones_current_recipe(
    current_corpus, tmp_path, monkeypatch
):
    from hippo_brain.bench.knowledge_corpus import read_records

    root, _, recipe = current_corpus
    calls = []
    original = httpx.AsyncClient
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-only")

    def answer(request):
        payload = json.loads(request.content)
        calls.append(payload)
        assert payload["model"] == "jev-1.13.0"
        assert len(payload["questions"]) == 14
        return httpx.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    t: {"type": "noul", "noul": 0.95 if t == "observability" else 0.1}
                    for t in payload["questions"]
                },
                "usage": {"input_tokens": 20, "output_tokens": 10},
            },
        )

    monkeypatch.setattr(
        tags.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(answer), **kw),
    )
    out = tmp_path / "jev-current"
    result = await tags.classify_corpus(
        root, out, backend="jev", recipe_path=recipe, max_requests=1
    )
    assert result["errors"] == 0 and result["requests"] == result["network_calls"] == 1
    row = read_records(out / "predictions.jsonl")[0]
    assert row["model"] == "1.13.0"
    assert row["response"]["model"] == "jev-1.13.0"
    assert row["request_hash"] == tags.digest(calls[0])
    assert row["usage"] == {"input_tokens": 20, "output_tokens": 10}
    manifest = tags.load(out / "manifest.json")
    for filename, checksum in manifest["implementation_sha256"].items():
        assert tags.sha256(out / "implementation" / filename) == checksum
    clone = tmp_path / "jev-clone.sqlite"
    publication = tags.apply_membership_clone(
        root / "baseline.sqlite", out / "predictions.jsonl", clone, recipe_path=recipe
    )
    assert publication["applied"] == 1 and all(publication["preservation"].values())
    with pytest.raises(ValueError, match="identity/input/recipe"):
        tags.apply_membership_clone(
            root / "baseline.sqlite", out / "predictions.jsonl", tmp_path / "wrong-recipe.sqlite"
        )


@pytest.mark.parametrize("failure", ["different-model", "invalid-vector"])
def test_vector_preparation_failure_leaves_clone_channels_unchanged(
    current_corpus, tmp_path, monkeypatch, failure
):
    root, predictions, recipe = current_corpus
    original = httpx.Client
    response = {
        "model": "different" if failure == "different-model" else "test-embedding",
        "data": [{"index": 0, "embedding": [0.5] * (2 if failure == "invalid-vector" else 768)}],
    }
    monkeypatch.setattr(
        tags.httpx,
        "Client",
        lambda **kw: original(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)), **kw
        ),
    )
    out = tmp_path / "invalid-vector.sqlite"
    with pytest.raises(ValueError, match="model differs|768 finite"):
        tags.apply_projection_clone(
            root / "baseline.sqlite",
            predictions,
            out,
            arm="vectors",
            embedding_model="test-embedding",
            base_url="http://fake/v1",
            recipe_path=recipe,
        )
    assert not out.with_suffix(".completion.json").exists()
    with (
        closing(tags.open_readonly(root / "baseline.sqlite")) as baseline,
        closing(tags.open_readonly(out)) as failed,
    ):
        for table in ("knowledge_nodes", "knowledge_vectors"):
            assert tags._table_hash(baseline, table) == tags._table_hash(failed, table)
        assert tags._fts_hash(baseline) == tags._fts_hash(failed)


@pytest.mark.parametrize(
    "artifact",
    [
        "run.lock",
        "manifest.json",
        "predictions.jsonl",
        "inflight.json",
        "completion.json",
        "implementation",
        "implementation/bench",
        "implementation/jev.py",
    ],
)
async def test_classifier_rejects_artifact_symlinks_before_any_external_write(
    current_corpus, tmp_path, monkeypatch, artifact
):
    root, _, recipe = current_corpus
    production = tmp_path / "fake-xdg" / "hippo"
    production.mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(production.parent))
    protected = production / "protected.jsonl"
    protected.write_bytes(b"protected production bytes\n")
    before = protected.stat()
    out = tmp_path / "symlink-run"
    out.mkdir()
    link = out / artifact
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(
        production if artifact in ("implementation", "implementation/bench") else protected
    )
    with pytest.raises(ValueError, match="symlink"):
        await tags.classify_corpus(root, out, backend="rules", recipe_path=recipe)
    assert protected.read_bytes() == b"protected production bytes\n"
    assert protected.stat().st_mtime_ns == before.st_mtime_ns
    assert protected.stat().st_mode == before.st_mode
    assert sorted(path.name for path in production.iterdir()) == ["protected.jsonl"]


async def test_classifier_resume_rejects_symlinked_checkpoint(
    current_corpus, tmp_path, monkeypatch
):
    root, _, recipe = current_corpus
    out = tmp_path / "completed-run"
    await tags.classify_corpus(root, out, backend="rules", recipe_path=recipe)
    production = tmp_path / "fake-xdg" / "hippo"
    production.mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(production.parent))
    target = production / "protected.jsonl"
    target.write_bytes((out / "predictions.jsonl").read_bytes())
    original = target.read_bytes()
    (out / "predictions.jsonl").unlink()
    (out / "predictions.jsonl").symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        await tags.classify_corpus(root, out, backend="rules", recipe_path=recipe)
    assert target.read_bytes() == original


@pytest.mark.parametrize(
    "selected_status,unselected_status",
    [
        ("ready", "pending"),
        ("ready", "processing"),
        ("ready", "ready"),
        ("pending", "pending"),
        ("failed", "ready"),
    ],
)
def test_membership_clone_replays_only_selected_existing_classifier_state(
    current_corpus, tmp_path, selected_status, unselected_status
):
    from hippo_brain.classification import apply_result, claim, enqueue_node, load_recipe

    root, predictions, recipe_path = current_corpus
    source = root / "baseline.sqlite"
    recipe = load_recipe(recipe_path)
    selected_predictions = [row for row in tags.load(predictions) if row["node_uuid"] == "first"]
    predictions.write_text(json.dumps(selected_predictions))
    with closing(vector_store.open_conn(source)) as conn:
        ddl = (
            Path(tags.__file__).resolve().parents[4]
            / "crates/hippo-core/src/schema/classification.sql"
        )
        conn.executescript(ddl.read_text())
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for node_id in (1, 2):
                enqueue_node(conn, node_id, enabled=True, recipe=recipe, now_ms=1)
            for work in claim(conn, recipe=recipe, limit=2, now_ms=2):
                response = {
                    "model": recipe.model_id,
                    "answers": {
                        topic: {
                            "type": "noul",
                            "noul": 0.95 if work.node_id == 2 and topic == "observability" else 0.1,
                        }
                        for topic in recipe.topics
                    },
                }
                assert apply_result(conn, work, response, recipe=recipe, now_ms=2)
            for node_id, status in ((1, selected_status), (2, unselected_status)):
                conn.execute(
                    "UPDATE knowledge_node_classifications SET status=?,attempts=3,lease_expires_at=3,error='preserve unrelated state' WHERE node_id=?",
                    (status, node_id),
                )
        original_states = conn.execute(
            "SELECT * FROM knowledge_node_classifications ORDER BY node_id"
        ).fetchall()
        unrelated_links = conn.execute(
            "SELECT * FROM knowledge_node_entities WHERE knowledge_node_id=2 ORDER BY entity_id"
        ).fetchall()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    original_sha = tags.sha256(source)
    out = tmp_path / "selected-clone.sqlite"
    result = tags.apply_membership_clone(source, predictions, out, recipe_path=recipe_path)
    assert result["applied"] == 1 and all(result["preservation"].values())
    assert tags.sha256(source) == original_sha
    with closing(tags.open_readonly(out)) as conn, closing(tags.open_readonly(source)) as original:
        assert (
            original.execute(
                "SELECT * FROM knowledge_node_classifications ORDER BY node_id"
            ).fetchall()
            == original_states
        )
        assert (
            conn.execute("SELECT * FROM knowledge_node_classifications WHERE node_id=2").fetchone()
            == original_states[1]
        )
        assert (
            conn.execute(
                "SELECT * FROM knowledge_node_entities WHERE knowledge_node_id=2 ORDER BY entity_id"
            ).fetchall()
            == unrelated_links
        )
        assert conn.execute(
            "SELECT status,attempts,accepted_topics_json FROM knowledge_node_classifications WHERE node_id=1"
        ).fetchone() == ("ready", 1, '["observability"]')
