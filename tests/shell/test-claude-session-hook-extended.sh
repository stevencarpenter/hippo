#!/usr/bin/env bash
# Extended integration tests for shell/claude-session-hook.sh covering the
# less-trafficked branches. The core regression guards for #48/#54/#55 live in
# tests/shell/test-claude-session-hook.sh; those are the load-bearing ones.
# This file covers the edge cases the matrix at
# docs/capture-reliability/09-test-matrix.md tracks as F-19, F-20, F-21.
#
# Run: bash tests/shell/test-claude-session-hook-extended.sh
#
# Prerequisites: bash, tmux, python3. No daemon / real hippo binary required;
# we stub them on PATH so the hook's shape is exercised without side-effects.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOK_SCRIPT="$REPO_ROOT/shell/claude-session-hook.sh"

if [ ! -f "$HOOK_SCRIPT" ]; then
    echo "FAIL: hook script not found at $HOOK_SCRIPT" >&2
    exit 1
fi

if ! command -v tmux >/dev/null; then
    echo "SKIP: tmux not installed (brew install tmux)" >&2
    exit 0
fi

PASS=0
FAIL=0

assert() {
    local desc="$1"
    local cond="$2"
    if eval "$cond"; then
        echo "  [PASS] $desc"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $desc" >&2
        echo "         condition: $cond" >&2
        FAIL=$((FAIL + 1))
    fi
}

# Per-test setup: fresh tempdir, isolated tmux socket, stubs on PATH.
new_test_env() {
    TMP_DIR="$(mktemp -d -t hippo-hook-ext.XXXXXX)"
    TMUX_SOCKET="$TMP_DIR/tmux.sock"
    TMUX_CMD_BIN="tmux -S $TMUX_SOCKET"
    STUB_BIN="$TMP_DIR/bin"
    HOOK_LOG_DIR="$TMP_DIR/log"
    mkdir -p "$STUB_BIN" "$HOOK_LOG_DIR"

    # hippo stub — any arg-sequence succeeds. Used for both the `ingest`
    # subcommand in the TMUX_CMD (which runs inside a detached tmux window)
    # and the batch-fallback spawn (which runs in the background).
    cat >"$STUB_BIN/hippo" <<'STUB'
#!/usr/bin/env bash
# Record the invocation so tests can assert it.
echo "hippo-stub-invoked $*" >>"${HIPPO_STUB_LOG:-/dev/null}"
exec sleep 2
STUB
    chmod +x "$STUB_BIN/hippo"

    # tmux stub that proxies to the real tmux on our isolated socket. This
    # lets the hook call `tmux ...` unqualified and hit our test socket.
    local real_tmux
    real_tmux="$(command -v tmux)"
    cat >"$STUB_BIN/tmux" <<STUB
#!/usr/bin/env bash
exec "$real_tmux" -S "$TMUX_SOCKET" "\$@"
STUB
    chmod +x "$STUB_BIN/tmux"
}

cleanup() {
    # Kill whatever isolated tmux server this test created.
    [ -n "${TMUX_SOCKET:-}" ] && $TMUX_CMD_BIN kill-server 2>/dev/null || true
    [ -n "${TMP_DIR:-}" ] && rm -rf "$TMP_DIR"
}
trap cleanup EXIT

run_hook() {
    # Run the hook script with a transcript path and stubs on PATH.
    local transcript="$1"
    local hook_cwd="$2"
    local session_id="$3"
    local tmux_pane="${4:-}"

    # Ensure the transcript file exists so downstream polling succeeds.
    : > "$transcript"

    local hook_input
    hook_input="{\"transcript_path\":\"$transcript\",\"cwd\":\"$hook_cwd\",\"session_id\":\"$session_id\"}"

    # Pre-create the stub log so the background hippo subprocess always has
    # a path to append to, even if the subshell races against cleanup.
    : >"$TMP_DIR/hippo-stub.log"

    PATH="$STUB_BIN:$PATH" \
        XDG_DATA_HOME="$HOOK_LOG_DIR" \
        TMUX_PANE="$tmux_pane" \
        HIPPO_STUB_LOG="$TMP_DIR/hippo-stub.log" \
        bash "$HOOK_SCRIPT" <<<"$hook_input"
}

# ============================================================================
# Test F-19: session / project name with spaces and punctuation.
#
# Scenario: user opens Claude inside a directory with spaces in its name
# (e.g., "~/Documents/My Projects/acme"). The hook must propagate the project
# name into the tmux window name without broken quoting.
# ============================================================================
echo "Test F-19: cwd with spaces and punctuation produces a valid tmux window"
new_test_env

$TMUX_CMD_BIN new-session -d -s "spaced" -n "filler" "sleep 30"
$TMUX_CMD_BIN set-option -t "spaced" base-index 1 >/dev/null
pane_id="$($TMUX_CMD_BIN display-message -t "spaced:1" -p '#{pane_id}')"

