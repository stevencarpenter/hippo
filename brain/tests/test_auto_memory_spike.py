import json
from pathlib import Path

import pytest

from hippo_brain.bench.auto_memory_spike import (
    ScratchIndex,
    evaluate,
    load_fixture,
    markdown_headings,
    token_windows,
    whole_file,
)


FIXTURE = Path(__file__).parents[1] / "src/hippo_brain/_fixtures/auto_memory_spike"


def test_fixture_manifest_covers_required_shapes():
    manifest, documents = load_fixture(FIXTURE)
    features = {feature for item in manifest["documents"] for feature in item["features"]}
    assert {
        "index",
        "links",
        "list",
        "headings",
        "long",
        "code",
        "short",
        "duplicate-content",
    } <= features
    assert set(documents) == {item["path"] for item in manifest["documents"]}
    assert len(manifest["queries"]) >= 5


def test_chunkers_have_stable_distinct_shapes():
    text = "# One\nalpha beta gamma delta\n\n## Two\nepsilon zeta eta theta"
    assert len(whole_file("x.md", text)) == 1
    heading_chunks = markdown_headings("x.md", text)
    assert [c.heading for c in heading_chunks] == ["One", "Two"]
    assert [c.chunk_id for c in heading_chunks] == ["x.md#0", "x.md#1"]
    windows = token_windows("x.md", text, max_tokens=5, overlap=2)
    assert len(windows) == 4
    assert windows[0].text.split()[-2:] == windows[1].text.split()[:2]


def test_invalid_token_windows_are_rejected():
    with pytest.raises(ValueError):
        token_windows("x.md", "text", max_tokens=4, overlap=4)


def test_mutation_semantics_are_atomic_and_path_preserving():
    index = ScratchIndex()
    index.replace("a.md", "h1", whole_file("a.md", "alpha current"))
    index.replace("b.md", "h1", whole_file("b.md", "alpha current"))
    assert index.search("alpha") == ["a.md", "b.md"]

    with pytest.raises(RuntimeError):
        index.replace("a.md", "h2", whole_file("a.md", "broken replacement"), fail=True)
    assert index.search("alpha") == ["a.md", "b.md"]
    assert index.search("broken replacement") == []

    index.replace("a.md", "h3", whole_file("a.md", "gamma replacement"))
    assert index.search("alpha") == ["b.md"]
    assert index.search("gamma") == ["a.md"]

    index.rename("a.md", "renamed.md")
    assert index.search("gamma") == ["renamed.md"]
    index.delete("renamed.md")
    assert index.search("gamma") == []


def test_deferred_document_is_not_searchable():
    index = ScratchIndex()
    index.defer("pending.md", "hash")
    assert index.search("pending") == []
    assert index.conn.execute("SELECT indexed FROM documents").fetchone() == (0,)


def test_all_strategies_score_the_same_queries():
    report = evaluate(FIXTURE)
    assert set(report) == {"whole-file", "markdown-heading", "token-window"}
    assert all(data["hit_at_1"] >= 0.8 for data in report.values())
    assert report["whole-file"]["chunks"] < report["markdown-heading"]["chunks"]
    assert report["whole-file"]["chunks"] < report["token-window"]["chunks"]
    assert report["markdown-heading"]["chunks"] != report["token-window"]["chunks"]


def test_manifest_is_synthetic_and_contains_no_home_paths():
    text = json.dumps(json.loads((FIXTURE / "manifest.json").read_text()))
    assert "/Users/" not in text
    assert "carpenter" not in text.lower()
