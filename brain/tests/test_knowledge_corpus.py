"""Corpus lineage, honest unknowns, grouped metrics, and offline budget replay."""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pytest

from hippo_brain.bench import knowledge_corpus as corpus
from hippo_brain.evaluation import graded_retrieval_metrics, load_questions, paired_family_interval


async def test_live_harness_honors_filters_and_rejects_historical_evidence(monkeypatch):
    from hippo_brain import evaluation

    seen = []
    monkeypatch.setattr(
        evaluation, "retrieval_search", lambda *args, **kwargs: seen.append(kwargs["filters"]) or []
    )
    conn = sqlite3.connect(":memory:")
    try:
        kwargs = {
            "conn": conn,
            "inference_client": None,
            "embedding_model": "",
            "query_model": "",
            "run_synthesis": False,
            "mode": "hybrid",
            "limit": 10,
            "run_judge": False,
        }
        current = await evaluation.score_question(
            evaluation.Question("q", "text", filters={"source": "shell"}), **kwargs
        )
        assert current.error is None
        assert seen[0].source == "shell"
        historical = await evaluation.score_question(
            evaluation.Question("q", "text", as_of_ms=100), **kwargs
        )
        assert "frozen knowledge-corpus replay" in historical.error
        assert len(seen) == 1
    finally:
        conn.close()


def write(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value))
    return path


@pytest.fixture
def frozen(tmp_path: Path):
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as conn:
        conn.executescript("""
            CREATE TABLE knowledge_nodes (
                id INTEGER PRIMARY KEY, uuid TEXT, content TEXT, embed_text TEXT,
                node_type TEXT DEFAULT 'observation', outcome TEXT, tags TEXT DEFAULT '[]',
                created_at INTEGER, updated_at INTEGER
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY, session_id INTEGER, timestamp INTEGER,
                command TEXT, stdout TEXT, duration_ms INTEGER, source_kind TEXT, probe_tag TEXT
            );
            CREATE TABLE knowledge_node_events (knowledge_node_id INTEGER, event_id INTEGER);
            INSERT INTO events VALUES (1,1,100,'cargo test','passed',100,'shell',NULL);
            INSERT INTO events VALUES (2,1,101,'cargo build','done',100,'shell',NULL);
            INSERT INTO events VALUES (3,2,102,'python main.py','result',100,'shell',NULL);
            INSERT INTO events VALUES (4,1,103,'cargo check','missing link',100,'shell',NULL);
            INSERT INTO events VALUES (5,3,104,'probe','synthetic',100,'shell','probe');
            INSERT INTO knowledge_node_events VALUES (1,1),(2,2),(3,3),(4,5);
        """)
        for node_id, uid in enumerate(("a", "b", "c", "probe"), 1):
            conn.execute(
                "INSERT INTO knowledge_nodes(id,uuid,content,embed_text,created_at,updated_at) VALUES (?,?,?,?,100,100)",
                (node_id, uid, json.dumps({"summary": "summary " + uid}), "detail " + uid),
            )
    questions = write(
        tmp_path / "qa.json",
        [
            {
                "qa_id": "linked",
                "question": "Which test passed?",
                "golden_event_id": "shell-1",
                "source_filter": "shell",
            },
            {
                "qa_id": "gap",
                "question": "Where is the missing check?",
                "golden_event_id": "shell-4",
                "source_filter": "shell",
            },
        ],
    )
    root = tmp_path / "corpus"
    manifest = corpus.prepare(source, root, questions=questions, query_target=20, node_target=3)
    return root, manifest


def test_frozen_full_snapshot_preserves_gaps_and_groups_siblings(frozen, tmp_path):
    root, manifest = frozen
    loaded, questions, nodes = corpus.load_frozen(root)
    assert loaded == manifest
    assert manifest["node_count"] == 4
    assert manifest["actual_questions"] == 2
    assert manifest["classification_nodes"] == 3
    assert manifest["family_audit_human"] is False
    assert nodes["a"]["split"] == nodes["b"]["split"] == questions[0]["split"]
    assert questions[1]["coverage_gap_reason"] == "node_missing"
    assert questions[1]["source_eligible"] is True
    assert questions[1]["expected_answerability"] == "answerable"
    assert "probe" not in {
        n["node_uuid"] for n in corpus.read_records(root / "classification-review.json")
    }
    assert all(not q["question"] for q in corpus.read_records(root / "query-authoring.json"))
    with pytest.raises(FileExistsError):
        corpus.prepare(tmp_path / "source.sqlite", root)
    (root / "questions.json").write_text("[]")
    with pytest.raises(ValueError, match="changed"):
        corpus.load_frozen(root)


