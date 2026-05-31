# Hippo Bench Trust Blockers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `hippo-bench` produce non-empty, schema-current, labeled, downstream-scored run JSONL with real model gate verdicts instead of hard-coded pass records.

**Architecture:** Keep the existing `hippo_brain.bench` module layout. Add a small Q/A validation module and CLI subcommands so corpus/Q/A compatibility is machine-checked before any model run. Wire embeddings through `orchestrate_run -> run_one_model`, then derive model gates from actual attempt records and self-consistency vectors before writing `model_summary`.

**Tech Stack:** Python 3.14, `uv`, pytest, ruff, SQLite, existing OpenAI-compatible embedding endpoint via `bench.enrich_call.call_embedding`.

---

## Files

- Modify: `brain/src/hippo_brain/bench/cli.py`
  Add `qa validate` and `qa export-worklist` subcommands; keep `corpus verify` hash-only.
- Create: `brain/src/hippo_brain/bench/qa.py`
  Q/A JSONL loader, corpus-ID validation, worklist export, scoreable-count gate.
- Modify: `brain/src/hippo_brain/bench/preflight.py`
  Add a hard-fail Q/A scoreability check to run preflight.
- Modify: `brain/src/hippo_brain/bench/orchestrate.py`
  Construct an embedding function for CLI runs and compute real gates/verdicts.
- Modify: `brain/src/hippo_brain/bench/coordinator.py`
  Accept and use the embedding function already exposed in the signature; no direct CLI-specific code here.
- Modify: `brain/src/hippo_brain/bench/summary.py`
  Add a helper for “no self-consistency vectors” so missing SC is skipped, not converted to zero.
- Modify: `brain/src/hippo_brain/bench/README.md`
  Remove the two veracity caveats once fixed; document required corpus/Q/A validation.
- Modify: `docs/capture/bench-runbook.md`
  Add the schema-18 corpus rebuild and Q/A validation sequence before BT-29.
- Test: `brain/tests/test_bench_qa.py`
- Test: `brain/tests/test_bench_preflight.py`
- Test: `brain/tests/test_bench_orchestrate.py`
- Test: `brain/tests/test_bench_cli.py`

---

### Task 1: Add Q/A Fixture Validation

**Files:**
- Create: `brain/src/hippo_brain/bench/qa.py`
- Test: `brain/tests/test_bench_qa.py`

- [ ] **Step 1: Write failing tests for scoreable Q/A validation**

Create `brain/tests/test_bench_qa.py`:

```python
from __future__ import annotations

import sqlite3
from pathlib import Path

from hippo_brain.bench.qa import export_label_worklist, validate_qa_fixture


def _write_corpus_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE events (id INTEGER PRIMARY KEY, command TEXT)")
        conn.execute("CREATE TABLE claude_sessions (id INTEGER PRIMARY KEY, summary_text TEXT)")
        conn.execute("INSERT INTO events (id, command) VALUES (1, 'cargo test')")
        conn.execute("INSERT INTO claude_sessions (id, summary_text) VALUES (2, 'bench design')")
        conn.commit()
    finally:
        conn.close()


def test_validate_qa_fixture_counts_scoreable_items(tmp_path: Path) -> None:
    db = tmp_path / "corpus.sqlite"
    qa = tmp_path / "eval-qa-v1.jsonl"
    _write_corpus_db(db)
    qa.write_text(
        "\n".join(
            [
                '{"qa_id":"q1","question":"cmd?","golden_event_id":"shell-1"}',
                '{"qa_id":"q2","question":"session?","golden_event_id":"claude-2"}',
                '{"qa_id":"q3","question":"missing?","golden_event_id":"shell-999"}',
                '{"qa_id":"q4","question":"null?","golden_event_id":null}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    report = validate_qa_fixture(qa, db, min_scoreable=2)

    assert report.total == 4
    assert report.scoreable == 2
    assert report.unscoreable == 2
    assert report.passes is True
    assert report.missing_by_qa_id == {"q3": "shell-999", "q4": None}


def test_validate_qa_fixture_fails_under_minimum(tmp_path: Path) -> None:
    db = tmp_path / "corpus.sqlite"
    qa = tmp_path / "eval-qa-v1.jsonl"
    _write_corpus_db(db)
    qa.write_text('{"qa_id":"q1","question":"cmd?","golden_event_id":"shell-1"}\n')

    report = validate_qa_fixture(qa, db, min_scoreable=2)

    assert report.scoreable == 1
    assert report.passes is False
    assert "need at least 2 scoreable Q/A items" in report.detail


def test_export_label_worklist_writes_unlabeled_questions(tmp_path: Path) -> None:
    db = tmp_path / "corpus.sqlite"
    qa = tmp_path / "eval-qa-v1.jsonl"
    out = tmp_path / "worklist.jsonl"
    _write_corpus_db(db)
    qa.write_text(
        '{"qa_id":"q1","question":"cmd?","golden_event_id":null,"source_filter":"shell"}\n',
        encoding="utf-8",
    )

    count = export_label_worklist(qa, db, out)

    assert count == 1
    text = out.read_text(encoding="utf-8")
    assert '"qa_id": "q1"' in text
    assert '"candidate_event_ids": ["shell-1"]' in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run --project brain pytest brain/tests/test_bench_qa.py -q
```

