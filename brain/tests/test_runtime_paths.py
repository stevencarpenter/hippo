"""Query and ingestion config paths share the Rust XDG contract."""

import json
import runpy
import sqlite3
import sys
from pathlib import Path

import pytest

from hippo_brain import _load_runtime_settings, auto_memory
from hippo_brain.bench.prod_config import resolve_prod_brain_port
from hippo_brain.mcp import _load_config


@pytest.fixture(autouse=True)
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)


@pytest.mark.parametrize("xdg", [None, "", "custom"])
@pytest.mark.parametrize("has_config", [False, True])
def test_runtime_paths_use_xdg_or_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, xdg: str | None, has_config: bool
) -> None:
    config_base = tmp_path / ("xdg-config" if xdg == "custom" else ".config")
    data_base = tmp_path / ("xdg-data" if xdg == "custom" else ".local/share")
    if xdg is not None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_base) if xdg else "")
        monkeypatch.setenv("XDG_DATA_HOME", str(data_base) if xdg else "")
    if has_config:
        config_path = config_base / "hippo/config.toml"
        config_path.parent.mkdir(parents=True)
        config_path.write_text('[brain]\nport = 9888\n[models]\nquery = "fixture-model"\n')

    http = _load_runtime_settings()
    mcp = _load_config()
    for settings in (http, mcp):
        assert settings["data_dir"] == str(data_base / "hippo")
        assert settings["db_path"] == str(data_base / "hippo/hippo.db")
        assert settings["query_model"] == ("fixture-model" if has_config else "")
    assert http["port"] == resolve_prod_brain_port() == (9888 if has_config else 9175)
    assert not (data_base / "hippo").exists()


@pytest.mark.parametrize("has_xdg_config", [False, True])
def test_xdg_config_does_not_fall_back_to_conflicting_home_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, has_xdg_config: bool
) -> None:
    home_config = tmp_path / ".config/hippo/config.toml"
    home_config.parent.mkdir(parents=True)
    home_config.write_text('[brain]\nport = 9881\n[models]\nquery = "home-model"\n')
    xdg = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    if has_xdg_config:
        config = xdg / "hippo/config.toml"
        config.parent.mkdir(parents=True)
        config.write_text(
            '[brain]\nport = 9882\n[models]\nquery = "xdg-model"\n'
            f"[storage]\nconfig_dir = {json.dumps(str(home_config.parent))}\n"
        )

    http = _load_runtime_settings()
    mcp = _load_config()
    assert http["port"] == resolve_prod_brain_port() == (9882 if has_xdg_config else 9175)
    assert http["query_model"] == mcp["query_model"] == ("xdg-model" if has_xdg_config else "")


@pytest.mark.parametrize("use_tilde", [False, True])
def test_explicit_storage_data_dir_overrides_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, use_tilde: bool
) -> None:
    config_base = tmp_path / "xdg-config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_base))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg-data"))
    config_path = config_base / "hippo/config.toml"
    config_path.parent.mkdir(parents=True)
    data_dir = "~/explicit-data" if use_tilde else str(tmp_path / "explicit-data")
    config_path.write_text(f"[storage]\ndata_dir = {json.dumps(data_dir)}\n")
    for settings in (_load_runtime_settings(), _load_config()):
        assert settings["data_dir"] == str(tmp_path / "explicit-data")
        assert settings["db_path"] == str(tmp_path / "explicit-data/hippo.db")


@pytest.mark.parametrize("consumer", ["xcode", "auto-memory"])
@pytest.mark.parametrize(
    ("xdg", "storage_override"),
    [(None, None), ("", None), ("custom", None), ("custom", "absolute"), ("custom", "tilde")],
)
def test_ingestion_writes_to_the_query_database(
    tmp_path: Path,
    tmp_db,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    consumer: str,
    xdg: str | None,
    storage_override: str | None,
) -> None:
    config_base = tmp_path / ("xdg-config" if xdg == "custom" else ".config")
    data_base = tmp_path / ("xdg-data" if xdg == "custom" else ".local/share")
    if xdg is not None:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(config_base) if xdg else "")
        monkeypatch.setenv("XDG_DATA_HOME", str(data_base) if xdg else "")
    if xdg == "custom":
        home_config = tmp_path / ".config/hippo/config.toml"
        home_config.parent.mkdir(parents=True)
        home_config.write_text("[auto_memory]\nenabled = false\n")

    data_dir = tmp_path / "explicit-data" if storage_override else data_base / "hippo"
    data_dir.mkdir(parents=True)
    db_path = data_dir / "hippo.db"
    schema_conn, _ = tmp_db
    with sqlite3.connect(db_path) as conn:
        schema_conn.backup(conn)

    source = tmp_path / "MEMORY.md"
    source.write_text("# Database\n\nUse SQLite WAL.\n")
    config_path = config_base / "hippo/config.toml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    storage = ""
    if storage_override:
        value = "~/explicit-data" if storage_override == "tilde" else str(data_dir)
        storage = f"[storage]\ndata_dir = {json.dumps(value)}\n"
    config_path.write_text(
        storage
        + "[auto_memory]\nenabled = true\ndebounce_ms = 0\n"
        + "stable_idle_ms = 1\nstable_sample_ms = 1\nstable_timeout_ms = 100\n"
        + f'[[auto_memory.sources]]\npath = {json.dumps(str(source))}\nrepository = "fixture"\n'
    )
    assert _load_runtime_settings()["db_path"] == _load_config()["db_path"] == str(db_path)

    if consumer == "xcode":
        projects = tmp_path / "xcode-projects"
        session = projects / "fixture/session.jsonl"
        session.parent.mkdir(parents=True)
        session.write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-03-28T12:00:00.000Z",
                    "cwd": str(tmp_path),
                    "message": {"content": [{"type": "text", "text": "Use SQLite WAL."}]},
                }
            )
            + "\n"
        )
        script = Path(__file__).resolve().parents[2] / "scripts/hippo-ingest-claude.py"
        monkeypatch.setattr(sys, "argv", [str(script), "--claude-dir", str(projects)])
        runpy.run_path(str(script), run_name="__main__")
        with sqlite3.connect(db_path) as conn:
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM agentic_sessions WHERE harness = 'claude-code'"
                ).fetchone()[0]
                == 1
            )
    else:
        assert auto_memory.inventory_main([]) == 0
        assert json.loads(capsys.readouterr().out)["file_sources"] == 1
        assert auto_memory.poll_main([]) == 0
        assert json.loads(capsys.readouterr().out)["changed"] == 1
        assert auto_memory.reconcile_from_config(config_path)["changed"] == 0
        source.write_text("# Database\n\nUse SQLite WAL and a five-second busy timeout.\n")
        assert auto_memory.reconcile_file_main(["--file", str(source)]) == 0
        assert json.loads(capsys.readouterr().out)["changed"] is True
        with sqlite3.connect(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM memory_revisions").fetchone()[0] == 2

    if xdg == "custom":
        assert not (tmp_path / ".local/share/hippo/hippo.db").exists()
