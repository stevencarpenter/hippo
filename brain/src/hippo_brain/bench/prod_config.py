"""Resolve the prod brain URL the same way `hippo-brain serve` resolves its
own port — by reading `[brain].port` from `~/.config/hippo/config.toml`.

The bench used to hardcode `http://localhost:8000`, which never matched the
actual brain default of 9175. This caused `prod_brain_reachable: warn` in
every preflight and silently disabled the prod-pause guarantee.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

DEFAULT_BRAIN_PORT = 9175


def _config_path() -> Path:
    """Same precedence as `hippo-brain` itself: $XDG_CONFIG_HOME, then $HOME."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "hippo" / "config.toml"


def resolve_prod_brain_port(config_path: Path | None = None) -> int:
    """Return `[brain].port` from prod config.toml, or DEFAULT_BRAIN_PORT.

    Returns the default on any failure (missing file, malformed TOML, missing
    section). The bench's preflight will catch a mismatch separately — this
    function's job is only to produce a sensible default so the bench targets
    the same port the brain actually serves on.
    """
    path = config_path or _config_path()
    if not path.exists():
        return DEFAULT_BRAIN_PORT
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError, OSError:
        return DEFAULT_BRAIN_PORT
    port = data.get("brain", {}).get("port", DEFAULT_BRAIN_PORT)
    return int(port) if isinstance(port, int) else DEFAULT_BRAIN_PORT


def default_prod_brain_url(config_path: Path | None = None) -> str:
    return f"http://127.0.0.1:{resolve_prod_brain_port(config_path)}"