def test_cross_source_question_merges_families_before_splitting(tmp_path):
    source = tmp_path / "cross.sqlite"
    with sqlite3.connect(source) as conn:
        conn.executescript("""
            CREATE TABLE knowledge_nodes(id INTEGER, uuid TEXT, content TEXT, embed_text TEXT,
              node_type TEXT, outcome TEXT, created_at INTEGER, updated_at INTEGER);
            CREATE TABLE events(id INTEGER,session_id INTEGER,command TEXT,stdout TEXT,duration_ms INTEGER);
            CREATE TABLE knowledge_node_events(knowledge_node_id INTEGER,event_id INTEGER);
            INSERT INTO events VALUES(1,100,'compile','ok',100),(2,200,'run','ok',100);
            INSERT INTO knowledge_nodes VALUES(1,'a','{}','first','observation',NULL,1,1),
              (2,'b','{}','second','observation',NULL,1,1);
            INSERT INTO knowledge_node_events VALUES(1,1),(2,2);
        """)
    queries = write(
        tmp_path / "cross.json",
        [{"id": "cross", "question": "Compare the tasks", "source_keys": ["shell-1", "shell-2"]}],
    )
    root = tmp_path / "corpus"
    corpus.prepare(source, root, questions=queries, query_target=1, node_target=2)
    _, questions, nodes = corpus.load_frozen(root)
    assert (
        nodes["a"]["task_family_id"]
        == nodes["b"]["task_family_id"]
        == questions[0]["task_family_id"]
    )

    reviewed = write(
        tmp_path / "reviewed-groups.json",
        [
            {
                "id": "one",
                "question": "first",
                "source_keys": ["shell-1"],
                "task_family_id": "reviewed-task",
            },
            {
                "id": "two",
                "question": "second",
                "source_keys": ["shell-2"],
                "task_family_id": "reviewed-task",
            },
        ],
    )
    grouped = tmp_path / "reviewed-corpus"
    corpus.prepare(source, grouped, questions=reviewed, query_target=2, node_target=2)
    _, questions, nodes = corpus.load_frozen(grouped)
    assert (
        questions[0]["task_family_id"]
        == questions[1]["task_family_id"]
        == nodes["a"]["task_family_id"]
        == nodes["b"]["task_family_id"]
    )


def test_agent_ancestry_is_transitive_and_cycles_do_not_split():
    with sqlite3.connect(":memory:") as conn:
        conn.executescript(
            "CREATE TABLE agentic_sessions(id INTEGER,session_id TEXT,parent_session_id TEXT,harness TEXT); INSERT INTO agentic_sessions VALUES(1,'root',NULL,'codex'),(2,'child','root','codex'),(3,'grandchild','child','codex'),(4,'cycle-a','cycle-b','codex'),(5,'cycle-b','cycle-a','codex');"
        )
        sources, _ = corpus._source_catalog(conn)
    assert len({sources[f"codex-{i}"]["task_family_id"] for i in (1, 2, 3)}) == 1
    assert sources["codex-4"]["task_family_id"] == sources["codex-5"]["task_family_id"]


def test_http_mcp_captures_retain_origin_and_require_historical_review(frozen, tmp_path):
    root, _ = frozen
    captures = write(
        tmp_path / "captures.json",
        [
            {"id": origin, "origin": origin, "question": "real query", "captured_at_ms": 1}
            for origin in ("http", "mcp")
        ],
    )
    output = tmp_path / "capture-corpus"
    corpus.prepare(
        root / "baseline.sqlite", output, captures=captures, query_target=2, node_target=1
    )
    _, questions, _ = corpus.load_frozen(output)
    assert {q["origin"] for q in questions} == {"http", "mcp"}
    assert all(not q["as_of_reconstructable"] for q in questions)


