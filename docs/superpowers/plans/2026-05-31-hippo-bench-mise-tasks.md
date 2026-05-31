# hippo-bench mise task suite — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `bench:*` mise task suite that wraps the existing `hippo-bench` CLI, automates the easy-to-forget cold-start chain, and routes each run's transcript into the separate bench data tree.

**Architecture:** Pure `mise.toml` + bench-docs change. No Python/Rust source changes. The tasks rely on the bench's already-enforced isolation (separate `~/.local/share/hippo-bench/` root, re-rooted shadow stack, BT-10/I-4 sandbox assertion, prod-brain pause/resume) — they add ergonomics and a teed transcript, never re-implement isolation. Spec: `docs/superpowers/specs/2026-05-31-hippo-bench-mise-tasks-design.md`.

**Tech Stack:** mise (TOML task runner), bash (here-doc tasks, `#!/usr/bin/env bash` + `set -euo pipefail`), `uv run --project brain hippo-bench …`, Python 3 (stdlib `json`) for inline JSONL parsing.

---

## File Structure

- **Modify:** `mise.toml` — append one `# ── Bench ──` section with 10 `bench:*` tasks. This is the entire functional change.
- **Modify:** `brain/src/hippo_brain/bench/README.md` — add a "Running via mise" section.
- **Modify:** `docs/capture/bench-runbook.md` — add a one-line pointer to the mise tasks.

All bench tasks append to the **end** of `mise.toml` (after the final `install:skill` task), under a new section header. Task names use the `bench:` colon-namespace, matching the existing `otel:` / `build:` convention. Multi-step tasks use triple-single-quote (`'''`) literal here-doc strings (no TOML escaping); trivial wrappers use a single-line `run =`.

### Shared conventions (every here-doc task repeats these — mise does not share shell state)

```bash
ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/hippo-bench"
FIXTURES="$ROOT/fixtures"
RUNS="$ROOT/runs"
CORPUS="${BENCH_CORPUS_VERSION:-corpus-v2}"
```

Env knobs (all optional): `BENCH_CORPUS_VERSION`=corpus-v2, `BENCH_MIN_SCOREABLE`=1, `BENCH_BASE_URL` (else CLI reads prod config), `BENCH_EMBEDDING_MODEL` (else CLI reads prod config), `BENCH_DB_PATH` (else CLI default prod DB), `BENCH_SEED`=42, `FORCE` (re-init corpus), `BENCH_BT29_CONFIRM` (gate bt29).

---

## Task 1: Safe thin wrappers (`bench:qa:seed`, `bench:recover`, `bench:determinism`, `bench:corpus:verify`)

**Files:**
- Modify: `mise.toml` (append at end of file)

- [ ] **Step 1: Confirm the tasks do not exist yet (red)**

Run: `mise tasks ls 2>/dev/null | grep -c '^bench:' || true`
Expected: `0`

- [ ] **Step 2: Append the Bench section header + four safe wrappers**

Append to the end of `mise.toml`:

```toml
# ── Bench (model benchmark; separate data root, not normal hippo execution) ──

[tasks."bench:qa:seed"]
description = "Seed the Q/A fixture (eval-qa-v1.jsonl) from the committed template"
run = "uv run --project brain python3 brain/src/hippo_brain/bench/qa_seed.py"

[tasks."bench:recover"]
description = "Clear a stale bench pause lockfile left by a SIGKILLed run"
run = "uv run --project brain hippo-bench recover"

[tasks."bench:determinism"]
description = "Compare N bench run JSONL files against the BT-29 determinism budget"
run = "uv run --project brain hippo-bench determinism ${@}"

[tasks."bench:corpus:verify"]
description = "Verify a bench corpus snapshot's content hashes against its manifest"
run = "uv run --project brain hippo-bench corpus verify --corpus-version \"${BENCH_CORPUS_VERSION:-corpus-v2}\""
```

- [ ] **Step 3: Verify mise parses and lists the tasks (green)**

Run: `mise tasks ls | grep '^bench:'`
Expected: four lines — `bench:corpus:verify`, `bench:determinism`, `bench:qa:seed`, `bench:recover` — each with its description.