Expected: import failure for `hippo_brain.bench.qa`.

- [ ] **Step 3: Implement `bench.qa`**

Create `brain/src/hippo_brain/bench/qa.py`:

```python
from __future__ import annotations

import contextlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class QaValidationReport:
    total: int
    scoreable: int
    unscoreable: int
    min_scoreable: int
    missing_by_qa_id: dict[str, str | None]

    @property
    def passes(self) -> bool:
        return self.scoreable >= self.min_scoreable

    @property
    def detail(self) -> str:
        if self.passes:
            return (
                f"scoreable Q/A items: {self.scoreable}/{self.total} "
                f"(minimum {self.min_scoreable})"
            )
        return (
            f"need at least {self.min_scoreable} scoreable Q/A items; "
            f"found {self.scoreable}/{self.total}"
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "scoreable": self.scoreable,
            "unscoreable": self.unscoreable,
            "min_scoreable": self.min_scoreable,
            "missing_by_qa_id": dict(self.missing_by_qa_id),
            "passes": self.passes,
            "detail": self.detail,
        }


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line:
                obj = json.loads(line)
                if not isinstance(obj, dict):
                    raise ValueError(f"{path}: expected JSON object per line")
                items.append(obj)
    return items


def collect_corpus_event_ids(corpus_sqlite: Path) -> set[str]:
    specs = [
        ("shell", "events", "id"),
        ("claude", "claude_sessions", "id"),
        ("browser", "browser_events", "id"),
        ("workflow", "workflow_runs", "id"),
    ]
    ids: set[str] = set()
    with contextlib.closing(sqlite3.connect(f"file:{corpus_sqlite}?mode=ro", uri=True)) as conn:
        for prefix, table, id_col in specs:
            try:
                rows = conn.execute(f"SELECT {id_col} FROM {table}").fetchall()
            except sqlite3.OperationalError:
                continue
            ids.update(f"{prefix}-{row[0]}" for row in rows if row[0] is not None)
    return ids


def validate_qa_fixture(
    qa_path: Path,
    corpus_sqlite: Path,
    *,
    min_scoreable: int,
) -> QaValidationReport:
    items = _load_jsonl(qa_path)
    corpus_ids = collect_corpus_event_ids(corpus_sqlite)
    missing: dict[str, str | None] = {}
    scoreable = 0
    for idx, item in enumerate(items, start=1):
        qa_id = str(item.get("qa_id") or item.get("id") or f"line-{idx}")
        golden = item.get("golden_event_id")
        if isinstance(golden, str) and golden in corpus_ids:
            scoreable += 1
        else:
            missing[qa_id] = golden if isinstance(golden, str) else None
    return QaValidationReport(
        total=len(items),
        scoreable=scoreable,
        unscoreable=len(items) - scoreable,
        min_scoreable=min_scoreable,
        missing_by_qa_id=missing,
    )


def export_label_worklist(qa_path: Path, corpus_sqlite: Path, out_path: Path) -> int:
    items = _load_jsonl(qa_path)
    corpus_ids = sorted(collect_corpus_event_ids(corpus_sqlite))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out_path.open("w", encoding="utf-8") as f:
        for idx, item in enumerate(items, start=1):
            golden = item.get("golden_event_id")
            if isinstance(golden, str) and golden in corpus_ids:
                continue
            source_filter = item.get("source_filter")
            candidates = [
                event_id
                for event_id in corpus_ids
                if not isinstance(source_filter, str) or event_id.startswith(f"{source_filter}-")
            ]
            f.write(
                json.dumps(
                    {
                        "qa_id": item.get("qa_id") or item.get("id") or f"line-{idx}",
                        "question": item.get("question"),
                        "source_filter": source_filter,
                        "current_golden_event_id": golden,
                        "candidate_event_ids": candidates,
                    },
                    sort_keys=True,
                )
                + "\n"
            )
            written += 1
    return written
```

- [ ] **Step 4: Run tests**

Run:

```bash
uv run --project brain pytest brain/tests/test_bench_qa.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/bench/qa.py brain/tests/test_bench_qa.py
git commit -m "feat(bench): validate scoreable QA fixtures"
```

---

### Task 2: Add CLI and Preflight Gates for Scoreable Q/A

**Files:**
- Modify: `brain/src/hippo_brain/bench/cli.py`
- Modify: `brain/src/hippo_brain/bench/preflight.py`
- Test: `brain/tests/test_bench_cli.py`
- Test: `brain/tests/test_bench_preflight.py`

- [ ] **Step 1: Write failing preflight test**

Append to `brain/tests/test_bench_preflight.py`:

