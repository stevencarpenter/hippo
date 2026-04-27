# Hippo-Bench v2 — Ralph Loop Implementation Plan

**Status:** Ready for autonomous execution  
**Spec ref:** `docs/superpowers/specs/2026-04-27-hippo-bench-v2-design.md`  
**State file:** `.ralph/hippo-bench-v2-state.json`  
**Task prefix:** `RB2-`

---

## How to Read This Plan

Each task has:
- **ID** — `RB2-NN`, referenced by `deps`
- **Phase / depth** — tasks at the same depth are order-independent; pick any whose deps are `completed`
- **Budget** — estimated wall time; hard budget is 1.5x, after which the loop marks `blocked`
- **File(s)** — real paths in the worktree, never invented
- **Work** — exactly what to implement; no ambiguity
- **Verify** — verbatim shell command(s) the loop MUST run; all must exit 0 for `completed`

**Worktree root:** `/Users/carpenter/projects/hippo/.claude/worktrees/youthful-kirch-7d3b27`  
**All paths below are relative to the worktree root unless prefixed with `~`.**

---

## State File Schema

`.ralph/hippo-bench-v2-state.json`:

```json
{
  "schema_version": 1,
  "plan_file": "docs/superpowers/plans/2026-04-27-hippo-bench-v2-ralph-plan.md",
  "tasks": {
    "RB2-01": {
      "status": "pending",
      "deps": [],
      "last_attempt_iso": null,
      "last_error": null
    }
  }
}
```

`status` values: `"pending"` | `"in_progress"` | `"completed"` | `"blocked"`

The loop picks the next `pending` task whose every `dep` is `completed`, marks it `in_progress`, attempts it, marks `completed` or `blocked` (with `last_error`), then exits.

---

## Phase Structure

```
Phase 0: Infrastructure / Telemetry fix (unblocks everything)
  RB2-01  Rust telemetry.rs — EnvResourceDetector fix
  RB2-02  Python telemetry.py — verify env-var pickup; doc if already correct
  RB2-03  State file bootstrap (.ralph/ dir + initial JSON)

Phase 1: Pause/Resume RPC (unblocks pre-flight + coordinator)
  RB2-04  server.py — add /control/pause and /control/resume endpoints
  RB2-05  test_bench_pause_rpc.py — stub-server tests for pause/resume + mid-run restart

Phase 2: Corpus v2 (unblocks shadow-stack seeding + downstream-proxy)
  RB2-06  bench/corpus_v2.py — new module: time-bucketed sampling, SQLite snapshot, JSONL sidecar, manifest
  RB2-07  bench/paths.py — extend with v2 paths (hippo-bench XDG tree, corpus-v2.sqlite, overlay, Q/A)
  RB2-08  test_bench_corpus_v2.py — determinism, SQLite/JSONL equivalence, schema-version assertion, time-bucketing

Phase 3: Shadow Stack (unblocks coordinator v2)
  RB2-09  bench/shadow_stack.py — new module: process-group spawn, env injection, health probe, SIGTERM/SIGKILL teardown
  RB2-10  test_bench_shadow_stack.py — spawn/teardown, orphan prevention, process_ready_ms, env vars

Phase 4: Q/A Fixture Seeding (unblocks downstream-proxy pass)
  RB2-11  fixtures/qa-v1.jsonl — create the Q/A fixture file (100+ items)
  RB2-12  bench/downstream_proxy.py — new module: Q/A loading, hit@K, MRR, NDCG, ask_synthesis sample

Phase 5: Downstream-Proxy Tests
  RB2-13  test_bench_downstream_proxy.py — filtering, Hit@K, MRR, mode-aware retrieval

Phase 6: Telemetry Isolation
  RB2-14  test_bench_telemetry_isolation.py — env var assertion, service.namespace on spans (mock collector)
  RB2-15  crates/hippo-daemon/tests/telemetry_env_resource_test.rs — Rust EnvResourceDetector test

Phase 7: Coordinator v2
  RB2-16  bench/coordinator_v2.py — new module: per-model lifecycle using shadow_stack, downstream_proxy, pause RPC
  RB2-17  bench/preflight_v2.py — extend preflight with v2 checks (prod brain pause, snapshot hash, disk free 2GB)
  RB2-18  bench/orchestrate_v2.py — new orchestrator: calls coordinator_v2, writes v2 JSONL record shapes

Phase 8: Output Schema v2
  RB2-19  bench/output_v2.py — v2 dataclasses: RunManifestRecord, ModelSummaryRecord, RunEndRecord with v2 fields
  RB2-20  bench/schemas_v2.py — extend for v2 corpus_meta schema-version assertion

Phase 9: CLI v2
  RB2-21P bench/__init__.py — update version to 0.2.0 (prereq for RB2-21)
  RB2-21  bench/cli.py — extend with v2 subcommands

Phase 10: Grafana Dashboards
  RB2-22  prod dashboards — add service_namespace="" filter to all 4 existing dashboard panel queries
  RB2-23  otel/grafana/dashboards/bench-run-overview.json — new Grafana dashboard
  RB2-24  otel/grafana/dashboards/bench-model-drilldown.json — new Grafana dashboard
  RB2-25  otel/grafana/dashboards/bench-model-comparison.json — new Grafana dashboard

Phase 11: Docs + Acceptance Gate
  RB2-26  brain/src/hippo_brain/bench/README.md — update with v2 usage, cross-link design docs
  RB2-27  Dry-run smoke test — hippo-bench run --dry-run --models qwen3.5-35b-a3b --corpus-version corpus-v2
  RB2-28  Full test suite pass — uv run --project brain pytest brain/tests -v (all v1 + v2 tests pass)
```

---

## Tasks (Detailed)

### Phase 0: Infrastructure / Telemetry Fix

---

#### RB2-01 — Rust telemetry.rs: add EnvResourceDetector

**Deps:** none  
**Budget:** 15 min  
**File:** `crates/hippo-daemon/src/telemetry.rs`

**Work:**

Replace the `resource()` function (lines 18-22) with a version that merges `EnvResourceDetector` so env-injected `OTEL_RESOURCE_ATTRIBUTES` (e.g., `service.namespace=hippo-bench`) are picked up at process startup.

Current code (lines 18-22):
```rust
fn resource(service_name: &str) -> Resource {
    Resource::builder()
        .with_service_name(service_name.to_string())
        .build()
}
```

Required replacement:
```rust
fn resource(service_name: &str) -> Resource {
    Resource::builder()
        .with_service_name(service_name.to_string())
        .with_detectors(&[Box::new(opentelemetry_sdk::resource::EnvResourceDetector::new())])
        .build()
}
```

`EnvResourceDetector` is in `opentelemetry_sdk::resource`; no new Cargo dependency required. The existing `opentelemetry_sdk = { version = "0.31", features = ["rt-tokio"] }` dep in the workspace Cargo.toml is sufficient.

**Verify:**
```bash
cargo build -p hippo-daemon --features otel 2>&1 | tail -5
```
Exit 0 required. If `EnvResourceDetector` is absent from the 0.31 API, check `opentelemetry_sdk::resource::env` as an alternate path and adjust accordingly.

---

#### RB2-02 — Python telemetry.py: verify env-var pickup

**Deps:** none  
**Budget:** 10 min  
**File:** `brain/src/hippo_brain/telemetry.py`

**Work:**

Read `brain/src/hippo_brain/telemetry.py` line 100. The current code is:
```python
resource = Resource.create({"service.name": service_name})
```

The Python OTel SDK's `Resource.create()` reads `OTEL_RESOURCE_ATTRIBUTES` from the environment via its default detector chain. This is already correct — no functional change needed.

Add a comment directly above line 100 confirming env pickup is active:
```python
# Resource.create() merges OTEL_RESOURCE_ATTRIBUTES from the environment
# via the Python SDK default detector chain. bench/shadow_stack.py injects
# service.namespace=hippo-bench here at process spawn time.
resource = Resource.create({"service.name": service_name})
```

**Verify:**
```bash
python3 -c "
import os
os.environ['OTEL_RESOURCE_ATTRIBUTES'] = 'service.namespace=test-ns,bench.run_id=test-run'
from opentelemetry.sdk.resources import Resource
r = Resource.create({'service.name': 'hippo-brain'})
attrs = dict(r.attributes)
assert attrs.get('service.namespace') == 'test-ns', f'Got: {attrs}'
assert attrs.get('bench.run_id') == 'test-run', f'Got: {attrs}'
print('PASS: env resource pickup works')
"
```

---

#### RB2-03 — Bootstrap state file