- [ ] **Step 4: Smoke-test the two safe-to-run wrappers**

Run: `mise run bench:recover`
Expected: exits 0 — either "recovered" or a no-op message (idempotent; safe even if no prod brain).

`bench:qa:seed` overwrites `eval-qa-v1.jsonl` with the committed template, which would clobber any hand-labeled goldens already present on this host. Back it up first if it exists, then seed:

```bash
F=~/.local/share/hippo-bench/fixtures/eval-qa-v1.jsonl
[ -e "$F" ] && cp "$F" "$F.bak.$(date +%s)" && echo "backed up existing fixture"
mise run bench:qa:seed
```
Expected: exits 0, prints a count line, and the fixture exists. (If you had custom labels, restore from the `.bak.*` copy afterward.)

- [ ] **Step 5: Commit**

```bash
git add mise.toml
git commit -m "feat(bench): add safe bench mise wrappers (qa:seed, recover, determinism, corpus:verify)"
```

---

## Task 2: Guarded prereq tasks (`bench:qa:validate`, `bench:corpus:init`)

**Files:**
- Modify: `mise.toml` (append at end)

- [ ] **Step 1: Confirm absent (red)**

Run: `mise tasks ls | grep -cE '^bench:(qa:validate|corpus:init)' || true`
Expected: `0`

- [ ] **Step 2: Append `bench:qa:validate`**

```toml
[tasks."bench:qa:validate"]
description = "Validate Q/A golden coverage against the corpus (BENCH_MIN_SCOREABLE, default 1)"
run = '''
#!/usr/bin/env bash
set -euo pipefail
ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/hippo-bench"
FIXTURES="$ROOT/fixtures"
CORPUS="${BENCH_CORPUS_VERSION:-corpus-v2}"
uv run --project brain hippo-bench qa validate \
  --qa-path "$FIXTURES/eval-qa-v1.jsonl" \
  --corpus-sqlite "$FIXTURES/$CORPUS.sqlite" \
  --min-scoreable "${BENCH_MIN_SCOREABLE:-1}"
'''
```

- [ ] **Step 3: Append `bench:corpus:init` (banner + FORCE guard)**

```toml
[tasks."bench:corpus:init"]
description = "Sample a fresh bench corpus from the LIVE prod DB (read-only); FORCE=1 to overwrite"
run = '''
#!/usr/bin/env bash
set -euo pipefail
CORPUS="${BENCH_CORPUS_VERSION:-corpus-v2}"
DB="${BENCH_DB_PATH:-${XDG_DATA_HOME:-$HOME/.local/share}/hippo/hippo.db}"
echo "── BENCH corpus init ─────────────────────────────────────────"
echo "  Samples the LIVE prod DB read-only: $DB"
echo "  Writes snapshot to ~/.local/share/hippo-bench/fixtures/$CORPUS.*"
echo "  This is a bench operation, not normal hippo execution."
echo "──────────────────────────────────────────────────────────────"
if [ ! -e "$DB" ]; then
    echo "ERROR: source DB not found: $DB" >&2
    exit 1
fi
args=(--corpus-version "$CORPUS" --seed "${BENCH_SEED:-42}" --db-path "$DB")
if [ "${FORCE:-}" = "1" ]; then
    args+=(--bump-version "$CORPUS")
fi
uv run --project brain hippo-bench corpus init "${args[@]}"
'''
```

- [ ] **Step 4: Verify present + parse (green)**

Run: `mise tasks ls | grep -E '^bench:(qa:validate|corpus:init)'`
Expected: both tasks listed.

- [ ] **Step 5: Safe smoke — `corpus:init` wiring without sampling real data**

Run: `BENCH_DB_PATH=/nonexistent.db mise run bench:corpus:init`
Expected: prints the banner, then `ERROR: source DB not found: /nonexistent.db`, exit 1. Proves wiring + banner without touching the real prod DB.

Run: `mise run bench:qa:validate; echo "exit=$?"`
Expected: runs the validator. If the corpus snapshot is absent it exits non-zero with a clear "corpus missing" style message; if present, prints the scoreable count. Either way: no crash, no prod touch.