def test_external_storage_rejects_repository_symlink(tmp_path):
    link = tmp_path / "repo-link"
    link.symlink_to(Path(__file__).resolve().parents[2], target_is_directory=True)
    with pytest.raises(ValueError, match="outside the repository"):
        corpus._json(link / "never-created.json", {})


def test_graded_metrics_use_exponential_gains_and_preserve_unknowns():
    actual = graded_retrieval_metrics(["b", "a"], {"a": 3, "b": 1})
    expected = (1 + 7 / math.log2(3)) / (7 + 1 / math.log2(3))
    assert actual["ndcg_at_5"] == pytest.approx(expected)
    assert actual["hit_at_1"] == 0
    assert actual["mrr"] == 0.5
    assert graded_retrieval_metrics(["unknown"], {"a": 3})["ndcg_at_5"] is None
    assert graded_retrieval_metrics(["irrelevant"], {"irrelevant": 0})["ndcg_at_5"] is None
    assert graded_retrieval_metrics(["irrelevant"], {"irrelevant": 0, "a": 3})["ndcg_at_5"] == 0
    with pytest.raises(ValueError):
        graded_retrieval_metrics(["a", "a"], {"a": 3})
    with pytest.raises(ValueError):
        graded_retrieval_metrics(["a"], {"a": True})


def test_cluster_bootstrap_is_paired_and_does_not_invent_independent_families():
    candidate = {"a": 1.0, "b": 1.0, "c": 1.0}
    baseline = {"a": 0.0, "b": 0.0, "c": 0.0}
    result = paired_family_interval(
        candidate, baseline, {"a": "same", "b": "same", "c": "other"}, samples=200
    )
    assert result["families"] == 2
    assert result["lower"] == result["upper"] == 1
    one = paired_family_interval(candidate, baseline, {k: "same" for k in candidate}, samples=200)
    assert one["mean"] == 1 and one["lower"] is None
    with pytest.raises(ValueError, match="identical"):
        paired_family_interval(candidate, {}, {}, samples=200)


def test_question_loader_retains_lineage_and_gap(tmp_path):
    path = tmp_path / "questions.jsonl"
    row = {
        "id": "q",
        "question": "text",
        "coverage_gap_reason": "node_missing",
        "source_keys": ["shell-4"],
        "task_family_id": "task",
        "split": "test",
        "as_of_ms": 100,
    }
    path.write_text(json.dumps(row) + "\n" + json.dumps({**row, "id": "q2"}) + "\n")
    result = load_questions(path)
    assert result[0].coverage_gap_reason == "node_missing"
    assert result[1].source_keys == ["shell-4"]
    assert result[1].as_of_ms == 100
    path.write_text(json.dumps(row) + "\n")
    assert load_questions(path)[0].id == "q"


def judgment(qid="linked", uid="a", grade=3, **kwargs):
    return {
        "query_id": qid,
        "question": "Which test passed?",
        "node_uuid": uid,
        "grade": grade,
        "evidence": "source line",
        "annotator": "reviewer",
        "annotator_type": "human",
        "rubric_version": corpus.RUBRIC,
        "adjudication": "reviewed",
        **kwargs,
    }


def test_blinded_pool_covers_union_and_rejects_incomplete_cutoff(frozen, tmp_path):
    root, _ = frozen
    rankings = write(
        tmp_path / "rankings.json",
        [
            {"query_id": "linked", "arm": "retrieval", "order": ["a", "b"]},
            {"query_id": "linked", "arm": "jev", "order": ["c", "a"]},
        ],
    )
    output = tmp_path / "pool.json"
    corpus.pool(root, rankings, output)
    rows = corpus.read_records(output)
    assert {r["node_uuid"] for r in rows if r["query_id"] == "linked"} == {"a", "b", "c"}
    assert all("arm" not in r and "rank" not in r and r["grade"] is None for r in rows)
    _, questions, nodes = corpus.load_frozen(root)
    linked = next(q for q in questions if q["id"] == "linked")
    assert all(r["query_input_hash"] == corpus.query_input_hash(linked) for r in rows)
    assert all(r["node_input_hash"] == nodes[r["node_uuid"]]["input_hash"] for r in rows)
    with pytest.raises(ValueError, match="cutoff"):
        corpus.pool(root, rankings, tmp_path / "bad-pool.json", cutoff=5)


