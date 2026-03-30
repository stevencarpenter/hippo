"""Build-time version with fallback to package metadata."""

from importlib.metadata import version as _pkg_version


def get_version() -> str:
    """Return the full version string (e.g. '0.2.0-dev.3+g63ea88d').

    Reads from the build-stamped _version.py first. Falls back to the
    static version in pyproject.toml via importlib.metadata.
    """
    try:
        from hippo_brain._version import __version__

        return __version__
    except ImportError:
        return _pkg_version("hippo-brain")
