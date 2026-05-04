"""Resolve the prod brain URL the same way `hippo-brain serve` resolves its
own port — by reading `[brain].port` from `~/.config/hippo/config.toml`.

The bench used to hardcode `http://localhost:8000`, which never matched the
actual brain default of 9175 (`hippo_brain.__init__._default_settings`).
Result: `prod_brain_reachable: warn` in every preflight, and the prod-pause
guarantee silently skipped because preflight only aborts on `pass + fail`,
not `warn + fail`.

Parse failures here warn to stderr instead of failing silently — a silent
fallback would re-enable the same prod-pause-skip footgun the bench exists
to avoid measuring under.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path

DEFAULT_BRAIN_PORT = 9175


def _config_path() -> Path:
    """Same precedence as `hippo-brain` itself: $XDG_CONFIG_HOME, then $HOME.

    Per the XDG Base Directory spec, an empty XDG_CONFIG_HOME is treated as
    unset — falling back to $HOME/.config.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "hippo" / "config.toml"


def resolve_prod_brain_port(config_path: Path | None = None) -> int:
    """Return `[brain].port` from prod config.toml, or DEFAULT_BRAIN_PORT.

    Returns the default silently for the normal "no config file" case, and
    with a stderr warning for unreadable, malformed, or out-of-spec values.
    """
    path = config_path or _config_path()
    if not path.exists():
        return DEFAULT_BRAIN_PORT
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        # Race with .exists() above — file disappeared. Treat as missing.
        return DEFAULT_BRAIN_PORT
    except (PermissionError, IsADirectoryError) as e:
        _warn(f"cannot read {path}: {e!s}")
        return DEFAULT_BRAIN_PORT
    except tomllib.TOMLDecodeError as e:
        _warn(f"malformed TOML in {path}: {e!s}")
        return DEFAULT_BRAIN_PORT

    port = data.get("brain", {}).get("port", DEFAULT_BRAIN_PORT)
    # bool is an int subclass in Python — `port = true` would silently land
    # as port=1 without this guard. Floats and strings are also rejected.
    if not isinstance(port, int) or isinstance(port, bool):
        _warn(f"[brain].port in {path} is not an integer (got {type(port).__name__}={port!r})")
        return DEFAULT_BRAIN_PORT
    if not 1 <= port <= 65535:
        _warn(f"[brain].port in {path} is out of range (got {port})")
        return DEFAULT_BRAIN_PORT
    return port


def _warn(msg: str) -> None:
    print(
        f"warning: {msg}; defaulting to brain port {DEFAULT_BRAIN_PORT}",
        file=sys.stderr,
    )


def default_prod_brain_url(config_path: Path | None = None) -> str:
    return f"http://127.0.0.1:{resolve_prod_brain_port(config_path)}"
