import json
import sqlite3

import pytest

from hippo_brain.bench.corpus import (
    CorpusEntry,
    init_corpus,
    load_corpus,
    sample_from_hippo_db,
    verify_corpus,
    write_corpus,
)


@pytest.fixture
def tmp_corpus_path(tmp_path):
    return tmp_path / "corpus-v1.jsonl"


@pytest.fixture
def tmp_manifest_path(tmp_path):
    return tmp_path / "corpus-v1.manifest.json"


def test_corpus_entry_hashes_are_deterministic():
    e1 = CorpusEntry(
        event_id="e1", source="shell", redacted_content="ls -la", reference_enrichment=None
    )
    e2 = CorpusEntry(
        event_id="e1", source="shell", redacted_content="ls -la", reference_enrichment=None
    )
    assert e1.content_sha256 == e2.content_sha256


def test_corpus_entry_hash_differs_on_content_change():
    e1 = CorpusEntry(
        event_id="e1", source="shell", redacted_content="ls -la", reference_enrichment=None
    )
    e2 = CorpusEntry(
        event_id="e1", source="shell", redacted_content="ls -la ", reference_enrichment=None
    )
    assert e1.content_sha256 != e2.content_sha256


def test_write_and_load_roundtrip(tmp_corpus_path, tmp_manifest_path):
    entries = [
        CorpusEntry(
            event_id="a", source="shell", redacted_content="echo hi", reference_enrichment=None
        ),
        CorpusEntry(
            event_id="b",
            source="claude",
            redacted_content="convo",
            reference_enrichment={"summary": "x"},
        ),
    ]
    write_corpus(entries, tmp_corpus_path, tmp_manifest_path, corpus_version="corpus-v1", seed=42)
    loaded = list(load_corpus(tmp_corpus_path))
    assert len(loaded) == 2
    assert loaded[0].event_id == "a"
    assert loaded[1].reference_enrichment == {"summary": "x"}


def test_verify_detects_tampering(tmp_corpus_path, tmp_manifest_path):
    entries = [
        CorpusEntry(
            event_id="a", source="shell", redacted_content="echo hi", reference_enrichment=None
        )
    ]
    write_corpus(entries, tmp_corpus_path, tmp_manifest_path, corpus_version="corpus-v1", seed=42)
    # Tamper.
    content = tmp_corpus_path.read_text()
    tmp_corpus_path.write_text(content.replace("echo hi", "rm -rf /"))
    ok, detail = verify_corpus(tmp_corpus_path, tmp_manifest_path)
    assert not ok
    assert "hash" in detail.lower() or "mismatch" in detail.lower()


def test_verify_passes_untampered(tmp_corpus_path, tmp_manifest_path):
    entries = [
        CorpusEntry(
            event_id="a", source="shell", redacted_content="echo hi", reference_enrichment=None
        )
    ]
    write_corpus(entries, tmp_corpus_path, tmp_manifest_path, corpus_version="corpus-v1", seed=42)
    ok, detail = verify_corpus(tmp_corpus_path, tmp_manifest_path)
    assert ok, detail