```python
def test_run_all_preflight_fails_when_qa_has_no_scoreable_items(tmp_path, monkeypatch):
    from hippo_brain.bench import preflight

    corpus = tmp_path / "corpus.sqlite"
    manifest = tmp_path / "corpus.manifest.json"
    qa = tmp_path / "eval-qa-v1.jsonl"
    corpus.write_bytes(b"")
    manifest.write_text("{}")
    qa.write_text('{"qa_id":"q1","question":"x","golden_event_id":null}\n')

    monkeypatch.setattr(preflight, "check_prod_brain_reachable", lambda _u: preflight.CheckResult("prod_brain_reachable", "warn", "off"))
    monkeypatch.setattr(preflight, "check_prod_brain_pauseable", lambda _u, skip: preflight.CheckResult("prod_brain_pauseable", "warn", "off"))
    monkeypatch.setattr(preflight, "check_corpus_present", lambda _c, _m: preflight.CheckResult("corpus_present", "pass", "schema_version=18"))
    monkeypatch.setattr(preflight, "check_inference_reachable", lambda _u: preflight.CheckResult("inference_reachable", "pass", "HTTP 200"))
    monkeypatch.setattr(preflight, "check_disk_free_bench", lambda _p: preflight.CheckResult("disk_free_bench", "pass", "ok"))
    monkeypatch.setattr(preflight, "check_brain_port_free", lambda _p: preflight.CheckResult("brain_port_free", "pass", "ok"))
    monkeypatch.setattr(preflight, "bench_qa_path", lambda: qa)
    monkeypatch.setattr(preflight, "validate_qa_fixture", lambda *_a, **_k: type("R", (), {"passes": False, "detail": "need at least 1 scoreable Q/A items"})())

    checks, aborted = preflight.run_all_preflight(
        brain_url="http://127.0.0.1:9175",
        corpus_sqlite=corpus,
        manifest=manifest,
        inference_url="http://localhost:1234/v1",
        skip_prod_pause=True,
        min_scoreable_qa=1,
    )

    assert aborted is True
    assert any(c.name == "qa_scoreable" and c.status == "fail" for c in checks)
```

- [ ] **Step 2: Write failing CLI tests**

Append to `brain/tests/test_bench_cli.py`:

```python
def test_cli_qa_validate_prints_report(monkeypatch, tmp_path, capsys):
    from hippo_brain.bench import cli

    qa = tmp_path / "qa.jsonl"
    corpus = tmp_path / "corpus.sqlite"
    qa.write_text("")
    corpus.write_bytes(b"")

    class Report:
        passes = True
        detail = "scoreable Q/A items: 3/3 (minimum 1)"

        def to_dict(self):
            return {"scoreable": 3, "total": 3, "passes": True}

    monkeypatch.setattr(cli, "validate_qa_fixture", lambda *_a, **_k: Report())

    code = cli.main([
        "qa",
        "validate",
        "--qa-path",
        str(qa),
        "--corpus-sqlite",
        str(corpus),
        "--min-scoreable",
        "1",
    ])

    assert code == 0
    assert "scoreable Q/A items" in capsys.readouterr().out


def test_cli_qa_export_worklist(monkeypatch, tmp_path):
    from hippo_brain.bench import cli

    qa = tmp_path / "qa.jsonl"
    corpus = tmp_path / "corpus.sqlite"
    out = tmp_path / "worklist.jsonl"
    qa.write_text("")
    corpus.write_bytes(b"")
    monkeypatch.setattr(cli, "export_label_worklist", lambda *_a: 7)

    code = cli.main([
        "qa",
        "export-worklist",
        "--qa-path",
        str(qa),
        "--corpus-sqlite",
        str(corpus),
        "--out",
        str(out),
    ])

    assert code == 0
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
uv run --project brain pytest brain/tests/test_bench_preflight.py::test_run_all_preflight_fails_when_qa_has_no_scoreable_items brain/tests/test_bench_cli.py::test_cli_qa_validate_prints_report brain/tests/test_bench_cli.py::test_cli_qa_export_worklist -q
```

Expected: import/signature/parser failures.

- [ ] **Step 4: Implement preflight Q/A check**

In `brain/src/hippo_brain/bench/preflight.py`, add imports:

```python
from hippo_brain.bench.paths import bench_qa_path, hippo_bench_root
from hippo_brain.bench.qa import validate_qa_fixture
```

Replace the existing `hippo_bench_root` import line if necessary so there is only one `paths` import.

Add:

```python
def check_qa_scoreable(corpus_sqlite: Path, min_scoreable: int = 1) -> CheckResult:
    qa_path = bench_qa_path()
    if not qa_path.exists():
        return CheckResult(
            name="qa_scoreable",
            status="fail",
            detail=f"Q/A fixture missing: {qa_path}",
        )
    report = validate_qa_fixture(qa_path, corpus_sqlite, min_scoreable=min_scoreable)
    return CheckResult(
        name="qa_scoreable",
        status="pass" if report.passes else "fail",
        detail=report.detail,
    )
```

> **Implementation note (intentional deviation from this plan).** As shipped,
> `check_qa_scoreable` returns `status="warn"` (not `"fail"`) when the Q/A fixture
> or corpus is *absent*, so enrichment-only gate runs remain legal; only a fixture
> that is **present but under-scoreable** aborts the run. This deliberately
> weakens the "missing fixture is fatal" wording above in exchange for supporting
> enrichment-only runs — see the `run_all_preflight` docstring. Separately, the
> `min_scoreable_qa` gate is threaded end-to-end through `orchestrate_run` (param)
> and the `run` CLI subcommand (`--min-scoreable-qa`, default 1), with `bench:run`
> wiring it from `BENCH_MIN_SCOREABLE` — the same env var `bench:qa:validate` uses.
> Without that plumbing the run path silently used the default of 1 and could
> publish metrics over a single Q/A item. (Added during code review, 2026-05-31.)