- [ ] **Step 6: Commit**

```bash
git add mise.toml
git commit -m "feat(bench): add bench:qa:validate and guarded bench:corpus:init"
```

---

## Task 3: Reporting tasks (`bench:summary`, `bench:status`)

**Files:**
- Modify: `mise.toml` (append at end)

- [ ] **Step 1: Confirm absent (red)**

Run: `mise tasks ls | grep -cE '^bench:(summary|status)' || true`
Expected: `0`

- [ ] **Step 2: Append `bench:summary` (newest run if no arg)**

```toml
[tasks."bench:summary"]
description = "Print the gate scorecard for a bench run JSONL (newest run if omitted)"
run = '''
#!/usr/bin/env bash
set -euo pipefail
RUNS="${XDG_DATA_HOME:-$HOME/.local/share}/hippo-bench/runs"
FILE="${1:-}"
if [ -z "$FILE" ]; then
    FILE="$(ls -t "$RUNS"/run-*.jsonl 2>/dev/null | head -1 || true)"
    if [ -z "$FILE" ]; then
        echo "No bench runs found under $RUNS" >&2
        exit 1
    fi
    echo "Newest run: $FILE"
fi
uv run --project brain hippo-bench summary "$FILE"
'''
```

- [ ] **Step 3: Append `bench:status` (readiness doctor)**

```toml
[tasks."bench:status"]
description = "Show bench readiness: fixtures, last run + verdict, stale pause lock, prod-brain state"
run = '''
#!/usr/bin/env bash
set -uo pipefail
ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/hippo-bench"
FIXTURES="$ROOT/fixtures"
RUNS="$ROOT/runs"
CORPUS="${BENCH_CORPUS_VERSION:-corpus-v2}"

mark() { if [ -e "$1" ]; then echo "  [ok] $2"; else echo "  [--] $2 (missing)"; fi; }

echo "Bench root: $ROOT"
if [ -d "$ROOT" ]; then echo "  [ok] root exists"; else echo "  [--] root missing (no runs yet)"; fi

echo "Fixtures:"
mark "$FIXTURES/$CORPUS.sqlite" "corpus snapshot ($CORPUS.sqlite)"
mark "$FIXTURES/$CORPUS.manifest.json" "corpus manifest"
mark "$FIXTURES/eval-qa-v1.jsonl" "Q/A fixture (eval-qa-v1.jsonl)"

echo "Latest run:"
LATEST="$(ls -t "$RUNS"/run-*.jsonl 2>/dev/null | head -1 || true)"
if [ -n "$LATEST" ]; then
    echo "  $LATEST"
    python3 - "$LATEST" <<'PY'
import json, sys
models, reason = {}, None
with open(sys.argv[1]) as fh:
    for line in fh:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        rt = rec.get("record_type") or rec.get("type")
        if rt == "model_summary":
            mid = rec.get("model", {}).get("id", "?")
            models[mid] = "pass" if rec.get("tier0_verdict", {}).get("passed") else "fail"
        elif rt == "run_end":
            reason = rec.get("reason")
for mid, v in models.items():
    print(f"    {mid}: {v}")
if reason:
    print(f"    reason: {reason}")
if not models and not reason:
    print("    (no model summaries — run incomplete?)")
PY
else
    echo "  (none)"
fi

echo "Pause lock:"
if [ -e "$ROOT/pause.lock" ]; then
    echo "  [!!] $ROOT/pause.lock present — prod brain may still be paused. Run: mise run bench:recover"
else
    echo "  [ok] no stale pause lock"
fi

echo "Prod brain:"
PROD_PORT="$(grep -E '^[[:space:]]*port[[:space:]]*=' "${XDG_CONFIG_HOME:-$HOME/.config}/hippo/config.toml" 2>/dev/null | head -1 | grep -oE '[0-9]+' | head -1 || true)"
PROD_PORT="${PROD_PORT:-9175}"
if H="$(curl -sf "http://127.0.0.1:$PROD_PORT/health" 2>/dev/null)"; then
    PAUSED="$(printf '%s' "$H" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("paused","?"))' 2>/dev/null || echo '?')"
    echo "  [ok] reachable on :$PROD_PORT (paused=$PAUSED)"
else
    echo "  [--] not reachable on :$PROD_PORT (not running, or different port)"
fi
'''
```

