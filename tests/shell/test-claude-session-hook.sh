#!/usr/bin/env bash
# Smoke test for shell/claude-session-hook.sh tmux targeting.
#
# Regression guard for two bugs:
#
#   1. Pre-#48: `tmux new-window -d -t "$TMUX_TARGET_SESSION"` parsed the bare
#      session name as a target-window, hitting "index N in use" when
#      base-index=1 and window 1 already existed.
#
#   2. Post-#48 (H1 sev1): `tmux new-window -d` with no `-t` dropped every
#      session because the hook runs as a child of Claude Code — $TMUX is not
#      inherited, so tmux had no session to target.
#
# The fix is `-t "${TMUX_TARGET_SESSION}:"` (trailing colon = "next unused
# index in this specific session"). This test exercises that exact shape.
#
# Run: bash tests/shell/test-claude-session-hook.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
HOOK_SCRIPT="$REPO_ROOT/shell/claude-session-hook.sh"

if [ ! -f "$HOOK_SCRIPT" ]; then
    echo "FAIL: hook script not found at $HOOK_SCRIPT" >&2
    exit 1
fi

# Use an isolated tmux socket so we do not pollute the developer's tmux
# server and so parallel CI runs cannot clobber each other.
TMP_DIR="$(mktemp -d -t hippo-hook-test.XXXXXX)"
TMUX_SOCKET="$TMP_DIR/tmux.sock"
TMUX="tmux -S $TMUX_SOCKET"

cleanup() {
    $TMUX kill-server 2>/dev/null || true
    rm -rf "$TMP_DIR"
}
trap cleanup EXIT

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

# ---------------------------------------------------------------------------
# Test 1: fix holds under the exact conditions that broke the pre-#48 code.
#
# Setup: session "1" (numeric name, matching the user's real setup) with
# base-index=1 and window 1 already occupied. A bare `-t 1` would error with
# "index 1 in use"; `-t "1:"` must succeed and pick the next free index.
# ---------------------------------------------------------------------------
echo "Test 1: tmux -t 'session:' picks next free index (no 'index N in use')"

$TMUX new-session -d -s "1" -n "existing-1" "sleep 30"
$TMUX set-option -t "1" base-index 1 >/dev/null

# Representative env as set by the hook before the tmux branch.
TMUX_TARGET_SESSION="1"
WINDOW_NAME="🦛 hippo·abc123"
TMUX_CMD="sleep 30"

# Exercise the exact invocation shape from the hook. Capture stderr to catch
# the "index N in use" regression explicitly.
# shellcheck disable=SC2034  # hook_stderr is referenced inside `assert` eval strings
hook_stderr="$($TMUX new-window -d -t "${TMUX_TARGET_SESSION}:" -n "$WINDOW_NAME" "$TMUX_CMD" 2>&1 1>/dev/null || true)"

assert "no 'index N in use' error from tmux" \
    "[ -z \"\$hook_stderr\" ]"

# Verify the window landed in the correct session with the expected name.
windows="$($TMUX list-windows -t "1" -F '#{window_index} #{window_name}')"
assert "new window exists in session '1' with expected name" \
    "echo \"\$windows\" | grep -qF '🦛 hippo·abc123'"

# Verify it was auto-indexed (not 1, since 1 was already taken).
new_idx="$(echo "$windows" | awk '/🦛 hippo·abc123/ {print $1}')"
assert "new window auto-picked an index above base-index (got ${new_idx:-<none>})" \
    "[ -n \"\$new_idx\" ] && [ \"\$new_idx\" -gt 1 ]"

$TMUX kill-session -t "1"

# ---------------------------------------------------------------------------
# Test 2: end-to-end invocation of the hook script itself.
#
# Feeds representative SessionStart JSON on stdin (matching the Claude Code
# hook contract) and asserts the hook logs "spawned tmux window in session="
# for the target session. Uses TMUX_PANE to simulate being invoked from a
# child of a tmux-attached Claude process.
# ---------------------------------------------------------------------------
echo
echo "Test 2: end-to-end hook invocation creates window in target session"

$TMUX new-session -d -s "worksess" -n "filler" "sleep 30"
$TMUX set-option -t "worksess" base-index 1 >/dev/null