def test_imports_require_evidence_and_reject_future_or_conflicting_labels(frozen, tmp_path):
    root, _ = frozen
    labels = write(tmp_path / "labels.json", [judgment()])
    output = tmp_path / "imported.json"
    assert corpus.import_labels(root, labels, output)["human_judgments"] == 1
    _, questions, nodes = corpus.load_frozen(root)
    qmap = {q["id"]: q for q in questions}
    with pytest.raises(ValueError, match="evidence"):
        corpus.validate_judgments([judgment(evidence="")], qmap, nodes)
    with pytest.raises(ValueError, match="evidence"):
        corpus.validate_judgments([judgment(evidence=None)], qmap, nodes)
    qmap["linked"]["as_of_ms"] = 99
    with pytest.raises(ValueError, match="after"):
        corpus.validate_judgments([judgment()], qmap, nodes)
    with pytest.raises(ValueError, match="adjudication"):
        corpus._resolved_judgments([judgment(), judgment(grade=0, annotator="second")])


def test_double_review_requires_every_judged_candidate():
    rows = [judgment(), judgment(annotator="second"), judgment(uid="b", grade=0)]
    assert corpus._resolved_judgments(rows)[2] == set()
    rows.append(judgment(uid="b", grade=0, annotator="second"))
    assert corpus._resolved_judgments(rows)[2] == {"linked"}


def test_judgments_bind_semantic_query_and_node_evidence_across_finalization(frozen, tmp_path):
    root, _ = frozen
    _, questions, nodes = corpus.load_frozen(root)
    qmap = {q["id"]: q for q in questions}
    labeled = judgment(
        query_input_hash=corpus.query_input_hash(qmap["linked"]),
        node_input_hash=nodes["a"]["input_hash"],
    )
    original_hash = labeled["query_input_hash"]
    reviewed = []
    for q in questions:
        reviewed.append({**q, "annotation_status": "human_reviewed"})
    release = tmp_path / "reviewed-corpus"
    corpus.finalize(root, write(tmp_path / "reviewed.json", reviewed), release, family_auditor="A")
    _, finalized, final_nodes = corpus.load_frozen(release)
    final_map = {q["id"]: q for q in finalized}
    assert corpus.query_input_hash(final_map["linked"]) == original_hash
    assert corpus.validate_judgments([labeled], final_map, final_nodes)[0] == labeled
    for change in (
        {"question": "Which deployment failed?"},
        {"filters": {"source": "browser"}},
        {"as_of_ms": qmap["linked"]["as_of_ms"] + 1},
        {"source_keys": ["shell:other"]},
        {"source_content_hashes": {"shell:1": "changed"}},
    ):
        changed = {**final_map, "linked": {**final_map["linked"], **change}}
        with pytest.raises(ValueError, match="query input hash"):
            corpus.validate_judgments([labeled], changed, final_nodes)
    changed_nodes = {**final_nodes, "a": {**final_nodes["a"], "input_hash": "changed"}}
    with pytest.raises(ValueError, match="node input hash"):
        corpus.validate_judgments([labeled], final_map, changed_nodes)
    with pytest.raises(ValueError, match="exact frozen question"):
        corpus.validate_judgments([judgment(question="Old question")], final_map, final_nodes)


