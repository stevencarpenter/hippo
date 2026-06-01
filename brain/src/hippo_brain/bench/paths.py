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


_DEFAULT_CORPUS_VERSION = "corpus-v2"


def corpus_sqlite_path(version: str = _DEFAULT_CORPUS_VERSION) -> Path:
    return bench_fixtures_dir() / f"{version}.sqlite"


def corpus_jsonl_path(version: str = _DEFAULT_CORPUS_VERSION) -> Path:
    return bench_fixtures_dir() / f"{version}.jsonl"


def corpus_manifest_path(version: str = _DEFAULT_CORPUS_VERSION) -> Path:
    return bench_fixtures_dir() / f"{version}.manifest.json"


def corpus_overlay_path(version: str = _DEFAULT_CORPUS_VERSION) -> Path:
    return bench_fixtures_dir() / f"{version}.overlay.sqlite"


def bench_qa_path(version: str = "eval-qa-v1") -> Path:
    return bench_fixtures_dir() / f"{version}.jsonl"


def bench_run_tree(run_id: str, model_id: str, create: bool = False) -> Path:
    """Per-model ephemeral run directory."""
    p = bench_runs_dir() / run_id / model_id.replace("/", "_")
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def bench_results_db_path() -> Path:
    """Durable, all-local bench results datastore. Separate file from hippo.db."""
    return hippo_bench_root() / "bench-results.db"