**Deps:** none  
**Budget:** 5 min  
**File:** `.ralph/hippo-bench-v2-state.json` (create)

**Work:**

Create `.ralph/` directory and write the initial state file with all tasks in `pending` status.

```json
{
  "schema_version": 1,
  "plan_file": "docs/superpowers/plans/2026-04-27-hippo-bench-v2-ralph-plan.md",
  "tasks": {
    "RB2-01": {"status": "pending", "deps": [], "last_attempt_iso": null, "last_error": null},
    "RB2-02": {"status": "pending", "deps": [], "last_attempt_iso": null, "last_error": null},
    "RB2-03": {"status": "pending", "deps": [], "last_attempt_iso": null, "last_error": null},
    "RB2-04": {"status": "pending", "deps": ["RB2-01", "RB2-02"], "last_attempt_iso": null, "last_error": null},
    "RB2-05": {"status": "pending", "deps": ["RB2-04"], "last_attempt_iso": null, "last_error": null},
    "RB2-06": {"status": "pending", "deps": ["RB2-03"], "last_attempt_iso": null, "last_error": null},
    "RB2-07": {"status": "pending", "deps": ["RB2-03"], "last_attempt_iso": null, "last_error": null},
    "RB2-08": {"status": "pending", "deps": ["RB2-06", "RB2-07"], "last_attempt_iso": null, "last_error": null},
    "RB2-09": {"status": "pending", "deps": ["RB2-01", "RB2-02"], "last_attempt_iso": null, "last_error": null},
    "RB2-10": {"status": "pending", "deps": ["RB2-09"], "last_attempt_iso": null, "last_error": null},
    "RB2-11": {"status": "pending", "deps": ["RB2-07"], "last_attempt_iso": null, "last_error": null},
    "RB2-12": {"status": "pending", "deps": ["RB2-11"], "last_attempt_iso": null, "last_error": null},
    "RB2-13": {"status": "pending", "deps": ["RB2-12"], "last_attempt_iso": null, "last_error": null},
    "RB2-14": {"status": "pending", "deps": ["RB2-09", "RB2-02"], "last_attempt_iso": null, "last_error": null},
    "RB2-15": {"status": "pending", "deps": ["RB2-01"], "last_attempt_iso": null, "last_error": null},
    "RB2-16": {"status": "pending", "deps": ["RB2-09", "RB2-04", "RB2-12"], "last_attempt_iso": null, "last_error": null},
    "RB2-17": {"status": "pending", "deps": ["RB2-04", "RB2-06", "RB2-07"], "last_attempt_iso": null, "last_error": null},
    "RB2-18": {"status": "pending", "deps": ["RB2-16", "RB2-17", "RB2-19"], "last_attempt_iso": null, "last_error": null},
    "RB2-19": {"status": "pending", "deps": ["RB2-06"], "last_attempt_iso": null, "last_error": null},
    "RB2-20": {"status": "pending", "deps": ["RB2-06"], "last_attempt_iso": null, "last_error": null},
    "RB2-21P": {"status": "pending", "deps": ["RB2-18"], "last_attempt_iso": null, "last_error": null},
    "RB2-21": {"status": "pending", "deps": ["RB2-18", "RB2-21P"], "last_attempt_iso": null, "last_error": null},
    "RB2-22": {"status": "pending", "deps": ["RB2-01"], "last_attempt_iso": null, "last_error": null},
    "RB2-23": {"status": "pending", "deps": ["RB2-22"], "last_attempt_iso": null, "last_error": null},
    "RB2-24": {"status": "pending", "deps": ["RB2-22"], "last_attempt_iso": null, "last_error": null},
    "RB2-25": {"status": "pending", "deps": ["RB2-22"], "last_attempt_iso": null, "last_error": null},
    "RB2-26": {"status": "pending", "deps": ["RB2-21"], "last_attempt_iso": null, "last_error": null},
    "RB2-27": {"status": "pending", "deps": ["RB2-21"], "last_attempt_iso": null, "last_error": null},
    "RB2-28": {"status": "pending", "deps": ["RB2-05", "RB2-08", "RB2-10", "RB2-13", "RB2-14", "RB2-15", "RB2-27"], "last_attempt_iso": null, "last_error": null}
  }
}
```

**Verify:**
```bash
python3 -c "
import json
with open('.ralph/hippo-bench-v2-state.json') as f:
    s = json.load(f)
assert s['schema_version'] == 1
assert all(t['status'] == 'pending' for t in s['tasks'].values())
print('PASS:', len(s['tasks']), 'tasks in pending state')
"
```

---

### Phase 1: Pause/Resume RPC

---

#### RB2-04 — brain/server.py: add /control/pause and /control/resume

**Deps:** RB2-01, RB2-02  
**Budget:** 25 min  
**File:** `brain/src/hippo_brain/server.py`

**Work:**

Add two new state attributes to `BrainServer.__init__` and two new route handlers. The enrichment loop must check the pause flag before each iteration.

1. In `BrainServer.__init__`, after `self.enrichment_running = False`, add:
   ```python
   self._paused: bool = False
   self._paused_at_iso: str | None = None
   ```

2. Add route handler `async def control_pause(self, request: Request) -> JSONResponse`:
   - Sets `self._paused = True`
   - Sets `self._paused_at_iso` to current ISO timestamp (idempotent: if already paused, returns existing `paused_at`)
   - Returns `{"paused_at": self._paused_at_iso, "in_flight_finished": True}`
   - Status 200 always

3. Add route handler `async def control_resume(self, request: Request) -> JSONResponse`:
   - Sets `self._paused = False`, clears `self._paused_at_iso`
   - Returns `{"resumed_at": <current iso>}`
   - Status 200 always (idempotent)

4. In the enrichment loop method (search for `async def _enrichment_loop` or `async def _run_enrichment_loop`), add at the top of the while-True body, BEFORE any DB or LLM calls:
   ```python
   if self._paused:
       await asyncio.sleep(self.poll_interval_secs)
       continue
   ```

5. Wire the new routes into the Starlette `Route` list:
   - `Route("/control/pause", self.control_pause, methods=["POST"])`
   - `Route("/control/resume", self.control_resume, methods=["POST"])`

6. Update the `/health` response JSON to include:
   ```python
   "paused": self._paused,
   "paused_at": self._paused_at_iso,
   ```

**Verify:**
```bash
uv run --project brain ruff check brain/src/hippo_brain/server.py
uv run --project brain ruff format --check brain/src/hippo_brain/server.py
uv run --project brain pytest brain/tests/test_server.py brain/tests/test_server_extended.py -v -x --tb=short 2>&1 | tail -20
```
All tests must pass (no new regressions from the route additions).

---

#### RB2-05 — test_bench_pause_rpc.py + bench/pause_rpc.py

**Deps:** RB2-04  
**Budget:** 20 min  
**Files:** `brain/tests/test_bench_pause_rpc.py` (create), `brain/src/hippo_brain/bench/pause_rpc.py` (create)

**Work:**

First, create `brain/src/hippo_brain/bench/pause_rpc.py` — a thin HTTP client for the pause/resume endpoints:

```python
"""Thin client for the hippo-brain pause/resume control RPC."""
from __future__ import annotations
import datetime as _dt
import httpx


class PauseRpcClient:
    """Calls POST /control/pause and POST /control/resume on the prod brain."""

    def __init__(self, base_url: str, skip: bool = False):
        self.base_url = base_url.rstrip("/")
        self.skip = skip

    def probe_health(self) -> dict | None:
        """Return /health JSON or None if unreachable."""
        if self.skip:
            return None
        try:
            r = httpx.get(f"{self.base_url}/health", timeout=5.0)
            return r.json()
        except Exception:
            return None

    def pause(self) -> dict | None:
        """POST /control/pause. Returns response JSON or None on skip."""
        if self.skip:
            return None
        r = httpx.post(f"{self.base_url}/control/pause", timeout=10.0)
        r.raise_for_status()
        return r.json()

    def resume(self) -> dict | None:
        """POST /control/resume. Best-effort — swallows errors (called in atexit)."""
        if self.skip:
            return None
        try:
            r = httpx.post(f"{self.base_url}/control/resume", timeout=10.0)
            return r.json()
        except Exception:
            return None
```

Then create `brain/tests/test_bench_pause_rpc.py` with these test cases (use `starlette.testclient.TestClient` to test against a real `BrainServer` with a mocked DB connection):

