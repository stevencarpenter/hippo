from __future__ import annotations

import sqlite3
from pathlib import Path

from hippo_brain.bench.qa import export_label_worklist, validate_qa_fixture


def _write_corpus_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, command TEXT)")
        conn.execute("CREATE TABLE claude_sessions (id INTEGER PRIMARY KEY, summary_text TEXT)")
        conn.execute("INSERT INTO events (id, command) VALUES (1, 'cargo test')")
        conn.execute("INSERT INTO claude_sessions (id, summary_text) VALUES (2, 'bench design')")
        conn.commit()
    finally:
        conn.close()


def test_validate_qa_fixture_counts_scoreable_items(tmp_path: Path) -> None:
    db = tmp_path / "corpus.sqlite"
    qa = tmp_path / "eval-qa-v1.jsonl"
    _write_corpus_db(db)
    qa.write_text(
        "\n".join(
            [
                '{"qa_id":"q1","question":"cmd?","golden_event_id":"shell-1"}',
                '{"qa_id":"q2","question":"session?","golden_event_id":"claude-2"}',
                '{"qa_id":"q3","question":"missing?","golden_event_id":"shell-999"}',
                '{"qa_id":"q4","question":"null?","golden_event_id":null}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = validate_qa_fixture(qa, db, min_scoreable=2)

    assert report.total == 4
    assert report.scoreable == 2
    assert report.unscoreable == 2
    assert report.passes is True
    assert report.missing_by_qa_id == {"q3": "shell-999", "q4": None}


def test_validate_qa_fixture_fails_under_minimum(tmp_path: Path) -> None:
    db = tmp_path / "corpus.sqlite"
    qa = tmp_path / "eval-qa-v1.jsonl"
    _write_corpus_db(db)
    qa.write_text('{"qa_id":"q1","question":"cmd?","golden_event_id":"shell-1"}\n')

    report = validate_qa_fixture(qa, db, min_scoreable=2)

    assert report.scoreable == 1
    assert report.passes is False
    assert "need at least 2 scoreable Q/A items" in report.detail


def test_export_label_worklist_writes_unlabeled_questions(tmp_path: Path) -> None:
    db = tmp_path / "corpus.sqlite"
    qa = tmp_path / "eval-qa-v1.jsonl"
    out = tmp_path / "worklist.jsonl"
    _write_corpus_db(db)
    qa.write_text(
        '{"qa_id":"q1","question":"cmd?","golden_event_id":null,"source_filter":"shell"}\n',
        encoding="utf-8",
    )

    count = export_label_worklist(qa, db, out)

    assert count == 1
    text = out.read_text(encoding="utf-8")
    assert '"qa_id": "q1"' in text
    assert '"candidate_event_ids": ["shell-1"]' in text
