#!/usr/bin/env bash
# Regression guard for capture-reliability F-5 (issue #50).
#
# The hook at shell/claude-session-hook.sh assumes it runs as a DIRECT CHILD
# of Claude Code, so `$PPID` IS the Claude process PID. This test documents
# and enforces that assumption — a wrapper process (e.g., the `claade`
# personal wrapper that wraps `claude`) would break the chain by inserting
# itself between Claude and the hook.
#
# Scope: this test exercises the hook's own PID logic in isolation. It does
# NOT simulate a wrapper process reorganising the real-world PID chain —
# that requires solving #50, which is an open investigation. When #50 lands
# a fix (either "walk the chain" or "accept the wrapper PID and let the
# tailer poll for the transcript"), update this test to the new contract
# and remove the "DOCUMENTS CURRENT BEHAVIOR" banner below.
#
# Run: bash tests/shell/test-hook-pid-ppid.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOK_SCRIPT="$REPO_ROOT/shell/claude-session-hook.sh"

if [ ! -f "$HOOK_SCRIPT" ]; then
    echo "FAIL: hook script not found at $HOOK_SCRIPT" >&2
    exit 1
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

TMP_DIR="$(mktemp -d -t hippo-hook-pid.XXXXXX)"
trap 'rm -rf "$TMP_DIR"' EXIT

STUB_BIN="$TMP_DIR/bin"
HOOK_LOG_DIR="$TMP_DIR/log"
mkdir -p "$STUB_BIN" "$HOOK_LOG_DIR"

# Record the HIPPO_WATCH_PID the hook passes into the tmux command. This is
# the load-bearing assertion: whatever PID the hook decides is "Claude's
# PID" ends up as the environment variable the Rust tailer uses.
cat >"$STUB_BIN/hippo" <<'STUB'
#!/usr/bin/env bash
# The hook invokes this as the body of the tmux new-window command, but the
# tmux stub short-circuits that path. We also get invoked for the batch
# fallback. Record anything we can glean from the environment either way.
{
    echo "argv: $*"
    echo "HIPPO_WATCH_PID=${HIPPO_WATCH_PID:-<unset>}"
} >>"${HIPPO_STUB_LOG:-/dev/null}"
exec sleep 2
STUB
chmod +x "$STUB_BIN/hippo"

# tmux stub that captures the full TMUX_CMD argument as written by the hook.
# We care specifically about the `HIPPO_WATCH_PID=<n>` prefix the hook builds
# at line 85 of claude-session-hook.sh.
cat >"$STUB_BIN/tmux" <<'STUB'
#!/usr/bin/env bash
# Record the full command line so tests can parse the embedded TMUX_CMD.
echo "tmux-stub argv: $*" >>"${TMUX_STUB_LOG:-/dev/null}"

case "$1" in
    display-message)
        # Return a fake session name so the hook enters the "inside tmux" branch.
        echo "worksess"
        exit 0
        ;;
    list-sessions|has-session)
        exit 0
        ;;
    new-window|new-session)
        # Swallow the command without actually spawning.
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
STUB
chmod +x "$STUB_BIN/tmux"

TRANSCRIPT="$TMP_DIR/abcdef-0000-1111.jsonl"
: > "$TRANSCRIPT"

# ============================================================================
# Test 1: baseline — when the hook runs as a direct child of this test
# script, $PPID is the test-script PID, and the hook must propagate that
# exact PID as HIPPO_WATCH_PID. This pins down the "PPID is Claude's PID"
# contract.
# ============================================================================
echo "Test 1: hook propagates its own \$PPID as HIPPO_WATCH_PID (current contract)"

export HIPPO_STUB_LOG="$TMP_DIR/hippo.log"
export TMUX_STUB_LOG="$TMP_DIR/tmux.log"
: >"$HIPPO_STUB_LOG"
: >"$TMUX_STUB_LOG"

# Invoke the hook. Its $PPID will be this script's PID.
EXPECTED_CLAUDE_PID="$$"

hook_input="{\"transcript_path\":\"$TRANSCRIPT\",\"cwd\":\"/tmp/proj\",\"session_id\":\"abcdef-0000\"}"

PATH="$STUB_BIN:$PATH" \
    XDG_DATA_HOME="$HOOK_LOG_DIR" \
    TMUX_PANE="%0" \
    bash "$HOOK_SCRIPT" <<<"$hook_input"

# The tmux stub recorded the new-window argv. The third-to-last arg is the
# TMUX_CMD string containing `HIPPO_WATCH_PID=<pid>`.
# shellcheck disable=SC2034
tmux_log_for_assert="$TMUX_STUB_LOG"
assert "tmux was invoked with a new-window command" \
    "grep -q 'new-window' \"\$tmux_log_for_assert\""

