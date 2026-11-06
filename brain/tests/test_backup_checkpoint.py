import logging
import subprocess

import pytest

from hippo_brain import backup_checkpoint


@pytest.fixture
def configured_db(tmp_path, monkeypatch):
    db = tmp_path / "hippo.db"
    db.touch()
    monkeypatch.setattr("hippo_brain._load_runtime_settings", lambda: {"db_path": str(db)})
    return db


def _fake_run(monkeypatch, returncode, stderr=""):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, returncode, "", stderr)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def test_runs_for_configured_db(configured_db, monkeypatch):
    calls = _fake_run(monkeypatch, 0)
    backup_checkpoint.checkpoint_before_mutation("re-enrich", configured_db)
    assert calls == [["hippo", "backup", "now", "--label", "re-enrich"]]


def test_skips_other_db(configured_db, tmp_path, monkeypatch):
    calls = _fake_run(monkeypatch, 0)
    other = tmp_path / "other.db"
    other.touch()
    backup_checkpoint.checkpoint_before_mutation("re-enrich", other)
    assert calls == []


def test_exit_3_warns(configured_db, monkeypatch, caplog):
    _fake_run(monkeypatch, 3)
    with caplog.at_level(logging.WARNING):
        backup_checkpoint.checkpoint_before_mutation("re-enrich", configured_db)
    assert "not configured" in caplog.text


def test_failure_raises(configured_db, monkeypatch):
    _fake_run(monkeypatch, 1, "disk full\n")
    with pytest.raises(SystemExit) as e:
        backup_checkpoint.checkpoint_before_mutation("re-enrich", configured_db)
    assert "disk full" in str(e.value)