- [ ] **Step 4: Verify present + parse (green)**

Run: `mise tasks ls | grep -E '^bench:(summary|status)'`
Expected: both listed.

- [ ] **Step 5: Smoke-test both**

Run: `mise run bench:status`
Expected: prints the Bench root, a Fixtures block with `[ok]`/`[--]` marks, a Latest-run line, a Pause-lock line, and a Prod-brain line. Exits 0 regardless of what's present (it is `set -uo pipefail`, no `-e`, so missing pieces report `[--]` rather than aborting).

Run: `mise run bench:summary; echo "exit=$?"`
Expected: if no runs exist, prints "No bench runs found …" and exit 1; if a run JSONL exists, prints the gate scorecard table.

- [ ] **Step 6: Commit**

```bash
git add mise.toml
git commit -m "feat(bench): add bench:summary and bench:status reporting tasks"
```

---

## Task 4: Centerpiece `bench:run <model-id>` (guided prereqs + teed transcript + scorecards)

**Files:**
- Modify: `mise.toml` (append at end)

- [ ] **Step 1: Confirm absent (red)**

Run: `mise tasks ls | grep -c '^bench:run' || true`
Expected: `0`

- [ ] **Step 2: Append `bench:run`**

```toml
[tasks."bench:run"]
description = "Benchmark a model id end-to-end (guided prereqs, teed transcript, gate + retrieval scorecards)"
run = '''
#!/usr/bin/env bash
set -euo pipefail

MODEL="${1:-}"
if [ -z "$MODEL" ]; then
    echo "Usage: mise run bench:run <model-id>   (comma-separate for multiple)" >&2
    exit 1
fi

ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/hippo-bench"
FIXTURES="$ROOT/fixtures"
RUNS="$ROOT/runs"
CORPUS="${BENCH_CORPUS_VERSION:-corpus-v2}"

echo "── BENCH MODE ────────────────────────────────────────────────"
echo "  Data root : $ROOT  (separate from prod ~/.local/share/hippo)"
echo "  Prod brain: will be PAUSED for the run, then resumed"
echo "  This is a benchmark, not normal hippo execution."
echo "──────────────────────────────────────────────────────────────"

# Safe prereqs only (idempotent; never samples the live prod DB).
uv run --project brain hippo-bench recover || true
if [ ! -e "$FIXTURES/eval-qa-v1.jsonl" ]; then
    echo "==> Q/A fixture missing; seeding from template..."
    uv run --project brain python3 brain/src/hippo_brain/bench/qa_seed.py
fi

# Guided guard: corpus init samples prod, so require it explicitly.
if [ ! -e "$FIXTURES/$CORPUS.sqlite" ]; then
    echo "ERROR: corpus snapshot missing: $FIXTURES/$CORPUS.sqlite" >&2
    echo "Create it first (samples your live hippo.db read-only):" >&2
    echo "    mise run bench:corpus:init" >&2
    exit 1
fi

mkdir -p "$RUNS"
STEM="run-$(date -u +%Y%m%dT%H%M%SZ)-$(hostname -s)"
RUN_JSONL="$RUNS/$STEM.jsonl"
RUN_LOG="$RUNS/$STEM.log"

extra=()
[ -n "${BENCH_BASE_URL:-}" ] && extra+=(--base-url "$BENCH_BASE_URL")
[ -n "${BENCH_EMBEDDING_MODEL:-}" ] && extra+=(--embedding-model "$BENCH_EMBEDDING_MODEL")

echo "==> Running bench (transcript -> $RUN_LOG)"
set -o pipefail
uv run --project brain hippo-bench run \
    --models "$MODEL" \
    --corpus-version "$CORPUS" \
    ${extra[@]+"${extra[@]}"} \
    --out "$RUN_JSONL" 2>&1 | tee "$RUN_LOG"
rc=${PIPESTATUS[0]}

echo ""
echo "==> Gate scorecard"
uv run --project brain hippo-bench summary "$RUN_JSONL" || true

echo ""
echo "==> Retrieval scorecard (hybrid mode)"
python3 - "$RUN_JSONL" <<'PY'
import json, sys
def f3(x):
    return f"{x:.3f}" if isinstance(x, (int, float)) else "n/a"
any_row = False
with open(sys.argv[1]) as fh:
    for line in fh:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if (rec.get("record_type") or rec.get("type")) != "model_summary":
            continue
        any_row = True
        mid = rec.get("model", {}).get("id", "?")
        hy = ((rec.get("downstream_proxy") or {}).get("modes") or {}).get("hybrid")
        if not hy:
            print(f"  {mid}: (no retrieval metrics — Q/A fixture absent or unscoreable)")
        else:
            print(f"  {mid}: MRR={f3(hy.get('mrr'))}  Hit@1={f3(hy.get('hit_at_1'))}  "
                  f"Hit@5={f3(hy.get('hit_at_5'))}  (n={hy.get('scored_count')})")
if not any_row:
    print("  (no model summaries in run)")
PY

echo ""
echo "Results   : $RUN_JSONL"
echo "Transcript: $RUN_LOG"
exit "$rc"
'''
```

