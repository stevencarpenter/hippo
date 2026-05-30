"""Tests for hippo_brain.bench.prod_config — prod brain URL/port resolver.

Regression context: bench v2 hardcoded `http://localhost:8000` as the prod
brain URL, but `hippo-brain serve` defaults to port 9175 (see
`hippo_brain.__init__._default_settings`). The mismatch silently warned in
every preflight and skipped the prod-pause guarantee. This module reads
`[brain].port` from prod config.toml the way the brain itself does.

Per the silent-failure audit on PR #130 follow-up: parse failures must NOT
fall back silently — that re-enables the same prod-pause-skip footgun.
These tests assert stderr warnings fire on every fall-back path except
"file legitimately doesn't exist."
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hippo_brain.bench.prod_config import (
    DEFAULT_BRAIN_PORT,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_INFERENCE_BASE_URL,
    default_embedding_model,
    default_inference_base_url,
    default_prod_brain_url,
    resolve_prod_brain_port,
)


def test_default_when_config_missing(tmp_path: Path, capsys: pytest.CaptureFixture):
    missing = tmp_path / "no-such-config.toml"
    assert resolve_prod_brain_port(missing) == DEFAULT_BRAIN_PORT
    assert default_prod_brain_url(missing) == f"http://127.0.0.1:{DEFAULT_BRAIN_PORT}"
    # Missing config is the normal install state — must NOT warn.
    captured = capsys.readouterr()
    assert captured.err == ""


def test_reads_brain_port_from_config(tmp_path: Path, capsys: pytest.CaptureFixture):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[brain]\nport = 12345\n")
    assert resolve_prod_brain_port(cfg) == 12345
    assert default_prod_brain_url(cfg) == "http://127.0.0.1:12345"
    assert capsys.readouterr().err == ""


def test_default_when_section_missing(tmp_path: Path, capsys: pytest.CaptureFixture):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[storage]\ndata_dir = '/tmp'\n")
    assert resolve_prod_brain_port(cfg) == DEFAULT_BRAIN_PORT
    # Missing [brain] section is benign (uses defaults), no warn.
    assert capsys.readouterr().err == ""


def test_warns_on_malformed_toml(tmp_path: Path, capsys: pytest.CaptureFixture):
    cfg = tmp_path / "config.toml"
    cfg.write_text("this is not :: valid = toml\n[][\n")
    assert resolve_prod_brain_port(cfg) == DEFAULT_BRAIN_PORT
    err = capsys.readouterr().err
    assert "malformed TOML" in err
    assert str(cfg) in err
    assert f"port {DEFAULT_BRAIN_PORT}" in err


def test_warns_on_unreadable_config(tmp_path: Path, capsys: pytest.CaptureFixture):
    """A directory at the config path raises IsADirectoryError on open. We
    can't reliably create a file we can't read on the test fs (root owns
    chmod 000), so IsADirectoryError stands in for the broader OSError
    family this catches."""
    cfg_dir = tmp_path / "config.toml"  # directory, not a file
    cfg_dir.mkdir()
    assert resolve_prod_brain_port(cfg_dir) == DEFAULT_BRAIN_PORT
    err = capsys.readouterr().err
    assert "cannot read" in err
    assert str(cfg_dir) in err


def test_warns_on_non_int_port(tmp_path: Path, capsys: pytest.CaptureFixture):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[brain]\nport = 'nine-thousand'\n")
    assert resolve_prod_brain_port(cfg) == DEFAULT_BRAIN_PORT
    err = capsys.readouterr().err
    assert "not an integer" in err
    assert "str" in err


def test_warns_on_float_port(tmp_path: Path, capsys: pytest.CaptureFixture):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[brain]\nport = 9175.0\n")
    assert resolve_prod_brain_port(cfg) == DEFAULT_BRAIN_PORT
    err = capsys.readouterr().err
    assert "not an integer" in err
    assert "float" in err


def test_warns_on_bool_port(tmp_path: Path, capsys: pytest.CaptureFixture):
    """bool is an int subclass in Python — `port = true` would silently land
    as port=1 without an isinstance(port, bool) guard."""
    cfg = tmp_path / "config.toml"
    cfg.write_text("[brain]\nport = true\n")
    assert resolve_prod_brain_port(cfg) == DEFAULT_BRAIN_PORT
    err = capsys.readouterr().err
    assert "not an integer" in err
    assert "bool" in err


def test_warns_on_out_of_range_port(tmp_path: Path, capsys: pytest.CaptureFixture):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[brain]\nport = 99999\n")
    assert resolve_prod_brain_port(cfg) == DEFAULT_BRAIN_PORT
    err = capsys.readouterr().err
    assert "out of range" in err
    assert "99999" in err


def test_warns_when_brain_key_is_scalar_not_table(tmp_path: Path, capsys: pytest.CaptureFixture):
    """Codex review on PR #130: `brain = 1` (scalar at the [brain] key) is
    valid TOML but would crash `data.get("brain", {}).get("port")` with
    AttributeError. Since this resolver runs at argparse-default time, the
    crash would blow up CLI parser construction — before any subcommand
    can even be requested. Must defend against the non-dict shape."""
    cfg = tmp_path / "config.toml"
    cfg.write_text('brain = "not-a-table"\n')
    assert resolve_prod_brain_port(cfg) == DEFAULT_BRAIN_PORT
    err = capsys.readouterr().err
    assert "[brain]" in err
    assert "not a table" in err


def test_xdg_config_home_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When XDG_CONFIG_HOME is set, the resolver looks there first — matching
    `hippo-brain`'s own behavior."""
    xdg = tmp_path / "xdg"
    (xdg / "hippo").mkdir(parents=True)
    (xdg / "hippo" / "config.toml").write_text("[brain]\nport = 7777\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("HOME", str(tmp_path / "wrong-home"))
    assert resolve_prod_brain_port() == 7777


def test_empty_xdg_config_home_treated_as_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Per XDG Base Directory spec: an empty XDG_CONFIG_HOME is treated as
    unset, falling back to $HOME/.config."""
    home = tmp_path / "home"
    (home / ".config" / "hippo").mkdir(parents=True)
    (home / ".config" / "hippo" / "config.toml").write_text("[brain]\nport = 4444\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", "")  # empty, must be ignored
    monkeypatch.setenv("HOME", str(home))
    assert resolve_prod_brain_port() == 4444


# ---------------------------------------------------------------------------
# default_inference_base_url
# ---------------------------------------------------------------------------


def test_inference_base_url_reads_from_config(tmp_path: Path, capsys: pytest.CaptureFixture):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[inference]\nbase_url = "http://localhost:9999/v1"\n')
    assert default_inference_base_url(cfg) == "http://localhost:9999/v1"
    assert capsys.readouterr().err == ""


def test_inference_base_url_default_when_file_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    missing = tmp_path / "no-such-config.toml"
    assert default_inference_base_url(missing) == DEFAULT_INFERENCE_BASE_URL
    # Missing config is the normal install state — must NOT warn.
    assert capsys.readouterr().err == ""


def test_inference_base_url_default_when_section_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[storage]\ndata_dir = '/tmp'\n")
    assert default_inference_base_url(cfg) == DEFAULT_INFERENCE_BASE_URL
    # Missing [inference] section is benign — must NOT warn.
    assert capsys.readouterr().err == ""


def test_inference_base_url_warns_on_malformed_toml(tmp_path: Path, capsys: pytest.CaptureFixture):
    cfg = tmp_path / "config.toml"
    cfg.write_text("this is not :: valid = toml\n[][\n")
    assert default_inference_base_url(cfg) == DEFAULT_INFERENCE_BASE_URL
    err = capsys.readouterr().err
    assert "malformed TOML" in err
    assert str(cfg) in err


def test_inference_base_url_warns_on_wrong_type(tmp_path: Path, capsys: pytest.CaptureFixture):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[inference]\nbase_url = 123\n")
    assert default_inference_base_url(cfg) == DEFAULT_INFERENCE_BASE_URL
    err = capsys.readouterr().err
    assert "not a string" in err
    assert str(cfg) in err


# ---------------------------------------------------------------------------
# default_embedding_model
# ---------------------------------------------------------------------------


def test_embedding_model_reads_from_config(tmp_path: Path, capsys: pytest.CaptureFixture):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[models]\nembedding = "my-custom-model"\n')
    assert default_embedding_model(cfg) == "my-custom-model"
    assert capsys.readouterr().err == ""


def test_embedding_model_default_when_file_missing(tmp_path: Path, capsys: pytest.CaptureFixture):
    missing = tmp_path / "no-such-config.toml"
    assert default_embedding_model(missing) == DEFAULT_EMBEDDING_MODEL
    # Missing config is the normal install state — must NOT warn.
    assert capsys.readouterr().err == ""


def test_embedding_model_default_when_section_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture
):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[storage]\ndata_dir = '/tmp'\n")
    assert default_embedding_model(cfg) == DEFAULT_EMBEDDING_MODEL
    # Missing [models] section is benign — must NOT warn.
    assert capsys.readouterr().err == ""


def test_embedding_model_warns_on_malformed_toml(tmp_path: Path, capsys: pytest.CaptureFixture):
    cfg = tmp_path / "config.toml"
    cfg.write_text("this is not :: valid = toml\n[][\n")
    assert default_embedding_model(cfg) == DEFAULT_EMBEDDING_MODEL
    err = capsys.readouterr().err
    assert "malformed TOML" in err
    assert str(cfg) in err


def test_embedding_model_warns_on_wrong_type(tmp_path: Path, capsys: pytest.CaptureFixture):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[models]\nembedding = 42\n")
    assert default_embedding_model(cfg) == DEFAULT_EMBEDDING_MODEL
    err = capsys.readouterr().err
    assert "not a string" in err
    assert str(cfg) in err
