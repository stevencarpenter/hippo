#!/usr/bin/env bash
# ralph-loop-bench-v2.sh — Autonomous Ralph loop driver for hippo-bench v2
#
# Usage:
#   cd /path/to/hippo/worktree
#   bash scripts/ralph-loop-bench-v2.sh [--dry-run]
#
# Configuration via env vars:
#   RALPH_MAX_ITERS       Max loop iterations (default: 60)
#   RALPH_MAX_WALL_HOURS  Max total wall clock hours (default: 8)
#   RALPH_FAIL_BUDGET     Consecutive failures on same task before marking blocked (default: 3)
#   RALPH_DRY_RUN         If set to "1", print the prompt but don't invoke claude (default: 0)
#   RALPH_WORKTREE        Worktree root (default: directory of this script's parent)
#
# Requires:
#   - claude CLI on PATH (claude -p mode)
#   - python3 on PATH
#   - .ralph/hippo-bench-v2-state.json initialized (run RB2-03 first, or this script bootstraps it)
#
# Output:
#   .ralph/logs/iter-NNNN.log — per-iteration transcript
#   .ralph/hippo-bench-v2-state.json — updated after each iteration
#
# Exit codes:
#   0 — all tasks completed
#   1 — hard abort (blocked > threshold, max iters, or wall clock exceeded)
#   2 — fatal error (missing prerequisites)

set -euo pipefail

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKTREE="${RALPH_WORKTREE:-$(dirname "$SCRIPT_DIR")}"
MAX_ITERS="${RALPH_MAX_ITERS:-60}"
MAX_WALL_HOURS="${RALPH_MAX_WALL_HOURS:-8}"
FAIL_BUDGET="${RALPH_FAIL_BUDGET:-3}"
DRY_RUN="${RALPH_DRY_RUN:-0}"

STATE_FILE="${WORKTREE}/.ralph/hippo-bench-v2-state.json"
PLAN_FILE="${WORKTREE}/docs/superpowers/plans/2026-04-27-hippo-bench-v2-ralph-plan.md"
LOG_DIR="${WORKTREE}/.ralph/logs"

# ---------------------------------------------------------------------------
# Prerequisites check
# ---------------------------------------------------------------------------

if ! command -v claude &>/dev/null; then
    echo "ERROR: claude not found on PATH. Install claude CLI first." >&2
    exit 2
fi

if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found on PATH." >&2
    exit 2
fi

if [[ ! -f "$PLAN_FILE" ]]; then
    echo "ERROR: plan file not found: $PLAN_FILE" >&2
    exit 2
fi

# ---------------------------------------------------------------------------
# Bootstrap state file if missing (equivalent to running RB2-03 manually)
# ---------------------------------------------------------------------------

export RALPH_WORKTREE_INNER="${WORKTREE}"

if [[ ! -f "$STATE_FILE" ]]; then
    echo "[ralph] State file not found. Bootstrapping .ralph/hippo-bench-v2-state.json ..."
    mkdir -p "${WORKTREE}/.ralph"
    python3 - <<'PYEOF'