- [ ] **Step 3: Verify present + parse (green)**

Run: `mise tasks ls | grep '^bench:run'`
Expected: `bench:run` listed.

- [ ] **Step 4: Smoke — usage error path**

Run: `mise run bench:run; echo "exit=$?"`
Expected: prints `Usage: mise run bench:run <model-id> …`, `exit=1`.

- [ ] **Step 5: Smoke — corpus-missing fast-fail (no prod touch, no server needed)**

Run: `BENCH_CORPUS_VERSION=corpus-smoke-absent mise run bench:run dummy-model; echo "exit=$?"`
Expected: prints the BENCH MODE banner, runs `recover` (no-op), seeds the Q/A fixture if absent, then prints `ERROR: corpus snapshot missing: …/corpus-smoke-absent.sqlite` and the `mise run bench:corpus:init` remediation, `exit=1`. It must **not** invoke `hippo-bench run` (no inference call, no prod pause).

- [ ] **Step 6: Commit**

```bash
git add mise.toml
git commit -m "feat(bench): add guided bench:run with teed transcript and dual scorecards"
```

> **Note (full end-to-end):** A real `mise run bench:run <loaded-model-id>` requires the inference server up with the model loaded and a corpus snapshot present; it pauses/resumes the prod brain. That is a manual operator verification, not a plan smoke step.

---

## Task 5: Guarded `bench:bt29 <model-id>`

**Files:**
- Modify: `mise.toml` (append at end)

- [ ] **Step 1: Confirm absent (red)**

Run: `mise tasks ls | grep -c '^bench:bt29' || true`
Expected: `0`

- [ ] **Step 2: Append `bench:bt29`**

```toml
[tasks."bench:bt29"]
description = "BT-29 determinism: 3 runs of a model + budget gate (guarded by BENCH_BT29_CONFIRM=1)"
run = '''
#!/usr/bin/env bash
set -euo pipefail

MODEL="${1:-}"
if [ -z "$MODEL" ]; then
    echo "Usage: BENCH_BT29_CONFIRM=1 mise run bench:bt29 <model-id>" >&2
    exit 1
fi
if [ "${BENCH_BT29_CONFIRM:-}" != "1" ]; then
    echo "Refusing: BT-29 runs the model 3x (~90 min, pauses prod brain each time)." >&2
    echo "Re-run with BENCH_BT29_CONFIRM=1 to proceed." >&2
    exit 1
fi

ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/hippo-bench"
RUNS="$ROOT/runs"
FIXTURES="$ROOT/fixtures"
CORPUS="${BENCH_CORPUS_VERSION:-corpus-v2}"
if [ ! -e "$FIXTURES/$CORPUS.sqlite" ]; then
    echo "ERROR: corpus snapshot missing: $FIXTURES/$CORPUS.sqlite" >&2
    echo "Create it first: mise run bench:corpus:init" >&2
    exit 1
fi

extra=()
[ -n "${BENCH_BASE_URL:-}" ] && extra+=(--base-url "$BENCH_BASE_URL")
[ -n "${BENCH_EMBEDDING_MODEL:-}" ] && extra+=(--embedding-model "$BENCH_EMBEDDING_MODEL")

DIR="$RUNS/bt29-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$DIR"
for i in 1 2 3; do
    echo "==> BT-29 run $i/3 -> $DIR/r$i.jsonl"
    uv run --project brain hippo-bench run \
        --models "$MODEL" \
        --corpus-version "$CORPUS" \
        ${extra[@]+"${extra[@]}"} \
        --out "$DIR/r$i.jsonl"
done

echo "==> Determinism gate"
uv run --project brain hippo-bench determinism "$DIR/r1.jsonl" "$DIR/r2.jsonl" "$DIR/r3.jsonl"
'''
```

