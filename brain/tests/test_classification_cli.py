"""Operator commands are bounded, resumable, and do not infer or migrate."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from hippo_brain import classification
from hippo_brain import classification_cli as cli
from hippo_brain.retrieval import Filters
from tests.test_classification import node
from tests.test_topic_retrieval import add_node


@pytest.fixture(autouse=True)
def reset_recipe(monkeypatch):
    classification.configure(False)
    monkeypatch.setattr(cli, "_load_runtime_settings", lambda: {"classification": {}})
    yield
    classification.configure(False)


def test_status_reads_without_credentials_or_creating_database(
    tmp_db, tmp_path, monkeypatch, capsys
):
    conn, database = tmp_db
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    assert cli.main(["status", "--database", str(database)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["available"] and result["counts"] == {}
    assert conn.execute("SELECT count(*) FROM knowledge_node_classifications").fetchone()[0] == 0
    missing = tmp_path / "missing.db"
    with pytest.raises(FileNotFoundError):
        cli.status(missing)
    assert not missing.exists()
    conn.execute("DROP TABLE knowledge_node_classifications")
    conn.commit()
    assert cli.status(database)["available"] is False


def test_backfill_resumes_exact_recipe_and_freezes_original_population(tmp_db, tmp_path):
    conn, database = tmp_db
    for i in range(3):
        node(conn, f"node-{i}")
    cursor = tmp_path / "evidence" / "backfill.json"
    first = cli.backfill(database, cursor, limit=2)
    assert (first["previous_after_id"], first["after_id"], first["scanned"], first["queued"]) == (
        0,
        2,
        2,
        2,
    )
    assert not first["complete"]
    state = json.loads(cursor.read_text())
    assert state["recipe_hash"] == classification.default_recipe().recipe_hash
    assert state["database"]["path"] == str(database.resolve())
    assert state["high_watermark_uuid"] == "node-2"
    assert cursor.stat().st_mode & 0o777 == 0o600
    node(conn, "arrived-later")
    second = cli.backfill(database, cursor, limit=2)
    assert (
        second["previous_after_id"],
        second["after_id"],
        second["scanned"],
        second["queued"],
    ) == (2, 3, 1, 1)
    assert second["complete"]
    final = cli.backfill(database, cursor)
    assert final["scanned"] == final["queued"] == 0
    assert conn.execute(
        "SELECT node_id FROM knowledge_node_classifications ORDER BY node_id"
    ).fetchall() == [(1,), (2,), (3,)]


def test_cursor_publication_failure_replays_committed_page_without_new_revision(
    tmp_db, tmp_path, monkeypatch
):
    conn, database = tmp_db
    node(conn)
    cursor = tmp_path / "backfill.json"
    with monkeypatch.context() as patch:

        def fail_replace(*_):
            raise OSError("injected rename failure")

        patch.setattr(cli.os, "replace", fail_replace)
        with pytest.raises(OSError, match="rename failure"):
            cli.backfill(database, cursor)
    assert not cursor.exists()
    assert not list(tmp_path.glob(".classification-*"))
    assert conn.execute(
        "SELECT status,requested_revision FROM knowledge_node_classifications"
    ).fetchall() == [("pending", 1)]
    replay = cli.backfill(database, cursor)
    assert replay["scanned"] == 1 and replay["queued"] == 0
    assert conn.execute(
        "SELECT requested_revision FROM knowledge_node_classifications"
    ).fetchall() == [(1,)]


def test_failed_database_page_does_not_advance_cursor_or_leave_partial_work(tmp_db, tmp_path):
    conn, database = tmp_db
    node(conn, "first")
    node(conn, "second")
    conn.executescript(
        "CREATE TRIGGER refuse_classification BEFORE INSERT ON knowledge_node_classifications WHEN NEW.node_id=2 BEGIN SELECT RAISE(ABORT,'injected'); END;"
    )
    cursor = tmp_path / "backfill.json"
    with pytest.raises(sqlite3.IntegrityError, match="injected"):
        cli.backfill(database, cursor)
    assert not cursor.exists()
    assert conn.execute("SELECT count(*) FROM knowledge_node_classifications").fetchone()[0] == 0


def test_resume_rejects_different_database_and_changed_node_boundary(tmp_db, tmp_path):
    conn, database = tmp_db
    node(conn, "first")
    node(conn, "second")
    cursor = tmp_path / "backfill.json"
    cli.backfill(database, cursor, limit=1)
    other = tmp_path / "other.db"
    copy = sqlite3.connect(other)
    try:
        conn.backup(copy)
    finally:
        copy.close()
    with pytest.raises(ValueError, match="does not match"):
        cli.backfill(other, cursor)
    with conn:
        conn.execute("UPDATE knowledge_nodes SET uuid='replacement' WHERE id=2")
    with pytest.raises(ValueError, match="node identity changed"):
        cli.backfill(database, cursor)
    assert json.loads(cursor.read_text())["after_id"] == 1


@pytest.mark.parametrize("change", ["recipe", "schema", "position"])
def test_resume_rejects_changed_recipe_schema_and_invalid_cursor(
    tmp_db, tmp_path, monkeypatch, change
):
    conn, database = tmp_db
    node(conn)
    cursor = tmp_path / "backfill.json"
    cli.backfill(database, cursor)
    if change == "recipe":
        recipe = replace(classification.default_recipe(), thresholds={"database-storage": 0.9})
        monkeypatch.setattr(classification, "default_recipe", lambda: recipe)
    elif change == "schema":
        conn.execute("PRAGMA user_version=999")
        conn.commit()
    else:
        value = json.loads(cursor.read_text())
        value["after_id"] = -1
        cursor.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="does not match|incompatible"):
        cli.backfill(database, cursor)


def test_backfill_rejects_unmigrated_database_bad_limits_and_unsafe_cursor(tmp_db, tmp_path):
    conn, database = tmp_db
    node(conn)
    for cursor in (database, Path(str(database) + "-wal"), Path.cwd() / "cursor.json"):
        with pytest.raises(ValueError):
            cli.backfill(database, cursor)
    link = tmp_path / "database-link.json"
    link.symlink_to(database)
    with pytest.raises(ValueError, match="separate"):
        cli.backfill(database, link)
    for limit in (0, 1001):
        with pytest.raises(ValueError, match="limit"):
            cli.backfill(database, tmp_path / "cursor.json", limit=limit)
    conn.execute("DROP TABLE knowledge_node_classifications")
    conn.commit()
    with pytest.raises(ValueError, match="migration"):
        cli.backfill(database, tmp_path / "cursor.json")
    assert not (tmp_path / "cursor.json").exists()


def test_connections_include_exact_topic_and_both_current_provenances(tmp_db):
    conn, database = tmp_db
    add_node(conn, 1)
    add_node(conn, 2)
    add_node(conn, 3, excluded=True)
    add_node(conn, 4, project="/other")
    add_node(conn, 5)
    conn.execute("UPDATE knowledge_nodes SET embed_text='changed' WHERE id=5")
    conn.commit()
    before = conn.execute("SELECT count(*) FROM knowledge_node_entities").fetchone()[0]
    result = cli.connections(database, "uuid-001", filters=Filters(project="hippo", source="shell"))
    assert [row["node_uuid"] for row in result["connections"]] == ["uuid-002"]
    topic = result["connections"][0]["topics"][0]
    assert topic["topic_id"] == "knowledge-retrieval"
    assert topic["from"]["uuid"] == "uuid-001"
    assert topic["to"]["uuid"] == "uuid-002"
    assert topic["from"]["recipe_hash"] == topic["to"]["recipe_hash"] == result["recipe_hash"]
    assert topic["from"]["input_hash"] and topic["to"]["revision"] == 1
    assert topic["to"]["probability"] == 0.9
    assert conn.execute("SELECT count(*) FROM knowledge_node_entities").fetchone()[0] == before
    assert len(cli.connections(database, "uuid-001", limit=1)["connections"]) == 1
    with pytest.raises(ValueError, match="eligible"):
        cli.connections(database, "uuid-003")
    with pytest.raises(ValueError, match="eligible"):
        cli.connections(database, "missing")


def test_cli_backfill_and_connections_options(tmp_db, tmp_path, capsys):
    conn, database = tmp_db
    add_node(conn, 1)
    conn.commit()
    assert (
        cli.main(
            [
                "backfill",
                "--database",
                str(database),
                "--cursor",
                str(tmp_path / "cursor.json"),
                "--limit",
                "1",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["complete"]
    assert (
        cli.main(
            ["connections", "--database", str(database), "--node", "uuid-001", "--source", "shell"]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["connections"] == []
    with pytest.raises(SystemExit) as error:
        cli.main(["connections", "--database", str(database), "--node", "uuid-001", "--limit", "0"])
    assert error.value.code == 2
    assert "connection limit" in capsys.readouterr().err


def test_cli_uses_deployment_recipe_and_explicit_override_cannot_resume_old_recipe(
    tmp_db, tmp_path, monkeypatch, capsys
):
    from hippo_brain import _load_runtime_settings

    conn, database = tmp_db
    node(conn)
    fake_home = tmp_path / "home"
    config = fake_home / ".config" / "hippo" / "config.toml"
    config.parent.mkdir(parents=True)
    deployed = tmp_path / "deployed-recipe.json"
    deployed.write_text(json.dumps({"thresholds": {"database-storage": 0.95}}))
    override = tmp_path / "override-recipe.json"
    override.write_text(json.dumps({"thresholds": {"database-storage": 0.85}}))
    config.write_text(f"[classification]\nrecipe_path = {json.dumps(str(deployed))}\n")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    monkeypatch.setattr(cli, "_load_runtime_settings", _load_runtime_settings)

    assert cli.main(["status", "--database", str(database)]) == 0
    deployed_hash = classification.load_recipe(deployed).recipe_hash
    assert json.loads(capsys.readouterr().out)["recipe_hash"] == deployed_hash
    # Selecting a recipe for an operator command does not enable writer hooks.
    assert not classification.enqueue_node(conn, 1)
    cursor = tmp_path / "cursor.json"
    args = ["backfill", "--database", str(database), "--cursor", str(cursor)]
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["recipe_hash"] == deployed_hash
    assert (
        conn.execute("SELECT recipe_hash FROM knowledge_node_classifications").fetchone()[0]
        == deployed_hash
    )
    with pytest.raises(SystemExit) as error:
        cli.main([*args, "--recipe", str(override)])
    assert error.value.code == 2
    assert "recipe" in capsys.readouterr().err
    assert json.loads(cursor.read_text())["recipe_hash"] == deployed_hash
    assert cli.main(["status", "--database", str(database), "--recipe", str(override)]) == 0
    assert (
        json.loads(capsys.readouterr().out)["recipe_hash"]
        == classification.load_recipe(override).recipe_hash
    )


def test_export_writes_consistent_parquet_and_refuses_overwrite(tmp_db, tmp_path, capsys):
    duckdb = pytest.importorskip("duckdb")
    conn, database = tmp_db
    add_node(conn, 1)
    add_node(conn, 2)
    conn.execute(
        "UPDATE knowledge_node_classifications SET status='pending', "
        "probabilities_json='{}', accepted_topics_json='[]' WHERE node_id=2"
    )
    conn.commit()
    out = tmp_path / "export"
    assert cli.main(["export", "--database", str(database), "--out", str(out)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["rows"]["classifications"] == 2
    rows = duckdb.sql(
        f"SELECT node_id, status, node_type, latency_ms IS NOT NULL FROM "
        f"'{out / 'classifications.parquet'}' ORDER BY node_id"
    ).fetchall()
    assert [r[:2] for r in rows] == [(1, "ready"), (2, "pending")]
    topics = duckdb.sql(
        f"SELECT node_id, topic, prob, accepted FROM '{out / 'topic-probabilities.parquet'}'"
    ).fetchall()
    assert topics and all(r[0] == 1 for r in topics)
    assert result["rows"]["topic_probabilities"] == len(topics)
    assert sorted(path.name for path in out.iterdir()) == [
        "classifications.parquet",
        "topic-probabilities.parquet",
    ]
    with pytest.raises(ValueError, match="already exist"):
        cli.export(database, out)
    with pytest.raises(ValueError, match="outside"):
        cli.export(database, Path(__file__).parent / "export")


def test_export_publishes_nothing_on_failure_and_handles_quoted_paths(
    tmp_db, tmp_path, monkeypatch
):
    pytest.importorskip("duckdb")
    conn, database = tmp_db
    add_node(conn, 1)
    conn.commit()
    quoted = tmp_path / "it's here"
    assert cli.export(database, quoted)["rows"]["classifications"] == 1
    out = tmp_path / "failed"
    real_link = cli.os.link
    calls = []

    def fail_second(source, target):
        calls.append(target)
        if len(calls) == 2:
            raise OSError("disk full")
        real_link(source, target)

    monkeypatch.setattr(cli.os, "link", fail_second)
    with pytest.raises(OSError, match="disk full"):
        cli.export(database, out)
    assert list(out.iterdir()) == []


def test_export_duckdb_failure_exits_cleanly(tmp_db, tmp_path, monkeypatch, capsys):
    duckdb = pytest.importorskip("duckdb")
    conn, database = tmp_db
    add_node(conn, 1)
    conn.commit()

    def broken():
        raise duckdb.IOException("cannot open")

    monkeypatch.setattr(duckdb, "connect", broken)
    out = tmp_path / "broken"
    with pytest.raises(SystemExit) as error:
        cli.main(["export", "--database", str(database), "--out", str(out)])
    assert error.value.code == 2
    assert "Parquet export failed" in capsys.readouterr().err
    assert list(out.iterdir()) == []