import json, pathlib, os
worktree = os.environ.get("RALPH_WORKTREE_INNER")
state_path = pathlib.Path(worktree) / ".ralph" / "hippo-bench-v2-state.json"
state = {
    "schema_version": 1,
    "plan_file": "docs/superpowers/plans/2026-04-27-hippo-bench-v2-ralph-plan.md",
    "tasks": {
        "RB2-01": {"status": "pending", "deps": [], "last_attempt_iso": None, "last_error": None},
        "RB2-02": {"status": "pending", "deps": [], "last_attempt_iso": None, "last_error": None},
        "RB2-03": {"status": "pending", "deps": [], "last_attempt_iso": None, "last_error": None},
        "RB2-04": {"status": "pending", "deps": ["RB2-01", "RB2-02"], "last_attempt_iso": None, "last_error": None},
        "RB2-05": {"status": "pending", "deps": ["RB2-04"], "last_attempt_iso": None, "last_error": None},
        "RB2-06": {"status": "pending", "deps": ["RB2-03"], "last_attempt_iso": None, "last_error": None},
        "RB2-07": {"status": "pending", "deps": ["RB2-03"], "last_attempt_iso": None, "last_error": None},
        "RB2-08": {"status": "pending", "deps": ["RB2-06", "RB2-07"], "last_attempt_iso": None, "last_error": None},
        "RB2-09": {"status": "pending", "deps": ["RB2-01", "RB2-02"], "last_attempt_iso": None, "last_error": None},
        "RB2-10": {"status": "pending", "deps": ["RB2-09"], "last_attempt_iso": None, "last_error": None},
        "RB2-11": {"status": "pending", "deps": ["RB2-07"], "last_attempt_iso": None, "last_error": None},
        "RB2-12": {"status": "pending", "deps": ["RB2-11"], "last_attempt_iso": None, "last_error": None},
        "RB2-13": {"status": "pending", "deps": ["RB2-12"], "last_attempt_iso": None, "last_error": None},
        "RB2-14": {"status": "pending", "deps": ["RB2-09", "RB2-02"], "last_attempt_iso": None, "last_error": None},
        "RB2-15": {"status": "pending", "deps": ["RB2-01"], "last_attempt_iso": None, "last_error": None},
        "RB2-16": {"status": "pending", "deps": ["RB2-09", "RB2-04", "RB2-12"], "last_attempt_iso": None, "last_error": None},
        "RB2-17": {"status": "pending", "deps": ["RB2-04", "RB2-06", "RB2-07"], "last_attempt_iso": None, "last_error": None},
        "RB2-18": {"status": "pending", "deps": ["RB2-16", "RB2-17", "RB2-19"], "last_attempt_iso": None, "last_error": None},
        "RB2-19": {"status": "pending", "deps": ["RB2-06"], "last_attempt_iso": None, "last_error": None},
        "RB2-20": {"status": "pending", "deps": ["RB2-06"], "last_attempt_iso": None, "last_error": None},
        "RB2-21P": {"status": "pending", "deps": ["RB2-18"], "last_attempt_iso": None, "last_error": None},
        "RB2-21": {"status": "pending", "deps": ["RB2-18", "RB2-21P"], "last_attempt_iso": None, "last_error": None},
        "RB2-22": {"status": "pending", "deps": ["RB2-01"], "last_attempt_iso": None, "last_error": None},
        "RB2-23": {"status": "pending", "deps": ["RB2-22"], "last_attempt_iso": None, "last_error": None},
        "RB2-24": {"status": "pending", "deps": ["RB2-22"], "last_attempt_iso": None, "last_error": None},
        "RB2-25": {"status": "pending", "deps": ["RB2-22"], "last_attempt_iso": None, "last_error": None},
        "RB2-26": {"status": "pending", "deps": ["RB2-21"], "last_attempt_iso": None, "last_error": None},
        "RB2-27": {"status": "pending", "deps": ["RB2-21"], "last_attempt_iso": None, "last_error": None},
        "RB2-28": {"status": "pending", "deps": ["RB2-05", "RB2-08", "RB2-10", "RB2-13", "RB2-14", "RB2-15", "RB2-27"], "last_attempt_iso": None, "last_error": None},
    }
}
state_path.write_text(json.dumps(state, indent=2))
print(f"Bootstrapped state file: {state_path}")
PYEOF
fi

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

mkdir -p "$LOG_DIR"
LOOP_START_EPOCH=$(python3 -c "import time; print(int(time.time()))")
LOOP_LOG="${LOG_DIR}/loop-$(date +%Y%m%dT%H%M%S).log"
echo "[ralph] Loop started at $(date -u +%Y-%m-%dT%H:%M:%SZ)" | tee -a "$LOOP_LOG"
echo "[ralph] Worktree: $WORKTREE" | tee -a "$LOOP_LOG"
echo "[ralph] Plan: $PLAN_FILE" | tee -a "$LOOP_LOG"
echo "[ralph] State: $STATE_FILE" | tee -a "$LOOP_LOG"
echo "[ralph] Max iters: $MAX_ITERS | Max wall: ${MAX_WALL_HOURS}h | Fail budget: $FAIL_BUDGET" | tee -a "$LOOP_LOG"

# ---------------------------------------------------------------------------
# Helper: read state and compute next task
# ---------------------------------------------------------------------------