Update `run_all_preflight` signature:

```python
def run_all_preflight(
    brain_url: str,
    corpus_sqlite: Path,
    manifest: Path,
    inference_url: str,
    skip_prod_pause: bool,
    brain_port: int = 18923,
    min_scoreable_qa: int = 1,
) -> tuple[list[CheckResult], bool]:
```

Inside `run_all_preflight`, after `port_check`:

```python
    qa_check = check_qa_scoreable(corpus_sqlite, min_scoreable=min_scoreable_qa)

    checks = [
        reachable,
        pauseable,
        corpus_check,
        inference_check,
        bench_disk,
        port_check,
        qa_check,
    ]
```

Add `or qa_check.status == "fail"` to the `aborted` expression.

- [ ] **Step 5: Implement CLI subcommands**

In `brain/src/hippo_brain/bench/cli.py`, add imports near the existing imports:

```python
from hippo_brain.bench.qa import export_label_worklist, validate_qa_fixture
```

Add command handlers:

```python
def _cmd_qa_validate(args: argparse.Namespace) -> int:
    report = validate_qa_fixture(
        Path(args.qa_path),
        Path(args.corpus_sqlite),
        min_scoreable=args.min_scoreable,
    )
    print(report.detail)
    if args.json:
        print(json.dumps(report.to_dict(), sort_keys=True))
    return 0 if report.passes else 1


def _cmd_qa_export_worklist(args: argparse.Namespace) -> int:
    count = export_label_worklist(
        Path(args.qa_path),
        Path(args.corpus_sqlite),
        Path(args.out),
    )
    print(f"wrote {count} unscoreable Q/A items to {args.out}")
    return 0
```

In `_build_parser`, after `corpus` commands:

```python
    qa = sub.add_parser("qa", help="Validate and label the bench Q/A fixture")
    qa_sub = qa.add_subparsers(dest="qa_command", required=True)

    qv = qa_sub.add_parser("validate", help="Validate Q/A golden_event_id coverage")
    qv.add_argument("--qa-path", default=str(bench_qa_path()))
    qv.add_argument("--corpus-sqlite", default=str(corpus_sqlite_path("corpus-v2")))
    qv.add_argument("--min-scoreable", type=int, default=1)
    qv.add_argument("--json", action="store_true")
    qv.set_defaults(func=_cmd_qa_validate)

    qw = qa_sub.add_parser("export-worklist", help="Export unlabeled Q/A items for annotation")
    qw.add_argument("--qa-path", default=str(bench_qa_path()))
    qw.add_argument("--corpus-sqlite", default=str(corpus_sqlite_path("corpus-v2")))
    qw.add_argument("--out", required=True)
    qw.set_defaults(func=_cmd_qa_export_worklist)
```

Also import `bench_qa_path` from `paths`.

- [ ] **Step 6: Run focused tests**

```bash
uv run --project brain pytest brain/tests/test_bench_qa.py brain/tests/test_bench_preflight.py brain/tests/test_bench_cli.py -q
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add brain/src/hippo_brain/bench/cli.py brain/src/hippo_brain/bench/preflight.py brain/tests/test_bench_cli.py brain/tests/test_bench_preflight.py
git commit -m "fix(bench): fail preflight on unscoreable QA fixture"
```

---

### Task 3: Rebuild Corpus for Schema 18 and Label Q/A

**Files:**
- Modify generated local artifacts under `~/.local/share/hippo-bench/fixtures/`
- Modify: `docs/capture/bench-runbook.md`
- Modify: `brain/src/hippo_brain/bench/README.md`

- [ ] **Step 1: Rebuild the corpus from the live schema-18 DB**

Run:

```bash
uv run --project brain hippo-bench corpus init \
  --corpus-version corpus-v2 \
  --bump-version corpus-v2 \
  --seed 42 \
  --corpus-days 90 \
  --corpus-buckets 9 \
  --shell-min 50 \
  --claude-min 50 \
  --browser-min 50 \
  --workflow-min 50
```

Expected output includes:

```text
wrote 200 entries
sqlite: /Users/carpenter/.local/share/hippo-bench/fixtures/corpus-v2.sqlite
jsonl:  /Users/carpenter/.local/share/hippo-bench/fixtures/corpus-v2.jsonl
manifest: /Users/carpenter/.local/share/hippo-bench/fixtures/corpus-v2.manifest.json
```

- [ ] **Step 2: Verify schema and hashes**

Run:

```bash
uv run --project brain hippo-bench corpus verify --corpus-version corpus-v2
uv run --project brain python - <<'PY'
import json
from pathlib import Path
from hippo_brain.schema_version import EXPECTED_SCHEMA_VERSION

manifest = json.loads((Path.home() / ".local/share/hippo-bench/fixtures/corpus-v2.manifest.json").read_text())
print(f"manifest_schema={manifest['schema_version']} expected={EXPECTED_SCHEMA_VERSION}")
raise SystemExit(0 if manifest["schema_version"] == EXPECTED_SCHEMA_VERSION == 18 else 1)
PY
```