- [ ] **Step 3: Verify present + parse (green)**

Run: `mise tasks ls | grep '^bench:bt29'`
Expected: `bench:bt29` listed.

- [ ] **Step 4: Smoke — confirm guard refuses without the env flag**

Run: `mise run bench:bt29 dummy-model; echo "exit=$?"`
Expected: prints "Refusing: BT-29 runs the model 3x …", `exit=1`. Must **not** start any run.

Run: `mise run bench:bt29; echo "exit=$?"`
Expected: prints the usage line, `exit=1`.

- [ ] **Step 5: Commit**

```bash
git add mise.toml
git commit -m "feat(bench): add guarded bench:bt29 determinism flow"
```

---

## Task 6: Documentation (README section + runbook pointer)

**Files:**
- Modify: `brain/src/hippo_brain/bench/README.md`
- Modify: `docs/capture/bench-runbook.md`

- [ ] **Step 1: Read the README to find the insertion point**

Run: `sed -n '1,40p' brain/src/hippo_brain/bench/README.md`
Expected: see the H1 title and opening description. Identify the first `## ` subsection after the intro.

- [ ] **Step 2: Insert a "Running via mise" section**

Add this section immediately **before** the first existing `## ` subsection (i.e., right after the opening description paragraph) in `brain/src/hippo_brain/bench/README.md`:

```markdown
## Running via mise (recommended)

`mise` tasks wrap the CLI and automate the easy-to-forget setup. They write only
to the separate bench data root (`~/.local/share/hippo-bench/`), never to prod.

**Cold start → scored model:**

```bash
mise run bench:corpus:init       # one-time: sample a corpus from the live DB (read-only)
mise run bench:corpus:verify     # optional: re-check the snapshot hashes
mise run bench:qa:validate       # optional: confirm Q/A goldens resolve against the corpus
mise run bench:run <model-id>    # seeds Q/A if needed, runs, tees a transcript, prints scorecards
mise run bench:summary           # re-print the newest run's gate scorecard
```

`bench:run` is guided: it auto-seeds the Q/A fixture and clears any stale pause
lock, but if the corpus snapshot is missing it stops and tells you to run
`bench:corpus:init` (which is the only step that reads your live `hippo.db`).
Each run's full console transcript is teed to
`~/.local/share/hippo-bench/runs/<stem>.log` next to the results JSONL.

**Tasks:**

| Task | Purpose |
|---|---|
| `bench:run <model-id>` | End-to-end benchmark (guided prereqs, teed transcript, gate + retrieval scorecards) |
| `bench:status` | Readiness doctor: fixtures present, last run verdict, stale pause lock, prod-brain state |
| `bench:corpus:init` | Sample a corpus from the live prod DB (read-only); `FORCE=1` to overwrite |
| `bench:corpus:verify` | Verify a corpus snapshot against its manifest |
| `bench:qa:seed` | Seed `eval-qa-v1.jsonl` from the committed template (idempotent) |
| `bench:qa:validate` | Validate Q/A golden coverage (`BENCH_MIN_SCOREABLE`, default 1; use 100 to publish) |
| `bench:summary [file]` | Gate scorecard for a run JSONL (newest if omitted) |
| `bench:determinism <files…>` | BT-29 budget comparison over existing run files |
| `bench:bt29 <model-id>` | 3 runs + determinism gate; requires `BENCH_BT29_CONFIRM=1` (~90 min) |
| `bench:recover` | Clear a stale pause lock from a crashed run |

**Env knobs:** `BENCH_CORPUS_VERSION` (corpus-v2), `BENCH_MIN_SCOREABLE` (1),
`BENCH_BASE_URL` / `BENCH_EMBEDDING_MODEL` (else read from prod config),
`BENCH_DB_PATH` (corpus source DB), `BENCH_SEED` (42), `FORCE`, `BENCH_BT29_CONFIRM`.
```

