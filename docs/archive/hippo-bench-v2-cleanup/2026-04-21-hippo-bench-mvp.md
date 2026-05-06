# Hippo-Bench MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the Tier 0 automated shakeout of local enrichment models for `hippo-bench`, with JSONL output that captures schema validity, refusal detection, latency percentiles, self-consistency, entity-type sanity, and system-load metrics for every (model × event) pair.

**Architecture:** New Python package `brain/src/hippo_brain/bench/` exposing a `hippo-bench` console script. Deterministic per-model orchestration driven by the `lms` CLI (hard dependency). Reuses `hippo_brain.client` (LM Studio HTTP), `hippo_brain.models` (schema validators), `hippo_brain.embeddings` (self-consistency cosine), and `hippo_brain.redaction` (corpus sample-time re-redaction). Emits a single JSONL file per run with three record types: `run_manifest`, `attempt`, `model_summary`.

**Tech Stack:** Python 3.14, `uv` for env + runs, `httpx` for HTTP, `psutil` for process metrics (new dep), `lms` CLI via subprocess, `pytest` + `pytest-asyncio` for tests, `ruff` for lint/format. macOS/Apple Silicon target.

**Design spec:** [docs/superpowers/specs/2026-04-21-hippo-bench-design.md](../specs/2026-04-21-hippo-bench-design.md) — authoritative source for thresholds, record shapes, and open questions.

---

## Prerequisites (read first)

Before starting Task 1, read these to understand the existing project conventions:

- [brain/pyproject.toml](../../../brain/pyproject.toml) — dev commands (`uv run --project brain ...`), ruff config, Python 3.14
- [brain/src/hippo_brain/client.py](../../../brain/src/hippo_brain/client.py) — existing LM Studio HTTP client wired with OTel; reuse its patterns
- [brain/src/hippo_brain/models.py](../../../brain/src/hippo_brain/models.py) — `EnrichmentResult`, `validate_enrichment_data()`; the schema validators already exist for shell source
- [brain/src/hippo_brain/enrichment.py](../../../brain/src/hippo_brain/enrichment.py) — eligibility logic, entity types per source
- [brain/src/hippo_brain/embeddings.py](../../../brain/src/hippo_brain/embeddings.py) — how embeddings flow; we need embedding calls for self-consistency
- [brain/src/hippo_brain/evaluation.py](../../../brain/src/hippo_brain/evaluation.py) — `hippo-eval` is the sibling CLI; follow its CLI + output shape
- [CLAUDE.md](../../../CLAUDE.md) — repo conventions (uv, ruff, Python 3.14, launchd labels)

**Reference commands (run from repo root, not `brain/`):**

```
# Install deps + lockfile update:
uv sync --project brain

# Single test file:
uv run --project brain pytest brain/tests/test_bench_gates.py -v

# Whole bench test suite:
uv run --project brain pytest brain/tests/test_bench_*.py -v

# Lint:
uv run --project brain ruff check brain/src/hippo_brain/bench brain/tests

# Format check:
uv run --project brain ruff format --check brain/src/hippo_brain/bench brain/tests

# Format apply:
uv run --project brain ruff format brain/src/hippo_brain/bench brain/tests
```

---

## File Structure

**New files under `brain/src/hippo_brain/bench/`:**

```
bench/
├── __init__.py           # public surface: run(), init_corpus(), __version__
├── cli.py                # argparse entry; `hippo-bench run|corpus init|corpus verify|summary`
├── config.py             # BenchConfig dataclass, threshold defaults, XDG path resolution
├── paths.py              # XDG-resolved paths: fixture, runs dir, manifest
├── lms.py                # subprocess wrapper around `lms` CLI (list, load, unload, wait-ready)
├── corpus.py             # sample from SQLite DB, write JSONL fixture, write manifest, verify
├── schemas.py            # per-source enrichment JSON schemas (shell/claude/browser/workflow)
├── gates.py              # five Tier 0 gates as pure functions
├── metrics.py            # sampling thread for RSS/CPU/load
├── output.py             # JSONL record builders + writer
├── preflight.py          # individual pre-flight checks returning (name, status, detail)
├── coordinator.py        # per-model lifecycle: unload-all → load → warmup → run → unload → cooldown
└── runner.py             # main pass + self-consistency pass; composes corpus + gates + output
```

**New test files under `brain/tests/`:**

```
tests/
├── test_bench_corpus.py
├── test_bench_gates.py
├── test_bench_metrics.py
├── test_bench_output.py
├── test_bench_preflight.py
├── test_bench_coordinator.py
├── test_bench_lms.py
├── test_bench_schemas.py
└── test_bench_cli.py
```

**Files to modify:**