Expected: `ok` from verify and `manifest_schema=18 expected=18`.

- [ ] **Step 3: Export the Q/A labeling worklist**

Run:

```bash
uv run --project brain hippo-bench qa export-worklist \
  --qa-path ~/.local/share/hippo-bench/fixtures/eval-qa-v1.jsonl \
  --corpus-sqlite ~/.local/share/hippo-bench/fixtures/corpus-v2.sqlite \
  --out /tmp/hippo-bench-qa-worklist.jsonl
```

Expected: it writes every item whose `golden_event_id` is null, placeholder, or absent from the rebuilt corpus.

- [ ] **Step 4: Label every worklist row against the rebuilt corpus**

For each row in `/tmp/hippo-bench-qa-worklist.jsonl`, choose exactly one `candidate_event_ids` value that answers the row’s `question`. Edit `~/.local/share/hippo-bench/fixtures/eval-qa-v1.jsonl` so each object has a real string `golden_event_id`.

Use this command while labeling to inspect candidate corpus payloads:

```bash
uv run --project brain python - <<'PY'
import json
import sys
from pathlib import Path

event_ids = set(sys.argv[1:])
corpus_jsonl = Path.home() / ".local/share/hippo-bench/fixtures/corpus-v2.jsonl"
for raw in corpus_jsonl.read_text(encoding="utf-8").splitlines():
    row = json.loads(raw)
    if row["event_id"] in event_ids:
        print(json.dumps(row, indent=2, sort_keys=True)[:4000])
PY shell-1 claude-2
```

Replace `shell-1 claude-2` with the candidate IDs for the worklist row being labeled.

- [ ] **Step 5: Validate labeled Q/A fixture**

Run:

```bash
uv run --project brain hippo-bench qa validate \
  --qa-path ~/.local/share/hippo-bench/fixtures/eval-qa-v1.jsonl \
  --corpus-sqlite ~/.local/share/hippo-bench/fixtures/corpus-v2.sqlite \
  --min-scoreable 100 \
  --json
```

Expected: exit 0 and JSON with `"scoreable": 100`, `"unscoreable": 0`, `"passes": true`.

- [ ] **Step 6: Run preflight without LM Studio checks isolated**

Run:

```bash
uv run --project brain python - <<'PY'
from hippo_brain.bench.preflight import check_corpus_present, check_qa_scoreable
from hippo_brain.bench.paths import corpus_manifest_path, corpus_sqlite_path

corpus = corpus_sqlite_path("corpus-v2")
manifest = corpus_manifest_path("corpus-v2")
for check in [
    check_corpus_present(corpus, manifest),
    check_qa_scoreable(corpus, min_scoreable=100),
]:
    print(f"{check.name}: {check.status}: {check.detail}")
    if check.status != "pass":
        raise SystemExit(1)
PY
```

Expected:

```text
corpus_present: pass: schema_version=18
qa_scoreable: pass: scoreable Q/A items: 100/100 (minimum 100)
```

- [ ] **Step 7: Update runbook and README**

In `docs/capture/bench-runbook.md`, add this block before the BT-29 loop:

```markdown
### Required pre-BT-29 corpus/Q/A gate

Before running the three BT-29 model passes:

```bash
uv run --project brain hippo-bench corpus verify --corpus-version corpus-v2
uv run --project brain hippo-bench qa validate \
  --qa-path ~/.local/share/hippo-bench/fixtures/eval-qa-v1.jsonl \
  --corpus-sqlite ~/.local/share/hippo-bench/fixtures/corpus-v2.sqlite \
  --min-scoreable 100
```

Both commands must exit 0. If Q/A validation fails, export a worklist:

```bash
uv run --project brain hippo-bench qa export-worklist \
  --qa-path ~/.local/share/hippo-bench/fixtures/eval-qa-v1.jsonl \
  --corpus-sqlite ~/.local/share/hippo-bench/fixtures/corpus-v2.sqlite \
  --out /tmp/hippo-bench-qa-worklist.jsonl
```
```

In `brain/src/hippo_brain/bench/README.md`, replace the unlabeled-Q/A caveat with:

```markdown
6. **Q/A labels are corpus-specific.** After rebuilding `corpus-v2.sqlite`,
   run `hippo-bench qa validate --min-scoreable 100`; benchmark retrieval
   metrics are invalid unless every Q/A item is scoreable against that exact
   corpus hash.
```

- [ ] **Step 8: Commit docs only**

Do not commit files from `~/.local/share`. Commit only repo docs/code:

```bash
git add docs/capture/bench-runbook.md brain/src/hippo_brain/bench/README.md
git commit -m "docs(bench): require schema-current labeled QA before BT-29"
```

---

### Task 4: Wire Embedding Function Through CLI Runs

**Files:**
- Modify: `brain/src/hippo_brain/bench/orchestrate.py`
- Test: `brain/tests/test_bench_orchestrate.py`

- [ ] **Step 1: Write failing test that `orchestrate_run` passes `embedding_fn`**

Append to `brain/tests/test_bench_orchestrate.py`:

```python
def test_orchestrate_passes_real_embedding_fn_to_model_runner(stub_corpus, tmp_path, monkeypatch):
    sqlite, manifest = stub_corpus
    out = tmp_path / "run.jsonl"
    captured = {}

    clean_result = ModelRunResult(
        model="m1",
        attempts=[],
        per_event_vectors=[],
        peak_metrics={},
        wall_clock_sec=1,
        cooldown_timeout=False,
        process_ready_ms=10,
        queue_drain_wall_clock_sec=0,
        downstream_proxy={},
        prod_brain_restarted_during_bench=False,
        timeout_during_drain=False,
        errors=[],
    )

    def fake_call_embedding(*, base_url, model, text, timeout_sec):
        captured["embedding_call"] = {
            "base_url": base_url,
            "model": model,
            "text": text,
            "timeout_sec": timeout_sec,
        }
        return [0.1, 0.2, 0.3]

    def fake_run_one_model(**kwargs):
        captured["embedding_fn"] = kwargs["embedding_fn"]
        return clean_result

    monkeypatch.setattr("hippo_brain.bench.orchestrate.call_embedding", fake_call_embedding)

    with (
        patch("hippo_brain.bench.orchestrate.run_one_model", side_effect=fake_run_one_model),
        patch("hippo_brain.bench.orchestrate.PauseRpcClient") as PauseClient,
    ):
        PauseClient.return_value.probe_health.return_value = None
        orchestrate_run(
            candidate_models=["m1"],
            corpus_sqlite=sqlite,
            manifest_path=manifest,
            out_path=out,
            inference_url="http://localhost:1234/v1",
            embedding_model="embed-test",
            skip_checks=True,
            skip_prod_pause=True,
            dry_run=False,
        )

    assert captured["embedding_fn"]("question text") == [0.1, 0.2, 0.3]
    assert captured["embedding_call"] == {
        "base_url": "http://localhost:1234/v1",
        "model": "embed-test",
        "text": "question text",
        "timeout_sec": 120,
    }
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run --project brain pytest brain/tests/test_bench_orchestrate.py::test_orchestrate_passes_real_embedding_fn_to_model_runner -q
```

Expected: `KeyError: 'embedding_fn'` or assertion failure because `run_one_model` was called without it.

- [ ] **Step 3: Implement embedding function wiring**

In `brain/src/hippo_brain/bench/orchestrate.py`, add import:

```python
from hippo_brain.bench.enrich_call import call_embedding
```

Before the model loop, after `models_with_prod_restart_event`:

```python
        normalized_inference_url = (
            inference_url if inference_url.endswith("/v1") else f"{inference_url.rstrip('/')}/v1"
        )

        def embedding_fn(text: str) -> list[float]:
            return call_embedding(
                base_url=normalized_inference_url,
                model=embedding_model,
                text=text,
                timeout_sec=120,
            )
```

In the `run_one_model` call, replace the inline URL normalization and add `embedding_fn`:

```python
                    inference_url=normalized_inference_url,
                    embedding_fn=embedding_fn,
```

- [ ] **Step 4: Run focused tests**

```bash
uv run --project brain pytest brain/tests/test_bench_orchestrate.py -q
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/bench/orchestrate.py brain/tests/test_bench_orchestrate.py
git commit -m "fix(bench): wire embeddings into CLI downstream proxy"
```

---

### Task 5: Compute Real Gates and Verdicts

**Files:**
- Modify: `brain/src/hippo_brain/bench/summary.py`
- Modify: `brain/src/hippo_brain/bench/orchestrate.py`
- Test: `brain/tests/test_bench_summary.py`
- Test: `brain/tests/test_bench_orchestrate.py`

- [ ] **Step 1: Add summary helper tests**

Append to `brain/tests/test_bench_summary.py`:

```python
from hippo_brain.bench.gates import SelfConsistencyResult
from hippo_brain.bench.summary import self_consistency_gate_values


def test_self_consistency_gate_values_skip_when_no_pairwise_scores():
    mean, minimum = self_consistency_gate_values([])
    assert mean is None
    assert minimum is None


def test_self_consistency_gate_values_return_real_scores():
    mean, minimum = self_consistency_gate_values(
        [
            [[1.0, 0.0], [1.0, 0.0]],
            [[1.0, 0.0], [0.0, 1.0]],
        ]
    )
    assert mean == 0.5
    assert minimum == 0.0
```

- [ ] **Step 2: Add orchestrate test for real gate verdict**

Append to `brain/tests/test_bench_orchestrate.py`:

```python
def test_orchestrate_writes_computed_gates_instead_of_hardcoded_pass(stub_corpus, tmp_path):
    sqlite, manifest = stub_corpus
    out = tmp_path / "run.jsonl"
    bad_attempt = AttemptRecord(
        run_id="run-x",
        model={"id": "m1"},
        event={"event_id": "shell-1", "source": "shell", "content_hash": "h"},
        attempt_idx=0,
        purpose="main",
        timestamps={"total_ms": 100},
        raw_output="not json",
        parsed_output=None,
        gates={
            "schema_valid": False,
            "refusal_detected": False,
            "echo_similarity": 0.1,
            "entity_type_sanity": {},
        },
        system_snapshot={},
    )
    fake_result = ModelRunResult(
        model="m1",
        attempts=[bad_attempt],
        per_event_vectors=[],
        peak_metrics={},
        wall_clock_sec=1,
        cooldown_timeout=False,
        process_ready_ms=10,
        queue_drain_wall_clock_sec=0,
        downstream_proxy={"modes": {"hybrid": {"mrr": 0.4, "hit_at_1": 0.5}}},
        prod_brain_restarted_during_bench=False,
        timeout_during_drain=False,
        errors=[],
    )

    with (
        patch("hippo_brain.bench.orchestrate.run_one_model", return_value=fake_result),
        patch("hippo_brain.bench.orchestrate.PauseRpcClient") as PauseClient,
    ):
        PauseClient.return_value.probe_health.return_value = None
        orchestrate_run(
            candidate_models=["m1"],
            corpus_sqlite=sqlite,
            manifest_path=manifest,
            out_path=out,
            skip_checks=True,
            skip_prod_pause=True,
            dry_run=False,
        )

    records = [json.loads(line) for line in out.read_text().splitlines() if line]
    summary = next(r for r in records if r["record_type"] == "model_summary")
    assert summary["gates"]["schema_validity_rate"] == 0.0
    assert summary["tier0_verdict"]["passed"] is False
    assert "schema_validity_rate" in summary["tier0_verdict"]["failed_gates"]
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
uv run --project brain pytest brain/tests/test_bench_summary.py::test_self_consistency_gate_values_skip_when_no_pairwise_scores brain/tests/test_bench_summary.py::test_self_consistency_gate_values_return_real_scores brain/tests/test_bench_orchestrate.py::test_orchestrate_writes_computed_gates_instead_of_hardcoded_pass -q
```

Expected: missing helper and hard-coded pass assertion failure.

- [ ] **Step 4: Implement self-consistency helper**

In `brain/src/hippo_brain/bench/summary.py`, add import:

```python
from hippo_brain.bench.gates import self_consistency_score
```

Add:

```python
def self_consistency_gate_values(
    per_event_vectors: list[list[list[float]]],
) -> tuple[float | None, float | None]:
    scored_events = [vectors for vectors in per_event_vectors if len(vectors) >= 2]
    if not scored_events:
        return None, None
    score = self_consistency_score(scored_events)
    return score.mean, score.min
```

- [ ] **Step 5: Implement real gate computation in orchestrate**

In `brain/src/hippo_brain/bench/orchestrate.py`, add imports:

```python
from hippo_brain.bench.config import DEFAULT_THRESHOLDS
from hippo_brain.bench.summary import (
    aggregate_model_summary,
    compute_verdict,
    self_consistency_gate_values,
)
```

Before writing `ModelSummaryRecord` for a successful model:

```python
            sc_mean, sc_min = self_consistency_gate_values(result.per_event_vectors)
            gates = aggregate_model_summary(
                result.attempts,
                self_consistency_mean=sc_mean,
                self_consistency_min=sc_min,
            )
            verdict = compute_verdict(gates, DEFAULT_THRESHOLDS)
            if result.errors:
                verdict["passed"] = False
                verdict["failed_gates"].append("model_errors")
                verdict["notes"].append("model lifecycle recorded structured errors")
            if result.timeout_during_drain:
                verdict["passed"] = False
                verdict["failed_gates"].append("queue_drain_timeout")
                verdict["notes"].append("queue did not drain before timeout")
            if result.prod_brain_restarted_during_bench:
                verdict["passed"] = False
                verdict["failed_gates"].append("prod_brain_restart")
                verdict["notes"].append("prod brain restarted during bench window")
```

Then replace:

```python
                    gates={},
                    tier0_verdict={
                        "passed": True,
                        "failed_gates": [],
                        "skipped_gates": [],
                        "notes": [],
                    },
```

with:

```python
                    gates=gates,
                    tier0_verdict=verdict,
```

In the manifest record, set thresholds:

```python
                gate_thresholds=dict(DEFAULT_THRESHOLDS),
```

- [ ] **Step 6: Run focused tests**

```bash
uv run --project brain pytest brain/tests/test_bench_summary.py brain/tests/test_bench_orchestrate.py -q
```

Expected: pass.

- [ ] **Step 7: Commit**

```bash
git add brain/src/hippo_brain/bench/summary.py brain/src/hippo_brain/bench/orchestrate.py brain/tests/test_bench_summary.py brain/tests/test_bench_orchestrate.py
git commit -m "fix(bench): compute model gates from run results"
```

---

### Task 6: End-to-End Verification Gate

**Files:**
- Modify: `brain/src/hippo_brain/bench/README.md`
- Optional local outputs: `/tmp/hippo-bench-smoke.jsonl`

- [ ] **Step 1: Run all bench tests**

```bash
uv run --project brain pytest brain/tests/test_bench*.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run lint**

```bash
uv run --project brain ruff check brain/src/hippo_brain/bench brain/tests/test_bench*.py
```

Expected: `All checks passed!`

- [ ] **Step 3: Verify corpus and Q/A gates**

```bash
uv run --project brain hippo-bench corpus verify --corpus-version corpus-v2
uv run --project brain hippo-bench qa validate \
  --qa-path ~/.local/share/hippo-bench/fixtures/eval-qa-v1.jsonl \
  --corpus-sqlite ~/.local/share/hippo-bench/fixtures/corpus-v2.sqlite \
  --min-scoreable 100
