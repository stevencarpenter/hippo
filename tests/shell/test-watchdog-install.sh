#!/usr/bin/env bash
# Smoke test for T-2 watchdog install + alarms CLI.
#
# Asserts:
#   1. com.hippo.watchdog.plist exists in ~/Library/LaunchAgents after install.
#   2. launchctl prints the service as loaded in the current GUI session.
#   3. A mock alarm row round-trips through `hippo alarms list` and `ack`.
#
# Run:   bash tests/shell/test-watchdog-install.sh
# Prereq: `hippo daemon install --force` must have been run first.
#         (The CI job that drives this is expected to call install beforehand.)
#
# Cleanup: removes the test alarm row from capture_alarms on exit.
#          Does NOT unload/reload the watchdog service — launchd state is
#          preserved across the test.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# Locate the hippo binary — prefer the symlink in ~/.local/bin so we test
# the installed artefact, not a stale debug build.
if command -v hippo >/dev/null 2>&1; then
    HIPPO_BIN="$(command -v hippo)"
elif [ -f "$HOME/.local/bin/hippo" ]; then
    HIPPO_BIN="$HOME/.local/bin/hippo"
elif [ -f "$REPO_ROOT/target/release/hippo" ]; then
    HIPPO_BIN="$REPO_ROOT/target/release/hippo"
else
    echo "FAIL: hippo binary not found. Build with: cargo build --release" >&2
    exit 1
fi

PLIST_PATH="$HOME/Library/LaunchAgents/com.hippo.watchdog.plist"
WATCHDOG_LABEL="com.hippo.watchdog"
DB_PATH="${XDG_DATA_HOME:-$HOME/.local/share}/hippo/hippo.db"

PASS=0
FAIL=0
TEST_ALARM_ID=""

cleanup() {
    # Remove all I-TEST alarm rows created by this test so we don't pollute
    # the real alarm table (on both normal exit and failure).
    if [ -f "$DB_PATH" ]; then
        sqlite3 "$DB_PATH" \
            "DELETE FROM capture_alarms WHERE invariant_id = 'I-TEST';" \
            2>/dev/null || true
    fi
}
trap cleanup EXIT

# Pre-clean any I-TEST rows left over from a previous interrupted run.
if [ -f "$DB_PATH" ]; then
    sqlite3 "$DB_PATH" \
        "DELETE FROM capture_alarms WHERE invariant_id = 'I-TEST';" \
        2>/dev/null || true
fi

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

echo "=== T-2 watchdog install test ==="
echo "  hippo: $HIPPO_BIN"
echo "  plist: $PLIST_PATH"
echo "  db:    $DB_PATH"
echo

# ---------------------------------------------------------------------------
# 1. Plist exists in LaunchAgents
# ---------------------------------------------------------------------------
echo "-- 1. Plist installation --"
assert "com.hippo.watchdog.plist exists in ~/Library/LaunchAgents" \
    "[ -f '$PLIST_PATH' ]"

assert "plist contains StartInterval=60" \
    "grep -q 'StartInterval' '$PLIST_PATH' && grep -A1 'StartInterval' '$PLIST_PATH' | grep -q '60'"

assert "plist does NOT contain KeepAlive key" \
    "! grep -q 'KeepAlive' '$PLIST_PATH'"

assert "plist RunAtLoad is false" \
    "grep -A1 'RunAtLoad' '$PLIST_PATH' | grep -q '<false/>'"

assert "plist ProgramArguments includes 'watchdog' and 'run'" \
    "grep -q 'watchdog' '$PLIST_PATH' && grep -q '<string>run</string>' '$PLIST_PATH'"

assert "plist log paths reference watchdog" \
    "grep -q 'watchdog.stdout.log' '$PLIST_PATH' && grep -q 'watchdog.stderr.log' '$PLIST_PATH'"

# ---------------------------------------------------------------------------
# 2. Service is loaded in launchd
# ---------------------------------------------------------------------------
echo
echo "-- 2. launchd service loaded --"

LAUNCHCTL_OUT=$(launchctl print "gui/$(id -u)/$WATCHDOG_LABEL" 2>&1 || true)

assert "launchctl print gui/\$(id -u)/com.hippo.watchdog exits 0 (service loaded)" \
    "launchctl print 'gui/$(id -u)/$WATCHDOG_LABEL' >/dev/null 2>&1"

assert "launchctl output contains the label" \
    "echo '$LAUNCHCTL_OUT' | grep -q '$WATCHDOG_LABEL'"

# ---------------------------------------------------------------------------
# 3. Mock alarm round-trip
# ---------------------------------------------------------------------------
echo
echo "-- 3. Alarm round-trip (list / ack) --"

if [ ! -f "$DB_PATH" ]; then
    echo "  [SKIP] DB not found at $DB_PATH — skipping alarm round-trip" >&2
    FAIL=$((FAIL + 1))
else
    NOW_MS=$(date +%s)000  # epoch milliseconds (rough)

    # Insert a test alarm row and capture the rowid in one connection so
    # last_insert_rowid() is reliable.
    TEST_ALARM_ID=$(sqlite3 "$DB_PATH" \
        "INSERT INTO capture_alarms (invariant_id, raised_at, details_json)
         VALUES ('I-TEST', $NOW_MS, '{\"source\":\"test\",\"since_ms\":1000}');
         SELECT last_insert_rowid();")

    assert "test alarm row was inserted (id=$TEST_ALARM_ID)" \
        "[ -n '$TEST_ALARM_ID' ] && [ '$TEST_ALARM_ID' -gt 0 ]"

    # hippo alarms list should print the row and exit 1 (active alarms exist).
    LIST_OUTPUT=$("$HIPPO_BIN" alarms list 2>&1 || true)
    LIST_EXIT=$("$HIPPO_BIN" alarms list >/dev/null 2>&1; echo $?) || true

    assert "hippo alarms list prints the test invariant" \
        "echo '$LIST_OUTPUT' | grep -q 'I-TEST'"

    assert "hippo alarms list exits 1 when active alarms present" \
        "! '$HIPPO_BIN' alarms list >/dev/null 2>&1"

    # Acknowledge the test alarm.
    "$HIPPO_BIN" alarms ack "$TEST_ALARM_ID" --note "hippo-test-watchdog-install"

    assert "acked alarm no longer appears in hippo alarms list" \
        "! '$HIPPO_BIN' alarms list 2>&1 | grep -q 'I-TEST'"

    # hippo alarms list should exit 0 now (no active alarms) — unless other real
    # alarms exist, in which case this assert is still meaningful: we verify that
    # specifically I-TEST is gone from the output.
    FINAL_LIST=$("$HIPPO_BIN" alarms list 2>&1 || true)
    assert "I-TEST no longer in alarms list after ack" \
        "! echo '$FINAL_LIST' | grep -q 'I-TEST'"

    # Second ack is idempotent.
    "$HIPPO_BIN" alarms ack "$TEST_ALARM_ID" --note "second-ack" 2>/dev/null
    assert "re-ack is idempotent (exits 0)" \
        "'$HIPPO_BIN' alarms ack '$TEST_ALARM_ID' >/dev/null 2>&1"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo
echo "=== Results: $PASS passed, $FAIL failed ==="

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