- `brain/pyproject.toml` — add `psutil>=6` dep, add `hippo-bench` console script
- `.gitignore` — add `.local/share/hippo/bench/fixtures/**` pattern if repo is inside that path (defensive — user's real data dir is outside the repo, but this catches accidental symlinks)

---

## Task 1: Scaffold the bench package + CLI entrypoint

**Files:**
- Create: `brain/src/hippo_brain/bench/__init__.py`
- Create: `brain/src/hippo_brain/bench/cli.py`
- Create: `brain/tests/test_bench_cli.py`
- Modify: `brain/pyproject.toml`

- [ ] **Step 1: Write the failing smoke test**

Create `brain/tests/test_bench_cli.py`:

```python
import subprocess


def test_cli_help_smoke():
    """hippo-bench --help exits 0 and mentions the three subcommands."""
    result = subprocess.run(
        ["uv", "run", "--project", "brain", "hippo-bench", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "run" in result.stdout
    assert "corpus" in result.stdout
    assert "summary" in result.stdout
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --project brain pytest brain/tests/test_bench_cli.py -v`
Expected: FAIL — console script `hippo-bench` doesn't exist yet.

- [ ] **Step 3: Create package `__init__.py`**

Create `brain/src/hippo_brain/bench/__init__.py`:

```python
"""hippo-bench — local enrichment model shakeout benchmark.

Tier 0 only in MVP. See docs/superpowers/specs/2026-04-21-hippo-bench-design.md
for the full design.
"""

__version__ = "0.1.0"
```

- [ ] **Step 4: Create minimal CLI**

Create `brain/src/hippo_brain/bench/cli.py`:

```python
"""hippo-bench CLI entrypoint."""

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hippo-bench",
        description="Local enrichment model shakeout benchmark (Tier 0 MVP).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("run", help="Run the bench against loaded candidate models")

    corpus = sub.add_parser("corpus", help="Manage the bench corpus fixture")
    corpus_sub = corpus.add_subparsers(dest="corpus_command", required=True)
    corpus_sub.add_parser("init", help="Sample the fixture from the live hippo DB")
    corpus_sub.add_parser("verify", help="Re-check fixture content hashes")

    summary = sub.add_parser("summary", help="Pretty-print a run")
    summary.add_argument("run_file", help="Path to a run JSONL file")

    args = parser.parse_args(argv)

    # Subcommands are stubs for now; each task fills one in.
    print(f"hippo-bench {args.command} — not yet implemented", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Register console script + add psutil dep**

Edit `brain/pyproject.toml`. In the `[project.scripts]` block add:

```toml
hippo-bench = "hippo_brain.bench.cli:main"
```

In the `[project].dependencies` list append:

```toml
"psutil>=6",
```

- [ ] **Step 6: Sync deps**

Run: `uv sync --project brain`
Expected: success, `psutil` installs.

- [ ] **Step 7: Run test to verify it passes**

Run: `uv run --project brain pytest brain/tests/test_bench_cli.py::test_cli_help_smoke -v`
Expected: PASS.

- [ ] **Step 8: Lint + format**

Run: `uv run --project brain ruff check brain/src/hippo_brain/bench brain/tests/test_bench_cli.py`
Run: `uv run --project brain ruff format --check brain/src/hippo_brain/bench brain/tests/test_bench_cli.py`
Expected: clean. If format check fails, run `uv run --project brain ruff format brain/src/hippo_brain/bench brain/tests/test_bench_cli.py`.

- [ ] **Step 9: Commit**

```bash
git add brain/src/hippo_brain/bench/ brain/tests/test_bench_cli.py brain/pyproject.toml brain/uv.lock
git commit -m "feat(bench): scaffold hippo-bench package + CLI stub

Adds brain/src/hippo_brain/bench/ with a minimal argparse CLI exposing
run, corpus, and summary subcommands. Subcommands return exit 1 until
filled in by later tasks. Adds psutil dependency for later metrics work."
```

---

## Task 2: Config + paths

**Files:**
- Create: `brain/src/hippo_brain/bench/paths.py`
- Create: `brain/src/hippo_brain/bench/config.py`
- Create: `brain/tests/test_bench_config.py`

- [ ] **Step 1: Write failing tests for paths**

Create `brain/tests/test_bench_config.py`:

```python
import os
from pathlib import Path

import pytest

from hippo_brain.bench.config import BenchConfig, DEFAULT_THRESHOLDS
from hippo_brain.bench.paths import bench_root, corpus_path, runs_dir


def test_bench_root_respects_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    root = bench_root()
    assert root == tmp_path / "hippo" / "bench"


def test_bench_root_default(monkeypatch, tmp_path):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    root = bench_root()
    assert root == tmp_path / ".local" / "share" / "hippo" / "bench"


def test_corpus_path_layout(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    p = corpus_path("corpus-v1")
    assert p == tmp_path / "hippo" / "bench" / "fixtures" / "corpus-v1.jsonl"


def test_runs_dir_created_on_access(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    d = runs_dir(create=True)
    assert d.is_dir()


def test_default_thresholds_shape():
    # Every threshold listed in the design spec must be present and typed.
    assert DEFAULT_THRESHOLDS["schema_validity_min"] == 0.95
    assert DEFAULT_THRESHOLDS["refusal_max"] == 0.0
    assert DEFAULT_THRESHOLDS["latency_p95_max_ms"] == 60_000
    assert DEFAULT_THRESHOLDS["self_consistency_min"] == 0.7
    assert DEFAULT_THRESHOLDS["entity_sanity_min"] == 0.9


def test_bench_config_roundtrip(tmp_path):
    cfg = BenchConfig(
        corpus_version="corpus-v1",
        candidate_models=["qwen3.5-35b-a3b"],
        self_consistency_events=5,
        self_consistency_runs_per_event=5,
        latency_ceiling_sec=60,
        thresholds=dict(DEFAULT_THRESHOLDS),
        fixture_path=tmp_path / "corpus-v1.jsonl",
        out_path=tmp_path / "run.jsonl",
        skip_checks=False,
    )
    d = cfg.to_dict()
    assert d["corpus_version"] == "corpus-v1"
    assert d["thresholds"]["schema_validity_min"] == 0.95
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --project brain pytest brain/tests/test_bench_config.py -v`
Expected: FAIL — imports don't resolve.

- [ ] **Step 3: Implement paths module**

Create `brain/src/hippo_brain/bench/paths.py`:

```python
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
```

- [ ] **Step 4: Implement config module**

Create `brain/src/hippo_brain/bench/config.py`:

```python
"""BenchConfig dataclass + threshold defaults."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_THRESHOLDS: dict[str, float | int] = {
    "schema_validity_min": 0.95,
    "refusal_max": 0.0,
    "echo_similarity_max": 0.5,
    "latency_p95_max_ms": 60_000,
    "self_consistency_min": 0.7,
    "entity_sanity_min": 0.9,
}


@dataclass
class BenchConfig:
    corpus_version: str
    candidate_models: list[str]
    self_consistency_events: int
    self_consistency_runs_per_event: int
    latency_ceiling_sec: int
    thresholds: dict[str, float | int]
    fixture_path: Path
    out_path: Path
    skip_checks: bool
    warmup_calls: int = 3
    metrics_sample_interval_ms: int = 250
    cooldown_max_sec: int = 90

    def to_dict(self) -> dict[str, Any]:
        return {
            "corpus_version": self.corpus_version,
            "candidate_models": list(self.candidate_models),
            "self_consistency_events": self.self_consistency_events,
            "self_consistency_runs_per_event": self.self_consistency_runs_per_event,
            "latency_ceiling_sec": self.latency_ceiling_sec,
            "thresholds": dict(self.thresholds),
            "fixture_path": str(self.fixture_path),
            "out_path": str(self.out_path),
            "skip_checks": self.skip_checks,
            "warmup_calls": self.warmup_calls,
            "metrics_sample_interval_ms": self.metrics_sample_interval_ms,
            "cooldown_max_sec": self.cooldown_max_sec,
        }
```

- [ ] **Step 5: Verify tests pass**

Run: `uv run --project brain pytest brain/tests/test_bench_config.py -v`
Expected: all PASS.

- [ ] **Step 6: Lint + commit**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/ brain/tests/test_bench_config.py
uv run --project brain ruff format brain/src/hippo_brain/bench/ brain/tests/test_bench_config.py
git add brain/src/hippo_brain/bench/paths.py brain/src/hippo_brain/bench/config.py brain/tests/test_bench_config.py
git commit -m "feat(bench): add XDG path resolver + BenchConfig dataclass

Paths honor XDG_DATA_HOME (falls back to ~/.local/share). BenchConfig
carries the full run-time config; DEFAULT_THRESHOLDS holds the Tier 0
gate thresholds from the design spec."
```

---

## Task 3: Per-source enrichment schemas

**Files:**
- Create: `brain/src/hippo_brain/bench/schemas.py`
- Create: `brain/tests/test_bench_schemas.py`

Context: the existing `hippo_brain.models.validate_enrichment_data()` validates the shell-source schema only. For bench we need schema descriptors for all four sources so gates can validate structurally.

- [ ] **Step 1: Write failing tests**

Create `brain/tests/test_bench_schemas.py`:

```python
import pytest

from hippo_brain.bench.schemas import SOURCE_SCHEMAS, validate_against_schema


def test_all_four_sources_present():
    assert set(SOURCE_SCHEMAS.keys()) == {"shell", "claude", "browser", "workflow"}


def test_shell_schema_accepts_canonical_payload():
    payload = {
        "summary": "Ran cargo test for hippo-core",
        "intent": "verify",
        "outcome": "success",
        "entities": {
            "projects": ["hippo"],
            "tools": ["cargo"],
            "files": [],
            "services": [],
            "errors": [],
        },
    }
    ok, errors = validate_against_schema(payload, "shell")
    assert ok, errors


def test_shell_schema_rejects_missing_summary():
    payload = {"intent": "verify", "outcome": "success", "entities": {}}
    ok, errors = validate_against_schema(payload, "shell")
    assert not ok
    assert any("summary" in e for e in errors)


def test_shell_schema_rejects_bad_outcome():
    payload = {
        "summary": "x",
        "intent": "y",
        "outcome": "maybe",
        "entities": {"projects": [], "tools": [], "files": [], "services": [], "errors": []},
    }
    ok, errors = validate_against_schema(payload, "shell")
    assert not ok
    assert any("outcome" in e for e in errors)


def test_claude_schema_accepts_canonical_payload():
    payload = {
        "summary": "Session about enrichment bugfix",
        "entities": {
            "projects": ["hippo"],
            "topics": ["enrichment"],
            "files": ["brain/src/hippo_brain/enrichment.py"],
            "decisions": [],
            "errors": [],
        },
    }
    ok, errors = validate_against_schema(payload, "claude")
    assert ok, errors


def test_browser_schema_accepts_canonical_payload():
    payload = {
        "summary": "Read Rust docs on trait objects",
        "entities": {
            "topics": ["rust", "trait objects"],
            "urls": [],
            "projects": [],
        },
    }
    ok, errors = validate_against_schema(payload, "browser")
    assert ok, errors


def test_workflow_schema_accepts_canonical_payload():
    payload = {
        "summary": "CI run for commit abc123: tests passed",
        "entities": {
            "projects": ["hippo"],
            "jobs": ["test"],
            "errors": [],
        },
    }
    ok, errors = validate_against_schema(payload, "workflow")
    assert ok, errors


def test_rejects_non_dict():
    ok, errors = validate_against_schema("not a dict", "shell")
    assert not ok


def test_rejects_non_string_summary():
    payload = {"summary": 42, "intent": "x", "outcome": "success", "entities": {}}
    ok, errors = validate_against_schema(payload, "shell")
    assert not ok
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --project brain pytest brain/tests/test_bench_schemas.py -v`
Expected: FAIL — imports missing.

- [ ] **Step 3: Implement schemas module**

Create `brain/src/hippo_brain/bench/schemas.py`:

```python
"""Per-source enrichment JSON schemas for Tier 0 structural validation.

Kept independent from hippo_brain.models (which is tuned to the live
enrichment pipeline's needs) so bench can evolve gate thresholds
without touching production code paths.
"""

from __future__ import annotations

from dataclasses import dataclass

_VALID_OUTCOMES = {"success", "partial", "failure", "unknown"}


@dataclass(frozen=True)
class SourceSchema:
    required_top_level: tuple[str, ...]
    entity_categories: tuple[str, ...]
    constrained_enums: dict[str, frozenset[str]]  # field name -> allowed values
    summary_min_chars: int = 1
    summary_max_chars: int = 2000


SOURCE_SCHEMAS: dict[str, SourceSchema] = {
    "shell": SourceSchema(
        required_top_level=("summary", "intent", "outcome", "entities"),
        entity_categories=("projects", "tools", "files", "services", "errors"),
        constrained_enums={"outcome": frozenset(_VALID_OUTCOMES)},
    ),
    "claude": SourceSchema(
        required_top_level=("summary", "entities"),
        entity_categories=("projects", "topics", "files", "decisions", "errors"),
        constrained_enums={},
    ),
    "browser": SourceSchema(
        required_top_level=("summary", "entities"),
        entity_categories=("topics", "urls", "projects"),
        constrained_enums={},
    ),
    "workflow": SourceSchema(
        required_top_level=("summary", "entities"),
        entity_categories=("projects", "jobs", "errors"),
        constrained_enums={},
    ),
}


def validate_against_schema(payload: object, source: str) -> tuple[bool, list[str]]:
    """Return (passed, errors). Never raises."""
    errors: list[str] = []
    schema = SOURCE_SCHEMAS.get(source)
    if schema is None:
        return False, [f"unknown source {source!r}"]

    if not isinstance(payload, dict):
        return False, [f"expected dict, got {type(payload).__name__}"]

    for field in schema.required_top_level:
        if field not in payload:
            errors.append(f"missing required field {field!r}")

    summary = payload.get("summary")
    if summary is not None:
        if not isinstance(summary, str):
            errors.append(f"summary must be a string, got {type(summary).__name__}")
        else:
            n = len(summary)
            if n < schema.summary_min_chars:
                errors.append(f"summary too short ({n} chars)")
            if n > schema.summary_max_chars:
                errors.append(f"summary too long ({n} chars)")

    entities = payload.get("entities")
    if entities is not None:
        if not isinstance(entities, dict):
            errors.append(f"entities must be a dict, got {type(entities).__name__}")
        else:
            for cat in schema.entity_categories:
                v = entities.get(cat, [])
                if not isinstance(v, list):
                    errors.append(f"entities.{cat} must be a list")
                    continue
                for i, item in enumerate(v):
                    if not isinstance(item, str):
                        errors.append(f"entities.{cat}[{i}] must be a string")

    for field_name, allowed in schema.constrained_enums.items():
        if field_name in payload and payload[field_name] not in allowed:
            errors.append(
                f"{field_name} must be one of {sorted(allowed)}, got {payload[field_name]!r}"
            )

    return (not errors), errors
```

- [ ] **Step 4: Verify tests pass**

Run: `uv run --project brain pytest brain/tests/test_bench_schemas.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/schemas.py brain/tests/test_bench_schemas.py
uv run --project brain ruff format brain/src/hippo_brain/bench/schemas.py brain/tests/test_bench_schemas.py
git add brain/src/hippo_brain/bench/schemas.py brain/tests/test_bench_schemas.py
git commit -m "feat(bench): per-source enrichment schema validators

Adds SOURCE_SCHEMAS for shell/claude/browser/workflow with
validate_against_schema() returning (passed, errors). Kept independent
from hippo_brain.models so bench thresholds can evolve independently."
```

---

## Task 4: Tier 0 gate — schema validity

**Files:**
- Create: `brain/src/hippo_brain/bench/gates.py`
- Create: `brain/tests/test_bench_gates.py`

- [ ] **Step 1: Write failing tests**

Create `brain/tests/test_bench_gates.py`:

```python
from hippo_brain.bench.gates import check_schema_validity


def test_schema_validity_passes_valid_shell_payload():
    raw = (
        '{"summary": "x", "intent": "verify", "outcome": "success",'
        ' "entities": {"projects": [], "tools": [], "files": [],'
        ' "services": [], "errors": []}}'
    )
    r = check_schema_validity(raw, "shell")
    assert r.passed
    assert r.parsed is not None
    assert r.errors == []


def test_schema_validity_fails_unparseable():
    r = check_schema_validity("not { json", "shell")
    assert not r.passed
    assert r.parsed is None
    assert any("parse" in e.lower() or "json" in e.lower() for e in r.errors)


def test_schema_validity_fails_missing_field():
    r = check_schema_validity('{"summary": "x"}', "shell")
    assert not r.passed
    assert r.parsed is not None
    assert any("required" in e for e in r.errors)


def test_schema_validity_strips_fence_blocks():
    fenced = (
        "```json\n"
        '{"summary": "x", "intent": "y", "outcome": "success",'
        ' "entities": {"projects": [], "tools": [], "files": [],'
        ' "services": [], "errors": []}}\n'
        "```"
    )
    r = check_schema_validity(fenced, "shell")
    assert r.passed, r.errors
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --project brain pytest brain/tests/test_bench_gates.py::test_schema_validity_passes_valid_shell_payload -v`
Expected: FAIL — imports missing.

- [ ] **Step 3: Implement schema validity gate**

Create `brain/src/hippo_brain/bench/gates.py`:

```python
"""Tier 0 gate functions. Each returns a typed result struct; never raises."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from hippo_brain.bench.schemas import validate_against_schema

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


@dataclass
class SchemaCheckResult:
    passed: bool
    parsed: dict | None
    errors: list[str] = field(default_factory=list)


def _strip_code_fence(text: str) -> str:
    m = _FENCE_RE.match(text)
    return m.group(1) if m else text


def check_schema_validity(raw_output: str, source: str) -> SchemaCheckResult:
    """Parse raw LLM output and validate it against the source's schema."""
    text = _strip_code_fence(raw_output)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        return SchemaCheckResult(passed=False, parsed=None, errors=[f"json parse error: {e.msg}"])

    if not isinstance(parsed, dict):
        return SchemaCheckResult(
            passed=False,
            parsed=None,
            errors=[f"expected top-level object, got {type(parsed).__name__}"],
        )

    ok, errors = validate_against_schema(parsed, source)
    return SchemaCheckResult(passed=ok, parsed=parsed, errors=errors)
```

- [ ] **Step 4: Verify tests pass**

Run: `uv run --project brain pytest brain/tests/test_bench_gates.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/gates.py brain/tests/test_bench_gates.py
uv run --project brain ruff format brain/src/hippo_brain/bench/gates.py brain/tests/test_bench_gates.py
git add brain/src/hippo_brain/bench/gates.py brain/tests/test_bench_gates.py
git commit -m "feat(bench): Tier 0 gate — schema validity

Adds check_schema_validity() that parses raw LLM output (stripping
fenced code blocks) and validates against the per-source schema."
```

---

## Task 5: Tier 0 gate — refusal / pathology detection

**Files:**
- Modify: `brain/src/hippo_brain/bench/gates.py`
- Modify: `brain/tests/test_bench_gates.py`

- [ ] **Step 1: Append failing tests**

Append to `brain/tests/test_bench_gates.py`:

```python
from hippo_brain.bench.gates import check_refusal_pathology


def test_refusal_detected_on_cannot_help():
    r = check_refusal_pathology(
        raw_output="I'm sorry, but I cannot help with that request.",
        input_text="ls -la",
        parsed=None,
    )
    assert r.refusal_detected
    assert "cannot" in " ".join(r.refusal_patterns_matched).lower()


def test_refusal_detected_on_as_an_ai():
    r = check_refusal_pathology(
        raw_output="As an AI, I cannot answer this.",
        input_text="cargo test",
        parsed=None,
    )
    assert r.refusal_detected


def test_no_refusal_on_normal_output():
    r = check_refusal_pathology(
        raw_output='{"summary": "Normal output"}',
        input_text="ls",
        parsed={"summary": "Normal output"},
    )
    assert not r.refusal_detected


def test_trivial_summary_flagged():
    r = check_refusal_pathology(
        raw_output='{"summary": "ok"}',
        input_text="ran a big test suite",
        parsed={"summary": "ok"},
    )
    assert r.trivial_summary


def test_empty_summary_flagged():
    r = check_refusal_pathology(
        raw_output='{"summary": ""}',
        input_text="x",
        parsed={"summary": ""},
    )
    assert r.trivial_summary


def test_whitespace_only_summary_flagged():
    r = check_refusal_pathology(
        raw_output='{"summary": "   "}',
        input_text="x",
        parsed={"summary": "   "},
    )
    assert r.trivial_summary


def test_echo_similarity_high_when_output_matches_input():
    prompt = "cargo test --release ran for 42 seconds and 103 tests passed"
    r = check_refusal_pathology(
        raw_output=prompt,
        input_text=prompt,
        parsed=None,
    )
    assert r.echo_similarity > 0.8


def test_echo_similarity_low_when_output_distinct():
    r = check_refusal_pathology(
        raw_output='{"summary": "Ran tests"}',
        input_text="Completely unrelated long-form prose about databases.",
        parsed={"summary": "Ran tests"},
    )
    assert r.echo_similarity < 0.3
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --project brain pytest brain/tests/test_bench_gates.py -v`
Expected: new tests FAIL.

- [ ] **Step 3: Append refusal/pathology gate**

Append to `brain/src/hippo_brain/bench/gates.py`:

```python
_REFUSAL_PATTERNS = (
    re.compile(r"\bI'?m sorry\b", re.IGNORECASE),
    re.compile(r"\bI (?:cannot|can['’]?t|won['’]?t)\b", re.IGNORECASE),
    re.compile(r"\bas an AI\b", re.IGNORECASE),
    re.compile(r"\bI'?m unable to\b", re.IGNORECASE),
    re.compile(r"\bI don'?t have the ability\b", re.IGNORECASE),
    re.compile(r"\bI'?m not able to\b", re.IGNORECASE),
)


@dataclass
class RefusalPathologyResult:
    refusal_detected: bool
    refusal_patterns_matched: list[str]
    trivial_summary: bool
    echo_similarity: float


def _char_ngrams(s: str, n: int = 4) -> set[str]:
    s = s.lower().strip()
    if len(s) < n:
        return {s} if s else set()
    return {s[i : i + n] for i in range(len(s) - n + 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def check_refusal_pathology(
    raw_output: str, input_text: str, parsed: dict | None
) -> RefusalPathologyResult:
    """Detect refusal phrases, trivial summaries, and echo of input."""
    patterns_matched: list[str] = []
    for pat in _REFUSAL_PATTERNS:
        m = pat.search(raw_output)
        if m:
            patterns_matched.append(m.group(0))

    trivial = False
    if parsed is not None and isinstance(parsed, dict):
        summary = parsed.get("summary")
        if summary is None or not isinstance(summary, str) or len(summary.strip()) < 4:
            trivial = True

    echo = _jaccard(_char_ngrams(raw_output), _char_ngrams(input_text))

    return RefusalPathologyResult(
        refusal_detected=bool(patterns_matched),
        refusal_patterns_matched=patterns_matched,
        trivial_summary=trivial,
        echo_similarity=echo,
    )
```

- [ ] **Step 4: Verify tests pass**

Run: `uv run --project brain pytest brain/tests/test_bench_gates.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/gates.py brain/tests/test_bench_gates.py
uv run --project brain ruff format brain/src/hippo_brain/bench/gates.py brain/tests/test_bench_gates.py
git add brain/src/hippo_brain/bench/gates.py brain/tests/test_bench_gates.py
git commit -m "feat(bench): Tier 0 gate — refusal / pathology detection

Detects refusal phrases (regex), trivial/empty summaries, and echo of
input via 4-gram Jaccard similarity (cheap, deterministic, no embedding
needed at this gate)."
```

---

## Task 6: Tier 0 gate — entity-type sanity

**Files:**
- Modify: `brain/src/hippo_brain/bench/gates.py`
- Modify: `brain/tests/test_bench_gates.py`

- [ ] **Step 1: Append failing tests**

Append to `brain/tests/test_bench_gates.py`:

```python
from hippo_brain.bench.gates import check_entity_sanity


def test_entity_sanity_accepts_path_like_files():
    payload = {
        "entities": {
            "files": ["src/main.rs", "brain/src/hippo_brain/enrichment.py", ".env"],
            "tools": ["cargo"],
            "projects": ["hippo"],
            "services": ["launchd"],
            "errors": [],
        }
    }
    r = check_entity_sanity(payload, "shell")
    assert r.passed
    assert r.files_path_rate >= 0.9


def test_entity_sanity_flags_sentence_in_files():
    payload = {
        "entities": {
            "files": ["The summary of this command output is a file", "ok.py"],
            "tools": [],
            "projects": [],
            "services": [],
            "errors": [],
        }
    }
    r = check_entity_sanity(payload, "shell")
    assert r.files_path_rate <= 0.5


def test_entity_sanity_flags_long_tool_names():
    payload = {
        "entities": {
            "files": [],
            "tools": [
                "cargo",
                "This is a sentence pretending to be a tool name that should fail.",
            ],
            "projects": [],
            "services": [],
            "errors": [],
        }
    }
    r = check_entity_sanity(payload, "shell")
    assert r.tools_sanity_rate <= 0.6


def test_entity_sanity_no_entities_is_vacuously_pass():
    payload = {"entities": {}}
    r = check_entity_sanity(payload, "shell")
    assert r.passed
    assert r.per_category_rates == {}
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --project brain pytest brain/tests/test_bench_gates.py -v`
Expected: new tests FAIL.

- [ ] **Step 3: Append entity sanity gate**

Append to `brain/src/hippo_brain/bench/gates.py`:

```python
_PATH_LIKE = re.compile(r"[/\\]|^\.\w+$|\.\w{1,8}$")
_WHITESPACE_WORDS = re.compile(r"\s+")


@dataclass
class EntitySanityResult:
    passed: bool
    per_category_rates: dict[str, float]
    files_path_rate: float = 1.0
    tools_sanity_rate: float = 1.0
    projects_sanity_rate: float = 1.0


def _file_looks_like_path(s: str) -> bool:
    if not isinstance(s, str) or not s:
        return False
    if len(s) > 200:
        return False
    return bool(_PATH_LIKE.search(s))


def _tool_looks_sane(s: str) -> bool:
    if not isinstance(s, str) or not s:
        return False
    if len(s) > 40:
        return False
    words = _WHITESPACE_WORDS.findall(s)
    if len(words) + 1 > 3:  # more than 3 tokens
        return False
    if s.rstrip().endswith((".", "!", "?")):
        return False
    return True


def _project_looks_sane(s: str) -> bool:
    if not isinstance(s, str) or not s:
        return False
    if len(s) > 80:
        return False
    return not any(c.isspace() for c in s if c not in "-_")


_CATEGORY_CHECKERS = {
    "files": _file_looks_like_path,
    "tools": _tool_looks_sane,
    "projects": _project_looks_sane,
}


def check_entity_sanity(parsed: dict, source: str, min_rate: float = 0.9) -> EntitySanityResult:
    entities = parsed.get("entities") if isinstance(parsed, dict) else None
    per_cat: dict[str, float] = {}
    if not isinstance(entities, dict):
        return EntitySanityResult(passed=True, per_category_rates={})

    for cat, checker in _CATEGORY_CHECKERS.items():
        values = entities.get(cat)
        if not isinstance(values, list) or not values:
            continue
        hits = sum(1 for v in values if checker(v))
        per_cat[cat] = hits / len(values)

    all_pass = all(rate >= min_rate for rate in per_cat.values())
    return EntitySanityResult(
        passed=all_pass,
        per_category_rates=per_cat,
        files_path_rate=per_cat.get("files", 1.0),
        tools_sanity_rate=per_cat.get("tools", 1.0),
        projects_sanity_rate=per_cat.get("projects", 1.0),
    )
```

- [ ] **Step 4: Verify tests pass**

Run: `uv run --project brain pytest brain/tests/test_bench_gates.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/gates.py brain/tests/test_bench_gates.py
uv run --project brain ruff format brain/src/hippo_brain/bench/gates.py brain/tests/test_bench_gates.py
git add brain/src/hippo_brain/bench/gates.py brain/tests/test_bench_gates.py
git commit -m "feat(bench): Tier 0 gate — entity-type sanity

Deterministic heuristics per entity category (files look like paths,
tools are short and non-sentence, projects are short identifiers).
Unchecked categories are vacuous passes."
```

---

## Task 7: Tier 0 gate — self-consistency via embeddings

**Files:**
- Modify: `brain/src/hippo_brain/bench/gates.py`
- Modify: `brain/tests/test_bench_gates.py`

The existing `hippo_brain.client` has an embedding helper; if not, we call LM Studio's embedding endpoint directly. We compute mean pairwise cosine similarity across N sampled outputs.

- [ ] **Step 1: Append failing tests**

Append to `brain/tests/test_bench_gates.py`:

```python
import math

from hippo_brain.bench.gates import mean_pairwise_cosine, self_consistency_score


def test_mean_pairwise_cosine_identical_vectors():
    v = [1.0, 0.0, 0.0]
    score = mean_pairwise_cosine([v, v, v, v])
    assert math.isclose(score, 1.0, abs_tol=1e-6)


def test_mean_pairwise_cosine_orthogonal_vectors():
    a = [1.0, 0.0]
    b = [0.0, 1.0]
    score = mean_pairwise_cosine([a, b])
    assert math.isclose(score, 0.0, abs_tol=1e-6)


def test_mean_pairwise_cosine_single_vector_returns_nan_marker():
    score = mean_pairwise_cosine([[1.0, 0.0]])
    assert score is None


def test_self_consistency_score_aggregates_per_event():
    # Two events, each with three runs. First event converges; second diverges.
    per_event_vectors = [
        [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0]],  # perfect
        [[1.0, 0.0], [0.0, 1.0], [1.0, 0.0]],  # partial
    ]
    r = self_consistency_score(per_event_vectors)
    assert 0.0 < r.mean < 1.0
    assert r.min < r.mean
    assert r.max > r.mean
    assert r.per_event_scores == pytest.approx([1.0, 0.333333, 1.0 / 3][:2], abs=0.05)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --project brain pytest brain/tests/test_bench_gates.py -v`
Expected: new tests FAIL.

- [ ] **Step 3: Append self-consistency helpers**

Append to `brain/src/hippo_brain/bench/gates.py`:

```python
import math


@dataclass
class SelfConsistencyResult:
    mean: float
    min: float
    max: float
    per_event_scores: list[float]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def mean_pairwise_cosine(vectors: list[list[float]]) -> float | None:
    """Mean of cos(v_i, v_j) across all i < j. None if fewer than 2 vectors."""
    if len(vectors) < 2:
        return None
    total = 0.0
    count = 0
    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            total += _cosine(vectors[i], vectors[j])
            count += 1
    return total / count if count else 0.0


def self_consistency_score(per_event_vectors: list[list[list[float]]]) -> SelfConsistencyResult:
    """Given list of per-event vector lists, return aggregated self-consistency."""
    per_event: list[float] = []
    for vectors in per_event_vectors:
        score = mean_pairwise_cosine(vectors)
        if score is not None:
            per_event.append(score)
    if not per_event:
        return SelfConsistencyResult(mean=0.0, min=0.0, max=0.0, per_event_scores=[])
    return SelfConsistencyResult(
        mean=sum(per_event) / len(per_event),
        min=min(per_event),
        max=max(per_event),
        per_event_scores=per_event,
    )
```

- [ ] **Step 4: Verify tests pass**

Run: `uv run --project brain pytest brain/tests/test_bench_gates.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/gates.py brain/tests/test_bench_gates.py
uv run --project brain ruff format brain/src/hippo_brain/bench/gates.py brain/tests/test_bench_gates.py
git add brain/src/hippo_brain/bench/gates.py brain/tests/test_bench_gates.py
git commit -m "feat(bench): Tier 0 gate — self-consistency cosine aggregation

Pure-function cosine + mean-pairwise aggregator. Embedding-call wiring
happens in the runner task; this task owns the math only."
```

---

## Task 8: `lms` CLI wrapper

**Files:**
- Create: `brain/src/hippo_brain/bench/lms.py`
- Create: `brain/tests/test_bench_lms.py`

- [ ] **Step 1: Write failing tests**

Create `brain/tests/test_bench_lms.py`:

```python
from unittest.mock import patch, MagicMock

import pytest

from hippo_brain.bench.lms import LmsError, LmsUnavailable, ensure_available, list_loaded, load, unload, unload_all


def test_ensure_available_ok_when_binary_present():
    with patch("shutil.which", return_value="/usr/local/bin/lms"):
        # Should not raise.
        ensure_available()


def test_ensure_available_raises_when_binary_missing():
    with patch("shutil.which", return_value=None):
        with pytest.raises(LmsUnavailable):
            ensure_available()


def test_list_loaded_parses_json_output():
    fake_proc = MagicMock(returncode=0, stdout='[{"identifier":"qwen-35b","state":"loaded"}]')
    with patch("subprocess.run", return_value=fake_proc):
        result = list_loaded()
    assert result == [{"identifier": "qwen-35b", "state": "loaded"}]


def test_list_loaded_raises_on_nonzero_exit():
    fake_proc = MagicMock(returncode=1, stdout="", stderr="boom")
    with patch("subprocess.run", return_value=fake_proc):
        with pytest.raises(LmsError):
            list_loaded()


def test_load_invokes_lms_load_with_identifier():
    fake_proc = MagicMock(returncode=0, stdout="")
    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        load("qwen-35b")
    args = mock_run.call_args.args[0]
    assert args[:2] == ["lms", "load"]
    assert "qwen-35b" in args


def test_unload_invokes_lms_unload():
    fake_proc = MagicMock(returncode=0, stdout="")
    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        unload("qwen-35b")
    args = mock_run.call_args.args[0]
    assert args[:2] == ["lms", "unload"]


def test_unload_all_invokes_unload_all():
    fake_proc = MagicMock(returncode=0, stdout="")
    with patch("subprocess.run", return_value=fake_proc) as mock_run:
        unload_all()
    args = mock_run.call_args.args[0]
    assert args[:3] == ["lms", "unload", "--all"]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --project brain pytest brain/tests/test_bench_lms.py -v`
Expected: FAIL — imports missing.

- [ ] **Step 3: Implement lms wrapper**

Create `brain/src/hippo_brain/bench/lms.py`:

```python
"""Subprocess wrapper around LM Studio's `lms` CLI.

`lms` is a hard dependency; absence aborts the bench at pre-flight time.
"""

from __future__ import annotations

import json
import shutil
import subprocess


class LmsUnavailable(RuntimeError):
    """Raised when the `lms` CLI is not on PATH."""


class LmsError(RuntimeError):
    """Raised when an `lms` subprocess call exits non-zero."""


_DEFAULT_TIMEOUT_SEC = 300


def ensure_available() -> None:
    if shutil.which("lms") is None:
        raise LmsUnavailable(
            "`lms` CLI not found on PATH. Install LM Studio "
            "(https://lmstudio.ai) and run `lms --version` to verify."
        )


def _run(args: list[str], timeout: int = _DEFAULT_TIMEOUT_SEC) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def list_loaded() -> list[dict]:
    proc = _run(["lms", "ls", "--json"])
    if proc.returncode != 0:
        raise LmsError(f"lms ls failed: {proc.stderr.strip()}")
    stdout = proc.stdout.strip()
    if not stdout:
        return []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise LmsError(f"could not parse lms ls output: {e}") from e
    if not isinstance(data, list):
        raise LmsError(f"lms ls returned non-list: {type(data).__name__}")
    return data


def load(identifier: str, timeout: int = _DEFAULT_TIMEOUT_SEC) -> None:
    proc = _run(["lms", "load", identifier], timeout=timeout)
    if proc.returncode != 0:
        raise LmsError(f"lms load {identifier!r} failed: {proc.stderr.strip()}")


def unload(identifier: str) -> None:
    proc = _run(["lms", "unload", identifier])
    if proc.returncode != 0:
        raise LmsError(f"lms unload {identifier!r} failed: {proc.stderr.strip()}")


def unload_all() -> None:
    proc = _run(["lms", "unload", "--all"])
    if proc.returncode != 0:
        raise LmsError(f"lms unload --all failed: {proc.stderr.strip()}")
```

- [ ] **Step 4: Verify tests pass**

Run: `uv run --project brain pytest brain/tests/test_bench_lms.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/lms.py brain/tests/test_bench_lms.py
uv run --project brain ruff format brain/src/hippo_brain/bench/lms.py brain/tests/test_bench_lms.py
git add brain/src/hippo_brain/bench/lms.py brain/tests/test_bench_lms.py
git commit -m "feat(bench): lms CLI wrapper with hard-availability gate

Subprocess wrapper for lms ls/load/unload/unload --all. Raises
LmsUnavailable if the binary is absent and LmsError on non-zero exit."
```

---

## Task 9: System metrics sampler

**Files:**
- Create: `brain/src/hippo_brain/bench/metrics.py`
- Create: `brain/tests/test_bench_metrics.py`

- [ ] **Step 1: Write failing tests**

Create `brain/tests/test_bench_metrics.py`:

```python
import time
from unittest.mock import MagicMock, patch

from hippo_brain.bench.metrics import MetricsSampler, Snapshot


def test_snapshot_shape():
    s = Snapshot(
        monotonic_ns=123,
        lmstudio_rss_mb=100.5,
        lmstudio_cpu_pct=50.0,
        load_avg_5s=2.1,
        mem_free_mb=1024.0,
    )
    assert s.lmstudio_rss_mb == 100.5


def test_sampler_finds_lmstudio_process():
    fake_proc = MagicMock()
    fake_proc.name.return_value = "LM Studio Helper"
    fake_proc.pid = 42
    fake_proc.memory_info.return_value = MagicMock(rss=100 * 1024 * 1024)
    fake_proc.cpu_percent.return_value = 75.0

    with patch("psutil.process_iter", return_value=[fake_proc]):
        sampler = MetricsSampler(sample_interval_ms=10)
        pid = sampler._discover_lmstudio_pid()
    assert pid == 42


def test_sampler_aggregates_peak():
    sampler = MetricsSampler(sample_interval_ms=1)
    sampler._samples = [
        Snapshot(1, 100.0, 50.0, 1.0, 2000.0),
        Snapshot(2, 200.0, 90.0, 1.5, 1800.0),
        Snapshot(3, 150.0, 70.0, 1.2, 1900.0),
    ]
    peak = sampler.peak()
    assert peak["lmstudio_rss_mb"] == 200.0
    assert peak["lmstudio_cpu_pct"] == 90.0
    assert peak["load_avg_5s"] == 1.5


def test_sampler_latest_returns_most_recent():
    sampler = MetricsSampler(sample_interval_ms=1)
    sampler._samples = [
        Snapshot(1, 100.0, 50.0, 1.0, 2000.0),
        Snapshot(2, 200.0, 90.0, 1.5, 1800.0),
    ]
    latest = sampler.latest()
    assert latest.monotonic_ns == 2
    assert latest.lmstudio_rss_mb == 200.0


def test_sampler_latest_none_when_empty():
    sampler = MetricsSampler(sample_interval_ms=1)
    assert sampler.latest() is None
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --project brain pytest brain/tests/test_bench_metrics.py -v`
Expected: FAIL — imports missing.

- [ ] **Step 3: Implement metrics sampler**

Create `brain/src/hippo_brain/bench/metrics.py`:

```python
"""Background sampler for per-model system metrics."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

import psutil


@dataclass
class Snapshot:
    monotonic_ns: int
    lmstudio_rss_mb: float
    lmstudio_cpu_pct: float
    load_avg_5s: float
    mem_free_mb: float


_LMSTUDIO_NAME_HINTS = ("lm studio", "lmstudio", "lms")


class MetricsSampler:
    def __init__(self, sample_interval_ms: int = 250):
        self.sample_interval_ms = sample_interval_ms
        self._samples: list[Snapshot] = []
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._pid: int | None = None

    @staticmethod
    def _discover_lmstudio_pid() -> int | None:
        best: int | None = None
        best_rss = 0
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                name = (proc.info.get("name") or "").lower()
            except psutil.Error:
                continue
            if any(hint in name for hint in _LMSTUDIO_NAME_HINTS):
                try:
                    rss = proc.memory_info().rss
                except psutil.Error:
                    continue
                if rss > best_rss:
                    best_rss = rss
                    best = proc.info["pid"]
        return best

    def _sample_once(self, proc: psutil.Process | None) -> Snapshot:
        rss_mb = 0.0
        cpu_pct = 0.0
        if proc is not None:
            try:
                rss_mb = proc.memory_info().rss / (1024 * 1024)
                cpu_pct = proc.cpu_percent(interval=None)
            except psutil.Error:
                pass
        load_1, _, _ = os.getloadavg()
        vm = psutil.virtual_memory()
        return Snapshot(
            monotonic_ns=time.monotonic_ns(),
            lmstudio_rss_mb=rss_mb,
            lmstudio_cpu_pct=cpu_pct,
            load_avg_5s=load_1,
            mem_free_mb=vm.available / (1024 * 1024),
        )

    def _run_loop(self) -> None:
        proc: psutil.Process | None = None
        if self._pid is not None:
            try:
                proc = psutil.Process(self._pid)
                proc.cpu_percent(interval=None)  # prime
            except psutil.Error:
                proc = None
        interval = self.sample_interval_ms / 1000.0
        while not self._stop.is_set():
            self._samples.append(self._sample_once(proc))
            self._stop.wait(interval)

    def start(self) -> None:
        self._pid = self._discover_lmstudio_pid()
        self._stop.clear()
        self._samples = []
        self._thread = threading.Thread(target=self._run_loop, name="bench-metrics", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def latest(self) -> Snapshot | None:
        return self._samples[-1] if self._samples else None

    def peak(self) -> dict[str, float]:
        if not self._samples:
            return {
                "lmstudio_rss_mb": 0.0,
                "lmstudio_cpu_pct": 0.0,
                "load_avg_5s": 0.0,
                "mem_free_mb": 0.0,
            }
        return {
            "lmstudio_rss_mb": max(s.lmstudio_rss_mb for s in self._samples),
            "lmstudio_cpu_pct": max(s.lmstudio_cpu_pct for s in self._samples),
            "load_avg_5s": max(s.load_avg_5s for s in self._samples),
            "mem_free_mb": min(s.mem_free_mb for s in self._samples),
        }
```

- [ ] **Step 4: Verify tests pass**

Run: `uv run --project brain pytest brain/tests/test_bench_metrics.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/metrics.py brain/tests/test_bench_metrics.py
uv run --project brain ruff format brain/src/hippo_brain/bench/metrics.py brain/tests/test_bench_metrics.py
git add brain/src/hippo_brain/bench/metrics.py brain/tests/test_bench_metrics.py
git commit -m "feat(bench): system metrics sampler thread

MetricsSampler discovers LM Studio PID by process name, samples
RSS/CPU/load every 250ms in a daemon thread, exposes peak() and
latest() snapshots for the runner."
```

---

## Task 10: Pre-flight checks

**Files:**
- Create: `brain/src/hippo_brain/bench/preflight.py`
- Create: `brain/tests/test_bench_preflight.py`

- [ ] **Step 1: Write failing tests**

Create `brain/tests/test_bench_preflight.py`:

```python
from unittest.mock import MagicMock, patch

from hippo_brain.bench.preflight import (
    CheckResult,
    check_disk_space,
    check_lms_cli,
    check_lmstudio_reachable,
    check_power_plugged,
    run_all_preflight,
)


def test_check_result_is_dict_serializable():
    r = CheckResult(name="x", status="pass", detail="ok")
    assert r.to_dict() == {"check": "x", "status": "pass", "detail": "ok"}


def test_check_lms_cli_pass(tmp_path):
    with patch("shutil.which", return_value="/usr/local/bin/lms"):
        r = check_lms_cli()
    assert r.status == "pass"


def test_check_lms_cli_fail_aborts():
    with patch("shutil.which", return_value=None):
        r = check_lms_cli()
    assert r.status == "fail"


def test_check_lmstudio_reachable_pass():
    fake_resp = MagicMock(status_code=200)
    with patch("httpx.get", return_value=fake_resp):
        r = check_lmstudio_reachable("http://localhost:1234/v1/models")
    assert r.status == "pass"


def test_check_lmstudio_reachable_fail_on_connection_refused():
    import httpx

    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        r = check_lmstudio_reachable("http://localhost:1234/v1/models")
    assert r.status == "fail"


def test_check_disk_space_pass(tmp_path):
    fake = MagicMock(free=10 * 1024**3)
    with patch("shutil.disk_usage", return_value=fake):
        r = check_disk_space(tmp_path, min_gb=2.0)
    assert r.status == "pass"


def test_check_disk_space_fail(tmp_path):
    fake = MagicMock(free=100 * 1024**2)
    with patch("shutil.disk_usage", return_value=fake):
        r = check_disk_space(tmp_path, min_gb=2.0)
    assert r.status == "fail"


def test_check_power_plugged_warns_on_battery():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Battery Power\n 'Battery' 45%; discharging"
        )
        r = check_power_plugged()
    assert r.status == "warn"


def test_check_power_plugged_pass_when_plugged():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="AC Power\n 'InternalBattery' 100%; charged"
        )
        r = check_power_plugged()
    assert r.status == "pass"


def test_run_all_preflight_aborts_on_hard_fail(tmp_path):
    with (
        patch("shutil.which", return_value=None),  # lms missing
        patch("shutil.disk_usage", return_value=MagicMock(free=10 * 1024**3)),
    ):
        checks = run_all_preflight(tmp_path, lmstudio_url="http://localhost:1234/v1/models")
    assert any(c.status == "fail" for c in checks)
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --project brain pytest brain/tests/test_bench_preflight.py -v`
Expected: FAIL — imports missing.

- [ ] **Step 3: Implement preflight**

Create `brain/src/hippo_brain/bench/preflight.py`:

```python
"""Individual pre-flight checks for bench hygiene."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

Status = Literal["pass", "warn", "fail"]


@dataclass
class CheckResult:
    name: str
    status: Status
    detail: str = ""

    def to_dict(self) -> dict:
        return {"check": self.name, "status": self.status, "detail": self.detail}


def check_lms_cli() -> CheckResult:
    path = shutil.which("lms")
    if path is None:
        return CheckResult(
            name="lms_cli",
            status="fail",
            detail="lms CLI missing. Install LM Studio and run `lms --version`.",
        )
    return CheckResult(name="lms_cli", status="pass", detail=path)


def check_lmstudio_reachable(url: str) -> CheckResult:
    try:
        resp = httpx.get(url, timeout=5.0)
    except httpx.HTTPError as e:
        return CheckResult(
            name="lmstudio_reachable",
            status="fail",
            detail=f"HTTP error contacting {url}: {e}",
        )
    if resp.status_code >= 400:
        return CheckResult(
            name="lmstudio_reachable",
            status="fail",
            detail=f"got HTTP {resp.status_code}",
        )
    return CheckResult(name="lmstudio_reachable", status="pass", detail=f"HTTP {resp.status_code}")


def check_power_plugged() -> CheckResult:
    proc = subprocess.run(["pmset", "-g", "batt"], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return CheckResult(name="power_plugged", status="warn", detail="pmset failed")
    out = proc.stdout
    if "AC Power" in out:
        return CheckResult(name="power_plugged", status="pass", detail="AC")
    if "Battery Power" in out or "discharging" in out:
        return CheckResult(
            name="power_plugged",
            status="warn",
            detail="on battery — results not cross-comparable",
        )
    return CheckResult(name="power_plugged", status="warn", detail="unknown state")


def check_disk_space(path: Path, min_gb: float = 2.0) -> CheckResult:
    usage = shutil.disk_usage(path)
    free_gb = usage.free / (1024**3)
    if free_gb < min_gb:
        return CheckResult(
            name="disk_free",
            status="fail",
            detail=f"only {free_gb:.2f} GB free (need {min_gb})",
        )
    return CheckResult(name="disk_free", status="pass", detail=f"{free_gb:.2f} GB free")


def check_hippo_services() -> CheckResult:
    proc = subprocess.run(
        ["launchctl", "list"], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        return CheckResult(name="hippo_services", status="warn", detail="launchctl list failed")
    running = [line for line in proc.stdout.splitlines() if "com.sjcarpenter.hippo" in line]
    if running:
        return CheckResult(
            name="hippo_services",
            status="warn",
            detail=f"{len(running)} hippo launchd agent(s) running; bench will not pause them in MVP",
        )
    return CheckResult(name="hippo_services", status="pass", detail="no hippo agents running")


def check_spotlight_idle() -> CheckResult:
    proc = subprocess.run(
        ["mdutil", "-s", "/"], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        return CheckResult(name="spotlight", status="warn", detail="mdutil failed")
    if "Indexing enabled" in proc.stdout:
        return CheckResult(name="spotlight", status="warn", detail="indexing enabled")
    return CheckResult(name="spotlight", status="pass", detail="idle")


def run_all_preflight(path: Path, lmstudio_url: str) -> list[CheckResult]:
    return [
        check_lms_cli(),
        check_lmstudio_reachable(lmstudio_url),
        check_power_plugged(),
        check_disk_space(path),
        check_hippo_services(),
        check_spotlight_idle(),
    ]
```

- [ ] **Step 4: Verify tests pass**

Run: `uv run --project brain pytest brain/tests/test_bench_preflight.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/preflight.py brain/tests/test_bench_preflight.py
uv run --project brain ruff format brain/src/hippo_brain/bench/preflight.py brain/tests/test_bench_preflight.py
git add brain/src/hippo_brain/bench/preflight.py brain/tests/test_bench_preflight.py
git commit -m "feat(bench): pre-flight hygiene checks

Individual checks for lms presence, LM Studio reachability, power
state, disk space, hippo service state, Spotlight indexing. Each
returns (name, status, detail); run_all_preflight composes them."
```

---

## Task 11: Corpus sampler + manifest

**Files:**
- Create: `brain/src/hippo_brain/bench/corpus.py`
- Create: `brain/tests/test_bench_corpus.py`

- [ ] **Step 1: Write failing tests**

Create `brain/tests/test_bench_corpus.py`:

```python
import hashlib
import json
import sqlite3

import pytest

from hippo_brain.bench.corpus import (
    CorpusEntry,
    compute_corpus_hash,
    init_corpus,
    load_corpus,
    verify_corpus,
    write_corpus,
)


@pytest.fixture
def tmp_corpus_path(tmp_path):
    return tmp_path / "corpus-v1.jsonl"


@pytest.fixture
def tmp_manifest_path(tmp_path):
    return tmp_path / "corpus-v1.manifest.json"


def test_corpus_entry_hashes_are_deterministic():
    e1 = CorpusEntry(
        event_id="e1", source="shell", redacted_content="ls -la", reference_enrichment=None
    )
    e2 = CorpusEntry(
        event_id="e1", source="shell", redacted_content="ls -la", reference_enrichment=None
    )
    assert e1.content_sha256 == e2.content_sha256


def test_corpus_entry_hash_differs_on_content_change():
    e1 = CorpusEntry(
        event_id="e1", source="shell", redacted_content="ls -la", reference_enrichment=None
    )
    e2 = CorpusEntry(
        event_id="e1", source="shell", redacted_content="ls -la ", reference_enrichment=None
    )
    assert e1.content_sha256 != e2.content_sha256


def test_write_and_load_roundtrip(tmp_corpus_path, tmp_manifest_path):
    entries = [
        CorpusEntry(
            event_id="a", source="shell", redacted_content="echo hi", reference_enrichment=None
        ),
        CorpusEntry(
            event_id="b",
            source="claude",
            redacted_content="convo",
            reference_enrichment={"summary": "x"},
        ),
    ]
    write_corpus(entries, tmp_corpus_path, tmp_manifest_path, corpus_version="corpus-v1", seed=42)
    loaded = list(load_corpus(tmp_corpus_path))
    assert len(loaded) == 2
    assert loaded[0].event_id == "a"
    assert loaded[1].reference_enrichment == {"summary": "x"}


def test_verify_detects_tampering(tmp_corpus_path, tmp_manifest_path):
    entries = [
        CorpusEntry(
            event_id="a", source="shell", redacted_content="echo hi", reference_enrichment=None
        )
    ]
    write_corpus(entries, tmp_corpus_path, tmp_manifest_path, corpus_version="corpus-v1", seed=42)
    # Tamper.
    content = tmp_corpus_path.read_text()
    tmp_corpus_path.write_text(content.replace("echo hi", "rm -rf /"))
    ok, detail = verify_corpus(tmp_corpus_path, tmp_manifest_path)
    assert not ok
    assert "hash" in detail.lower() or "mismatch" in detail.lower()


def test_verify_passes_untampered(tmp_corpus_path, tmp_manifest_path):
    entries = [
        CorpusEntry(
            event_id="a", source="shell", redacted_content="echo hi", reference_enrichment=None
        )
    ]
    write_corpus(entries, tmp_corpus_path, tmp_manifest_path, corpus_version="corpus-v1", seed=42)
    ok, detail = verify_corpus(tmp_corpus_path, tmp_manifest_path)
    assert ok, detail


def test_init_corpus_stratified_sampling(tmp_path, tmp_corpus_path, tmp_manifest_path):
    db_path = tmp_path / "fake.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE events (id INTEGER PRIMARY KEY, source TEXT, payload TEXT);
        """
    )
    for i in range(20):
        conn.execute(
            "INSERT INTO events (source, payload) VALUES (?, ?)",
            ("shell", json.dumps({"command": f"cmd-{i}", "stdout": "ok", "stderr": ""})),
        )
    for i in range(10):
        conn.execute(
            "INSERT INTO events (source, payload) VALUES (?, ?)",
            ("claude", json.dumps({"transcript": f"session-{i}"})),
        )
    conn.commit()
    conn.close()

    entries = init_corpus(
        db_path=db_path,
        fixture_path=tmp_corpus_path,
        manifest_path=tmp_manifest_path,
        corpus_version="corpus-v1",
        source_counts={"shell": 5, "claude": 3, "browser": 0, "workflow": 0},
        seed=42,
    )
    assert len(entries) == 8
    shell_entries = [e for e in entries if e.source == "shell"]
    claude_entries = [e for e in entries if e.source == "claude"]
    assert len(shell_entries) == 5
    assert len(claude_entries) == 3


def test_init_corpus_is_deterministic_with_seed(tmp_path):
    """Two init_corpus calls with the same seed produce identical event ordering."""
    db_path = tmp_path / "fake.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        "CREATE TABLE events (id INTEGER PRIMARY KEY, source TEXT, payload TEXT);"
    )
    for i in range(30):
        conn.execute(
            "INSERT INTO events (source, payload) VALUES (?, ?)",
            ("shell", json.dumps({"command": f"cmd-{i}"})),
        )
    conn.commit()
    conn.close()

    entries_a = init_corpus(
        db_path=db_path,
        fixture_path=tmp_path / "a.jsonl",
        manifest_path=tmp_path / "a.manifest.json",
        corpus_version="corpus-v1",
        source_counts={"shell": 5, "claude": 0, "browser": 0, "workflow": 0},
        seed=42,
    )
    entries_b = init_corpus(
        db_path=db_path,
        fixture_path=tmp_path / "b.jsonl",
        manifest_path=tmp_path / "b.manifest.json",
        corpus_version="corpus-v1",
        source_counts={"shell": 5, "claude": 0, "browser": 0, "workflow": 0},
        seed=42,
    )
    assert [e.event_id for e in entries_a] == [e.event_id for e in entries_b]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --project brain pytest brain/tests/test_bench_corpus.py -v`
Expected: FAIL — imports missing.

- [ ] **Step 3: Implement corpus module**

Create `brain/src/hippo_brain/bench/corpus.py`:

```python
"""Corpus fixture sampling, writing, loading, and verification."""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import random
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class CorpusEntry:
    event_id: str
    source: str
    redacted_content: str
    reference_enrichment: dict | None = None
    content_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        h = hashlib.sha256()
        h.update(self.source.encode("utf-8"))
        h.update(b"\x00")
        h.update(self.redacted_content.encode("utf-8"))
        self.content_sha256 = h.hexdigest()

    def to_json_line(self) -> str:
        return json.dumps(
            {
                "event_id": self.event_id,
                "source": self.source,
                "redacted_content": self.redacted_content,
                "reference_enrichment": self.reference_enrichment,
                "content_sha256": self.content_sha256,
            },
            sort_keys=True,
        )


def compute_corpus_hash(entries: Iterable[CorpusEntry]) -> str:
    h = hashlib.sha256()
    for e in entries:
        h.update(e.content_sha256.encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def write_corpus(
    entries: list[CorpusEntry],
    fixture_path: Path,
    manifest_path: Path,
    corpus_version: str,
    seed: int,
) -> None:
    fixture_path.parent.mkdir(parents=True, exist_ok=True)
    with fixture_path.open("w", encoding="utf-8") as f:
        for e in entries:
            f.write(e.to_json_line())
            f.write("\n")

    source_counts: dict[str, int] = {}
    for e in entries:
        source_counts[e.source] = source_counts.get(e.source, 0) + 1

    manifest: dict[str, Any] = {
        "corpus_version": corpus_version,
        "created_at_iso": _dt.datetime.now(tz=_dt.UTC).isoformat(),
        "seed": seed,
        "source_counts": source_counts,
        "event_ids_sha256": [
            {"event_id": e.event_id, "sha256": e.content_sha256} for e in entries
        ],
        "corpus_content_hash": compute_corpus_hash(entries),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")


def load_corpus(fixture_path: Path) -> Iterable[CorpusEntry]:
    with fixture_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            entry = CorpusEntry(
                event_id=obj["event_id"],
                source=obj["source"],
                redacted_content=obj["redacted_content"],
                reference_enrichment=obj.get("reference_enrichment"),
            )
            # Verify post-load hash still matches what was recorded.
            if entry.content_sha256 != obj["content_sha256"]:
                raise ValueError(
                    f"corpus entry {obj['event_id']!r} content hash mismatch "
                    f"(stored {obj['content_sha256']} vs recomputed {entry.content_sha256})"
                )
            yield entry


def verify_corpus(fixture_path: Path, manifest_path: Path) -> tuple[bool, str]:
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False, f"manifest not found: {manifest_path}"

    try:
        entries = list(load_corpus(fixture_path))
    except (FileNotFoundError, ValueError) as e:
        return False, f"corpus load failed: {e}"

    recomputed = compute_corpus_hash(entries)
    stored = manifest.get("corpus_content_hash")
    if stored != recomputed:
        return (
            False,
            f"corpus content hash mismatch (manifest {stored} vs recomputed {recomputed})",
        )
    return True, "ok"


def init_corpus(
    db_path: Path,
    fixture_path: Path,
    manifest_path: Path,
    corpus_version: str,
    source_counts: dict[str, int],
    seed: int,
) -> list[CorpusEntry]:
    """Stratified random sample from hippo.db events table.

    NOTE: The real hippo schema has separate tables per source (shell_events,
    claude_sessions, browser_events, workflow_runs). For MVP determinism, this
    function queries an "events" table with (id, source, payload) columns; adapt
    to real schema when wiring to production DB. The test fixture matches this
    shape so tests are hermetic.
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rng = random.Random(seed)
    selected: list[CorpusEntry] = []
    for source, count in source_counts.items():
        if count <= 0:
            continue
        rows = conn.execute(
            "SELECT id, payload FROM events WHERE source = ? ORDER BY id", (source,)
        ).fetchall()
        if not rows:
            continue
        picked = rng.sample(rows, k=min(count, len(rows)))
        picked.sort(key=lambda r: r["id"])  # stable order in fixture
        for row in picked:
            selected.append(
                CorpusEntry(
                    event_id=f"{source}-{row['id']}",
                    source=source,
                    redacted_content=row["payload"],
                    reference_enrichment=None,
                )
            )
    conn.close()

    write_corpus(selected, fixture_path, manifest_path, corpus_version, seed)
    return selected
```

- [ ] **Step 4: Verify tests pass**

Run: `uv run --project brain pytest brain/tests/test_bench_corpus.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/corpus.py brain/tests/test_bench_corpus.py
uv run --project brain ruff format brain/src/hippo_brain/bench/corpus.py brain/tests/test_bench_corpus.py
git add brain/src/hippo_brain/bench/corpus.py brain/tests/test_bench_corpus.py
git commit -m "feat(bench): corpus sampler + manifest

Stratified random sampling with fixed seed; manifest records content
hashes for tamper detection. Uses a generic (id, source, payload)
events shape to be adapted to hippo.db production schema in a later
task before first real run."
```

---

## Task 12: Production DB adapter for corpus init

**Files:**
- Modify: `brain/src/hippo_brain/bench/corpus.py`
- Modify: `brain/tests/test_bench_corpus.py`

Context: Task 11 used a simplified `events(id, source, payload)` shape for hermetic tests. This task adds real hippo.db adapters that read from the actual tables: `shell_events`, `claude_sessions`, `browser_events`, `workflow_runs`. Reference [brain/src/hippo_brain/enrichment.py](../../../brain/src/hippo_brain/enrichment.py) for the source-specific field sets.

- [ ] **Step 1: Write failing tests with real schema shapes**

Append to `brain/tests/test_bench_corpus.py`:

```python
from hippo_brain.bench.corpus import sample_from_hippo_db


def _make_hippo_schema(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE shell_events (
            id INTEGER PRIMARY KEY,
            command TEXT, stdout TEXT, stderr TEXT, duration_ms INTEGER,
            exit_code INTEGER, cwd TEXT, ts INTEGER
        );
        CREATE TABLE claude_sessions (
            id INTEGER PRIMARY KEY,
            session_id TEXT, transcript TEXT, message_count INTEGER,
            tool_calls_json TEXT, ts INTEGER
        );
        CREATE TABLE browser_events (
            id INTEGER PRIMARY KEY,
            url TEXT, title TEXT, dwell_ms INTEGER, scroll_depth REAL, ts INTEGER
        );
        CREATE TABLE workflow_runs (
            id INTEGER PRIMARY KEY,
            repo TEXT, workflow_name TEXT, conclusion TEXT, annotations_json TEXT, ts INTEGER
        );
        """
    )
    return conn


def test_sample_from_hippo_db_reads_each_source(tmp_path):
    db_path = tmp_path / "hippo.db"
    conn = _make_hippo_schema(db_path)
    conn.execute(
        "INSERT INTO shell_events (command, stdout, stderr, duration_ms, exit_code, cwd, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ls -la", "file listing", "", 42, 0, "/tmp", 0),
    )
    conn.execute(
        "INSERT INTO claude_sessions (session_id, transcript, message_count, tool_calls_json, ts)"
        " VALUES (?, ?, ?, ?, ?)",
        ("s1", "hello world", 5, "[]", 0),
    )
    conn.execute(
        "INSERT INTO browser_events (url, title, dwell_ms, scroll_depth, ts)"
        " VALUES (?, ?, ?, ?, ?)",
        ("https://docs.python.org/3/", "docs", 30_000, 0.8, 0),
    )
    conn.execute(
        "INSERT INTO workflow_runs (repo, workflow_name, conclusion, annotations_json, ts)"
        " VALUES (?, ?, ?, ?, ?)",
        ("hippo", "ci", "success", "[]", 0),
    )
    conn.commit()
    conn.close()

    entries = sample_from_hippo_db(
        db_path=db_path,
        source_counts={"shell": 1, "claude": 1, "browser": 1, "workflow": 1},
        seed=7,
    )
    sources = {e.source for e in entries}
    assert sources == {"shell", "claude", "browser", "workflow"}
    shell_entry = next(e for e in entries if e.source == "shell")
    assert "ls -la" in shell_entry.redacted_content
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --project brain pytest brain/tests/test_bench_corpus.py::test_sample_from_hippo_db_reads_each_source -v`
Expected: FAIL — `sample_from_hippo_db` doesn't exist.

- [ ] **Step 3: Implement `sample_from_hippo_db`**

Append to `brain/src/hippo_brain/bench/corpus.py`:

```python
from hippo_brain.redaction import redact_text


_SOURCE_QUERIES = {
    "shell": (
        "SELECT id, command, stdout, stderr, duration_ms, exit_code, cwd FROM shell_events",
        lambda row: json.dumps(
            {
                "command": row["command"],
                "stdout": row["stdout"],
                "stderr": row["stderr"],
                "duration_ms": row["duration_ms"],
                "exit_code": row["exit_code"],
                "cwd": row["cwd"],
            },
            sort_keys=True,
        ),
    ),
    "claude": (
        "SELECT id, session_id, transcript, message_count, tool_calls_json FROM claude_sessions",
        lambda row: json.dumps(
            {
                "session_id": row["session_id"],
                "transcript": row["transcript"],
                "message_count": row["message_count"],
                "tool_calls_json": row["tool_calls_json"],
            },
            sort_keys=True,
        ),
    ),
    "browser": (
        "SELECT id, url, title, dwell_ms, scroll_depth FROM browser_events",
        lambda row: json.dumps(
            {
                "url": row["url"],
                "title": row["title"],
                "dwell_ms": row["dwell_ms"],
                "scroll_depth": row["scroll_depth"],
            },
            sort_keys=True,
        ),
    ),
    "workflow": (
        "SELECT id, repo, workflow_name, conclusion, annotations_json FROM workflow_runs",
        lambda row: json.dumps(
            {
                "repo": row["repo"],
                "workflow_name": row["workflow_name"],
                "conclusion": row["conclusion"],
                "annotations_json": row["annotations_json"],
            },
            sort_keys=True,
        ),
    ),
}


def sample_from_hippo_db(
    db_path: Path, source_counts: dict[str, int], seed: int
) -> list[CorpusEntry]:
    """Stratified random sample from the real hippo.db schema."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rng = random.Random(seed)
    selected: list[CorpusEntry] = []
    try:
        for source, count in source_counts.items():
            if count <= 0:
                continue
            query, shape = _SOURCE_QUERIES[source]
            rows = conn.execute(query).fetchall()
            if not rows:
                continue
            picked = rng.sample(rows, k=min(count, len(rows)))
            picked.sort(key=lambda r: r["id"])
            for row in picked:
                raw_payload = shape(row)
                redacted = redact_text(raw_payload)
                selected.append(
                    CorpusEntry(
                        event_id=f"{source}-{row['id']}",
                        source=source,
                        redacted_content=redacted,
                        reference_enrichment=None,
                    )
                )
    finally:
        conn.close()
    return selected
```

**Note:** This task assumes `hippo_brain.redaction.redact_text(str) -> str` exists. If the actual module exposes a different function name, adapt the import and call site accordingly — read `brain/src/hippo_brain/redaction.py` and match its public API before running tests.

- [ ] **Step 4: Verify tests pass**

Run: `uv run --project brain pytest brain/tests/test_bench_corpus.py -v`
Expected: all PASS (both new and existing tests from Task 11).

- [ ] **Step 5: Lint + commit**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/corpus.py brain/tests/test_bench_corpus.py
uv run --project brain ruff format brain/src/hippo_brain/bench/corpus.py brain/tests/test_bench_corpus.py
git add brain/src/hippo_brain/bench/corpus.py brain/tests/test_bench_corpus.py
git commit -m "feat(bench): sample from real hippo.db schema per source

Per-source SELECT + payload-shape functions for shell_events,
claude_sessions, browser_events, workflow_runs. All payloads pass
through redaction.redact_text before storage."
```

---

## Task 13: JSONL output writer

**Files:**
- Create: `brain/src/hippo_brain/bench/output.py`
- Create: `brain/tests/test_bench_output.py`

- [ ] **Step 1: Write failing tests**

Create `brain/tests/test_bench_output.py`:

```python
import json
from pathlib import Path

from hippo_brain.bench.output import (
    AttemptRecord,
    ModelSummaryRecord,
    RunManifestRecord,
    RunWriter,
)


def test_run_manifest_record_serializes():
    r = RunManifestRecord(
        run_id="run-x",
        started_at_iso="2026-04-21T00:00:00Z",
        finished_at_iso=None,
        bench_version="0.1.0",
        host={"hostname": "mac", "os": "darwin", "arch": "arm64"},
        preflight_checks=[{"check": "lms_cli", "status": "pass"}],
        corpus_version="corpus-v1",
        corpus_content_hash="sha256:abc",
        candidate_models=["m1"],
        gate_thresholds={"schema_validity_min": 0.95},
        self_consistency_spec={"events": 5, "runs_per_event": 5},
    )
    d = r.to_dict()
    assert d["record_type"] == "run_manifest"
    assert d["run_id"] == "run-x"


def test_attempt_record_serializes():
    r = AttemptRecord(
        run_id="run-x",
        model={"id": "m1"},
        event={"event_id": "e1", "source": "shell", "content_hash": "h"},
        attempt_idx=0,
        purpose="main",
        timestamps={"start_iso": "t", "start_monotonic_ns": 1, "ttft_ms": 10, "total_ms": 20},
        raw_output="ok",
        parsed_output={"summary": "x"},
        gates={"schema_valid": True},
        system_snapshot={"lmstudio_rss_mb": 100.0},
    )
    d = r.to_dict()
    assert d["record_type"] == "attempt"
    assert d["attempt_idx"] == 0


def test_model_summary_serializes():
    r = ModelSummaryRecord(
        run_id="run-x",
        model={"id": "m1"},
        events_attempted=10,
        attempts_total=15,
        gates={"schema_validity_rate": 0.95},
        system_peak={"rss_max_mb": 200.0, "cpu_pct_max": 90.0, "wall_clock_sec": 60},
        tier0_verdict={"passed": True, "failed_gates": [], "notes": []},
    )
    d = r.to_dict()
    assert d["record_type"] == "model_summary"


def test_writer_emits_manifest_first(tmp_path):
    out = tmp_path / "run.jsonl"
    manifest = RunManifestRecord(
        run_id="r",
        started_at_iso="t",
        finished_at_iso=None,
        bench_version="0.1.0",
        host={},
        preflight_checks=[],
        corpus_version="v",
        corpus_content_hash="h",
        candidate_models=[],
        gate_thresholds={},
        self_consistency_spec={},
    )
    writer = RunWriter(out)
    writer.write_manifest(manifest)
    writer.close()

    lines = out.read_text().splitlines()
    assert len(lines) == 1
    first = json.loads(lines[0])
    assert first["record_type"] == "run_manifest"


def test_writer_appends_records(tmp_path):
    out = tmp_path / "run.jsonl"
    writer = RunWriter(out)
    writer.write_manifest(
        RunManifestRecord(
            run_id="r",
            started_at_iso="t",
            finished_at_iso=None,
            bench_version="0.1.0",
            host={},
            preflight_checks=[],
            corpus_version="v",
            corpus_content_hash="h",
            candidate_models=[],
            gate_thresholds={},
            self_consistency_spec={},
        )
    )
    writer.write_attempt(
        AttemptRecord(
            run_id="r",
            model={"id": "m"},
            event={"event_id": "e", "source": "shell", "content_hash": "h"},
            attempt_idx=0,
            purpose="main",
            timestamps={},
            raw_output="",
            parsed_output=None,
            gates={},
            system_snapshot={},
        )
    )
    writer.close()

    lines = out.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["record_type"] == "attempt"
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --project brain pytest brain/tests/test_bench_output.py -v`
Expected: FAIL — imports missing.

- [ ] **Step 3: Implement output module**

Create `brain/src/hippo_brain/bench/output.py`:

```python
"""JSONL record shapes + writer for bench runs."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class RunManifestRecord:
    run_id: str
    started_at_iso: str
    finished_at_iso: str | None
    bench_version: str
    host: dict[str, Any]
    preflight_checks: list[dict[str, Any]]
    corpus_version: str
    corpus_content_hash: str
    candidate_models: list[str]
    gate_thresholds: dict[str, Any]
    self_consistency_spec: dict[str, Any]
    lmstudio_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"record_type": "run_manifest"}
        d.update(asdict(self))
        return d


@dataclass
class AttemptRecord:
    run_id: str
    model: dict[str, Any]
    event: dict[str, Any]
    attempt_idx: int
    purpose: str  # "main" or "self_consistency"
    timestamps: dict[str, Any]
    raw_output: str
    parsed_output: dict | None
    gates: dict[str, Any]
    system_snapshot: dict[str, Any]
    timeout: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"record_type": "attempt"}
        d.update(asdict(self))
        return d


@dataclass
class ModelSummaryRecord:
    run_id: str
    model: dict[str, Any]
    events_attempted: int
    attempts_total: int
    gates: dict[str, Any]
    system_peak: dict[str, Any]
    tier0_verdict: dict[str, Any]
    cooldown_timeout: bool = False

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"record_type": "model_summary"}
        d.update(asdict(self))
        return d


class RunWriter:
    """Append-only JSONL writer for a single bench run."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.path.open("a", encoding="utf-8")

    def _write(self, obj: dict) -> None:
        self._f.write(json.dumps(obj, sort_keys=True))
        self._f.write("\n")
        self._f.flush()

    def write_manifest(self, r: RunManifestRecord) -> None:
        self._write(r.to_dict())

    def write_attempt(self, r: AttemptRecord) -> None:
        self._write(r.to_dict())

    def write_model_summary(self, r: ModelSummaryRecord) -> None:
        self._write(r.to_dict())

    def close(self) -> None:
        self._f.close()

    def __enter__(self) -> "RunWriter":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
```

- [ ] **Step 4: Verify tests pass**

Run: `uv run --project brain pytest brain/tests/test_bench_output.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/output.py brain/tests/test_bench_output.py
uv run --project brain ruff format brain/src/hippo_brain/bench/output.py brain/tests/test_bench_output.py
git add brain/src/hippo_brain/bench/output.py brain/tests/test_bench_output.py
git commit -m "feat(bench): JSONL record types + append writer

Three @dataclass record shapes (RunManifestRecord, AttemptRecord,
ModelSummaryRecord) with to_dict() prepending 'record_type'. RunWriter
is append-only with line-level flush so tail -f works."
```

---

## Task 14: LM Studio enrichment caller

**Files:**
- Create: `brain/src/hippo_brain/bench/enrich_call.py`
- Create: `brain/tests/test_bench_enrich_call.py`

Context: We need a thin bench-owned caller that sends an event to LM Studio and returns raw text + timing. It reuses prompting patterns from the existing enrichment pipeline but is simpler — no DB writes, no entity resolution.

- [ ] **Step 1: Write failing tests**

Create `brain/tests/test_bench_enrich_call.py`:

```python
from unittest.mock import MagicMock, patch

import pytest

from hippo_brain.bench.enrich_call import (
    CallResult,
    build_prompt,
    call_enrichment,
    call_embedding,
)


def test_build_prompt_includes_payload():
    p = build_prompt("ls -la", source="shell")
    assert "ls -la" in p
    assert "shell" in p.lower()


def test_build_prompt_differs_per_source():
    p_shell = build_prompt("x", "shell")
    p_claude = build_prompt("x", "claude")
    assert p_shell != p_claude


def test_call_enrichment_returns_timing_and_raw():
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {
        "choices": [{"message": {"content": '{"summary":"ok"}'}}]
    }
    with patch("httpx.post", return_value=fake_resp):
        r = call_enrichment(
            base_url="http://localhost:1234/v1",
            model="m1",
            payload="ls -la",
            source="shell",
            timeout_sec=60,
        )
    assert r.raw_output == '{"summary":"ok"}'
    assert r.total_ms > 0
    assert r.timeout is False


def test_call_enrichment_records_timeout():
    import httpx

    with patch("httpx.post", side_effect=httpx.TimeoutException("slow")):
        r = call_enrichment(
            base_url="http://localhost:1234/v1",
            model="m1",
            payload="x",
            source="shell",
            timeout_sec=1,
        )
    assert r.timeout is True
    assert r.raw_output == ""


def test_call_embedding_returns_vector():
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.json.return_value = {"data": [{"embedding": [0.1, 0.2, 0.3]}]}
    with patch("httpx.post", return_value=fake_resp):
        v = call_embedding(
            base_url="http://localhost:1234/v1",
            model="nomic-embed-text",
            text="hello",
            timeout_sec=60,
        )
    assert v == [0.1, 0.2, 0.3]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --project brain pytest brain/tests/test_bench_enrich_call.py -v`
Expected: FAIL — imports missing.

- [ ] **Step 3: Implement enrich_call module**

Create `brain/src/hippo_brain/bench/enrich_call.py`:

```python
"""Bench-owned enrichment + embedding HTTP calls to LM Studio.

Intentionally independent from hippo_brain.client so bench can call
arbitrary candidate models without disturbing production telemetry.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx


_PROMPT_TEMPLATES = {
    "shell": (
        "Summarize this shell event. Return JSON only with keys: "
        'summary (string), intent (string), outcome (one of: success, partial, '
        "failure, unknown), entities (object with keys: projects, tools, files, "
        "services, errors, each a list of strings).\n\n"
        "Event: {payload}"
    ),
    "claude": (
        "Summarize this Claude session. Return JSON only with keys: "
        "summary (string), entities (object with keys: projects, topics, files, "
        "decisions, errors, each a list of strings).\n\n"
        "Session: {payload}"
    ),
    "browser": (
        "Summarize this browser visit. Return JSON only with keys: "
        "summary (string), entities (object with keys: topics, urls, projects, "
        "each a list of strings).\n\n"
        "Visit: {payload}"
    ),
    "workflow": (
        "Summarize this CI workflow run. Return JSON only with keys: "
        "summary (string), entities (object with keys: projects, jobs, errors, "
        "each a list of strings).\n\n"
        "Run: {payload}"
    ),
}


@dataclass
class CallResult:
    raw_output: str
    ttft_ms: int | None
    total_ms: int
    timeout: bool


def build_prompt(payload: str, source: str) -> str:
    template = _PROMPT_TEMPLATES.get(source)
    if template is None:
        raise ValueError(f"unknown source {source!r}")
    return template.format(payload=payload)


def call_enrichment(
    base_url: str, model: str, payload: str, source: str, timeout_sec: int
) -> CallResult:
    prompt = build_prompt(payload, source)
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": "You emit strict JSON. No prose, no code fences."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.1,
    }
    start = time.monotonic()
    try:
        resp = httpx.post(url, json=body, timeout=timeout_sec)
    except httpx.TimeoutException:
        total_ms = int((time.monotonic() - start) * 1000)
        return CallResult(raw_output="", ttft_ms=None, total_ms=total_ms, timeout=True)
    total_ms = int((time.monotonic() - start) * 1000)
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    return CallResult(raw_output=content, ttft_ms=None, total_ms=total_ms, timeout=False)


def call_embedding(
    base_url: str, model: str, text: str, timeout_sec: int = 60
) -> list[float]:
    url = f"{base_url.rstrip('/')}/embeddings"
    resp = httpx.post(url, json={"model": model, "input": text}, timeout=timeout_sec)
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]
```

- [ ] **Step 4: Verify tests pass**

Run: `uv run --project brain pytest brain/tests/test_bench_enrich_call.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/enrich_call.py brain/tests/test_bench_enrich_call.py
uv run --project brain ruff format brain/src/hippo_brain/bench/enrich_call.py brain/tests/test_bench_enrich_call.py
git add brain/src/hippo_brain/bench/enrich_call.py brain/tests/test_bench_enrich_call.py
git commit -m "feat(bench): bench-owned enrichment + embedding caller

Independent from production client so bench can swap candidate models
without affecting OTel telemetry. Per-source prompt templates aligned
with the schemas module."
```

---

## Task 15: Runner — main pass + self-consistency pass

**Files:**
- Create: `brain/src/hippo_brain/bench/runner.py`
- Create: `brain/tests/test_bench_runner.py`

- [ ] **Step 1: Write failing tests**

Create `brain/tests/test_bench_runner.py`:

```python
from unittest.mock import MagicMock, patch

from hippo_brain.bench.corpus import CorpusEntry
from hippo_brain.bench.enrich_call import CallResult
from hippo_brain.bench.runner import run_model_main_pass, run_self_consistency_pass


@patch("hippo_brain.bench.runner.call_enrichment")
def test_main_pass_produces_one_attempt_per_event(mock_call):
    mock_call.return_value = CallResult(
        raw_output=(
            '{"summary": "ok", "intent": "x", "outcome": "success", '
            '"entities": {"projects": [], "tools": [], "files": [], "services": [], "errors": []}}'
        ),
        ttft_ms=None,
        total_ms=100,
        timeout=False,
    )
    entries = [
        CorpusEntry(event_id="e1", source="shell", redacted_content="ls"),
        CorpusEntry(event_id="e2", source="shell", redacted_content="pwd"),
    ]
    attempts = run_model_main_pass(
        base_url="http://x",
        model="m1",
        entries=entries,
        timeout_sec=10,
        metrics_snapshot=lambda: {"lmstudio_rss_mb": 100.0},
    )
    assert len(attempts) == 2
    assert all(a.purpose == "main" for a in attempts)
    assert attempts[0].event["event_id"] == "e1"


@patch("hippo_brain.bench.runner.call_embedding")
@patch("hippo_brain.bench.runner.call_enrichment")
def test_self_consistency_pass_embeds_each_output(mock_call, mock_embed):
    mock_call.return_value = CallResult(
        raw_output='{"summary": "ok", "intent": "x", "outcome": "success", "entities": {}}',
        ttft_ms=None,
        total_ms=50,
        timeout=False,
    )
    mock_embed.return_value = [1.0, 0.0, 0.0]
    entries = [
        CorpusEntry(event_id="e1", source="shell", redacted_content="ls"),
        CorpusEntry(event_id="e2", source="shell", redacted_content="pwd"),
    ]
    attempts, per_event_vectors = run_self_consistency_pass(
        base_url="http://x",
        model="m1",
        entries=entries,
        runs_per_event=3,
        embedding_model="nomic",
        timeout_sec=10,
        metrics_snapshot=lambda: {"lmstudio_rss_mb": 0.0},
    )
    assert len(attempts) == 2 * 3
    assert len(per_event_vectors) == 2
    assert all(len(v) == 3 for v in per_event_vectors)


@patch("hippo_brain.bench.runner.call_enrichment")
def test_main_pass_marks_timeouts(mock_call):
    mock_call.return_value = CallResult(
        raw_output="", ttft_ms=None, total_ms=1000, timeout=True
    )
    entries = [CorpusEntry(event_id="e1", source="shell", redacted_content="ls")]
    attempts = run_model_main_pass(
        base_url="http://x",
        model="m1",
        entries=entries,
        timeout_sec=1,
        metrics_snapshot=lambda: {},
    )
    assert attempts[0].timeout is True
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --project brain pytest brain/tests/test_bench_runner.py -v`
Expected: FAIL — imports missing.

- [ ] **Step 3: Implement runner**

Create `brain/src/hippo_brain/bench/runner.py`:

```python
"""Per-model passes: main (one attempt per event) + self-consistency."""

from __future__ import annotations

import datetime as _dt
import time
from collections.abc import Callable

from hippo_brain.bench.corpus import CorpusEntry
from hippo_brain.bench.enrich_call import call_embedding, call_enrichment
from hippo_brain.bench.gates import (
    check_entity_sanity,
    check_refusal_pathology,
    check_schema_validity,
)
from hippo_brain.bench.output import AttemptRecord


def _event_dict(entry: CorpusEntry) -> dict:
    return {
        "event_id": entry.event_id,
        "source": entry.source,
        "content_hash": entry.content_sha256,
    }


def _build_attempt(
    run_id: str,
    model: dict,
    entry: CorpusEntry,
    attempt_idx: int,
    purpose: str,
    call_result,
    gates: dict,
    parsed: dict | None,
    system_snapshot: dict,
) -> AttemptRecord:
    start_iso = _dt.datetime.now(tz=_dt.UTC).isoformat()
    return AttemptRecord(
        run_id=run_id,
        model=model,
        event=_event_dict(entry),
        attempt_idx=attempt_idx,
        purpose=purpose,
        timestamps={
            "start_iso": start_iso,
            "ttft_ms": call_result.ttft_ms,
            "total_ms": call_result.total_ms,
        },
        raw_output=call_result.raw_output,
        parsed_output=parsed,
        gates=gates,
        system_snapshot=system_snapshot,
        timeout=call_result.timeout,
    )


def _compute_gates(call_result, entry: CorpusEntry) -> tuple[dict, dict | None]:
    if call_result.timeout:
        return (
            {
                "schema_valid": False,
                "schema_errors": ["timeout"],
                "refusal_detected": False,
                "refusal_patterns_matched": [],
                "echo_similarity": 0.0,
                "entity_type_sanity": {},
            },
            None,
        )
    schema_result = check_schema_validity(call_result.raw_output, entry.source)
    refusal_result = check_refusal_pathology(
        raw_output=call_result.raw_output,
        input_text=entry.redacted_content,
        parsed=schema_result.parsed,
    )
    entity_sanity = (
        check_entity_sanity(schema_result.parsed, entry.source)
        if schema_result.parsed
        else None
    )
    return (
        {
            "schema_valid": schema_result.passed,
            "schema_errors": schema_result.errors,
            "refusal_detected": refusal_result.refusal_detected,
            "refusal_patterns_matched": refusal_result.refusal_patterns_matched,
            "trivial_summary": refusal_result.trivial_summary,
            "echo_similarity": refusal_result.echo_similarity,
            "entity_type_sanity": (
                entity_sanity.per_category_rates if entity_sanity is not None else {}
            ),
        },
        schema_result.parsed,
    )


def run_model_main_pass(
    *,
    base_url: str,
    model: str,
    entries: list[CorpusEntry],
    timeout_sec: int,
    metrics_snapshot: Callable[[], dict],
    run_id: str = "run-local",
) -> list[AttemptRecord]:
    model_dict = {"id": model}
    attempts: list[AttemptRecord] = []
    for entry in entries:
        cr = call_enrichment(
            base_url=base_url,
            model=model,
            payload=entry.redacted_content,
            source=entry.source,
            timeout_sec=timeout_sec,
        )
        gates, parsed = _compute_gates(cr, entry)
        attempts.append(
            _build_attempt(
                run_id=run_id,
                model=model_dict,
                entry=entry,
                attempt_idx=0,
                purpose="main",
                call_result=cr,
                gates=gates,
                parsed=parsed,
                system_snapshot=metrics_snapshot(),
            )
        )
    return attempts


def run_self_consistency_pass(
    *,
    base_url: str,
    model: str,
    entries: list[CorpusEntry],
    runs_per_event: int,
    embedding_model: str,
    timeout_sec: int,
    metrics_snapshot: Callable[[], dict],
    run_id: str = "run-local",
) -> tuple[list[AttemptRecord], list[list[list[float]]]]:
    model_dict = {"id": model}
    attempts: list[AttemptRecord] = []
    per_event_vectors: list[list[list[float]]] = []
    for entry in entries:
        event_vectors: list[list[float]] = []
        for i in range(runs_per_event):
            cr = call_enrichment(
                base_url=base_url,
                model=model,
                payload=entry.redacted_content,
                source=entry.source,
                timeout_sec=timeout_sec,
            )
            gates, parsed = _compute_gates(cr, entry)
            attempts.append(
                _build_attempt(
                    run_id=run_id,
                    model=model_dict,
                    entry=entry,
                    attempt_idx=i,
                    purpose="self_consistency",
                    call_result=cr,
                    gates=gates,
                    parsed=parsed,
                    system_snapshot=metrics_snapshot(),
                )
            )
            if not cr.timeout and cr.raw_output:
                try:
                    vec = call_embedding(
                        base_url=base_url,
                        model=embedding_model,
                        text=cr.raw_output,
                        timeout_sec=timeout_sec,
                    )
                    event_vectors.append(vec)
                except Exception:  # noqa: BLE001 — embedding failures are informational
                    pass
        per_event_vectors.append(event_vectors)
    return attempts, per_event_vectors
```

- [ ] **Step 4: Verify tests pass**

Run: `uv run --project brain pytest brain/tests/test_bench_runner.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/runner.py brain/tests/test_bench_runner.py
uv run --project brain ruff format brain/src/hippo_brain/bench/runner.py brain/tests/test_bench_runner.py
git add brain/src/hippo_brain/bench/runner.py brain/tests/test_bench_runner.py
git commit -m "feat(bench): per-model main + self-consistency passes

run_model_main_pass iterates corpus once per entry; run_self_
consistency_pass runs each of N events runs_per_event times and embeds
outputs for downstream cosine aggregation."
```

---

## Task 16: Model summary aggregation

**Files:**
- Create: `brain/src/hippo_brain/bench/summary.py`
- Create: `brain/tests/test_bench_summary.py`

- [ ] **Step 1: Write failing tests**

Create `brain/tests/test_bench_summary.py`:

```python
from hippo_brain.bench.output import AttemptRecord
from hippo_brain.bench.summary import aggregate_model_summary, compute_verdict


def _attempt(schema_valid=True, refusal=False, total_ms=1000, purpose="main", event_id="e1"):
    return AttemptRecord(
        run_id="r",
        model={"id": "m"},
        event={"event_id": event_id, "source": "shell", "content_hash": "h"},
        attempt_idx=0,
        purpose=purpose,
        timestamps={"total_ms": total_ms},
        raw_output="",
        parsed_output=None,
        gates={
            "schema_valid": schema_valid,
            "refusal_detected": refusal,
            "echo_similarity": 0.1,
            "entity_type_sanity": {"files": 1.0, "tools": 1.0},
        },
        system_snapshot={},
    )


def test_aggregate_schema_validity_rate():
    attempts = [
        _attempt(schema_valid=True),
        _attempt(schema_valid=True),
        _attempt(schema_valid=False, event_id="e2"),
    ]
    gates = aggregate_model_summary(
        attempts=attempts,
        self_consistency_mean=0.85,
        self_consistency_min=0.8,
    )
    assert gates["schema_validity_rate"] == 2 / 3
    assert gates["self_consistency_mean"] == 0.85


def test_aggregate_latency_percentiles():
    attempts = [_attempt(total_ms=v) for v in [100, 200, 300, 400, 500, 10_000]]
    gates = aggregate_model_summary(
        attempts=attempts,
        self_consistency_mean=0.9,
        self_consistency_min=0.8,
    )
    # p95 on 6 samples: index 5 (0-based) = 10_000
    assert gates["latency_p95_ms"] == 10_000


def test_verdict_pass_when_all_gates_pass():
    thresholds = {
        "schema_validity_min": 0.95,
        "refusal_max": 0.0,
        "latency_p95_max_ms": 60_000,
        "self_consistency_min": 0.7,
        "entity_sanity_min": 0.9,
    }
    gates = {
        "schema_validity_rate": 1.0,
        "refusal_rate": 0.0,
        "latency_p95_ms": 30_000,
        "self_consistency_mean": 0.9,
        "entity_sanity_mean": 0.95,
    }
    v = compute_verdict(gates, thresholds)
    assert v["passed"] is True
    assert v["failed_gates"] == []


def test_verdict_fail_lists_offending_gates():
    thresholds = {
        "schema_validity_min": 0.95,
        "refusal_max": 0.0,
        "latency_p95_max_ms": 60_000,
        "self_consistency_min": 0.7,
        "entity_sanity_min": 0.9,
    }
    gates = {
        "schema_validity_rate": 0.90,
        "refusal_rate": 0.1,
        "latency_p95_ms": 30_000,
        "self_consistency_mean": 0.8,
        "entity_sanity_mean": 0.95,
    }
    v = compute_verdict(gates, thresholds)
    assert v["passed"] is False
    assert "schema_validity_rate" in v["failed_gates"]
    assert "refusal_rate" in v["failed_gates"]
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --project brain pytest brain/tests/test_bench_summary.py -v`
Expected: FAIL — imports missing.

- [ ] **Step 3: Implement summary module**

Create `brain/src/hippo_brain/bench/summary.py`:

```python
"""Model-summary aggregation across all attempts for a single model."""

from __future__ import annotations

from hippo_brain.bench.output import AttemptRecord


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = int(round(pct * (len(s) - 1)))
    return s[k]


def aggregate_model_summary(
    attempts: list[AttemptRecord],
    self_consistency_mean: float,
    self_consistency_min: float,
) -> dict:
    total = len(attempts)
    if total == 0:
        return {
            "schema_validity_rate": 0.0,
            "refusal_rate": 0.0,
            "latency_p50_ms": 0,
            "latency_p95_ms": 0,
            "latency_p99_ms": 0,
            "self_consistency_mean": self_consistency_mean,
            "self_consistency_min": self_consistency_min,
            "entity_sanity_mean": 0.0,
        }

    valid = sum(1 for a in attempts if a.gates.get("schema_valid"))
    refusals = sum(1 for a in attempts if a.gates.get("refusal_detected"))
    latencies = [a.timestamps.get("total_ms", 0) for a in attempts]

    entity_rates: list[float] = []
    for a in attempts:
        per_cat = a.gates.get("entity_type_sanity", {})
        if isinstance(per_cat, dict) and per_cat:
            entity_rates.extend(per_cat.values())

    return {
        "schema_validity_rate": valid / total,
        "refusal_rate": refusals / total,
        "latency_p50_ms": int(_percentile(latencies, 0.50)),
        "latency_p95_ms": int(_percentile(latencies, 0.95)),
        "latency_p99_ms": int(_percentile(latencies, 0.99)),
        "self_consistency_mean": self_consistency_mean,
        "self_consistency_min": self_consistency_min,
        "entity_sanity_mean": sum(entity_rates) / len(entity_rates) if entity_rates else 1.0,
    }


def compute_verdict(gates: dict, thresholds: dict) -> dict:
    failed: list[str] = []
    if gates.get("schema_validity_rate", 0) < thresholds["schema_validity_min"]:
        failed.append("schema_validity_rate")
    if gates.get("refusal_rate", 1) > thresholds["refusal_max"]:
        failed.append("refusal_rate")
    if gates.get("latency_p95_ms", 0) > thresholds["latency_p95_max_ms"]:
        failed.append("latency_p95_ms")
    if gates.get("self_consistency_mean", 0) < thresholds["self_consistency_min"]:
        failed.append("self_consistency_mean")
    if gates.get("entity_sanity_mean", 0) < thresholds["entity_sanity_min"]:
        failed.append("entity_sanity_mean")
    return {"passed": not failed, "failed_gates": failed, "notes": []}
```

- [ ] **Step 4: Verify tests pass**

Run: `uv run --project brain pytest brain/tests/test_bench_summary.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/summary.py brain/tests/test_bench_summary.py
uv run --project brain ruff format brain/src/hippo_brain/bench/summary.py brain/tests/test_bench_summary.py
git add brain/src/hippo_brain/bench/summary.py brain/tests/test_bench_summary.py
git commit -m "feat(bench): model summary aggregation + verdict derivation

aggregate_model_summary computes schema validity rate, refusal rate,
latency percentiles, entity-sanity mean. compute_verdict derives pass
or fail with the list of offending gates."
```

---

## Task 17: Coordinator — per-model orchestration

**Files:**
- Create: `brain/src/hippo_brain/bench/coordinator.py`
- Create: `brain/tests/test_bench_coordinator.py`

- [ ] **Step 1: Write failing tests**

Create `brain/tests/test_bench_coordinator.py`:

```python
from unittest.mock import MagicMock, patch

from hippo_brain.bench.coordinator import run_one_model


@patch("hippo_brain.bench.coordinator.lms")
@patch("hippo_brain.bench.coordinator.MetricsSampler")
@patch("hippo_brain.bench.coordinator.run_self_consistency_pass")
@patch("hippo_brain.bench.coordinator.run_model_main_pass")
@patch("hippo_brain.bench.coordinator.call_enrichment")
@patch("hippo_brain.bench.coordinator.time.sleep", lambda _: None)
def test_run_one_model_lifecycle(
    mock_warmup, mock_main, mock_sc, mock_sampler_cls, mock_lms, tmp_path
):
    mock_lms.list_loaded.return_value = [{"identifier": "m1"}]
    mock_sampler = MagicMock()
    mock_sampler.peak.return_value = {"lmstudio_rss_mb": 100.0, "load_avg_5s": 1.0, "mem_free_mb": 1000.0, "lmstudio_cpu_pct": 50.0}
    mock_sampler.latest.return_value = MagicMock(
        lmstudio_rss_mb=100.0, lmstudio_cpu_pct=50.0, load_avg_5s=1.0, mem_free_mb=1000.0
    )
    mock_sampler_cls.return_value = mock_sampler
    mock_main.return_value = []
    mock_sc.return_value = ([], [])

    result = run_one_model(
        model="m1",
        base_url="http://x/v1",
        entries=[],
        sc_entries=[],
        runs_per_event=3,
        embedding_model="nomic",
        timeout_sec=10,
        warmup_calls=2,
        cooldown_max_sec=0,
        run_id="r",
    )
    assert result.model == "m1"
    mock_lms.unload_all.assert_called_once()
    assert mock_lms.load.call_args.args[0] == "m1"
    mock_lms.unload.assert_called_once()
    assert mock_warmup.call_count == 2


@patch("hippo_brain.bench.coordinator.lms")
@patch("hippo_brain.bench.coordinator.MetricsSampler")
@patch("hippo_brain.bench.coordinator.run_self_consistency_pass")
@patch("hippo_brain.bench.coordinator.run_model_main_pass")
@patch("hippo_brain.bench.coordinator.call_enrichment")
def test_run_one_model_unloads_on_exception(
    mock_warmup, mock_main, mock_sc, mock_sampler_cls, mock_lms
):
    mock_sampler = MagicMock()
    mock_sampler.peak.return_value = {}
    mock_sampler.latest.return_value = None
    mock_sampler_cls.return_value = mock_sampler
    mock_lms.load.side_effect = None
    mock_main.side_effect = RuntimeError("boom")

    try:
        run_one_model(
            model="m1",
            base_url="http://x/v1",
            entries=[],
            sc_entries=[],
            runs_per_event=1,
            embedding_model="nomic",
            timeout_sec=10,
            warmup_calls=0,
            cooldown_max_sec=0,
            run_id="r",
        )
    except RuntimeError:
        pass

    mock_lms.unload.assert_called_with("m1")
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --project brain pytest brain/tests/test_bench_coordinator.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement coordinator**

Create `brain/src/hippo_brain/bench/coordinator.py`:

```python
"""Per-model lifecycle: unload → load → warmup → main → self-consistency → unload → cooldown."""

from __future__ import annotations

import time
from dataclasses import dataclass

from hippo_brain.bench import lms
from hippo_brain.bench.corpus import CorpusEntry
from hippo_brain.bench.enrich_call import call_enrichment
from hippo_brain.bench.metrics import MetricsSampler
from hippo_brain.bench.output import AttemptRecord
from hippo_brain.bench.runner import run_model_main_pass, run_self_consistency_pass


@dataclass
class ModelRunResult:
    model: str
    attempts: list[AttemptRecord]
    per_event_vectors: list[list[list[float]]]
    peak_metrics: dict
    wall_clock_sec: int
    cooldown_timeout: bool


def _snapshot_fn(sampler: MetricsSampler):
    def fn() -> dict:
        s = sampler.latest()
        if s is None:
            return {}
        return {
            "lmstudio_rss_mb": s.lmstudio_rss_mb,
            "lmstudio_cpu_pct": s.lmstudio_cpu_pct,
            "load_avg_5s": s.load_avg_5s,
            "mem_free_mb": s.mem_free_mb,
        }

    return fn


def run_one_model(
    *,
    model: str,
    base_url: str,
    entries: list[CorpusEntry],
    sc_entries: list[CorpusEntry],
    runs_per_event: int,
    embedding_model: str,
    timeout_sec: int,
    warmup_calls: int,
    cooldown_max_sec: int,
    run_id: str,
) -> ModelRunResult:
    lms.unload_all()
    time.sleep(1)
    lms.load(model)

    # Warmup.
    for _ in range(warmup_calls):
        try:
            call_enrichment(
                base_url=base_url,
                model=model,
                payload="warmup",
                source="shell",
                timeout_sec=timeout_sec,
            )
        except Exception:  # noqa: BLE001 — warmup failures don't block the run
            pass

    sampler = MetricsSampler(sample_interval_ms=250)
    sampler.start()
    start = time.monotonic()
    cooldown_timeout = False
    try:
        main_attempts = run_model_main_pass(
            base_url=base_url,
            model=model,
            entries=entries,
            timeout_sec=timeout_sec,
            metrics_snapshot=_snapshot_fn(sampler),
            run_id=run_id,
        )
        sc_attempts, per_event_vectors = run_self_consistency_pass(
            base_url=base_url,
            model=model,
            entries=sc_entries,
            runs_per_event=runs_per_event,
            embedding_model=embedding_model,
            timeout_sec=timeout_sec,
            metrics_snapshot=_snapshot_fn(sampler),
            run_id=run_id,
        )
        attempts = main_attempts + sc_attempts
    finally:
        sampler.stop()
        wall_clock_sec = int(time.monotonic() - start)
        peak = sampler.peak()
        try:
            lms.unload(model)
        except lms.LmsError:
            pass

    cooldown_start = time.monotonic()
    while time.monotonic() - cooldown_start < cooldown_max_sec:
        s = sampler._sample_once(None)  # ad-hoc probe
        if s.load_avg_5s < 2.0:
            break
        time.sleep(2)
    else:
        if cooldown_max_sec > 0:
            cooldown_timeout = True

    return ModelRunResult(
        model=model,
        attempts=attempts,
        per_event_vectors=per_event_vectors,
        peak_metrics=peak,
        wall_clock_sec=wall_clock_sec,
        cooldown_timeout=cooldown_timeout,
    )
```

- [ ] **Step 4: Verify tests pass**

Run: `uv run --project brain pytest brain/tests/test_bench_coordinator.py -v`
Expected: all PASS.

- [ ] **Step 5: Lint + commit**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/coordinator.py brain/tests/test_bench_coordinator.py
uv run --project brain ruff format brain/src/hippo_brain/bench/coordinator.py brain/tests/test_bench_coordinator.py
git add brain/src/hippo_brain/bench/coordinator.py brain/tests/test_bench_coordinator.py
git commit -m "feat(bench): per-model coordinator — load, warmup, run, unload, cooldown

run_one_model encapsulates a single candidate's full lifecycle with
metrics sampling and best-effort unload on exception."
```

---

## Task 18: Wire `hippo-bench run` into CLI

**Files:**
- Modify: `brain/src/hippo_brain/bench/cli.py`
- Create: `brain/src/hippo_brain/bench/orchestrate.py`
- Create: `brain/tests/test_bench_orchestrate.py`

- [ ] **Step 1: Write failing test for full orchestration (dry-run mode)**

Create `brain/tests/test_bench_orchestrate.py`:

```python
from pathlib import Path
from unittest.mock import patch

from hippo_brain.bench.orchestrate import orchestrate_run


def test_orchestrate_dry_run_produces_manifest_only(tmp_path):
    # Empty corpus, no models — dry_run produces only the manifest.
    fixture = tmp_path / "corpus-v1.jsonl"
    manifest = tmp_path / "corpus-v1.manifest.json"
    fixture.write_text("")
    manifest.write_text('{"corpus_content_hash": "sha256:empty", "corpus_version": "corpus-v1"}')
    out = tmp_path / "run.jsonl"

    with patch("hippo_brain.bench.orchestrate.run_all_preflight") as mock_pf:
        mock_pf.return_value = []
        result = orchestrate_run(
            candidate_models=[],
            corpus_version="corpus-v1",
            fixture_path=fixture,
            manifest_path=manifest,
            base_url="http://localhost:1234/v1",
            embedding_model="nomic",
            out_path=out,
            timeout_sec=60,
            self_consistency_events=0,
            self_consistency_runs=0,
            skip_checks=True,
            dry_run=True,
        )
    assert out.exists()
    lines = out.read_text().splitlines()
    assert len(lines) == 1
    import json

    assert json.loads(lines[0])["record_type"] == "run_manifest"
    assert result.models_completed == []
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --project brain pytest brain/tests/test_bench_orchestrate.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement orchestrate module**

Create `brain/src/hippo_brain/bench/orchestrate.py`:

```python
"""Top-level orchestrator: pre-flight → per-model coordinator → summarize → write JSONL."""

from __future__ import annotations

import datetime as _dt
import json
import platform
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psutil

from hippo_brain.bench import __version__
from hippo_brain.bench.config import DEFAULT_THRESHOLDS
from hippo_brain.bench.coordinator import run_one_model
from hippo_brain.bench.corpus import load_corpus
from hippo_brain.bench.gates import self_consistency_score
from hippo_brain.bench.output import (
    ModelSummaryRecord,
    RunManifestRecord,
    RunWriter,
)
from hippo_brain.bench.preflight import run_all_preflight
from hippo_brain.bench.summary import aggregate_model_summary, compute_verdict


@dataclass
class OrchestrationResult:
    run_id: str
    out_path: Path
    models_completed: list[str] = field(default_factory=list)
    preflight_aborted: bool = False


def _build_run_id() -> str:
    ts = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%dT%H%M%S")
    return f"run-{ts}-{platform.node()}"


def _host_info() -> dict:
    vm = psutil.virtual_memory()
    return {
        "hostname": platform.node(),
        "os": f"{platform.system().lower()} {platform.release()}",
        "arch": platform.machine(),
        "cpu_brand": platform.processor() or "unknown",
        "total_mem_gb": round(vm.total / (1024**3), 1),
    }


def orchestrate_run(
    *,
    candidate_models: list[str],
    corpus_version: str,
    fixture_path: Path,
    manifest_path: Path,
    base_url: str,
    embedding_model: str,
    out_path: Path,
    timeout_sec: int,
    self_consistency_events: int,
    self_consistency_runs: int,
    skip_checks: bool,
    dry_run: bool,
) -> OrchestrationResult:
    run_id = _build_run_id()

    # Load manifest for corpus content hash.
    corpus_content_hash = "sha256:unknown"
    try:
        manifest_obj = json.loads(manifest_path.read_text(encoding="utf-8"))
        corpus_content_hash = manifest_obj.get("corpus_content_hash", "sha256:unknown")
    except FileNotFoundError:
        pass

    preflight = (
        [] if skip_checks else run_all_preflight(out_path.parent, f"{base_url}/models")
    )
    preflight_failed = any(c.status == "fail" for c in preflight)

    writer = RunWriter(out_path)
    manifest_record = RunManifestRecord(
        run_id=run_id,
        started_at_iso=_dt.datetime.now(tz=_dt.UTC).isoformat(),
        finished_at_iso=None,
        bench_version=__version__,
        host=_host_info(),
        preflight_checks=[c.to_dict() for c in preflight],
        corpus_version=corpus_version,
        corpus_content_hash=corpus_content_hash,
        candidate_models=list(candidate_models),
        gate_thresholds=dict(DEFAULT_THRESHOLDS),
        self_consistency_spec={
            "events": self_consistency_events,
            "runs_per_event": self_consistency_runs,
        },
    )
    writer.write_manifest(manifest_record)

    if dry_run or preflight_failed or not candidate_models:
        writer.close()
        return OrchestrationResult(
            run_id=run_id,
            out_path=out_path,
            models_completed=[],
            preflight_aborted=preflight_failed,
        )

    entries = list(load_corpus(fixture_path))
    sc_entries = entries[:self_consistency_events]

    completed: list[str] = []
    for model in candidate_models:
        result = run_one_model(
            model=model,
            base_url=base_url,
            entries=entries,
            sc_entries=sc_entries,
            runs_per_event=self_consistency_runs,
            embedding_model=embedding_model,
            timeout_sec=timeout_sec,
            warmup_calls=3,
            cooldown_max_sec=90,
            run_id=run_id,
        )
        for a in result.attempts:
            writer.write_attempt(a)

        sc = self_consistency_score(result.per_event_vectors)
        gates = aggregate_model_summary(
            attempts=result.attempts,
            self_consistency_mean=sc.mean,
            self_consistency_min=sc.min,
        )
        verdict = compute_verdict(gates, DEFAULT_THRESHOLDS)

        writer.write_model_summary(
            ModelSummaryRecord(
                run_id=run_id,
                model={"id": model},
                events_attempted=len(entries),
                attempts_total=len(result.attempts),
                gates=gates,
                system_peak={
                    **result.peak_metrics,
                    "wall_clock_sec": result.wall_clock_sec,
                },
                tier0_verdict=verdict,
                cooldown_timeout=result.cooldown_timeout,
            )
        )
        completed.append(model)

    writer.close()
    return OrchestrationResult(
        run_id=run_id, out_path=out_path, models_completed=completed, preflight_aborted=False
    )
```

- [ ] **Step 4: Verify orchestrate test passes**

Run: `uv run --project brain pytest brain/tests/test_bench_orchestrate.py -v`
Expected: PASS.

- [ ] **Step 5: Wire into CLI**

Edit `brain/src/hippo_brain/bench/cli.py` — replace `main()` with:

```python
"""hippo-bench CLI entrypoint."""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import platform
import sys
from pathlib import Path

from hippo_brain.bench.corpus import init_corpus, verify_corpus, sample_from_hippo_db, write_corpus
from hippo_brain.bench.orchestrate import orchestrate_run
from hippo_brain.bench.paths import corpus_manifest_path, corpus_path, runs_dir


def _cmd_corpus_init(args: argparse.Namespace) -> int:
    fixture = corpus_path(args.corpus_version)
    manifest = corpus_manifest_path(args.corpus_version)
    counts = {"shell": 15, "claude": 12, "browser": 10, "workflow": 3}
    entries = sample_from_hippo_db(
        db_path=Path(args.db_path), source_counts=counts, seed=args.seed
    )
    write_corpus(entries, fixture, manifest, args.corpus_version, args.seed)
    print(f"wrote {len(entries)} entries to {fixture}")
    return 0


def _cmd_corpus_verify(args: argparse.Namespace) -> int:
    fixture = corpus_path(args.corpus_version)
    manifest = corpus_manifest_path(args.corpus_version)
    ok, detail = verify_corpus(fixture, manifest)
    print(detail)
    return 0 if ok else 1


def _cmd_run(args: argparse.Namespace) -> int:
    fixture = corpus_path(args.corpus_version)
    manifest = corpus_manifest_path(args.corpus_version)
    ts = _dt.datetime.now(tz=_dt.UTC).strftime("%Y%m%dT%H%M%S")
    out = (
        Path(args.out)
        if args.out
        else runs_dir(create=True) / f"run-{ts}-{platform.node()}.jsonl"
    )
    models = args.models.split(",") if args.models else []
    result = orchestrate_run(
        candidate_models=models,
        corpus_version=args.corpus_version,
        fixture_path=fixture,
        manifest_path=manifest,
        base_url=args.base_url,
        embedding_model=args.embedding_model,
        out_path=out,
        timeout_sec=args.latency_ceiling_sec,
        self_consistency_events=args.self_consistency_events,
        self_consistency_runs=args.self_consistency_runs,
        skip_checks=args.skip_checks,
        dry_run=args.dry_run,
    )
    print(f"run_id={result.run_id} out={result.out_path}")
    return 0 if not result.preflight_aborted else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hippo-bench")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run")
    run.add_argument("--models", default="")
    run.add_argument("--corpus-version", default="corpus-v1")
    run.add_argument("--base-url", default="http://localhost:1234/v1")
    run.add_argument("--embedding-model", default="text-embedding-nomic-embed-text-v2-moe")
    run.add_argument("--latency-ceiling-sec", type=int, default=60)
    run.add_argument("--self-consistency-events", type=int, default=5)
    run.add_argument("--self-consistency-runs", type=int, default=5)
    run.add_argument("--skip-checks", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--out")
    run.set_defaults(func=_cmd_run)

    corpus = sub.add_parser("corpus")
    corpus_sub = corpus.add_subparsers(dest="corpus_command", required=True)
    ci = corpus_sub.add_parser("init")
    ci.add_argument("--corpus-version", default="corpus-v1")
    ci.add_argument("--seed", type=int, default=42)
    ci.add_argument(
        "--db-path",
        default=str(Path.home() / ".local" / "share" / "hippo" / "hippo.db"),
    )
    ci.set_defaults(func=_cmd_corpus_init)
    cv = corpus_sub.add_parser("verify")
    cv.add_argument("--corpus-version", default="corpus-v1")
    cv.set_defaults(func=_cmd_corpus_verify)

    summary = sub.add_parser("summary")
    summary.add_argument("run_file")
    summary.set_defaults(func=lambda a: _cmd_summary(a))

    args = parser.parse_args(argv)
    return args.func(args)


def _cmd_summary(args: argparse.Namespace) -> int:
    # Implemented in Task 19.
    print("hippo-bench summary — implemented in Task 19", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: Run CLI smoke + all bench tests**

Run: `uv run --project brain hippo-bench run --dry-run --skip-checks --corpus-version test-nonexistent || true`
Expected: exits without crash; produces a `run_manifest`-only JSONL in the runs dir (even though corpus is missing, dry-run still records the run).

Run: `uv run --project brain pytest brain/tests/test_bench_*.py -v`
Expected: all PASS.

- [ ] **Step 7: Lint + commit**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/ brain/tests
uv run --project brain ruff format brain/src/hippo_brain/bench/ brain/tests
git add brain/src/hippo_brain/bench/cli.py brain/src/hippo_brain/bench/orchestrate.py brain/tests/test_bench_orchestrate.py
git commit -m "feat(bench): hippo-bench run orchestrator + CLI wiring

Top-level orchestrate_run composes preflight, per-model coordinator,
and summarization into one JSONL output file. CLI subcommands (run,
corpus init|verify) are now fully wired."
```

---

## Task 19: `hippo-bench summary` pretty-printer

**Files:**
- Modify: `brain/src/hippo_brain/bench/cli.py`
- Create: `brain/src/hippo_brain/bench/pretty.py`
- Create: `brain/tests/test_bench_pretty.py`

- [ ] **Step 1: Write failing tests**

Create `brain/tests/test_bench_pretty.py`:

```python
import json

from hippo_brain.bench.pretty import render_summary_text


def test_render_summary_handles_manifest_only(tmp_path):
    f = tmp_path / "run.jsonl"
    f.write_text(
        json.dumps({"record_type": "run_manifest", "run_id": "r", "candidate_models": []}) + "\n"
    )
    text = render_summary_text(f)
    assert "run_id" in text.lower()
    assert "no model summaries" in text.lower()


def test_render_summary_includes_per_model_rows(tmp_path):
    f = tmp_path / "run.jsonl"
    lines = [
        json.dumps(
            {"record_type": "run_manifest", "run_id": "r", "candidate_models": ["m1", "m2"]}
        ),
        json.dumps(
            {
                "record_type": "model_summary",
                "run_id": "r",
                "model": {"id": "m1"},
                "events_attempted": 40,
                "attempts_total": 65,
                "gates": {
                    "schema_validity_rate": 0.95,
                    "refusal_rate": 0.0,
                    "latency_p95_ms": 12_000,
                    "self_consistency_mean": 0.9,
                    "entity_sanity_mean": 0.95,
                },
                "system_peak": {"rss_max_mb": 20_000, "wall_clock_sec": 1200},
                "tier0_verdict": {"passed": True, "failed_gates": [], "notes": []},
            }
        ),
        json.dumps(
            {
                "record_type": "model_summary",
                "run_id": "r",
                "model": {"id": "m2"},
                "events_attempted": 40,
                "attempts_total": 65,
                "gates": {
                    "schema_validity_rate": 0.80,
                    "refusal_rate": 0.05,
                    "latency_p95_ms": 8_000,
                    "self_consistency_mean": 0.7,
                    "entity_sanity_mean": 0.85,
                },
                "system_peak": {"rss_max_mb": 18_000, "wall_clock_sec": 900},
                "tier0_verdict": {
                    "passed": False,
                    "failed_gates": ["schema_validity_rate", "refusal_rate", "entity_sanity_mean"],
                    "notes": [],
                },
            }
        ),
    ]
    f.write_text("\n".join(lines) + "\n")

    text = render_summary_text(f)
    assert "m1" in text
    assert "m2" in text
    assert "pass" in text.lower()
    assert "fail" in text.lower()
    assert "0.95" in text  # schema validity
```

- [ ] **Step 2: Run tests to verify failure**

Run: `uv run --project brain pytest brain/tests/test_bench_pretty.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement pretty module**

Create `brain/src/hippo_brain/bench/pretty.py`:

```python
"""Pretty text rendering of a run JSONL file."""

from __future__ import annotations

import json
from pathlib import Path


def render_summary_text(run_file: Path) -> str:
    manifest: dict | None = None
    summaries: list[dict] = []
    for line in run_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        rt = obj.get("record_type")
        if rt == "run_manifest":
            manifest = obj
        elif rt == "model_summary":
            summaries.append(obj)

    lines: list[str] = []
    if manifest is None:
        return "no run_manifest found in file"

    lines.append(f"run_id = {manifest.get('run_id')}")
    lines.append(f"corpus_version = {manifest.get('corpus_version')}")
    lines.append(f"candidate_models = {manifest.get('candidate_models')}")
    lines.append("")

    if not summaries:
        lines.append("no model summaries in run (empty or dry-run)")
        return "\n".join(lines)

    header = (
        f"{'model':30} "
        f"{'verdict':7} "
        f"{'sch%':>6} "
        f"{'ref%':>6} "
        f"{'p95ms':>7} "
        f"{'sc_mean':>8} "
        f"{'ent%':>6} "
        f"{'walls':>6}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for s in summaries:
        g = s["gates"]
        v = s["tier0_verdict"]
        peak = s.get("system_peak", {})
        lines.append(
            f"{s['model']['id'][:30]:30} "
            f"{'pass' if v['passed'] else 'fail':7} "
            f"{g.get('schema_validity_rate', 0) * 100:5.1f}% "
            f"{g.get('refusal_rate', 0) * 100:5.1f}% "
            f"{g.get('latency_p95_ms', 0):7d} "
            f"{g.get('self_consistency_mean', 0):8.3f} "
            f"{g.get('entity_sanity_mean', 0) * 100:5.1f}% "
            f"{peak.get('wall_clock_sec', 0):5d}s"
        )
        if not v["passed"]:
            lines.append(f"  failed: {', '.join(v['failed_gates'])}")
    return "\n".join(lines)
```

- [ ] **Step 4: Wire into CLI**

Edit `brain/src/hippo_brain/bench/cli.py` — replace `_cmd_summary` with:

```python
def _cmd_summary(args: argparse.Namespace) -> int:
    from hippo_brain.bench.pretty import render_summary_text

    text = render_summary_text(Path(args.run_file))
    print(text)
    return 0
```

- [ ] **Step 5: Verify tests pass**

Run: `uv run --project brain pytest brain/tests/test_bench_pretty.py -v`
Expected: PASS.

Run: `uv run --project brain hippo-bench summary /nonexistent/run.jsonl || true`
Expected: graceful error or empty print, not traceback.

- [ ] **Step 6: Lint + commit**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/pretty.py brain/src/hippo_brain/bench/cli.py brain/tests/test_bench_pretty.py
uv run --project brain ruff format brain/src/hippo_brain/bench/pretty.py brain/src/hippo_brain/bench/cli.py brain/tests/test_bench_pretty.py
git add brain/src/hippo_brain/bench/pretty.py brain/src/hippo_brain/bench/cli.py brain/tests/test_bench_pretty.py
git commit -m "feat(bench): hippo-bench summary pretty text table

Text-only per-model table with verdict, gate rates, latency p95,
wall-clock. Markdown leaderboard + Pareto plot remain roadmap."
```

---

## Task 20: Fixture-dir gitignore + end-to-end verification

**Files:**
- Modify: `.gitignore` (repo root)
- Create: `brain/tests/test_bench_e2e.py`

- [ ] **Step 1: Add defensive gitignore entry**

Open the repo root `.gitignore` and append (if not already present):

```
# hippo-bench — corpus fixtures and run outputs MUST NOT enter git
.local/share/hippo/bench/
**/bench-fixtures/
**/bench-runs/
```

These are defensive: the real paths are outside the repo, but this catches accidental symlinks or misconfigured `XDG_DATA_HOME`.

- [ ] **Step 2: Write end-to-end test that composes all pieces**

Create `brain/tests/test_bench_e2e.py`:

```python
"""End-to-end test of bench orchestration without real LM Studio."""

import json
import sqlite3
from unittest.mock import patch

from hippo_brain.bench.corpus import sample_from_hippo_db, write_corpus
from hippo_brain.bench.enrich_call import CallResult
from hippo_brain.bench.orchestrate import orchestrate_run


def _seed_db(db_path):
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE shell_events (
            id INTEGER PRIMARY KEY, command TEXT, stdout TEXT, stderr TEXT,
            duration_ms INTEGER, exit_code INTEGER, cwd TEXT, ts INTEGER
        );
        CREATE TABLE claude_sessions (
            id INTEGER PRIMARY KEY, session_id TEXT, transcript TEXT,
            message_count INTEGER, tool_calls_json TEXT, ts INTEGER
        );
        CREATE TABLE browser_events (
            id INTEGER PRIMARY KEY, url TEXT, title TEXT, dwell_ms INTEGER,
            scroll_depth REAL, ts INTEGER
        );
        CREATE TABLE workflow_runs (
            id INTEGER PRIMARY KEY, repo TEXT, workflow_name TEXT,
            conclusion TEXT, annotations_json TEXT, ts INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO shell_events (command, stdout, stderr, duration_ms, exit_code, cwd, ts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ls -la", "listing", "", 10, 0, "/tmp", 0),
    )
    conn.execute(
        "INSERT INTO claude_sessions (session_id, transcript, message_count, tool_calls_json, ts)"
        " VALUES (?, ?, ?, ?, ?)",
        ("s1", "hello", 3, "[]", 0),
    )
    conn.commit()
    conn.close()


def _fake_enrich(source, *_args, **_kwargs):
    content = json.dumps(
        {
            "summary": "Synthetic enrichment for bench test",
            "intent": "test",
            "outcome": "success",
            "entities": {
                "projects": ["hippo"],
                "tools": ["pytest"],
                "files": [],
                "services": [],
                "errors": [],
            },
        }
    )
    return CallResult(raw_output=content, ttft_ms=None, total_ms=50, timeout=False)


def _fake_embed(*_args, **_kwargs):
    return [1.0, 0.0, 0.0]


@patch("hippo_brain.bench.runner.call_enrichment", side_effect=lambda **kw: _fake_enrich(kw["source"]))
@patch("hippo_brain.bench.runner.call_embedding", side_effect=_fake_embed)
@patch("hippo_brain.bench.coordinator.call_enrichment", side_effect=lambda **kw: _fake_enrich(kw["source"]))
@patch("hippo_brain.bench.coordinator.lms")
@patch("hippo_brain.bench.orchestrate.run_all_preflight", return_value=[])
def test_e2e_bench_run_composes_cleanly(
    _pf, mock_lms, _warmup, _embed, _main, tmp_path
):
    mock_lms.list_loaded.return_value = []
    db = tmp_path / "hippo.db"
    _seed_db(db)

    fixture = tmp_path / "corpus-v1.jsonl"
    manifest = tmp_path / "corpus-v1.manifest.json"
    entries = sample_from_hippo_db(
        db_path=db,
        source_counts={"shell": 1, "claude": 1, "browser": 0, "workflow": 0},
        seed=1,
    )
    write_corpus(entries, fixture, manifest, "corpus-v1", 1)

    out = tmp_path / "run.jsonl"
    result = orchestrate_run(
        candidate_models=["m1"],
        corpus_version="corpus-v1",
        fixture_path=fixture,
        manifest_path=manifest,
        base_url="http://localhost:1234/v1",
        embedding_model="nomic",
        out_path=out,
        timeout_sec=5,
        self_consistency_events=1,
        self_consistency_runs=2,
        skip_checks=True,
        dry_run=False,
    )

    records = [json.loads(line) for line in out.read_text().splitlines() if line]
    assert records[0]["record_type"] == "run_manifest"
    assert any(r["record_type"] == "attempt" for r in records)
    assert any(r["record_type"] == "model_summary" for r in records)
    assert result.models_completed == ["m1"]
```

- [ ] **Step 3: Run tests to verify pass**

Run: `uv run --project brain pytest brain/tests/test_bench_e2e.py -v`
Expected: PASS.

- [ ] **Step 4: Full test suite + lint**

Run: `uv run --project brain pytest brain/tests -v`
Expected: all bench tests PASS; no regressions elsewhere.

Run: `uv run --project brain ruff check brain/src/hippo_brain/bench brain/tests`
Run: `uv run --project brain ruff format --check brain/src/hippo_brain/bench brain/tests`
Expected: clean.

- [ ] **Step 5: Commit**

```bash
git add .gitignore brain/tests/test_bench_e2e.py
git commit -m "chore(bench): gitignore bench data dirs + end-to-end test

Defensive .gitignore entries for hippo-bench data paths (real paths
are outside the repo but this catches accidental symlinks). E2E test
stitches corpus→orchestrator→writer→summary with mocked LM Studio."
```

---

## Post-Plan Checklist (self-review results)

**Spec coverage:** every spec section has tasks:
- Corpus / fixture strategy → Tasks 11–12, 20
- Tier 0 gates (5) → Tasks 4–7
- System metrics → Task 9
- JSONL output (3 record types) → Task 13
- Coordinator / pre-flight → Tasks 10, 17
- CLI (run, corpus init|verify, summary) → Tasks 1, 18, 19
- Testing plan → dedicated tests in each task + E2E in Task 20
- Non-goals → explicitly preserved (no judge, no TUI, no leaderboard.md, no GPU metrics, no auto-trigger)

**Placeholder scan:** one note in Task 12 flags a "adapt if redaction.redact_text is named differently" — that's a real pending check, not a TBD; the task instructs the implementer to read `redaction.py` and match the API. Everything else has concrete code.

**Type consistency:** `CorpusEntry` signature stable across Tasks 11, 12, 14, 15, 17, 20. `AttemptRecord` / `ModelSummaryRecord` / `RunManifestRecord` stable across Tasks 13, 15, 16, 17, 18, 19. Gate return types stable after being defined in Tasks 4–7.

**Known risks flagged for implementer:**
1. The `hippo_brain.redaction` API name is unverified in the plan (Task 12). Read the module and match before calling.
2. Coordinator cooldown loop uses `sampler._sample_once` as an ad-hoc probe after sampler is stopped; this works but is slightly off the public API. Safe, but worth a re-design if the sampler's internals change.
3. `sample_from_hippo_db` assumes the real hippo.db has tables named `shell_events`, `claude_sessions`, `browser_events`, `workflow_runs`. Verify against the live schema (`hippo doctor` or `sqlite3 ~/.local/share/hippo/hippo.db '.schema'`) before the first real corpus-init run.

---

Plan complete and saved to [docs/superpowers/plans/2026-04-21-hippo-bench-mvp.md](2026-04-21-hippo-bench-mvp.md). Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints

**Which approach?**
