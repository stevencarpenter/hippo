"""XDG-resolved paths for hippo-bench fixtures and runs."""

from __future__ import annotations

import os
from pathlib import Path


def _xdg_data_home() -> Path:
    explicit = os.environ.get("XDG_DATA_HOME")
    if explicit:
        return Path(explicit)
    return Path(os.environ["HOME"]) / ".local" / "share"


def bench_root() -> Path:
    return _xdg_data_home() / "hippo" / "bench"


def fixtures_dir(create: bool = False) -> Path:
    p = bench_root() / "fixtures"
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def corpus_path(version: str) -> Path:
    return fixtures_dir() / f"{version}.jsonl"


def corpus_manifest_path(version: str) -> Path:
    return fixtures_dir() / f"{version}.manifest.json"


def runs_dir(create: bool = False) -> Path:
    p = bench_root() / "runs"
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p
