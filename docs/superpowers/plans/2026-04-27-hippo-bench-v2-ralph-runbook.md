# Hippo-Bench v2 — Ralph Loop Runbook

**Companion to:** `2026-04-27-hippo-bench-v2-ralph-plan.md`  
**Runner script:** `scripts/ralph-loop-bench-v2.sh`

---

## Pre-flight Checklist

Complete these checks before starting the loop. Each one matters.

### 1. Working tree is clean and on the right branch

```bash
cd /Users/carpenter/projects/hippo/.claude/worktrees/youthful-kirch-7d3b27
git status          # must show "nothing to commit, working tree clean"
git branch          # must show "* claude/youthful-kirch-7d3b27"
```

The loop will modify files in this worktree. A dirty tree complicates git history and risks overwriting uncommitted work. Stash or commit anything pending before starting.

### 2. Cargo builds clean (Rust baseline)

```bash
cargo build -p hippo-daemon 2>&1 | tail -5
cargo clippy -p hippo-daemon -- -D warnings 2>&1 | tail -5
```

Failures here are pre-existing issues the loop cannot fix. Resolve them first.

### 3. Existing tests pass (Python baseline)

```bash
uv run --project brain pytest brain/tests -q --tb=short 2>&1 | tail -10
```

The loop's terminal task (RB2-28) asserts ALL tests pass. If existing tests already fail, RB2-28 will block immediately. Fix them before running the loop.

### 4. claude CLI works in -p mode

```bash
claude -p "reply with the word pong" --output-format json --no-session-persistence 2>&1 | python3 -c "import json,sys; d=json.load(sys.stdin); print('ok' if not d.get('is_error') else 'ERROR')"
```

Should print `ok`. If it prints `ERROR` or crashes, your authentication is broken.

### 5. Hippo services state (does not need to be running)

The loop is implementing bench code, not running it. LM Studio does NOT need to be open. Hippo daemon and brain do not need to be running.

If they happen to be running: that is fine. The loop does not touch live services.

### 6. Disk space

The loop creates many Python files and a few SQLite fixtures during testing. Ensure at least 2GB free:

```bash
df -h ~/.local/share
```

### 7. Tmux session recommended (but not required)

```bash
tmux new-session -s ralph-bench-v2
# inside the session:
cd /Users/carpenter/projects/hippo/.claude/worktrees/youthful-kirch-7d3b27
```

Running inside tmux means you can detach (`Ctrl+b d`) and reattach later without killing the loop.

---

## Recommended Invocation

### Standard (tmux-attached)

```bash
cd /Users/carpenter/projects/hippo/.claude/worktrees/youthful-kirch-7d3b27

RALPH_MAX_ITERS=60 \
RALPH_MAX_WALL_HOURS=8 \
RALPH_FAIL_BUDGET=3 \
bash scripts/ralph-loop-bench-v2.sh 2>&1 | tee .ralph/logs/ralph-$(date +%Y%m%dT%H%M%S).log
```

`tee` is essential: the log file survives if the terminal closes.

### Detached (long-running, unattended)

```bash
# Inside tmux:
cd /Users/carpenter/projects/hippo/.claude/worktrees/youthful-kirch-7d3b27

RALPH_MAX_ITERS=60 \
RALPH_MAX_WALL_HOURS=8 \
RALPH_FAIL_BUDGET=3 \
bash scripts/ralph-loop-bench-v2.sh > .ralph/logs/ralph-$(date +%Y%m%dT%H%M%S).log 2>&1 &

echo "PID: $!"
# Detach with Ctrl+b d
```

### Dry-run (validate prompts without spending tokens)

```bash
RALPH_DRY_RUN=1 bash scripts/ralph-loop-bench-v2.sh
```

Prints the first prompt but does not invoke claude. Useful to verify the script's state-machine logic.

---

## Claude Max Plan Quota Notes

**This is the most important operational caveat.**

Claude Max plan has a **5-hour rolling message limit** (approximately; exact mechanics are unpublished). Each `claude -p` invocation is independent. In practice:

- Each iteration costs ~$0.15-$0.40 depending on context size and code written (based on the test run above showing $0.20 for a trivial prompt with 45k cached tokens)
- 28 tasks at an average of $0.30/iter = **roughly $8-12 total** for the full plan
- With retries and state reads: estimate $15-20 total
- Claude Max plan includes generous usage; this is unlikely to hit limits if run over 4-8 hours

**Rate limit mitigation:**

- The loop is serial (one iteration at a time). No parallel API calls.
- Each iteration is a fresh session (`--no-session-persistence`). No session state accumulates.
- If you hit a rate limit mid-loop, the script will fail with a non-zero exit from `claude`. The task will remain `in_progress`, be reset to `pending` on the next iteration, and be retried. Simply re-run the script — it will resume from the correct state.

**Token bloat risk:**

Each iteration re-reads the full plan file (~15k tokens) plus the current state JSON. This is intentional (hermeticity) but means per-iteration token counts are higher than if we maintained session context. The trade-off is worth it: a session-continuity approach risks compounding context corruption that is hard to debug.

