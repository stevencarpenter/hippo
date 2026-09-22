"""Capture is opt-in, redacted before clipping, bounded and external."""

import json
import stat

import pytest

from hippo_brain.decision_capture import capture_query, decision_root, external_path


def test_query_capture_does_not_require_reranking_and_deduplicates(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("HIPPO_QUERY_CAPTURE", raising=False)
    capture_query("Where is the source?", [])
    assert not decision_root().exists()
    monkeypatch.setenv("HIPPO_QUERY_CAPTURE", "1")
    for origin in ("probe", "benchmark"):
        capture_query("Do not sample this", [], origin=origin)
    assert not decision_root().exists()
    for _ in range(2):
        capture_query("Where is the source?", [], origin="mcp")
    paths = list((decision_root() / "query-captures").glob("*.json"))
    assert len(paths) == 1
    result = json.loads(paths[0].read_text())
    assert result["question"] == "Where is the source?" and result["candidates"] == []
    assert result["origin"] == "mcp" and result["captured_at_ms"] > 0
    assert stat.S_IMODE(paths[0].stat().st_mode) == 0o600


def test_secrets_are_redacted_before_truncation(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HIPPO_QUERY_CAPTURE", "1")
    secret = "ghp_" + "s" * 36
    capture_query("api_key=verysecretvalue", [{"uuid": "n", "summary": "a" * 1190 + secret}])
    body = next((decision_root() / "query-captures").glob("*.json")).read_text()
    assert "verysecretvalue" not in body and "ghp_" not in body
    assert "[REDACTED]" in body


def test_external_path_rejects_repository_and_production_symlinks(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    production = tmp_path / "data" / "hippo"
    production.mkdir(parents=True)
    link = tmp_path / "outside"
    link.symlink_to(production, target_is_directory=True)
    with pytest.raises(ValueError, match="production"):
        external_path(link / "run")
    with pytest.raises(ValueError, match="repository"):
        external_path(__file__)


def test_capture_disk_errors_do_not_fail_query(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("HIPPO_QUERY_CAPTURE", "1")
    root = decision_root()
    root.mkdir(parents=True)
    (root / "query-captures").write_text("occupied")
    capture_query("still works", [])