@pytest.mark.parametrize(
    ("baseline_size", "candidate_size", "gold_rank", "matched", "gain"),
    [(30, 60, 40, False, False), (60, 60, 40, False, False), (30, 30, 1, True, True)],
)
def test_candidate_discovery_uses_matched_top30_not_widened_recall(
    frozen, tmp_path, monkeypatch, baseline_size, candidate_size, gold_rank, matched, gain
):
    root, manifest = frozen
    _, original, original_nodes = corpus.load_frozen(root)
    questions = [
        {**original[0], "id": f"q{i}", "split": "test", "task_family_id": f"family-{i}"}
        for i in range(2)
    ]
    node_ids = [f"n{i}" for i in range(60)]
    nodes = {
        uid: {**original_nodes["a"], "node_uuid": uid, "input_hash": corpus.digest(uid)}
        for uid in [*node_ids, "a"]
    }
    monkeypatch.setattr(corpus, "load_frozen", lambda _: (manifest, questions, nodes))
    labels = write(
        tmp_path / "imported-discovery-labels.json",
        {
            "corpus_hash": corpus.digest(manifest),
            "rows": [
                judgment(
                    qid=q["id"],
                    uid=uid,
                    grade=3 if uid == "a" else 0,
                    query_input_hash=corpus.query_input_hash(q),
                    node_input_hash=node["input_hash"],
                )
                for q in questions
                for uid, node in nodes.items()
            ],
        },
    )
    candidate_order = node_ids[: candidate_size - 1]
    candidate_order.insert(gold_rank - 1, "a")
    rankings = write(
        tmp_path / "discovery-rankings.json",
        [
            {
                "query_id": q["id"],
                "arm": arm,
                "order": candidate_order if arm == "jev" else node_ids[:baseline_size],
                "input_hash": corpus.digest(q),
                "model": arm,
                "recipe_hash": "fixed",
                "status": "ok",
                "elapsed_ms": 100,
                "filter_violations": 0,
                "malformed_accepted": 0,
            }
            for q in questions
            for arm in ("retrieval", "local", "jev")
        ],
    )
    result = corpus.report(
        root, rankings, labels, tmp_path / "discovery.json", candidate="jev", cutoff=60
    )
    checks = result["candidate_discovery_checks"]
    assert checks["matched_candidate_pools_at_most_30"] is matched
    assert checks["paired_recall_gain"] is gain
    assert checks["positive_recall_lower_bound"] is gain
    assert result["candidate_discovery"]["paired"]["candidate_recall"]["mean"] == float(gain)
    widened = result["candidate_discovery"]["widened_pool_diagnostic"]
    assert widened["candidate_recall_difference"]["mean"] == 1
    assert widened["qualifies_for_production_gate"] is False


@pytest.mark.parametrize("filter_violations", [0, None, 1])
def test_report_keeps_missing_nodes_as_end_to_end_misses_and_blocks_unearned_release(
    frozen, tmp_path, filter_violations
):
    root, _ = frozen
    _, questions, _ = corpus.load_frozen(root)
    rankings = []
    for arm in ("retrieval", "local", "jev"):
        for q in questions:
            rankings.append(
                {
                    "query_id": q["id"],
                    "arm": arm,
                    "order": ["a"],
                    "input_hash": corpus.digest(q),
                    "model": arm,
                    "recipe_hash": "fixed",
                    "status": "ok",
                    "elapsed_ms": 100,
                    "filter_violations": filter_violations,
                    "malformed_accepted": 0,
                }
            )
    labels = write(tmp_path / "labels.json", [judgment()])
    imported = tmp_path / "imported.json"
    corpus.import_labels(root, labels, imported)
    ranking_path = write(tmp_path / "rankings.json", rankings)
    report = corpus.report(
        root,
        ranking_path,
        imported,
        tmp_path / "report.json",
        candidate="jev",
        split=questions[0]["split"],
    )
    assert report["denominators"]["answerable_missing_node"] == 1
    assert report["end_to_end"]["jev"]["hit_at_5"] == 0.5
    assert report["metrics"]["jev"]["linked"]["ndcg_at_5"] == 1
    assert report["passed"] is False
    assert "at_least_1000_questions" in report["failed_checks"]
    assert "reliability" in report["failed_checks"]
    assert report["checks"]["source_policy"] is (filter_violations == 0)
    assert report["checks"]["judgments_bound_to_frozen_inputs"] is False
    assert (tmp_path / "report.completion.json").exists()
    rankings[-1]["order"] = ["a", "b"]
    write(ranking_path, rankings)
    incomplete = corpus.report(
        root,
        ranking_path,
        imported,
        tmp_path / "incomplete.json",
        candidate="jev",
        split=questions[0]["split"],
    )
    assert not incomplete["passed"]
    for row in rankings:
        row["order"] = ["b"]
    write(ranking_path, rankings)
    missing_gold = write(tmp_path / "missing-gold-labels.json", [judgment(uid="b", grade=3)])
    imported_missing = tmp_path / "missing-gold-imported.json"
    corpus.import_labels(root, missing_gold, imported_missing)
    incomplete_gold = corpus.report(
        root,
        ranking_path,
        imported_missing,
        tmp_path / "missing-gold-report.json",
        candidate="jev",
        split=questions[0]["split"],
    )
    assert incomplete_gold["checks"]["complete_evidence"] is False
    assert any("lack judgments" in reason for reason in incomplete_gold["incomplete_reasons"])