---

## How to Monitor Mid-Loop

### Live tail of the loop log

```bash
tail -f .ralph/logs/ralph-*.log
```

### Per-iteration logs

```bash
ls -ltr .ralph/logs/iter-*.log   # one per completed iteration
tail -50 .ralph/logs/iter-0015.log  # inspect a specific iteration
```

### Current task state

```bash
python3 -c "
import json
with open('.ralph/hippo-bench-v2-state.json') as f:
    state = json.load(f)
tasks = state['tasks']
by_status = {}
for tid, t in tasks.items():
    s = t['status']
    by_status.setdefault(s, []).append(tid)
for status, ids in sorted(by_status.items()):
    print(f'{status}: {ids}')
"
```

### Healthy iteration signs

- Each iteration log shows "Claude exit: 0"
- The task status changes from `pending` → `completed`
- `brain/tests` and `cargo test` invocations in the iter log show "passed"
- `.ralph/logs/iter-NNNN.log` size is 10-100KB (larger = Claude wrote more code; that is expected)

### Thrashing signs (require intervention)

- Same task ID appears as "Next task" 3+ iterations in a row with `status != completed`
- `consecutive_failures` counter reaching FAIL_BUDGET → task gets marked `blocked`
- `is_error: true` in the claude JSON output (check iter log)
- Short iterations (<30s) that don't change any files (Claude is confused and answering without acting)

---

## How to Interrupt Safely

### Clean interruption (Ctrl+C or kill)

The script handles `set -euo pipefail` — a Ctrl+C during a `claude -p` invocation will kill that invocation and exit the bash script. The state file will have the task in `in_progress` state.

On the next run, `pick_next_task()` detects `in_progress` tasks and resets them to `pending`. **No manual state repair needed** — just re-run the script.

### Force-stop a runaway claude process

```bash
pkill -f "claude -p"
```

Then re-run the script. State is safe.

### Manual state repair (if needed)

If the state file is in an unexpected state after an abnormal stop:

```bash
python3 -c "
import json
with open('.ralph/hippo-bench-v2-state.json') as f:
    state = json.load(f)
# Reset all in_progress tasks to pending
for tid, t in state['tasks'].items():
    if t['status'] == 'in_progress':
        t['status'] = 'pending'
        print(f'Reset {tid} to pending')
with open('.ralph/hippo-bench-v2-state.json', 'w') as f:
    json.dump(state, f, indent=2)
"
```

### Manually mark a task completed (after verifying it yourself)

```bash
python3 -c "
import json
with open('.ralph/hippo-bench-v2-state.json') as f:
    state = json.load(f)
state['tasks']['RB2-01']['status'] = 'completed'
with open('.ralph/hippo-bench-v2-state.json', 'w') as f:
    json.dump(state, f, indent=2)
print('Done')
"
```

Only do this after running the task's verify commands yourself and confirming they pass.

### Manually mark a task blocked (to skip it)

```bash
python3 -c "
import json
with open('.ralph/hippo-bench-v2-state.json') as f:
    state = json.load(f)
state['tasks']['RB2-XX']['status'] = 'blocked'
state['tasks']['RB2-XX']['last_error'] = 'manually skipped'
with open('.ralph/hippo-bench-v2-state.json', 'w') as f:
    json.dump(state, f, indent=2)
"
```

This allows dependent tasks to proceed if they have no other blockers. Use with caution.

---

## Resuming After Interruption

Simply re-run the script from the same worktree directory:

```bash
cd /Users/carpenter/projects/hippo/.claude/worktrees/youthful-kirch-7d3b27
bash scripts/ralph-loop-bench-v2.sh 2>&1 | tee -a .ralph/logs/resumed-$(date +%Y%m%dT%H%M%S).log
```

The script reads the state file and picks up exactly where it left off. It will not re-attempt completed tasks.

---

## Best Practices for Autonomous Claude Code Loops on Claude Max

### Do

- **Run inside tmux.** A disconnected terminal kills the loop. Tmux protects against that.
- **Set conservative fail budgets.** `RALPH_FAIL_BUDGET=3` is the right starting point. Lower than 3 causes excessive blocking on transient failures; higher than 3 lets thrashing run undetected too long.
- **Verify via commands, not Claude's text.** The plan's verify commands exit 0 iff the work is actually correct. Claude saying "I implemented this successfully" is not a substitute. The loop already enforces this — every task's verify commands must run.
- **Keep tasks <= 30 min.** Longer tasks increase per-iteration token cost AND correlation between task success and context-window state. Tasks exceeding 30 min in the plan are subdivided; don't combine them back.
- **Checkpoint after each verify.** The state file IS the checkpoint. Losing the state file means losing all progress — back it up occasionally: `cp .ralph/hippo-bench-v2-state.json .ralph/hippo-bench-v2-state.json.bak`
- **Watch the first 3-5 iterations live.** This is where you catch systematic prompt failures (Claude consistently misunderstanding the task format) before spending tokens on a doomed run.

### Do not