```

Expected: both commands exit 0.

- [ ] **Step 4: Run real preflight**

Start LM Studio before this step. Then run:

```bash
uv run --project brain python - <<'PY'
from hippo_brain.bench.preflight import run_all_preflight
from hippo_brain.bench.paths import corpus_manifest_path, corpus_sqlite_path

checks, aborted = run_all_preflight(
    brain_url="http://127.0.0.1:9175",
    corpus_sqlite=corpus_sqlite_path("corpus-v2"),
    manifest=corpus_manifest_path("corpus-v2"),
    inference_url="http://localhost:1234/v1",
    skip_prod_pause=False,
    min_scoreable_qa=100,
)
for check in checks:
    print(f"{check.name}: {check.status}: {check.detail}")
print(f"aborted={aborted}")
raise SystemExit(1 if aborted else 0)
PY
```

Expected: `aborted=False`; `corpus_present`, `qa_scoreable`, `inference_reachable`, `brain_port_free`, and disk checks pass.

- [ ] **Step 5: Run one real smoke bench**

Use a small already-loaded model first:

```bash
uv run --project brain hippo-bench run \
  --models qwen3.6-35b-a3b-ud-mlx \
  --corpus-version corpus-v2 \
  --out /tmp/hippo-bench-smoke.jsonl
```

Expected: exit 0; output contains one `model_summary` record.

- [ ] **Step 6: Assert smoke output has downstream metrics and real gates**

```bash
uv run --project brain python - <<'PY'
import json
from pathlib import Path

records = [json.loads(line) for line in Path("/tmp/hippo-bench-smoke.jsonl").read_text().splitlines() if line]
summary = next(r for r in records if r["record_type"] == "model_summary")
hybrid = summary["downstream_proxy"]["modes"]["hybrid"]
assert "mrr" in hybrid and "hit_at_1" in hybrid, hybrid
assert summary["gates"], summary
assert isinstance(summary["tier0_verdict"]["passed"], bool), summary["tier0_verdict"]
print(json.dumps({
    "mrr": hybrid["mrr"],
    "hit_at_1": hybrid["hit_at_1"],
    "gates": summary["gates"],
    "verdict": summary["tier0_verdict"],
}, indent=2, sort_keys=True))
PY
```

Expected: printed JSON includes non-empty `gates` and `hybrid.mrr` / `hybrid.hit_at_1`.

- [ ] **Step 7: Update README caveats**

In `brain/src/hippo_brain/bench/README.md`, remove these obsolete caveats:

```markdown
6. **`downstream_proxy` is gated on `embedding_fn`** being constructed. The CLI
   flow currently doesn't construct one, so `downstream_proxy` is `{}` for
   CLI-driven runs. Tracked in [issue #133](https://github.com/stevencarpenter/hippo/issues/133).
7. **`eval-qa-v1.jsonl` golden_event_ids are unlabeled.** BT-29 cannot produce
   real MRR / Hit@1 signal until labeling lands. Also tracked in #133.
```

Replace them with:

```markdown
6. **Retrieval metrics require scoreable Q/A labels.** Run
   `hippo-bench qa validate --min-scoreable 100` before publishing any
   downstream-proxy MRR / Hit@1 number.
7. **Trust still requires BT-29.** A single run can now produce metrics, but
   model-ranking claims require the three-run determinism procedure in
   `docs/capture/bench-runbook.md`.
```

- [ ] **Step 8: Commit verification docs**

```bash
git add brain/src/hippo_brain/bench/README.md
git commit -m "docs(bench): update trust caveats after metric wiring"
```

---

## Final Acceptance Checklist

- [ ] `uv run --project brain pytest brain/tests/test_bench*.py -q` passes.
- [ ] `uv run --project brain ruff check brain/src/hippo_brain/bench brain/tests/test_bench*.py` passes.
- [ ] `hippo-bench corpus verify --corpus-version corpus-v2` exits 0.
- [ ] Corpus manifest reports `schema_version == 18`.
- [ ] `hippo-bench qa validate --min-scoreable 100` exits 0.
- [ ] Full preflight exits with `aborted=False` when LM Studio is running.
- [ ] A real smoke run writes `downstream_proxy.modes.hybrid.mrr`.
- [ ] A real smoke run writes non-empty `model_summary.gates`.
- [ ] A real smoke run no longer writes hard-coded `tier0_verdict.passed=true`; verdict reflects computed gates, structured errors, drain timeout, and prod-brain restart state.
- [ ] No stale pause lock remains at `~/.local/share/hippo-bench/pause.lock`.
- [ ] No listener remains on port `18923` after smoke run teardown.

## Remaining Non-Goals

- This plan does not implement `hippo-bench compare` with paired t confidence intervals.
- This plan does not implement judge-LLM rubric scoring.
- This plan does not expand the Q/A fixture to 150 items; it makes the current 100-item fixture scoreable and gates runs on that.
- This plan does not make cross-machine benchmark numbers comparable.