- [ ] **Step 3: Add a runbook pointer**

In `docs/capture/bench-runbook.md`, immediately after the opening description (before the first `## ` heading), add:

```markdown
> **mise shortcut:** the everyday flow is wrapped in `bench:*` mise tasks —
> `mise run bench:status`, `mise run bench:run <model-id>`, etc. See
> [the bench README](../../brain/src/hippo_brain/bench/README.md#running-via-mise-recommended).
> This runbook remains authoritative for the operator-gated BT-29 procedure.
```

- [ ] **Step 4: Verify the docs render and link correctly**

Run: `grep -n "Running via mise" brain/src/hippo_brain/bench/README.md`
Expected: one match (the new heading).

Run: `grep -n "mise shortcut" docs/capture/bench-runbook.md`
Expected: one match.

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/bench/README.md docs/capture/bench-runbook.md
git commit -m "docs(bench): document the bench:* mise task suite"
```

---

## Task 7: Final integration check

**Files:** none (verification only)

- [ ] **Step 1: All ten bench tasks are registered**

Run: `mise tasks ls | grep '^bench:' | wc -l`
Expected: `10` — `bench:bt29`, `bench:corpus:init`, `bench:corpus:verify`, `bench:determinism`, `bench:qa:seed`, `bench:qa:validate`, `bench:recover`, `bench:run`, `bench:status`, `bench:summary`.

- [ ] **Step 2: mise.toml is well-formed (no parse regressions)**

Run: `mise tasks ls >/dev/null && echo OK`
Expected: `OK` (a parse error would make this non-zero).

- [ ] **Step 3: Existing tasks unaffected — spot check**

Run: `mise tasks ls | grep -E '^(build|test|lint|start|stop)\b' | head`
Expected: the pre-existing tasks still listed with their descriptions.

- [ ] **Step 4: Readiness doctor end-to-end**

Run: `mise run bench:status`
Expected: clean readiness report, exit 0.

- [ ] **Step 5: Confirm clean working tree (all changes committed across Tasks 1–6)**

Run: `git status --porcelain`
Expected: empty (everything committed).

---

## Self-review notes

- **Spec coverage:** §4.1 building blocks → Tasks 1–3; §4.2 `bench:run` → Task 4; §4.3 `bench:bt29` → Task 5; §4.4 `bench:status` → Task 3; §5 conventions/env → embedded in every task + documented in Task 6; §6 docs → Task 6; §7 verification → smoke steps per task + Task 7. No gaps.
- **No prod-DB sampling in any smoke step.** The only corpus-touching verification (Task 2 Step 5) deliberately points `BENCH_DB_PATH` at a nonexistent file to exercise wiring without reading the real DB.
- **Type/field consistency:** JSONL parsing in `bench:status` (Task 3) and `bench:run` (Task 4) both read `record_type`/`type`, `model.id`, `tier0_verdict.passed`, and `downstream_proxy.modes.hybrid.{mrr,hit_at_1,hit_at_5,scored_count}` — consistent across both, matching `determinism.py` / `output.py` record shapes documented in the spec.
- **bash 3.2 safety:** empty-array expansions use the `${extra[@]+"${extra[@]}"}` guard (macOS default bash is 3.2, where `"${arr[@]}"` on an empty array under `set -u` errors). `corpus:init`'s `args` array is always non-empty so it uses the plain form.