- **Do not use `--continue` or session resumption.** Session context accumulates hallucinations and stale reasoning. Fresh per-iteration sessions are more reliable, at the cost of re-reading the plan each time.
- **Do not run two loops in the same worktree simultaneously.** They will race on the state file. Use separate worktrees for parallel experiments.
- **Do not set `RALPH_MAX_ITERS` above 100.** Beyond 100 iterations, the task plan should be redesigned, not extended.
- **Do not trust Claude's self-assessment.** If Claude writes "PASS: all tests pass" in its response but does not run the verify commands, the task is NOT completed. The verify commands are the ground truth.
- **Do not use `--dangerously-skip-permissions` in production worktrees.** The runner uses it because the loop needs to write files autonomously. The worktree is isolated from prod; this is acceptable.
- **Do not leave tasks in `in_progress` permanently.** If the state file shows `in_progress` and the loop is not running, reset to `pending`.

### Anti-patterns to watch for

**Infinite-narrowing tasks:** Claude breaks a task into sub-tasks, each smaller than the last, never actually writing production code. Symptom: many iterations with tiny file changes. Mitigation: each task's Work section specifies concrete deliverables; the loop's prompt enforces "one task per iteration."

**Undetectable thrash:** Claude writes code that seems correct but the verify command is subtly wrong (e.g., a test that always passes because it never actually runs). Mitigation: review the verify commands in the plan before starting the loop; they are specific and non-trivially satisfied.

**Context window saturation:** After many iterations of `--no-session-persistence`, each invocation starts fresh. This is by design. Saturation cannot occur. However, very long single tasks (large files) can hit output token limits. Symptom: truncated files. Mitigation: tasks are kept <= 30 min.

**Runaway costs with Claude API key (not Max):** If running with an API key instead of Claude Max, costs accumulate per token. Monitor with `--max-budget-usd` if concerned: add to the claude invocation line in the script. The current script does not set a per-iteration budget cap because Claude Max plan billing works differently. If you switch to API key auth, add `--max-budget-usd 2.00` to each `claude -p` invocation.

---

## Known Caveats of the claude -p Flow

### Per-iteration token cost scales with plan size

The plan file is ~15k tokens. The state JSON is ~3-5k tokens. Every iteration reads both. At 28 tasks with some retries, total context-read tokens are roughly 28 × 20k = 560k tokens input-side. With cached tokens (the plan is re-read identically each time), the actual API cost is much lower — the `cache_read_input_tokens` field in the JSON output shows the discount.

### No cross-iteration memory

Each `claude -p` invocation has no memory of prior iterations. This means:
- Claude cannot learn from mistakes made in iteration N when working on iteration N+1
- The plan's Work sections must be self-contained enough that Claude can understand them in isolation

This is intentional. Cross-iteration memory creates subtle state corruption bugs that are nearly impossible to debug.

### Verify commands run inside claude's tool environment

When claude executes bash commands via its Bash tool, they run in the worktree directory. Paths are absolute. However, `PATH` inside the tool environment may differ from your shell's PATH (e.g., `uv` might not be on PATH if your shell profile adds it specially). If you see "command not found: uv" errors in iter logs, prefix commands with the full path or add it to the claude invocation's environment.

### Task ordering is not perfectly parallelizable

The plan has tasks that could theoretically run in parallel (e.g., RB2-14 and RB2-22 have no shared deps). The serial loop runs them sequentially. This adds wall clock time but eliminates race conditions on shared files. The trade-off is worth it for a 28-task plan running over 8 hours.

### The loop does not verify that Claude updates the state file

The runner checks the state file after each iteration. If Claude forgets to update the state file (writing the code but not marking the task `completed`), the task will be retried next iteration. This is a self-healing property, not a bug. Claude should update the state file as the last action of each iteration — the prompt makes this explicit.

### If claude -p exits non-zero mid-iteration

The task remains `in_progress` (until the next iteration's `pick_next_task()` resets it to `pending`). The iteration log captures what happened. Check `.ralph/logs/iter-NNNN.log` for the error. Common causes:
- API timeout (transient; just retry)
- Permission denial from a Bash command (unlikely with `--dangerously-skip-permissions`)
- Model overload / 529 error (transient; retry after a short wait)

The script does not add sleep between iterations. If you observe systematic 529 errors, add `sleep 30` after the claude invocation in the script.

---

## Completion Criteria

The loop exits 0 when `pick_next_task()` returns "DONE" — meaning all 28 tasks are in `completed` state. At that point:

1. Run the final acceptance check manually:
   ```bash
   uv run --project brain pytest brain/tests -q 2>&1 | tail -5
   cargo test -p hippo-daemon --features otel 2>&1 | grep "test result"
   uv run --project brain hippo-bench run --dry-run --models qwen3.5-35b-a3b --corpus-version corpus-v2 --skip-prod-pause --out /tmp/final-smoke.jsonl && echo "smoke test passed"
   ```

2. Review the git diff to confirm only expected files were modified:
   ```bash
   git diff --name-only
   git diff --stat
   ```

3. Commit via the standard workflow (the user reviews + commits; the loop does not commit).
