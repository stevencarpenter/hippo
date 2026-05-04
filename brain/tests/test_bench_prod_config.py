"""Tests for hippo_brain.bench.prod_config — prod brain URL/port resolver.

Regression context: bench v2 hardcoded `http://localhost:8000` as the prod
brain URL, but `hippo-brain serve` defaults to port 9175 (see
`hippo_brain.__init__._default_settings`). The mismatch silently warned in
every preflight and skipped the prod-pause guarantee. This module reads
`[brain].port` from prod config.toml the way the brain itself does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hippo_brain.bench.prod_config import (
    DEFAULT_BRAIN_PORT,
    default_prod_brain_url,
    resolve_prod_brain_port,
)


def test_default_when_config_missing(tmp_path: Path):
    missing = tmp_path / "no-such-config.toml"
    assert resolve_prod_brain_port(missing) == DEFAULT_BRAIN_PORT
    assert default_prod_brain_url(missing) == f"http://127.0.0.1:{DEFAULT_BRAIN_PORT}"


def test_reads_brain_port_from_config(tmp_path: Path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[brain]\nport = 12345\n")
    assert resolve_prod_brain_port(cfg) == 12345
    assert default_prod_brain_url(cfg) == "http://127.0.0.1:12345"


def test_default_when_section_missing(tmp_path: Path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[storage]\ndata_dir = '/tmp'\n")
    assert resolve_prod_brain_port(cfg) == DEFAULT_BRAIN_PORT


def test_default_on_malformed_toml(tmp_path: Path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("this is not :: valid = toml\n[][\n")
    assert resolve_prod_brain_port(cfg) == DEFAULT_BRAIN_PORT


def test_default_on_non_int_port(tmp_path: Path):
    cfg = tmp_path / "config.toml"
    cfg.write_text("[brain]\nport = 'nine-thousand'\n")
    assert resolve_prod_brain_port(cfg) == DEFAULT_BRAIN_PORT


def test_xdg_config_home_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """When XDG_CONFIG_HOME is set, the resolver looks there first — matching
    `hippo-brain`'s own behavior. We verify by setting XDG_CONFIG_HOME to a
    dir that contains a brain port override."""
    xdg = tmp_path / "xdg"
    (xdg / "hippo").mkdir(parents=True)
    (xdg / "hippo" / "config.toml").write_text("[brain]\nport = 7777\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    monkeypatch.setenv("HOME", str(tmp_path / "wrong-home"))
    assert resolve_prod_brain_port() == 7777
