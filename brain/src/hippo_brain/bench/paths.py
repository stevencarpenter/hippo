"""XDG-resolved paths for hippo-bench fixtures and runs."""

from __future__ import annotations

import os
from pathlib import Path


def hippo_bench_root() -> Path:
    """XDG root for hippo-bench. Sibling of prod hippo data, NOT a child."""
    xdg = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share")))
    return xdg / "hippo-bench"


def bench_fixtures_dir(create: bool = False) -> Path:
    p = hippo_bench_root() / "fixtures"
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def bench_runs_dir(create: bool = False) -> Path:
    p = hippo_bench_root() / "runs"
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def corpus_sqlite_path() -> Path:
    return bench_fixtures_dir() / "corpus-v2.sqlite"


def corpus_jsonl_path() -> Path:
    return bench_fixtures_dir() / "corpus-v2.jsonl"


def corpus_manifest_path() -> Path:
    return bench_fixtures_dir() / "corpus-v2.manifest.json"


def corpus_overlay_path() -> Path:
    return bench_fixtures_dir() / "corpus-v2.overlay.sqlite"


def bench_qa_path(version: str = "eval-qa-v1") -> Path:
    return bench_fixtures_dir() / f"{version}.jsonl"


def bench_run_tree(run_id: str, model_id: str, create: bool = False) -> Path:
    """Per-model ephemeral run directory."""
    p = bench_runs_dir() / run_id / model_id.replace("/", "_")
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p