def test_review_finalization_requires_human_metadata_and_preserves_old_questions(frozen, tmp_path):
    root, _ = frozen
    rows = corpus.read_records(root / "questions.json")
    path = write(tmp_path / "reviewed.json", rows)
    with pytest.raises(ValueError, match="human_reviewed"):
        corpus.finalize(root, path, tmp_path / "release", family_auditor="Reviewer")
    for row in rows:
        row["annotation_status"] = "human_reviewed"
    write(path, rows)
    result = corpus.finalize(root, path, tmp_path / "release", family_auditor="Reviewer")
    assert result["questions"] == 2
    assert corpus.load_frozen(tmp_path / "release")[0]["family_audit_human"] is True
    assert corpus.load_frozen(root)[0]["family_audit_human"] is False


def test_offline_sweep_records_exhausted_evidence_and_unknown_usage(tmp_path):
    passes = [
        {
            "evidence_hash": "first",
            "signals": {"a": [0.0, 0.0, 0.0], "b": [1.0, 1.0, 1.0]},
            "elapsed_ms": 100,
            "requests": 1,
        },
        {
            "evidence_hash": "second",
            "signals": {"a": [1.0, 1.0, 1.0], "b": [0.0, 0.0, 0.0]},
            "elapsed_ms": 150,
            "requests": 1,
        },
    ]
    rows = [
        {"query_id": "q", "split": "development", "candidate_uuids": ["a", "b"], "passes": passes}
    ]
    signals = write(tmp_path / "signals.json", rows)
    labels = write(tmp_path / "labels.json", [judgment("q", "a", 3), judgment("q", "b", 0)])
    out = tmp_path / "sweep.json"
    corpus.sweep(signals, labels, out, weights=[(1.0, 0.0, 0.0)])
    result = json.loads(out.read_text())
    assert result["fresh_inference_calls"] == 0
    assert result["rows"][1]["marginal_ndcg"] > 0
    assert result["rows"][-1]["pass_budget"] == 100
    assert result["rows"][-1]["passes_used"] == 2
    assert result["rows"][-1]["tokens"] is None
    rows[0]["passes"][1]["evidence_hash"] = "first"
    write(signals, rows)
    with pytest.raises(ValueError, match="changed evidence"):
        corpus.sweep(signals, labels, tmp_path / "repeat.json")
    rows[0]["split"] = "test"
    write(signals, rows)
    with pytest.raises(ValueError, match="holdout"):
        corpus.sweep(signals, labels, tmp_path / "holdout.json")


def test_node_labels_keep_unknowns_separate_and_cannot_qualify_small_samples(frozen, tmp_path):
    from hippo_brain.classification import load_recipe

    root, _ = frozen
    review = corpus.read_records(root / "classification-review.json")
    labels = []
    predictions = []
    for node in review:
        labels.append(
            {
                **node,
                "annotator": "reviewer",
                "annotator_type": "human",
                "adjudication": "reviewed",
                "topics": {
                    topic: {
                        "label": "present" if topic == "observability" else "absent",
                        "evidence": "reviewed source span",
                    }
                    for topic in node["topics"]
                },
            }
        )
        predictions.append(
            {
                "node_uuid": node["node_uuid"],
                "input_hash": node["input_hash"],
                "recipe_hash": load_recipe(None).recipe_hash,
                "model": load_recipe(None).model_id,
                "backend": "jev",
                "status": "ok",
                "accepted_topics": ["observability"],
                "probabilities": {
                    topic: 0.95 if topic == "observability" else 0.1 for topic in node["topics"]
                },
            }
        )
    label_path = write(tmp_path / "node-labels.json", labels)
    imported = tmp_path / "node-labels-imported.json"
    assert corpus.import_node_labels(root, label_path, imported)["nodes"] == 3
    output = tmp_path / "node-report.json"
    report = corpus.classification_report(
        root,
        write(tmp_path / "predictions.json", predictions),
        imported,
        output,
        split=review[0]["split"],
    )
    assert report["micro_precision"] == 1
    assert report["passed"] is False
    assert report["publishable_topics"] == []
    labels[0]["topics"].pop("observability")
    write(label_path, labels)
    with pytest.raises(ValueError, match="every topic"):
        corpus.import_node_labels(root, label_path, tmp_path / "incomplete-node-labels.json")