# The hook wants a hippo binary to quote into the tmux command; a stub on
# PATH satisfies the probe without requiring a real build.
STUB_BIN="$TMP_DIR/bin"
mkdir -p "$STUB_BIN"
cat >"$STUB_BIN/hippo" <<'STUB'
#!/usr/bin/env bash
exec sleep 30
STUB
chmod +x "$STUB_BIN/hippo"

# Redirect the hook's debug log into the temp dir so assertions can read it.
HOOK_LOG_DIR="$TMP_DIR/log"
mkdir -p "$HOOK_LOG_DIR"

# Give the hook a transcript path that exists (its existence isn't checked by
# the hook, but an empty file keeps the contract realistic). The hook derives
# the window's short-id as the first 6 chars of the JSONL basename, so name
# the file so we can assert that slice precisely (expected: `deadbe`).
TRANSCRIPT="$TMP_DIR/deadbeef-cafe-1234.jsonl"
: > "$TRANSCRIPT"

# Get a real tmux pane id so the hook's `tmux display-message -t "$TMUX_PANE"`
# call can resolve the session name. Use our isolated socket.
pane_id="$($TMUX display-message -t "worksess:1" -p '#{pane_id}')"

# Point the hook at our isolated tmux socket by shadowing the `tmux` command.
# The hook invokes `tmux` (unqualified) in multiple places, all of which must
# hit our private socket. Resolve the real tmux absolute path here — using
# `env tmux` inside the stub would hit the stub again (infinite recursion)
# because the stub itself is on PATH.
REAL_TMUX="$(command -v tmux)"
if [ -z "$REAL_TMUX" ] || [ "$REAL_TMUX" = "$STUB_BIN/tmux" ]; then
    # Fall back to common install paths.
    for candidate in /opt/homebrew/bin/tmux /usr/local/bin/tmux /usr/bin/tmux; do
        if [ -x "$candidate" ]; then
            REAL_TMUX="$candidate"
            break
        fi
    done
fi
if [ -z "$REAL_TMUX" ]; then
    echo "FAIL: could not locate real tmux binary" >&2
    exit 1
fi

cat >"$STUB_BIN/tmux" <<STUB
#!/usr/bin/env bash
exec "$REAL_TMUX" -S "$TMUX_SOCKET" "\$@"
STUB
chmod +x "$STUB_BIN/tmux"

# Run the hook. It reads JSON on stdin.
hook_input=$(cat <<JSON
{"transcript_path":"$TRANSCRIPT","cwd":"/tmp/fakeproject","session_id":"deadbeef-cafe"}
JSON
)

PATH="$STUB_BIN:$PATH" \
    XDG_DATA_HOME="$HOOK_LOG_DIR" \
    TMUX_PANE="$pane_id" \
    bash "$HOOK_SCRIPT" <<<"$hook_input"

# Give tmux a beat to register the new window (new-window -d returns
# immediately but list-windows can race on slow CI).
sleep 0.2

windows="$($TMUX list-windows -t "worksess" -F '#{window_index} #{window_name}')"

assert "hook spawned a hippo window in target session 'worksess'" \
    "echo \"\$windows\" | grep -qF '🦛 fakeproject'"

assert "hook window name includes short session id prefix" \
    "echo \"\$windows\" | grep -qF '·deadbe'"

# Confirm the debug log captured the expected branch (proves `-t session:`
# path was taken, not the fallback).
# shellcheck disable=SC2034  # DEBUG_LOG is referenced inside `assert` eval strings
DEBUG_LOG="$HOOK_LOG_DIR/hippo/session-hook-debug.log"
assert "hook logged 'spawned tmux window in session=worksess'" \
    "grep -q 'spawned tmux window in session=worksess' \"\$DEBUG_LOG\""

# Explicit guard against the pre-#48 regression re-appearing.
assert "hook did not error with 'index N in use'" \
    "! grep -q 'index .* in use' \"\$DEBUG_LOG\""

$TMUX kill-session -t "worksess" 2>/dev/null || true

# ---------------------------------------------------------------------------
echo
echo "Results: $PASS passed, $FAIL failed"
if [ "$FAIL" -ne 0 ]; then
    exit 1
fi