TRANSCRIPT="$TMP_DIR/aaaaaa-bbbb-cccc.jsonl"
SPACED_CWD="/tmp/My Project Dir"
mkdir -p "$SPACED_CWD"
run_hook "$TRANSCRIPT" "$SPACED_CWD" "aaaaaa-bbbb" "$pane_id" >/dev/null 2>&1 || true
sleep 0.3

windows="$($TMUX_CMD_BIN list-windows -t "spaced" -F '#{window_name}')"
# shellcheck disable=SC2034
windows_for_assert="$windows"
assert "window name contains the spaced project base name" \
    "echo \"\$windows_for_assert\" | grep -qF 'My Project Dir'"

# Debug log confirms the spawn branch ran to completion (no premature exit on
# unquoted $HOOK_CWD).
DEBUG_LOG="$HOOK_LOG_DIR/hippo/session-hook-debug.log"
# shellcheck disable=SC2034
debug_log_for_assert="$DEBUG_LOG"
assert "hook logged the spawn for session=spaced" \
    "grep -qF 'spawned tmux window in session=spaced' \"\$debug_log_for_assert\""

cleanup

# ============================================================================
# Test F-20: no tmux server at all → batch fallback path.
#
# Scenario: user runs Claude Code outside of tmux and no tmux server has
# ever been started on their machine. The hook must invoke the hippo binary
# in the background with `ingest claude-session --batch` rather than spawn a
# window. Reference: hook lines 106-110.
# ============================================================================
echo
echo "Test F-20: no tmux server → batch-import fallback"
new_test_env
# Override the tmux stub so 'tmux list-sessions' returns failure (no server).
cat >"$STUB_BIN/tmux" <<'STUB'
#!/usr/bin/env bash
# Pretend every tmux call fails with "no server running".
echo "no server running on $PWD" >&2
exit 1
STUB
chmod +x "$STUB_BIN/tmux"

TRANSCRIPT="$TMP_DIR/ffffff-1111-2222.jsonl"
run_hook "$TRANSCRIPT" "/tmp/proj" "ffffff-1111" "" >/dev/null 2>&1 || true

# The hook detects "no tmux server" from the `tmux list-sessions` failure
# and takes the batch-import branch. The hippo binary invocation happens in
# a detached subshell whose output is redirected to /dev/null — and the
# hook prefers `target/{release,debug}/hippo` on disk over PATH, so a stub
# on PATH is NOT the right signal here. Instead, assert the branch was
# taken via the debug log; the binary-invocation shape is exercised by
# the separate claude_session.rs integration test in hippo-daemon/tests.
DEBUG_LOG="$HOOK_LOG_DIR/hippo/session-hook-debug.log"
# shellcheck disable=SC2034
debug_log_for_assert="$DEBUG_LOG"
assert "hook logged the 'no tmux server, batch-import' branch" \
    "grep -q 'no tmux server, batch-import' \"\$debug_log_for_assert\""

assert "hook did NOT spawn a tmux window (tmux server unavailable)" \
    "! grep -q 'spawned tmux window' \"\$debug_log_for_assert\""

cleanup

# ============================================================================
# Test F-21: TMUX_PANE unset but tmux server up → reuse/create 'hippo' session.
#
# Scenario: user attaches to a tmux session named something other than
# 'hippo', then starts Claude. The hook can't derive the session from
# TMUX_PANE (e.g., the env var didn't propagate through a wrapper), so it
# falls through to the "tmux server reachable but not inside tmux" branch
# and either reuses or creates a session named 'hippo'. Reference: hook
# lines 96-105.
# ============================================================================
echo
echo "Test F-21: TMUX_PANE unset + tmux server reachable → hippo-session fallback"
new_test_env

# Start an unrelated tmux session so `tmux list-sessions` succeeds.
$TMUX_CMD_BIN new-session -d -s "other" -n "filler" "sleep 30"

TRANSCRIPT="$TMP_DIR/222222-3333-4444.jsonl"
run_hook "$TRANSCRIPT" "/tmp/fallbackproj" "222222-3333" "" >/dev/null 2>&1 || true
sleep 0.3

# Either 'hippo' session was created or (if it already existed) reused.
sessions="$($TMUX_CMD_BIN list-sessions -F '#{session_name}')"
# shellcheck disable=SC2034
sessions_for_assert="$sessions"
assert "fallback created 'hippo' tmux session when TMUX_PANE was unset" \
    "echo \"\$sessions_for_assert\" | grep -qx hippo"

hippo_windows="$($TMUX_CMD_BIN list-windows -t "hippo" -F '#{window_name}')"
# shellcheck disable=SC2034
hippo_windows_for_assert="$hippo_windows"
assert "hippo session has a window named for the project" \
    "echo \"\$hippo_windows_for_assert\" | grep -qF 'fallbackproj'"

DEBUG_LOG="$HOOK_LOG_DIR/hippo/session-hook-debug.log"
# shellcheck disable=SC2034
debug_log_for_assert="$DEBUG_LOG"
assert "hook logged the fallback branch" \
    "grep -qE 'fallback tmux (window in existing session=hippo|session=hippo)' \"\$debug_log_for_assert\""

cleanup

# ============================================================================
echo
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -ne 0 ]; then
    exit 1
fi