async def test_current_corpus_classifiers_checkpoint_native_and_fresh_local_budget(
    frozen, tmp_path, monkeypatch
):
    import httpx
    from hippo_brain.bench import knowledge_tags as tags
    from hippo_brain.classification import load_recipe

    root, _ = frozen
    native = await tags.classify_corpus(root, tmp_path / "rules", backend="rules")
    assert native["complete"] and native["requests"] == 0
    assert (await tags.classify_corpus(root, tmp_path / "rules", backend="rules")) == native
    calls = []

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "local-test",
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    t: 0.95 if t == "observability" else 0.1
                                    for t in load_recipe(None).topics
                                }
                            )
                        }
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        )

    original = httpx.AsyncClient
    monkeypatch.setattr(
        tags.httpx, "AsyncClient", lambda **kwargs: original(transport=httpx.MockTransport(respond))
    )
    local = await tags.classify_corpus(
        root,
        tmp_path / "local",
        backend="local",
        model="local-test",
        base_url="http://test/v1",
        max_requests=1,
    )
    assert local["terminal_nodes"] == 1 and not local["complete"]
    assert local["budget_tokens"] >= 30
    assert len(calls) == 1 and calls[0]["max_tokens"] == 512
    result = corpus.read_records(tmp_path / "local/predictions.jsonl")[0]
    assert result["usage"] == {"input_tokens": 20, "output_tokens": 10}
    assert result["backend"] == "local" and result["accepted_topics"] == ["observability"]
    await tags.classify_corpus(
        root,
        tmp_path / "local",
        backend="local",
        model="local-test",
        base_url="http://test/v1",
        max_requests=1,
    )
    assert len(calls) == 1


def test_review_package_exposes_source_evidence_and_blank_human_work(frozen, tmp_path):
    root, _ = frozen
    out = tmp_path / "review"
    result = corpus.review_package(root, out)
    assert result["actual_questions"] == 2 and result["human_annotations"] == 0
    assert result["missing_node_questions"] == 1
    question = corpus.read_records(out / "query-review.json")[0]
    assert question["question"] and question["evidence_sources"][0]["excerpt"]
    assert question["reviewer"] == ""
    assert corpus.read_records(out / "node-review.json")[0]["rubric_version"] == corpus.TOPIC_RUBRIC
    assert "import-node-labels" in (out / "README.md").read_text()
    assert corpus.read_records(out / "retrieval-review.json")[0]["grade"] is None


def test_exact_connection_topic_and_discovery_usefulness_are_separate(frozen, tmp_path):
    root, _ = frozen
    _, _, nodes = corpus.load_frozen(root)
    predictions = write(
        tmp_path / "links-predictions.json",
        [
            {
                "node_uuid": uid,
                "input_hash": node["input_hash"],
                "status": "ok",
                "accepted_topics": ["observability"],
            }
            for uid, node in nodes.items()
            if node["eligible"]
        ],
    )
    pairs = tmp_path / "pair-review.json"
    sampled = corpus.connection_review(root, predictions, pairs, limit=2, split=nodes["a"]["split"])
    assert sampled["sampled_pairs"] == 1 and sampled["unique_endpoints"] == 2
    pending = json.loads(pairs.read_text())
    row = pending["rows"][0]
    row.update(
        {
            "membership_a": "present",
            "membership_b": "absent",
            "evidence_a": "observability evidence",
            "evidence_b": "topic is only incidental",
            "annotator": "reviewer",
            "annotator_type": "human",
            "adjudication": "reviewed",
            "useful": True,
            "discovery_task": "find related investigation",
        }
    )
    write(pairs, pending)
    result = corpus.connection_report(
        root, predictions, pairs, tmp_path / "links-report.json", plan=Path(sampled["plan"])
    )
    assert result["precision"] == 0
    assert result["discovery_task_success"] == 1
    assert result["lower_95"] is None and result["passed"] is False
    assert result["checks"]["immutable_sampling_plan"] is True
    row["evidence_b"] = None
    write(pairs, pending)
    with pytest.raises(ValueError, match="endpoint"):
        corpus.connection_report(root, predictions, pairs, tmp_path / "invalid-links.json")