assert "TMUX_CMD embeds HIPPO_WATCH_PID=\$PPID (= $EXPECTED_CLAUDE_PID)" \
    "grep -qE \"HIPPO_WATCH_PID=${EXPECTED_CLAUDE_PID}[^0-9]\" \"\$tmux_log_for_assert\""

# ============================================================================
# Test 2: wrapper scenario — invoke the hook through an intermediate bash
# process so that hook's $PPID is the WRAPPER, not the outer test shell.
# This simulates the `claade → claude → hook` shape (though not perfectly,
# since we cannot fake `claude` itself without building a real binary).
#
# Current behavior: the hook uses $PPID unconditionally, so it will pick
# the wrapper PID. We assert that shape so a future "walk the chain" fix
# has a visible contract-change signal (this test must be updated).
# ============================================================================
echo
echo "Test 2: hook uses direct \$PPID even when invoked via a wrapper"

: >"$TMUX_STUB_LOG"

# Capture the wrapper's PID by printing it before exec'ing the hook.
WRAPPER_SCRIPT="$TMP_DIR/wrapper.sh"
cat >"$WRAPPER_SCRIPT" <<WRAP
#!/usr/bin/env bash
echo "WRAPPER_PID=\$\$" >"$TMP_DIR/wrapper_pid"
exec bash "$HOOK_SCRIPT"
WRAP
chmod +x "$WRAPPER_SCRIPT"

PATH="$STUB_BIN:$PATH" \
    XDG_DATA_HOME="$HOOK_LOG_DIR" \
    TMUX_PANE="%0" \
    "$WRAPPER_SCRIPT" <<<"$hook_input"

WRAPPER_PID_LINE="$(cat "$TMP_DIR/wrapper_pid")"
# shellcheck disable=SC2034  # kept for diagnostic visibility when assertions fail
WRAPPER_PID="${WRAPPER_PID_LINE#WRAPPER_PID=}"

# Note: `exec bash "$HOOK_SCRIPT"` REPLACES the wrapper in-place, so the
# bash running the hook has WRAPPER_PID as its own PID, and the hook's
# $PPID is the outer test script ($$). That means current behavior on
# `exec`-based wrappers is: $PPID walks UP one level naturally. This is
# different from a `claude → subshell-that-forks → hook` chain where a
# real wrapper does not exec but forks.
#
# The documented gotcha in CLAUDE.md says: "The hook script runs as a
# direct child of Claude (claude → hook.sh), so $PPID IS the Claude
# process PID". This test asserts that $PPID == the direct parent PID
# at invocation time. With exec the direct parent is $$.

# shellcheck disable=SC2034
tmux_log_for_assert="$TMUX_STUB_LOG"
assert "tmux was invoked in the wrapper scenario" \
    "grep -q 'new-window' \"\$tmux_log_for_assert\""

# Whatever PID shows up must equal $$ (the outer script), because exec
# kept the same PPID. If a future fix changes the behavior, this will
# fail with a concrete PID mismatch that the dev can inspect.
assert "exec-based wrapper: hook sees outer shell as \$PPID ($$)" \
    "grep -qE \"HIPPO_WATCH_PID=$$[^0-9]\" \"\$tmux_log_for_assert\""

# ============================================================================
# Test 3: the HIPPO_WATCH_PID value must be a plain integer — a regression
# here would mean the hook's $PPID reference broke (e.g., became empty or
# contained a literal "$PPID" string due to a bad refactor).
# ============================================================================
echo
echo "Test 3: HIPPO_WATCH_PID is always an integer (never empty, never a literal)"

: >"$TMUX_STUB_LOG"
PATH="$STUB_BIN:$PATH" \
    XDG_DATA_HOME="$HOOK_LOG_DIR" \
    TMUX_PANE="%0" \
    bash "$HOOK_SCRIPT" <<<"$hook_input"

# Extract the PID the hook used.
# shellcheck disable=SC2034
tmux_log_for_assert="$TMUX_STUB_LOG"
pid_value="$(grep -oE 'HIPPO_WATCH_PID=[^ ]*' "$TMUX_STUB_LOG" | head -1 | cut -d= -f2 || true)"
# shellcheck disable=SC2034
pid_value_for_assert="$pid_value"
assert "extracted HIPPO_WATCH_PID value is non-empty" \
    "[ -n \"\$pid_value_for_assert\" ]"
assert "extracted HIPPO_WATCH_PID value is purely numeric" \
    "[[ \"\$pid_value_for_assert\" =~ ^[0-9]+$ ]]"

# ============================================================================
echo
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -ne 0 ]; then
    exit 1
fi