pick_next_task() {
    python3 - "$STATE_FILE" <<'PYEOF'
import json, sys

state_path = sys.argv[1]
with open(state_path) as f:
    state = json.load(f)

tasks = state["tasks"]

# Find completed set
completed = {tid for tid, t in tasks.items() if t["status"] == "completed"}
blocked = [tid for tid, t in tasks.items() if t["status"] == "blocked"]
pending = [
    tid for tid, t in tasks.items()
    if t["status"] == "pending" and all(dep in completed for dep in t.get("deps", []))
]

# Stats for exit conditions
in_progress = [tid for tid, t in tasks.items() if t["status"] == "in_progress"]
total = len(tasks)

# Reset stale in_progress tasks (prior iteration crashed mid-way)
if in_progress:
    # If something is in_progress, it was started but the loop was interrupted.
    # Treat it as pending again so the next iteration retries it.
    for tid in in_progress:
        tasks[tid]["status"] = "pending"
        pending.append(tid)
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)

if not pending:
    if len(completed) == total:
        print("DONE")
    elif len(blocked) > 5:
        print(f"ABORT_BLOCKED:{len(blocked)}")
    else:
        print(f"WAIT")
else:
    # Pick the first pending task (they are listed in rough dependency order)
    task_id = pending[0]
    print(task_id)
PYEOF
}

# ---------------------------------------------------------------------------
# Helper: check blocked count
# ---------------------------------------------------------------------------

blocked_count() {
    python3 -c "
import json, sys
with open('$STATE_FILE') as f:
    state = json.load(f)
print(sum(1 for t in state['tasks'].values() if t['status'] == 'blocked'))
"
}

# ---------------------------------------------------------------------------
# Helper: check all completed
# ---------------------------------------------------------------------------

all_completed() {
    python3 -c "
import json
with open('$STATE_FILE') as f:
    state = json.load(f)
tasks = state['tasks']
if all(t['status'] == 'completed' for t in tasks.values()):
    sys.exit(0)
else:
    sys.exit(1)
" 2>/dev/null && return 0 || return 1
}

# ---------------------------------------------------------------------------
# Helper: wall clock elapsed hours
# ---------------------------------------------------------------------------