1. `test_pause_returns_200_with_paused_at` — POST /control/pause returns 200 with `paused_at` (ISO) and `in_flight_finished=True`
2. `test_pause_idempotent` — Two POST /control/pause calls return the same `paused_at` timestamp
3. `test_resume_returns_200_with_resumed_at` — POST /control/pause then POST /control/resume returns 200 with `resumed_at` (ISO)
4. `test_resume_idempotent` — POST /control/resume without prior pause returns 200 (no crash)
5. `test_health_reflects_paused_state` — After POST /control/pause, GET /health returns `"paused": true`
6. `test_health_reflects_resumed_state` — After pause then resume, GET /health returns `"paused": false`
7. `test_skip_flag_no_http_calls` — `PauseRpcClient(base_url="http://x", skip=True).pause()` returns None without making any HTTP call (assert with `unittest.mock.patch("httpx.post")` never called)

**Verify:**
```bash
uv run --project brain pytest brain/tests/test_bench_pause_rpc.py -v --tb=short 2>&1 | tail -20
```
All 7 tests must pass.

---

### Phase 2: Corpus v2

---

#### RB2-06 — bench/corpus_v2.py: time-bucketed sampling + SQLite snapshot + JSONL sidecar

**Deps:** RB2-03  
**Budget:** 30 min  
**File:** `brain/src/hippo_brain/bench/corpus_v2.py` (create)

**Work:**

Create `brain/src/hippo_brain/bench/corpus_v2.py`. This is the v2 corpus module; it does NOT modify `corpus.py` (v1 tests depend on it).

Key requirements from spec:

**`sample_from_hippo_db_v2(db_path, corpus_days=90, corpus_buckets=9, shell_min=50, claude_min=50, browser_min=50, workflow_min=50, seed=42) -> list[CorpusEntry]`**:
- Divides the `corpus_days`-day window into `corpus_buckets` equal time buckets (epoch ms boundaries)
- For each source x bucket: selects events via `_SOURCE_QUERIES` from `corpus.py` (import and reuse), applies `is_enrichment_eligible()` filter, filters `probe_tag IS NOT NULL` events (for tables that have `probe_tag`)
- Applies `redact()` to each event payload
- Enforces per-source minimum floor regardless of time distribution
- Uses `random.Random(seed)` for all sampling

**`write_corpus_v2_sqlite(entries: list[CorpusEntry], dest_db: Path, schema_version: int) -> None`**:
- Creates a fresh SQLite at `dest_db`
- Writes the actual event rows into source-appropriate tables (shell_events, claude_session_segments, browser_events, workflow_runs) using the same column layout as the live hippo schema
- Pre-populates all four `*_enrichment_queue` tables with `status='pending'` rows for each event (this is the queue the shadow brain will drain)
- Creates a `corpus_meta` table: `(schema_version INTEGER, corpus_version TEXT, generated_at_iso TEXT, event_count INTEGER, seed INTEGER)`
- IMPORTANT: Read `brain/src/hippo_brain/schema_version.py` and `brain/src/hippo_brain/enrichment.py` to get exact table/column names before writing. The `claude_session_segments` table name and queue table names must match what the brain's enrichment code expects.

**`write_corpus_v2_jsonl(entries: list[CorpusEntry], dest_jsonl: Path) -> None`**:
- One JSON object per event including `event_id`, `source`, `redacted_content`, `content_sha256`, `bucket_index` (int 0 to corpus_buckets-1), `sampled_at_iso`

**`init_corpus_v2(db_path, dest_sqlite, dest_jsonl, manifest_path, corpus_version="corpus-v2", ...) -> list[CorpusEntry]`**:
- Atomic: calls sample, sqlite write, jsonl write, manifest write
- Raises `FileExistsError` if dest_sqlite exists and `force=False`
- Asserts sqlite event IDs == jsonl event IDs; raises `AssertionError` if not
- Manifest includes: `seed`, `corpus_version`, `schema_version`, `corpus_content_hash` (sha256 of the sqlite file), `jsonl_content_hash` (sha256 of the jsonl file), `source_counts`, `bucket_spec`, `generated_at_iso`

**`verify_corpus_v2(sqlite_path: Path, jsonl_path: Path, manifest_path: Path) -> tuple[bool, str]`**:
- Re-reads both files, recomputes sha256, compares to manifest; returns (True, "ok") or (False, reason)

**Verify:**
```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/corpus_v2.py
uv run --project brain ruff format --check brain/src/hippo_brain/bench/corpus_v2.py
uv run --project brain python3 -c "from hippo_brain.bench.corpus_v2 import sample_from_hippo_db_v2, verify_corpus_v2; print('import ok')"
```

---

#### RB2-07 — bench/paths.py: extend with v2 paths

**Deps:** RB2-03  
**Budget:** 10 min  
**File:** `brain/src/hippo_brain/bench/paths.py`

**Work:**

Append new functions to the existing `brain/src/hippo_brain/bench/paths.py`. Do NOT change existing functions. Add:

```python
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

def corpus_v2_sqlite_path() -> Path:
    return bench_fixtures_dir() / "corpus-v2.sqlite"

def corpus_v2_jsonl_path() -> Path:
    return bench_fixtures_dir() / "corpus-v2.jsonl"

def corpus_v2_manifest_path() -> Path:
    return bench_fixtures_dir() / "corpus-v2.manifest.json"

def corpus_v2_overlay_path() -> Path:
    return bench_fixtures_dir() / "corpus-v2.overlay.sqlite"

def bench_qa_path(version: str = "eval-qa-v1") -> Path:
    return bench_fixtures_dir() / f"{version}.jsonl"

def bench_run_tree(run_id: str, model_id: str, create: bool = False) -> Path:
    """Per-model ephemeral run directory."""
    p = bench_runs_dir() / run_id / model_id.replace("/", "_")
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p
```

Key invariant: `hippo_bench_root()` returns `~/.local/share/hippo-bench/`, which is a sibling of `~/.local/share/hippo/` (prod). Never a child. This prevents any path walk from accidentally crossing the prod DB boundary.

**Verify:**
```bash
uv run --project brain python3 -c "
from hippo_brain.bench.paths import hippo_bench_root, corpus_v2_sqlite_path, bench_run_tree
import pathlib
prod_db = pathlib.Path.home() / '.local' / 'share' / 'hippo' / 'hippo.db'
bench_sqlite = corpus_v2_sqlite_path()
assert 'hippo-bench' in str(bench_sqlite), f'Expected hippo-bench in path: {bench_sqlite}'
# Verify bench sqlite is NOT under prod hippo dir.
# Use path-component containment, not substring: '/x/hippo' is a prefix of
# '/x/hippo-bench' as a string but NOT as a path component, so substring
# checks falsely flag the (correct) hippo-bench layout. Resolve both and
# walk parents instead.
prod_resolved = prod_db.parent.resolve()
bench_resolved = bench_sqlite.parent.resolve()
assert prod_resolved not in bench_resolved.parents and prod_resolved != bench_resolved, (
    f'Bench corpus inside prod dir! prod={prod_resolved} bench={bench_resolved}'
)
print('PASS: paths correctly separated')
print('  prod db dir:', prod_resolved)
print('  bench sqlite:', bench_sqlite)
"
```

---

#### RB2-08 — test_bench_corpus_v2.py

**Deps:** RB2-06, RB2-07  
**Budget:** 25 min  
**File:** `brain/tests/test_bench_corpus_v2.py` (create)

**Work:**

Create `brain/tests/test_bench_corpus_v2.py`. Use temp dirs and in-memory/temp SQLite fixtures. Never touch `~/.local/share/hippo/hippo.db`.

Required test cases:

1. `test_sample_determinism` — Two calls with same seed produce identical event ID sequences
2. `test_time_bucket_coverage` — Events spanning 9 weeks; sample contains events from at least 7 distinct buckets
3. `test_source_minimum_floor` — 10 shell events available with shell_min=50; all 10 are included (can't exceed available)
4. `test_sqlite_jsonl_equivalence` — After `init_corpus_v2()`, sqlite event IDs == jsonl event IDs (same set, same count)
5. `test_schema_version_in_corpus_meta` — `corpus_meta.schema_version` in the generated sqlite matches `EXPECTED_SCHEMA_VERSION`
6. `test_verify_corpus_v2_passes` — `verify_corpus_v2()` returns `(True, "ok")` on fresh valid corpus
7. `test_verify_corpus_v2_fails_on_tamper` — Write a tampered byte to the sqlite; `verify_corpus_v2()` returns `(False, ...)` with "hash" in the message
8. `test_overwrite_protection` — Second call to `init_corpus_v2()` without `force=True` raises `FileExistsError`
9. `test_probe_tag_excluded` — Events with `probe_tag IS NOT NULL` excluded from sample
10. `test_enrichment_queue_seeded` — Generated sqlite has `status='pending'` rows in all four `*_enrichment_queue` tables for each included event

**Verify:**
```bash
uv run --project brain pytest brain/tests/test_bench_corpus_v2.py -v --tb=short 2>&1 | tail -30
```
All 10 tests must pass.

---

### Phase 3: Shadow Stack

---

#### RB2-09 — bench/shadow_stack.py: process-group spawn, env injection, teardown

**Deps:** RB2-01, RB2-02  
**Budget:** 30 min  
**File:** `brain/src/hippo_brain/bench/shadow_stack.py` (create)

**Work:**

Create `brain/src/hippo_brain/bench/shadow_stack.py`.

```python
"""Shadow process-group spawn and teardown for hippo-bench v2.

Spawns hippo-daemon + hippo-brain in their own process group with:
  XDG_DATA_HOME=<run_tree>
  XDG_CONFIG_HOME=<run_tree>/config
  OTEL_RESOURCE_ATTRIBUTES=service.namespace=hippo-bench,...

The caller must copy corpus-v2.sqlite to <run_tree>/hippo.db before calling
spawn_shadow_stack().
"""
```

Required public API:

```python
import dataclasses, os, pathlib, signal, subprocess, time
import httpx

@dataclasses.dataclass
class ShadowStack:
    daemon_proc: subprocess.Popen
    brain_proc: subprocess.Popen
    run_tree: pathlib.Path
    process_group_id: int
    brain_base_url: str

def spawn_shadow_stack(
    *,
    run_tree: pathlib.Path,
    run_id: str,
    model_id: str,
    corpus_version: str,
    embedding_model: str,
    brain_port: int = 18923,
    otel_enabled: bool = False,
) -> ShadowStack:
    ...

def wait_for_brain_ready(stack: ShadowStack, timeout_sec: float = 60.0) -> float:
    """Poll /health until 200. Returns elapsed seconds. Raises TimeoutError."""
    ...

def teardown_shadow_stack(stack: ShadowStack, sigkill_timeout_sec: float = 10.0) -> None:
    """SIGTERM the process group, wait, SIGKILL if still alive."""
    ...
```

Implementation notes:
- Env injection: build a dict from `os.environ.copy()` then override with XDG paths and OTEL vars. Never pass a bare `{}` (that strips PATH and breaks subprocess)
- `OTEL_RESOURCE_ATTRIBUTES` value: `f"service.namespace=hippo-bench,bench.run_id={run_id},bench.model_id={model_id},bench.corpus_version={corpus_version}"`
- Process group: use `start_new_session=True` in `subprocess.Popen` (Python 3.2+ clean API; equivalent to `preexec_fn=os.setsid` but doesn't fork-then-exec)
- `process_group_id = os.getpgid(daemon_proc.pid)` after spawn
- Daemon command: `shutil.which("hippo") or "hippo"`, subcommand `serve`
- Brain command: find `uv` via `shutil.which("uv")`, run `uv run --project brain hippo-brain --port <brain_port>`; alternatively find the brain's installed entry point directly
- Stdout: `subprocess.DEVNULL`; stderr: write to `run_tree / "logs" / "daemon.log"` and `run_tree / "logs" / "brain.log"` (create dir first)
- `teardown_shadow_stack`: `os.killpg(pgid, signal.SIGTERM)`, then poll for `proc.poll() is not None` up to `sigkill_timeout_sec`, then `os.killpg(pgid, signal.SIGKILL)`. Swallow `ProcessLookupError`.

**Verify:**
```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/shadow_stack.py
uv run --project brain ruff format --check brain/src/hippo_brain/bench/shadow_stack.py
uv run --project brain python3 -c "from hippo_brain.bench.shadow_stack import spawn_shadow_stack, teardown_shadow_stack, ShadowStack; print('import ok')"
```

---

#### RB2-10 — test_bench_shadow_stack.py

**Deps:** RB2-09  
**Budget:** 25 min  
**File:** `brain/tests/test_bench_shadow_stack.py` (create)

**Work:**

Create `brain/tests/test_bench_shadow_stack.py`. Use `unittest.mock.patch` throughout — do NOT actually spawn hippo processes.

Required test cases:

1. `test_env_injection_otel_resource_attributes` — Mock `subprocess.Popen`; assert the `env` kwarg passed to spawn calls contains `OTEL_RESOURCE_ATTRIBUTES` with `service.namespace=hippo-bench`, `bench.run_id=<id>`, `bench.model_id=<id>`
2. `test_env_injection_xdg_data_home` — Same mock; assert `XDG_DATA_HOME` equals `str(run_tree)` in the env
3. `test_start_new_session_flag` — Mock Popen; assert `start_new_session=True` is in kwargs (process group isolation)
4. `test_otel_disabled_by_default` — Mock Popen; without `otel_enabled=True`, `HIPPO_OTEL_ENABLED` is absent or `"0"` in env
5. `test_otel_enabled_when_requested` — Mock Popen; with `otel_enabled=True`, `HIPPO_OTEL_ENABLED="1"` in env
6. `test_teardown_sigterm_then_sigkill` — Mock `os.killpg`; call `teardown_shadow_stack()` with a stub; assert SIGTERM called first, SIGKILL called if processes still alive after timeout
7. `test_teardown_tolerates_process_lookup_error` — `os.killpg` raises `ProcessLookupError`; assert `teardown_shadow_stack()` does not re-raise
8. `test_wait_for_brain_ready_timeout` — Mock `httpx.get` to always raise `httpx.ConnectError`; assert `wait_for_brain_ready()` raises `TimeoutError`

**Verify:**
```bash
uv run --project brain pytest brain/tests/test_bench_shadow_stack.py -v --tb=short 2>&1 | tail -20
```
All 8 tests must pass.

---

### Phase 4: Q/A Fixture + Downstream Proxy

---

#### RB2-11 — Create Q/A fixture file (eval-qa-v1.jsonl)

**Deps:** RB2-07  
**Budget:** 20 min  
**Files:**
- `brain/src/hippo_brain/bench/qa_template.jsonl` (committed skeleton — 100+ items)
- `brain/src/hippo_brain/bench/qa_seed.py` (committed helper script)
- `~/.local/share/hippo-bench/fixtures/eval-qa-v1.jsonl` (runtime, not committed)

**Work:**

Create `brain/src/hippo_brain/bench/qa_template.jsonl` with at least 100 Q/A items. Seed from `brain/tests/eval_questions.json` (41 items; adapt schema). Add 59+ new items.

Schema per item:
```json
{"qa_id": "qa-001", "question": "...", "golden_event_id": "shell-PLACEHOLDER", "source_filter": "shell", "acceptable_answer_keywords": ["word1", "word2"], "tags": ["lookup"]}
```

Note: `golden_event_id` values in the template use `"-PLACEHOLDER"` suffixes since real event IDs are corpus-specific. These items will be filtered at run start (logged as `qa_filtered_count`). The template's purpose is to define the questions and keyword signals, not to pre-link event IDs.

Target distribution: 40 shell, 30 claude, 20 browser, 10 workflow.

Example shell questions: "What git command did I run to rebase onto main?", "Which cargo command builds the daemon with OTel?", "What was the exit code of my last failed make command?"

Example claude questions: "How did I fix the enrichment loop deadlock?", "What was the outcome of the claude session where I designed the bench corpus?", "Which tool calls appeared in the session about sqlite-vec migration?"

Example browser questions: "What site did I visit longest yesterday?", "Which documentation page did I read about OTel resource attributes?", "How long did I spend on the Grafana dashboard documentation?"

Example workflow questions: "What was the annotation on the failing CI step?", "Which workflow ran when the Rust build broke?", "What conclusion did the release workflow report?"

Create `brain/src/hippo_brain/bench/qa_seed.py`:
```python
"""Seed eval-qa-v1.jsonl into the bench fixtures directory from the committed template."""
from pathlib import Path
from hippo_brain.bench.paths import bench_fixtures_dir

def seed_qa_fixture() -> int:
    template = Path(__file__).parent / "qa_template.jsonl"
    dest = bench_fixtures_dir(create=True) / "eval-qa-v1.jsonl"
    content = template.read_text()
    dest.write_text(content)
    count = sum(1 for line in content.splitlines() if line.strip())
    print(f"Seeded {count} Q/A items to {dest}")
    return count

if __name__ == "__main__":
    seed_qa_fixture()
```

**Verify:**
```bash
uv run --project brain python3 brain/src/hippo_brain/bench/qa_seed.py
python3 -c "
import json, pathlib
dest = pathlib.Path.home() / '.local' / 'share' / 'hippo-bench' / 'fixtures' / 'eval-qa-v1.jsonl'
items = [json.loads(l) for l in dest.read_text().splitlines() if l.strip()]
assert len(items) >= 100, f'Need 100+, got {len(items)}'
for item in items:
    assert all(k in item for k in ('qa_id', 'question', 'golden_event_id', 'source_filter', 'acceptable_answer_keywords'))
sources = [i['source_filter'] for i in items]
assert sources.count('shell') >= 30, f'shell: {sources.count(\"shell\")}'
assert sources.count('claude') >= 20, f'claude: {sources.count(\"claude\")}'
print('PASS:', len(items), 'Q/A items valid')
"
```

---

#### RB2-12 — bench/downstream_proxy.py

**Deps:** RB2-11  
**Budget:** 25 min  
**File:** `brain/src/hippo_brain/bench/downstream_proxy.py` (create)

**Work:**

Create `brain/src/hippo_brain/bench/downstream_proxy.py`.

Required functions:

**`load_qa_items(qa_path: Path, corpus_event_ids: set[str]) -> tuple[list[dict], int]`**
- Reads JSONL Q/A file line by line
- Filters items whose `golden_event_id` is NOT in `corpus_event_ids`
- Returns `(included_items, filtered_count)`

**`score_single_retrieval(results: list, golden_event_id: str, k_values: list[int] = [1, 3, 5, 10]) -> dict`**
- `results` is a list of SearchResult objects or dicts with an `event_id` or `uuid` field — adapt to whatever retrieval.search returns. Look at `brain/src/hippo_brain/retrieval.py` SearchResult dataclass before writing.
- Returns `{"hit_at_k": {1: bool, 3: bool, 5: bool, 10: bool}, "rank": int | None, "mrr": float, "ndcg_at_10": float}`
- Rank is 1-based. MRR = 1/rank if found else 0.0. NDCG@10 with binary relevance (1 if golden in top 10, 0 otherwise, discounted by log2(rank+1)).

**`run_downstream_proxy_pass(conn, qa_items: list[dict], embedding_fn: callable, modes: list[str] = ["hybrid", "semantic", "lexical"], k: int = 10) -> dict`**
- For each item x mode: call `embedding_fn(item["question"])` -> query_vec; call `retrieval.search(conn, item["question"], query_vec, mode=mode, limit=k)`
- Aggregate mean hit_at_1, hit_at_3, hit_at_5, hit_at_10, mrr, ndcg_at_10 per mode
- Return dict matching spec's `downstream_proxy` shape (see spec §"Downstream-proxy gate")

**`run_ask_synthesis_sample(qa_items: list[dict], ask_fn: callable, sample_size: int = 10, seed: int = 42) -> dict`**
- Deterministic sample by index: `items_sorted_by_qa_id[i % len(items)]` for i in range(sample_size)
- For each: call `ask_fn(item["question"])` -> response string; check if any keyword in `item["acceptable_answer_keywords"]` appears in response (case-insensitive)
- Return `{"sampled": sample_size, "keyword_hit_rate": float}`

Important: `conn` is the BENCH DB connection (per-model corpus copy), not prod. The `embedding_fn` and `ask_fn` are injected callables, making the module testable without LM Studio.

**Verify:**
```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/downstream_proxy.py
uv run --project brain ruff format --check brain/src/hippo_brain/bench/downstream_proxy.py
uv run --project brain python3 -c "from hippo_brain.bench.downstream_proxy import load_qa_items, score_single_retrieval; print('import ok')"
```

---

### Phase 5: Downstream-Proxy Tests

---

#### RB2-13 — test_bench_downstream_proxy.py

**Deps:** RB2-12  
**Budget:** 20 min  
**File:** `brain/tests/test_bench_downstream_proxy.py` (create)

**Work:**

Create `brain/tests/test_bench_downstream_proxy.py`. Use stub retrieval results — no live DB required.

Required test cases:

1. `test_load_qa_items_filters_missing` — 5 Q/A items; corpus has 3 of the 5 golden IDs; assert filtered_count=2, len(included)=3
2. `test_score_hit_at_1` — Golden at rank 1; assert hit_at_k[1]=True, mrr=1.0
3. `test_score_hit_at_5_not_1` — Golden at rank 3; assert hit_at_k[1]=False, hit_at_k[5]=True, mrr=approx(1/3)
4. `test_score_not_found` — Golden absent; assert all hit_at_k False, mrr=0.0, rank=None
5. `test_ndcg_perfect` — Golden at rank 1 in top 10; ndcg_at_10=1.0
6. `test_mode_aggregation_mean_mrr` — `run_downstream_proxy_pass()` with mock returning rank 1 for every item in hybrid mode; assert hybrid mrr=1.0
7. `test_ask_synthesis_keyword_hit` — Mock `ask_fn` returns response containing the keyword; keyword_hit_rate=1.0
8. `test_ask_synthesis_sample_deterministic` — Two calls with same seed pick same Q/A items

**Verify:**
```bash
uv run --project brain pytest brain/tests/test_bench_downstream_proxy.py -v --tb=short 2>&1 | tail -20
```
All 8 tests must pass.

---

### Phase 6: Telemetry Isolation Tests

---

#### RB2-14 — test_bench_telemetry_isolation.py

**Deps:** RB2-09, RB2-02  
**Budget:** 20 min  
**File:** `brain/tests/test_bench_telemetry_isolation.py` (create)

**Work:**

Create `brain/tests/test_bench_telemetry_isolation.py`. Use mocks throughout.

Required test cases:

1. `test_otel_resource_attributes_contains_namespace` — Mock Popen; assert `OTEL_RESOURCE_ATTRIBUTES` in env contains `service.namespace=hippo-bench`
2. `test_otel_resource_attributes_contains_run_id` — Same; contains `bench.run_id=<test_run_id>`
3. `test_otel_resource_attributes_contains_model_id` — Same; contains `bench.model_id=<test_model_id>`
4. `test_python_sdk_picks_up_env_namespace` — Set `OTEL_RESOURCE_ATTRIBUTES=service.namespace=hippo-bench` in env; `Resource.create({"service.name": "test"})` returns resource with `service.namespace == "hippo-bench"`; clean up env after
5. `test_bench_namespace_is_not_empty` — The injected namespace is `"hippo-bench"` (non-empty); assert it is not `""` (which is the prod filter token)

**Verify:**
```bash
uv run --project brain pytest brain/tests/test_bench_telemetry_isolation.py -v --tb=short 2>&1 | tail -20
```
All 5 tests must pass.

---

#### RB2-15 — crates/hippo-daemon/tests/telemetry_env_resource_test.rs

**Deps:** RB2-01  
**Budget:** 15 min  
**File:** `crates/hippo-daemon/tests/telemetry_env_resource_test.rs` (create)

**Work:**

Create the Rust test file:

```rust
//! Test that EnvResourceDetector integration picks up OTEL_RESOURCE_ATTRIBUTES.
//! Only compiled with --features otel.

#[cfg(feature = "otel")]
mod env_resource_tests {
    use hippo_daemon::telemetry;

    #[tokio::test]
    async fn test_env_resource_detector_init_succeeds() {
        std::env::set_var(
            "OTEL_RESOURCE_ATTRIBUTES",
            "service.namespace=hippo-bench-test,bench.run_id=rust-test-001",
        );
        let guard = telemetry::init(
            "hippo-daemon-test",
            "http://localhost:19999",
            std::io::stderr,
        )
        .expect("telemetry init should succeed with EnvResourceDetector");
        guard.shutdown();
        std::env::remove_var("OTEL_RESOURCE_ATTRIBUTES");
    }

    #[test]
    fn test_env_resource_detector_type_is_accessible() {
        // Compile-time proof: this would fail to compile if EnvResourceDetector
        // is not in scope after the RB2-01 fix.
        let _det = opentelemetry_sdk::resource::EnvResourceDetector::new();
    }
}
```

**Verify:**
```bash
cargo test -p hippo-daemon --features otel env_resource 2>&1 | tail -15
```
Both tests pass (or are compiled-out if feature not enabled, which would show as `test [ignored]`).

---

### Phase 7: Coordinator v2

---

#### RB2-16 — bench/coordinator_v2.py

**Deps:** RB2-09, RB2-04, RB2-12  
**Budget:** 30 min  
**File:** `brain/src/hippo_brain/bench/coordinator_v2.py` (create)

**Work:**

Create `brain/src/hippo_brain/bench/coordinator_v2.py`. Per-model lifecycle for v2.

```python
"""Per-model v2 lifecycle:
unload → load → copy corpus → spawn shadow stack → warmup → timed drain →
downstream-proxy pass → self-consistency pass → teardown → cooldown.
"""
```

`ModelRunResultV2` dataclass:
```python
@dataclasses.dataclass
class ModelRunResultV2:
    model: str
    attempts: list  # AttemptRecord instances
    per_event_vectors: list
    peak_metrics: dict
    wall_clock_sec: int
    cooldown_timeout: bool
    process_ready_ms: int
    queue_drain_wall_clock_sec: int
    downstream_proxy: dict
    prod_brain_restarted_during_bench: bool
    timeout_during_drain: bool
```

`run_one_model_v2()` function must:
1. Call `lms.unload_all()` then `lms.load(model)` (from v1's `lms.py`)
2. Copy `corpus_v2_sqlite_path()` to `run_tree / "hippo.db"` using `shutil.copy2`
3. Call `spawn_shadow_stack(run_tree=run_tree, run_id=run_id, model_id=model, ...)` -> `stack`
4. Call `wait_for_brain_ready(stack)` and record `process_ready_ms`
5. Run 3 warmup enrichments: seed 3 events into the bench DB's enrichment queues with `status='pending'` and wait for them to drain (poll queue count every 2s with 60s timeout)
6. Start `MetricsSampler(sample_interval_ms=250)` after warmup
7. Wait for main queue drain: poll all four queue tables every 2s; done when total pending+processing = 0 for 2 consecutive polls; hard timeout = `drain_timeout_sec` (default 3600s); set `timeout_during_drain=True` if hit
8. Every 120s during drain: probe prod brain `/health`; if brain is reachable AND `paused=False` (was paused when we started), set `prod_brain_restarted_during_bench=True`
9. After drain: call `run_downstream_proxy_pass()` against bench DB
10. After downstream-proxy: call `run_self_consistency_pass()` from v1's `runner.py` (reuse directly — it talks to LM Studio, not the shadow stack)
11. Stop sampler; call `teardown_shadow_stack(stack)`
12. Cooldown (wait for `load_avg_1m < 2.0`, max 90s)
13. Return `ModelRunResultV2`

**Verify:**
```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/coordinator_v2.py
uv run --project brain ruff format --check brain/src/hippo_brain/bench/coordinator_v2.py
uv run --project brain python3 -c "from hippo_brain.bench.coordinator_v2 import run_one_model_v2, ModelRunResultV2; print('import ok')"
```

---

#### RB2-17 — bench/preflight_v2.py

**Deps:** RB2-04, RB2-06, RB2-07  
**Budget:** 20 min  
**File:** `brain/src/hippo_brain/bench/preflight_v2.py` (create)

**Work:**

Create `brain/src/hippo_brain/bench/preflight_v2.py`. Add v2-specific checks that extend v1's set.

Functions to implement (all return `CheckResult` from `preflight.py`):

1. `check_prod_brain_reachable(brain_url: str) -> CheckResult` — GET /health; 200=pass with PID; refused=warn; error=fail
2. `check_prod_brain_pauseable(brain_url: str, skip: bool) -> CheckResult` — if skip: pass "skipped"; else POST /control/pause; 200=pass; refused=warn; 4xx/5xx=fail
3. `check_corpus_v2_present(corpus_sqlite: Path, manifest: Path) -> CheckResult` — calls `verify_corpus_v2()`; False=fail; also checks schema_version in corpus_meta vs. EXPECTED_SCHEMA_VERSION
4. `check_disk_free_bench(bench_root: Path, min_gb: float = 2.0) -> CheckResult` — reuse logic from v1's `check_disk_space()`

`run_all_preflight_v2(brain_url: str, corpus_sqlite: Path, manifest: Path, lmstudio_url: str, skip_prod_pause: bool) -> tuple[list[CheckResult], bool]`:
- Returns `(checks, aborted)` where `aborted=True` if any hard-fail check fired
- Hard-fail conditions: corpus schema mismatch, corpus file missing, LM Studio unreachable, disk < 2GB, prod brain reachable AND not pauseable AND `--skip-prod-pause` not set

**Verify:**
```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/preflight_v2.py
uv run --project brain ruff format --check brain/src/hippo_brain/bench/preflight_v2.py
uv run --project brain python3 -c "from hippo_brain.bench.preflight_v2 import run_all_preflight_v2; print('import ok')"
```

---

#### RB2-18 — bench/orchestrate_v2.py

**Deps:** RB2-16, RB2-17, RB2-19  
**Budget:** 25 min  
**File:** `brain/src/hippo_brain/bench/orchestrate_v2.py` (create)

**Work:**

Create `brain/src/hippo_brain/bench/orchestrate_v2.py`. Top-level orchestrator for v2 runs.

Key behavior differences from v1's `orchestrate.py`:

1. Before model loop:
   - Build `run_manifest` with `RunManifestRecordV2` (host_baseline with load_avg_1m/5m, prod_state_at_start, corpus_schema_version, eval_qa_version)
   - Run `run_all_preflight_v2()`; abort if hard fail
   - If prod brain running: POST `/control/pause` via `PauseRpcClient`; register `atexit.register(pause_client.resume)` for best-effort cleanup
   - Record `prod_state_at_start`

2. For each model:
   - `lms_unload_all()` then `lms.load(model)`
   - Create `run_tree = bench_run_tree(run_id, model_id, create=True)`
   - Call `run_one_model_v2(...)`
   - Write all AttemptRecords, then `ModelSummaryRecordV2`
   - Track `models_with_prod_restart_event`

3. After model loop:
   - POST `/control/resume`
   - Write `RunEndRecordV2` with `prod_brain_resumed_ok` and `models_with_prod_restart_event`

4. Dry-run path (must work without LM Studio or corpus file):
   - Skip all preflight checks
   - Skip pause/resume RPC
   - Skip model loading and shadow stack spawn
   - Write manifest (`bench_version="0.2.0"`, `corpus_version` from args) and immediately write run_end with `reason="dry_run"`
   - The dry-run is the smoke test for acceptance criterion #2

**Verify:**
```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/orchestrate_v2.py
uv run --project brain ruff format --check brain/src/hippo_brain/bench/orchestrate_v2.py
uv run --project brain python3 -c "from hippo_brain.bench.orchestrate_v2 import orchestrate_run_v2; print('import ok')"
```

---

### Phase 8: Output Schema v2

---

#### RB2-19 — bench/output_v2.py

**Deps:** RB2-06  
**Budget:** 15 min  
**File:** `brain/src/hippo_brain/bench/output_v2.py` (create)

**Work:**

Create `brain/src/hippo_brain/bench/output_v2.py`. New dataclasses with v2 fields.

```python
"""v2 JSONL record shapes. Imports RunWriter from output.py (unchanged)."""
from __future__ import annotations
from dataclasses import asdict, dataclass, field
from typing import Any
from hippo_brain.bench.output import RunWriter  # reuse writer unchanged
```

Three dataclasses:

**`RunManifestRecordV2`**: all v1 fields PLUS:
- `bench_version: str = "0.2.0"`
- `corpus_schema_version: int = 0`
- `eval_qa_version: str = "eval-qa-v1"`
- `embedding_model: str = ""`
- `host_baseline: dict = field(default_factory=dict)` — keys: `load_avg_1m_at_start`, `load_avg_5m_at_start`
- `prod_state_at_start: dict = field(default_factory=dict)` — keys: `brain_pid`, `brain_paused`, `daemon_pid`, `daemon_running`

Each has `to_dict() -> dict[str, Any]` that sets `"record_type": "run_manifest"` and serializes with `asdict()`.

**`ModelSummaryRecordV2`**: all v1 fields PLUS:
- `process_ready_ms: int = 0`
- `queue_drain_wall_clock_sec: int = 0`
- `downstream_proxy: dict = field(default_factory=dict)`
- `prod_brain_restarted_during_bench: bool = False`
- `timeout_during_drain: bool = False`

**`RunEndRecordV2`**: all v1 fields PLUS:
- `prod_brain_resumed_ok: bool = True`
- `models_with_prod_restart_event: list[str] = field(default_factory=list)`

**Verify:**
```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/output_v2.py
uv run --project brain python3 -c "
from hippo_brain.bench.output_v2 import RunManifestRecordV2, ModelSummaryRecordV2, RunEndRecordV2
r = RunManifestRecordV2(run_id='test', started_at_iso='2026-01-01T00:00:00+00:00', finished_at_iso=None, host={}, preflight_checks=[], candidate_models=[])
d = r.to_dict()
assert d['record_type'] == 'run_manifest'
assert d['bench_version'] == '0.2.0'
assert 'corpus_schema_version' in d
assert 'host_baseline' in d
print('PASS: output_v2 dataclasses valid')
"
```

---

#### RB2-20 — bench/schemas_v2.py

**Deps:** RB2-06  
**Budget:** 10 min  
**File:** `brain/src/hippo_brain/bench/schemas_v2.py` (create)

**Work:**

Create `brain/src/hippo_brain/bench/schemas_v2.py` with one function:

```python
import sqlite3
from pathlib import Path

def assert_corpus_schema_version(bench_db: Path, expected_schema_version: int) -> None:
    """Read corpus_meta.schema_version from the bench SQLite; raise RuntimeError on mismatch."""
    conn = sqlite3.connect(bench_db)
    try:
        row = conn.execute("SELECT schema_version FROM corpus_meta").fetchone()
        if row is None:
            raise RuntimeError("corpus_meta table missing or empty — rebuild corpus")
        stored = row[0]
        if stored != expected_schema_version:
            raise RuntimeError(
                f"corpus schema version mismatch: bench corpus has schema_version={stored}, "
                f"live hippo has schema_version={expected_schema_version}. "
                "Rebuild corpus with: hippo-bench corpus init --bump-version"
            )
    finally:
        conn.close()
```

**Verify:**
```bash
uv run --project brain ruff check brain/src/hippo_brain/bench/schemas_v2.py
uv run --project brain python3 -c "from hippo_brain.bench.schemas_v2 import assert_corpus_schema_version; print('import ok')"
```

---

### Phase 9: CLI v2

---

#### RB2-21P — bench/__init__.py: version bump to 0.2.0

**Deps:** RB2-18  
**Budget:** 5 min  
**File:** `brain/src/hippo_brain/bench/__init__.py`

**Work:**

Read `brain/src/hippo_brain/bench/__init__.py`. Update `__version__` to `"0.2.0"`. Only change the version string — nothing else.

**Verify:**
```bash
uv run --project brain python3 -c "from hippo_brain.bench import __version__; assert __version__ == '0.2.0', f'Got {__version__}'; print('PASS: version =', __version__)"
```

---

#### RB2-21 — bench/cli.py: extend with v2 subcommands

**Deps:** RB2-18, RB2-21P  
**Budget:** 25 min  
**File:** `brain/src/hippo_brain/bench/cli.py`

**Work:**

Extend the existing `cli.py`. DO NOT rewrite existing v1 command handling — append new argument definitions and dispatch logic.

Changes:

1. **`corpus init` subcommand**: Add arguments:
   - `--corpus-version` default changed to `"corpus-v2"` (was `"corpus-v1"`)
   - `--corpus-days`, default 90
   - `--corpus-buckets`, default 9
   - `--shell-min`, `--claude-min`, `--browser-min`, `--workflow-min`, all default 50
   - `--bump-version <str>` — if provided, is passed to `init_corpus_v2()` as the new version string (overwrite allowed)
   - When `corpus_version` is `corpus-v2`, call `init_corpus_v2()` from `corpus_v2.py` and output both SQLite and JSONL paths
   - When `corpus_version` is `corpus-v1`, keep existing v1 behavior

2. **`corpus add-adversarial` subcommand** (new):
   - Positional: `event_id` (e.g., `shell-12345`)
   - `--reason <text>` (required)
   - `--source shell|claude|browser|workflow` (optional override)
   - Looks up event in prod DB, redacts, appends to `corpus_v2_overlay_path()`
   - Enforces 50-item cap (print error and exit 1 if at cap)

3. **`corpus verify`**: Route to `verify_corpus_v2()` for corpus-v2

4. **`run` subcommand**: Add arguments:
   - `--skip-prod-pause` (store_true)
   - `--with-ask-synthesis` (store_true, default off)
   - `--ask-synthesis-sample`, default 10
   - When `corpus_version` is `corpus-v2`, call `orchestrate_run_v2()` from `orchestrate_v2.py`

5. Keep all v1 `run` behavior unchanged when `--corpus-version corpus-v1`

**Verify:**
```bash
uv run --project brain hippo-bench --help 2>&1 | grep -E "corpus|run|summary"
uv run --project brain hippo-bench corpus --help 2>&1 | grep -E "init|verify|add-adversarial"
uv run --project brain ruff check brain/src/hippo_brain/bench/cli.py
uv run --project brain ruff format --check brain/src/hippo_brain/bench/cli.py
```

---

### Phase 10: Grafana Dashboards

---

#### RB2-22 — Prod dashboards: add service_namespace="" filter

**Deps:** RB2-01  
**Budget:** 20 min  
**Files:** `otel/grafana/dashboards/hippo-daemon.json`, `otel/grafana/dashboards/hippo-enrichment.json`, `otel/grafana/dashboards/hippo-overview.json`, `otel/grafana/dashboards/hippo-processes.json`

**Work:**

For every Prometheus `"expr"` field in all four dashboard JSON files that references a `hippo_` metric, add `service_namespace=""` inside the curly braces.

Pattern: if the metric already has labels `{foo="bar"}`, inject `service_namespace="",` as the first label: `{service_namespace="",foo="bar"}`. If the metric has no labels, add `{service_namespace=""}`.

The empty-string filter (`service_namespace=""`) matches rows where the label is absent OR set to `""` — both are the prod state. Bench rows have `service_namespace="hippo-bench"` and will not match.

For rate() expressions: `rate(hippo_X{...}[5m])` becomes `rate(hippo_X{service_namespace="",...}[5m])`.

Write each file back as indented JSON (use `json.dumps(data, indent=2)` to match existing formatting).

Do NOT change any field except `"expr"` strings inside `"targets"` arrays. Preserve all UIDs, panel titles, panel IDs, datasource configs.

**Verify:**
```bash
python3 -c "
import json, glob
dashboards = glob.glob('otel/grafana/dashboards/hippo-*.json')
for d in dashboards:
    if 'bench' in d: continue
    with open(d) as f:
        data = json.load(f)
    for panel in data.get('panels', []):
        for target in panel.get('targets', []):
            expr = target.get('expr', '')
            if 'hippo_' in expr:
                assert 'service_namespace' in expr, f'{d}: missing filter in: {expr[:120]}'
print('PASS: all', len(dashboards), 'prod dashboards have service_namespace filter')
"
```

---

#### RB2-23 — otel/grafana/dashboards/bench-run-overview.json

**Deps:** RB2-22  
**Budget:** 20 min  
**File:** `otel/grafana/dashboards/bench-run-overview.json` (create)

**Work:**

Create a valid Grafana dashboard JSON (v8 format). Base the structure on `otel/grafana/dashboards/hippo-enrichment.json` (copy and modify). Required properties:

- `"uid": "hippo-bench-run-overview"`
- `"title": "Hippo Bench — Run Overview"`
- All metric exprs use `{service_namespace="hippo-bench",bench_run_id="$run_id"}` filters

Template variables:
- `$run_id`: `"type": "query"`, query for `label_values(bench_run_id)` from Prometheus
- `$namespace`: constant `"hippo-bench"`

Required panels (minimum 4):

1. **Candidate Models** (stat): `count(count by (bench_model_id) (hippo_bench_queue_drain_sec{service_namespace="hippo-bench",bench_run_id="$run_id"}))`
2. **P95 Latency by Model** (bar gauge): `hippo_bench_latency_p95_ms{service_namespace="hippo-bench",bench_run_id="$run_id"} by (bench_model_id)` — or the appropriate gauge metric emitted by coordinator_v2
3. **Schema Validity Rate by Model** (bar gauge): `hippo_bench_schema_validity_rate{service_namespace="hippo-bench",bench_run_id="$run_id"}`
4. **Downstream Proxy Hit@1 by Model** (bar gauge): `hippo_bench_downstream_proxy_hit_at_1{service_namespace="hippo-bench",bench_run_id="$run_id"}`
5. **System Load Peak by Model** (time series): `hippo_bench_load_avg_1m_peak{service_namespace="hippo-bench",bench_run_id="$run_id"}`

Note on metric names: these are bench-specific gauge metrics emitted by coordinator_v2's `MetricsSampler` extension or a new bench-specific OTel meter. If the implementation phase uses different names, the implementing agent must update the dashboard expressions accordingly. The dashboard is a skeleton; exact metric names are locked during RB2-16 implementation.

**Verify:**
```bash
python3 -c "
import json
d = json.load(open('otel/grafana/dashboards/bench-run-overview.json'))
assert d.get('uid') == 'hippo-bench-run-overview', f'uid={d.get(\"uid\")}'
assert len(d.get('panels', [])) >= 4, f'panels={len(d.get(\"panels\", []))}'
print('PASS: bench-run-overview.json valid, uid correct,', len(d['panels']), 'panels')
"
```

---

#### RB2-24 — otel/grafana/dashboards/bench-model-drilldown.json

**Deps:** RB2-22  
**Budget:** 15 min  
**File:** `otel/grafana/dashboards/bench-model-drilldown.json` (create)

**Work:**

Create dashboard JSON. Properties:
- `"uid": "hippo-bench-model-drilldown"`
- `"title": "Hippo Bench — Model Drilldown"`
- Template variable: `$model_id` from `label_values(bench_model_id)`

Required panels (minimum 4):
1. Per-event latency histogram (time series)
2. System metrics over bench window (CPU%, load_avg, mem_free)
3. Gate pass rates (schema validity, refusal, entity sanity)
4. Downstream proxy metrics per mode (hybrid/semantic/lexical hit@K and MRR)

All exprs use `{service_namespace="hippo-bench",bench_model_id="$model_id"}`.

**Verify:**
```bash
python3 -c "import json; d=json.load(open('otel/grafana/dashboards/bench-model-drilldown.json')); assert d.get('uid') == 'hippo-bench-model-drilldown'; assert len(d.get('panels',[])) >= 4; print('PASS')"
```

---

#### RB2-25 — otel/grafana/dashboards/bench-model-comparison.json

**Deps:** RB2-22  
**Budget:** 15 min  
**File:** `otel/grafana/dashboards/bench-model-comparison.json` (create)

**Work:**

Create dashboard JSON. Properties:
- `"uid": "hippo-bench-model-comparison"`
- `"title": "Hippo Bench — Model Comparison"`
- Template variables: `$run_id`, `$model_a`, `$model_b`

Required panels (minimum 4 — side-by-side for model_a vs model_b):
1. P95 latency comparison
2. Schema validity rate comparison
3. Downstream Hit@1 comparison
4. System load peak comparison

**Verify:**
```bash
python3 -c "import json; d=json.load(open('otel/grafana/dashboards/bench-model-comparison.json')); assert d.get('uid') == 'hippo-bench-model-comparison'; assert len(d.get('panels',[])) >= 4; print('PASS')"
```

---

### Phase 11: Docs + Acceptance Gates

---

#### RB2-26 — bench/README.md: v2 usage section

**Deps:** RB2-21  
**Budget:** 15 min  
**File:** `brain/src/hippo_brain/bench/README.md`

**Work:**

Read the existing README. Append a `## v2 Usage` section. Do NOT remove or edit existing v1 content. New section must cover:

1. Prerequisites: `hippo-bench corpus init` to generate `corpus-v2.sqlite`, `uv run --project brain python3 brain/src/hippo_brain/bench/qa_seed.py` to seed the Q/A fixture
2. Running: `hippo-bench run --models <model> --corpus-version corpus-v2 [--with-ask-synthesis]`
3. Prod brain coordination: automatic pause/resume; override with `--skip-prod-pause`
4. Results: JSONL at `~/.local/share/hippo-bench/runs/`
5. Cross-reference both design docs:
   - "v1 design: `docs/superpowers/specs/2026-04-21-hippo-bench-design.md` (history-preserved)"
   - "v2 design: `docs/superpowers/specs/2026-04-27-hippo-bench-v2-design.md`"

**Verify:**
```bash
grep -q "v2 Usage" brain/src/hippo_brain/bench/README.md && echo "PASS: v2 section found" || echo "FAIL: v2 section missing"
grep -q "2026-04-27-hippo-bench-v2-design.md" brain/src/hippo_brain/bench/README.md && echo "PASS: v2 spec linked" || echo "FAIL: v2 spec link missing"
grep -q "2026-04-21-hippo-bench-design.md" brain/src/hippo_brain/bench/README.md && echo "PASS: v1 spec linked" || echo "FAIL: v1 spec link missing"
```

---

#### RB2-27 — Dry-run smoke test

**Deps:** RB2-21  
**Budget:** 10 min  
**File:** `/tmp/test-bench-v2-dryrun.jsonl` (ephemeral output)

**Work:**

Run the dry-run smoke test:

```bash
uv run --project brain hippo-bench run \
  --dry-run \
  --models qwen3.5-35b-a3b \
  --corpus-version corpus-v2 \
  --skip-prod-pause \
  --out /tmp/test-bench-v2-dryrun.jsonl
```

Then validate the output file.

In dry-run mode with `--skip-prod-pause`, the orchestrator must:
- NOT require LM Studio to be running
- NOT require a corpus file to exist
- Write a valid `run_manifest` record with `bench_version="0.2.0"` and `corpus_version="corpus-v2"`
- Write a valid `run_end` record with `reason="dry_run"`
- Exit 0

**Verify:**
```bash
uv run --project brain hippo-bench run --dry-run --models qwen3.5-35b-a3b --corpus-version corpus-v2 --skip-prod-pause --out /tmp/test-bench-v2-dryrun.jsonl
python3 -c "
import json
records = [json.loads(l) for l in open('/tmp/test-bench-v2-dryrun.jsonl') if l.strip()]
types = [r['record_type'] for r in records]
assert 'run_manifest' in types, f'missing run_manifest, got: {types}'
assert 'run_end' in types, f'missing run_end, got: {types}'
manifest = next(r for r in records if r['record_type'] == 'run_manifest')
assert manifest.get('bench_version') == '0.2.0', f'bench_version={manifest.get(\"bench_version\")}'
assert manifest.get('corpus_version') == 'corpus-v2', f'corpus_version={manifest.get(\"corpus_version\")}'
end = next(r for r in records if r['record_type'] == 'run_end')
assert end.get('reason') == 'dry_run', f'reason={end.get(\"reason\")}'
print('PASS: dry-run smoke test')
print('  records:', types)
"
```

---

#### RB2-28 — Full test suite pass

**Deps:** RB2-05, RB2-08, RB2-10, RB2-13, RB2-14, RB2-15, RB2-27  
**Budget:** 20 min  
**File:** none (verification only)

**Work:**

Run the complete Python and Rust test suites.

**Verify:**
```bash
uv run --project brain pytest brain/tests -v --tb=short -q 2>&1 | tail -10
# Must end with "N passed, 0 failed" (or "N passed, M skipped" — skips OK for tests requiring live services)
cargo test -p hippo-daemon --features otel 2>&1 | grep -E "^test result|FAILED|error"
# Must show "test result: ok." with 0 failures
```

---

## Dependency DAG

```
No deps (start here):   RB2-01  RB2-02  RB2-03

From RB2-01+02:         RB2-04 ── RB2-05
                        RB2-09 ── RB2-10, RB2-14
                        RB2-15
                        RB2-22 ── RB2-23, RB2-24, RB2-25

From RB2-03:            RB2-06 ── RB2-08 (with RB2-07)
                                   RB2-19, RB2-20
                        RB2-07 ── RB2-11 ── RB2-12 ── RB2-13

From RB2-04+09+12:      RB2-16
From RB2-04+06+07:      RB2-17
From RB2-16+17+19:      RB2-18 ── RB2-21P ── RB2-21 ── RB2-26, RB2-27

Terminal gate:          RB2-28 (waits on all test tasks + smoke test)
```

---

## Hard Exit Conditions

The runner exits immediately if:
1. All tasks `completed` — success
2. `blocked` count > 5 — systematic failure
3. Iteration count > `RALPH_MAX_ITERS` (default 60)
4. Wall clock > `RALPH_MAX_WALL_HOURS` (default 8)
5. Consecutive failures on the same task > 3 — mark blocked, move on

---

## Acceptance Criteria Mapping (from spec)

| # | Spec criterion | Covered by |
|---|---|---|
| 1 | corpus init produces both artifacts atomically, passing verify | RB2-06, RB2-08 |
| 2 | clean run produces JSONL with correct record types | RB2-27 (dry-run smoke) |
| 3 | pre-flight aborts on unreachable brain / schema mismatch / absent LM Studio | RB2-17, RB2-05 |
| 4 | bench DB never appears in prod hippo.db | RB2-07 (path separation), RB2-09 (XDG override) |
| 5 | telemetry isolation via service_namespace | RB2-01, RB2-14 |
| 6 | prod dashboards updated; brain resumes within 10s of run_end | RB2-22, RB2-04 |
| 7 | downstream-proxy metrics for >=80% of Q/A items | RB2-12, RB2-13 |
| 8 | teardown leaves no orphan processes | RB2-09, RB2-10 |
| 9 | all v1 + v2 tests pass | RB2-28 |
| 10 | README updated with v2 usage | RB2-26 |