def test_init_corpus_stratified_sampling(tmp_path, tmp_corpus_path, tmp_manifest_path):
    db_path = tmp_path / "fake.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE events (id INTEGER PRIMARY KEY, source TEXT, payload TEXT);
        """
    )
    for i in range(20):
        conn.execute(
            "INSERT INTO events (source, payload) VALUES (?, ?)",
            ("shell", json.dumps({"command": f"cmd-{i}", "stdout": "ok", "stderr": ""})),
        )
    for i in range(10):
        conn.execute(
            "INSERT INTO events (source, payload) VALUES (?, ?)",
            ("claude", json.dumps({"transcript": f"session-{i}"})),
        )
    conn.commit()
    conn.close()

    entries = init_corpus(
        db_path=db_path,
        fixture_path=tmp_corpus_path,
        manifest_path=tmp_manifest_path,
        corpus_version="corpus-v1",
        source_counts={"shell": 5, "claude": 3, "browser": 0, "workflow": 0},
        seed=42,
    )
    assert len(entries) == 8
    shell_entries = [e for e in entries if e.source == "shell"]
    claude_entries = [e for e in entries if e.source == "claude"]
    assert len(shell_entries) == 5
    assert len(claude_entries) == 3


def test_init_corpus_is_deterministic_with_seed(tmp_path):
    """Two init_corpus calls with the same seed produce identical event ordering."""
    db_path = tmp_path / "fake.db"
    conn = sqlite3.connect(db_path)
    conn.executescript("CREATE TABLE events (id INTEGER PRIMARY KEY, source TEXT, payload TEXT);")
    for i in range(30):
        conn.execute(
            "INSERT INTO events (source, payload) VALUES (?, ?)",
            ("shell", json.dumps({"command": f"cmd-{i}"})),
        )
    conn.commit()
    conn.close()

    entries_a = init_corpus(
        db_path=db_path,
        fixture_path=tmp_path / "a.jsonl",
        manifest_path=tmp_path / "a.manifest.json",
        corpus_version="corpus-v1",
        source_counts={"shell": 5, "claude": 0, "browser": 0, "workflow": 0},
        seed=42,
    )
    entries_b = init_corpus(
        db_path=db_path,
        fixture_path=tmp_path / "b.jsonl",
        manifest_path=tmp_path / "b.manifest.json",
        corpus_version="corpus-v1",
        source_counts={"shell": 5, "claude": 0, "browser": 0, "workflow": 0},
        seed=42,
    )
    assert [e.event_id for e in entries_a] == [e.event_id for e in entries_b]


def _make_hippo_schema(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE shell_events (
            id INTEGER PRIMARY KEY,
            command TEXT, stdout TEXT, stderr TEXT, duration_ms INTEGER,
            exit_code INTEGER, cwd TEXT, ts INTEGER
        );
        CREATE TABLE claude_sessions (
            id INTEGER PRIMARY KEY,
            session_id TEXT, transcript TEXT, message_count INTEGER,
            tool_calls_json TEXT, ts INTEGER
        );
        CREATE TABLE browser_events (
            id INTEGER PRIMARY KEY,
            url TEXT, title TEXT, dwell_ms INTEGER, scroll_depth REAL, ts INTEGER
        );
        CREATE TABLE workflow_runs (
            id INTEGER PRIMARY KEY,
            repo TEXT, workflow_name TEXT, conclusion TEXT, annotations_json TEXT, ts INTEGER
        );
        """
    )
    return conn


def test_sample_from_hippo_db_reads_each_source(tmp_path):
    db_path = tmp_path / "hippo.db"
    conn = _make_hippo_schema(db_path)
    conn.execute(
        "INSERT INTO shell_events (command, stdout, stderr, duration_ms, exit_code, cwd, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ls -la", "file listing", "", 42, 0, "/tmp", 0),
    )
    conn.execute(
        "INSERT INTO claude_sessions (session_id, transcript, message_count, tool_calls_json, ts)"
        " VALUES (?, ?, ?, ?, ?)",
        ("s1", "hello world", 5, "[]", 0),
    )
    conn.execute(
        "INSERT INTO browser_events (url, title, dwell_ms, scroll_depth, ts)"
        " VALUES (?, ?, ?, ?, ?)",
        ("https://docs.python.org/3/", "docs", 30_000, 0.8, 0),
    )
    conn.execute(
        "INSERT INTO workflow_runs (repo, workflow_name, conclusion, annotations_json, ts)"
        " VALUES (?, ?, ?, ?, ?)",
        ("hippo", "ci", "success", "[]", 0),
    )
    conn.commit()
    conn.close()

    entries = sample_from_hippo_db(
        db_path=db_path,
        source_counts={"shell": 1, "claude": 1, "browser": 1, "workflow": 1},
        seed=7,
    )
    sources = {e.source for e in entries}
    assert sources == {"shell", "claude", "browser", "workflow"}
    shell_entry = next(e for e in entries if e.source == "shell")
    assert "ls -la" in shell_entry.redacted_content