elapsed_hours() {
    local now
    now=$(python3 -c "import time; print(int(time.time()))")
    echo "scale=2; ($now - $LOOP_START_EPOCH) / 3600" | bc
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

consecutive_failures=0
last_task_id=""

for iter in $(seq 1 "$MAX_ITERS"); do
    iter_pad=$(printf "%04d" "$iter")
    iter_log="${LOG_DIR}/iter-${iter_pad}.log"

    echo "" | tee -a "$LOOP_LOG"
    echo "[ralph] === Iteration ${iter_pad} at $(date -u +%Y-%m-%dT%H:%M:%SZ) ===" | tee -a "$LOOP_LOG"

    # Wall clock check
    hours=$(elapsed_hours)
    if python3 -c "import sys; sys.exit(0 if float('$hours') < $MAX_WALL_HOURS else 1)"; then
        echo "[ralph] Wall clock OK: ${hours}h elapsed of ${MAX_WALL_HOURS}h max" | tee -a "$LOOP_LOG"
    else
        echo "[ralph] ABORT: wall clock exceeded ${MAX_WALL_HOURS}h (${hours}h elapsed)" | tee -a "$LOOP_LOG"
        exit 1
    fi

    # Blocked count check
    n_blocked=$(blocked_count)
    if [[ "$n_blocked" -gt 5 ]]; then
        echo "[ralph] ABORT: $n_blocked tasks blocked (threshold: 5)" | tee -a "$LOOP_LOG"
        exit 1
    fi

    # Pick next task
    next_task=$(pick_next_task)

    if [[ "$next_task" == "DONE" ]]; then
        echo "[ralph] SUCCESS: all tasks completed after $iter iterations" | tee -a "$LOOP_LOG"
        exit 0
    fi

    if [[ "$next_task" == ABORT_BLOCKED:* ]]; then
        echo "[ralph] ABORT: too many blocked tasks: $next_task" | tee -a "$LOOP_LOG"
        exit 1
    fi

    if [[ "$next_task" == "WAIT" ]]; then
        echo "[ralph] No pending tasks are unblocked; all remaining tasks have unsatisfied deps." | tee -a "$LOOP_LOG"
        echo "[ralph] This indicates a dependency cycle or all remaining tasks are blocked." | tee -a "$LOOP_LOG"
        echo "[ralph] Blocked count: $n_blocked" | tee -a "$LOOP_LOG"
        exit 1
    fi

    echo "[ralph] Next task: $next_task" | tee -a "$LOOP_LOG"

    # Consecutive failure check (same task)
    if [[ "$next_task" == "$last_task_id" ]]; then
        consecutive_failures=$((consecutive_failures + 1))
        if [[ "$consecutive_failures" -ge "$FAIL_BUDGET" ]]; then
            echo "[ralph] ABORT: task $next_task failed $consecutive_failures consecutive times (budget: $FAIL_BUDGET)" | tee -a "$LOOP_LOG"
            # Mark it blocked so the loop can continue to other tasks
            python3 -c "
import json, datetime
with open('$STATE_FILE') as f:
    state = json.load(f)
state['tasks']['$next_task']['status'] = 'blocked'
state['tasks']['$next_task']['last_error'] = 'consecutive failure budget exhausted'
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
"
            consecutive_failures=0
            last_task_id=""
            continue
        fi
    else
        consecutive_failures=0
        last_task_id="$next_task"
    fi

    # Mark task in_progress
    NOW_ISO=$(python3 -c "import datetime; print(datetime.datetime.now(datetime.UTC).isoformat())")
    python3 -c "
import json
with open('$STATE_FILE') as f:
    state = json.load(f)
state['tasks']['$next_task']['status'] = 'in_progress'
state['tasks']['$next_task']['last_attempt_iso'] = '$NOW_ISO'
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
"

    # Build the prompt for this iteration
    # IMPORTANT: We pass the full plan + current state inline so Claude has full context
    # without needing session continuity. Each iteration is hermetic.
    CURRENT_STATE=$(python3 -m json.tool "$STATE_FILE" 2>/dev/null || cat "$STATE_FILE")

    CLAUDE_PROMPT="You are executing one iteration of a Ralph loop for hippo-bench v2.

## Your job this iteration

Task to implement: **${next_task}**

## Files you need to read first

1. The full implementation plan:
   ${PLAN_FILE}

2. The current task state:
   ${STATE_FILE}

3. Read any source files mentioned in the task's 'File(s)' section before making changes.

## Execution rules (follow exactly)

1. Read the plan for task ${next_task}. Follow the 'Work' section precisely.
2. Implement the task. Create or edit files as specified.
3. Run EVERY command listed under 'Verify' for task ${next_task}.
4. If ALL verify commands exit 0: update ${STATE_FILE}, set ${next_task} status to 'completed'.
5. If ANY verify command fails: set ${next_task} status to 'blocked', record the error in 'last_error'.
6. Do NOT implement any other task. One task per iteration.
7. Do NOT skip the verify step. Claude's self-assessment is not a substitute for running the commands.
8. When updating ${STATE_FILE}: preserve all other task entries exactly. Only change the status and last_error for ${next_task}.

## Current state (for your reference)

${CURRENT_STATE}

## Worktree root

${WORKTREE}

All file paths in the plan are relative to this root unless they start with ~ or /.

## Task ${next_task} — begin now

Read the plan, implement the task, run the verify commands, update the state file."

    echo "[ralph] Invoking claude -p for task $next_task ..." | tee -a "$LOOP_LOG"
    echo "[ralph] Iteration log: $iter_log" | tee -a "$LOOP_LOG"

    if [[ "$DRY_RUN" == "1" ]]; then
        echo "[ralph] DRY_RUN=1: would invoke claude -p with the following prompt:" | tee -a "$LOOP_LOG"
        echo "$CLAUDE_PROMPT" | head -20 | tee -a "$LOOP_LOG"
        echo "[ralph] (prompt truncated for dry-run display)" | tee -a "$LOOP_LOG"
        # Simulate success
        python3 -c "
import json
with open('$STATE_FILE') as f:
    state = json.load(f)
state['tasks']['$next_task']['status'] = 'pending'  # reset for real run
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
"
        echo "[ralph] DRY_RUN: continuing to next iter" | tee -a "$LOOP_LOG"
        break
    fi

    # Invoke claude -p
    # --no-session-persistence: each iteration is hermetic; no cross-iteration context bleed
    # --output-format json: parseable exit signal
    # --dangerously-skip-permissions: required for autonomous file editing
    # --add-dir: grant access to the worktree
    # Timeout: 30 min per iteration (tasks are budgeted at <=30 min; 1.5x = 45 min but we cap at 30)
    ITER_START=$(python3 -c "import time; print(int(time.time()))")

    claude_exit=0
    claude_output=""

    claude_output=$(timeout 1800 claude \
        -p \
        --output-format json \
        --no-session-persistence \
        --dangerously-skip-permissions \
        --add-dir "${WORKTREE}" \
        "${CLAUDE_PROMPT}" 2>&1) || claude_exit=$?

    ITER_END=$(python3 -c "import time; print(int(time.time()))")
    ITER_ELAPSED=$((ITER_END - ITER_START))

    # Log the full output
    {
        echo "=== Iteration ${iter_pad} — Task ${next_task} ==="
        echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "Elapsed: ${ITER_ELAPSED}s"
        echo "Claude exit: ${claude_exit}"
        echo "--- Output ---"
        echo "$claude_output"
    } > "$iter_log"

    echo "[ralph] Claude exit: $claude_exit, elapsed: ${ITER_ELAPSED}s" | tee -a "$LOOP_LOG"

    # Parse the JSON output to check is_error
    is_error=$(echo "$claude_output" | python3 -c "
import json, sys
try:
    data = json.load(sys.stdin)
    print(str(data.get('is_error', False)).lower())
except Exception:
    print('true')
" 2>/dev/null || echo "true")

    if [[ "$claude_exit" -ne 0 || "$is_error" == "true" ]]; then
        echo "[ralph] Claude reported error for task $next_task" | tee -a "$LOOP_LOG"
        # Don't mark blocked yet — check the state file. Claude may have updated it.
        # If the state was NOT updated to 'completed', it remains 'in_progress';
        # pick_next_task will reset it to pending on the next iteration.
        echo "[ralph] Task $next_task will be retried next iteration." | tee -a "$LOOP_LOG"
    else
        echo "[ralph] Claude completed iteration. Reading state update..." | tee -a "$LOOP_LOG"
    fi

    # Check current state of the task
    task_status=$(python3 -c "
import json
with open('$STATE_FILE') as f:
    state = json.load(f)
print(state['tasks'].get('$next_task', {}).get('status', 'unknown'))
" 2>/dev/null || echo "unknown")

    echo "[ralph] Task $next_task status after iteration: $task_status" | tee -a "$LOOP_LOG"

    if [[ "$task_status" == "completed" ]]; then
        echo "[ralph] Task $next_task COMPLETED." | tee -a "$LOOP_LOG"
        consecutive_failures=0
        last_task_id=""
    elif [[ "$task_status" == "blocked" ]]; then
        echo "[ralph] Task $next_task BLOCKED." | tee -a "$LOOP_LOG"
        consecutive_failures=0
        last_task_id=""
    else
        # Still in_progress or unknown — reset to pending for retry
        echo "[ralph] Task $next_task not resolved (status: $task_status), resetting to pending." | tee -a "$LOOP_LOG"
        python3 -c "
import json
with open('$STATE_FILE') as f:
    state = json.load(f)
if state['tasks'].get('$next_task', {}).get('status') == 'in_progress':
    state['tasks']['$next_task']['status'] = 'pending'
with open('$STATE_FILE', 'w') as f:
    json.dump(state, f, indent=2)
"
    fi

done

# ---------------------------------------------------------------------------
# End of loop — check final state
# ---------------------------------------------------------------------------

echo "" | tee -a "$LOOP_LOG"
echo "[ralph] Loop finished after $MAX_ITERS iterations." | tee -a "$LOOP_LOG"

if python3 -c "
import json, sys
with open('$STATE_FILE') as f:
    state = json.load(f)
tasks = state['tasks']
completed = sum(1 for t in tasks.values() if t['status'] == 'completed')
total = len(tasks)
blocked = sum(1 for t in tasks.values() if t['status'] == 'blocked')
pending = sum(1 for t in tasks.values() if t['status'] == 'pending')
print(f'completed={completed}/{total} blocked={blocked} pending={pending}')
sys.exit(0 if completed == total else 1)
" 2>&1 | tee -a "$LOOP_LOG"; then
    echo "[ralph] SUCCESS: all tasks completed." | tee -a "$LOOP_LOG"
    exit 0
else
    echo "[ralph] INCOMPLETE: max iterations reached without completing all tasks." | tee -a "$LOOP_LOG"
    exit 1
fi