def test_connection_plan_requires_all_sampled_test_pairs_without_cherry_picking(
    tmp_path, monkeypatch
):
    root = tmp_path / "connections"
    root.mkdir()
    nodes = {
        f"node-{i}": {
            "node_uuid": f"node-{i}",
            "input_hash": corpus.digest(i),
            "eligible": i != 84,
            "split": "test" if i < 80 or i == 84 else "development",
            "task_family_id": f"family-{i}",
        }
        for i in range(85)
    }
    with sqlite3.connect(root / "baseline.sqlite") as conn:
        conn.execute("CREATE TABLE knowledge_nodes (uuid TEXT, content TEXT)")
        conn.executemany(
            "INSERT INTO knowledge_nodes VALUES (?,?)", [(uid, "evidence") for uid in nodes]
        )
    write(root / "taxonomy.json", {"observability": {}})
    manifest = {"family_audit_human": True}
    monkeypatch.setattr(corpus, "load_frozen", lambda _: (manifest, [], nodes))
    predictions = write(
        tmp_path / "predictions.json",
        [
            {
                "node_uuid": uid,
                "input_hash": node["input_hash"],
                "status": "ok",
                "accepted_topics": ["observability"],
            }
            for uid, node in nodes.items()
        ],
    )
    labels = tmp_path / "connection-review.json"
    sampled = corpus.connection_review(root, predictions, labels)
    plan = Path(sampled["plan"])
    assert sampled["sampled_pairs"] == 40
    planned = json.loads(plan.read_text())
    assert planned["expected_pairs"] == 40 and planned["split"] == "test"
    assert all(
        nodes[p[k]]["split"] == "test" for p in planned["pairs"] for k in ("node_a", "node_b")
    )
    evidence = json.loads(labels.read_text())
    for i, row in enumerate(evidence["rows"]):
        row.update(
            membership_a="present",
            membership_b="present" if i < 30 else "absent",
            evidence_a="topic appears in source",
            evidence_b="source checked",
            annotator="Human reviewer",
            annotator_type="human",
            adjudication="reviewed",
        )
    write(labels, evidence)
    full = corpus.connection_report(
        root, predictions, labels, tmp_path / "all-pairs.json", plan=plan
    )
    assert full["pairs"] == 40 and full["precision"] == 0.75 and full["passed"] is False
    evidence["rows"] = evidence["rows"][:30]
    write(labels, evidence)
    with pytest.raises(ValueError, match="every planned pair exactly once"):
        corpus.connection_report(root, predictions, labels, tmp_path / "omitted.json", plan=plan)
    legacy = corpus.connection_report(root, predictions, labels, tmp_path / "legacy.json")
    assert legacy["lower_95"] > 0.85
    assert legacy["checks"]["immutable_sampling_plan"] is False and legacy["passed"] is False
    planned["pairs"] = planned["pairs"][:30]
    planned["expected_pairs"] = 30
    tampered = write(tmp_path / "tampered-plan.json", planned)
    evidence["plan_sha256"] = corpus._sha(tampered)
    write(labels, evidence)
    with pytest.raises(ValueError, match="deterministic frozen selection"):
        corpus.connection_report(
            root, predictions, labels, tmp_path / "tampered.json", plan=tampered
        )
    development = corpus.connection_review(
        root, predictions, tmp_path / "development.json", split="development"
    )
    assert development["sampled_pairs"] == 2
