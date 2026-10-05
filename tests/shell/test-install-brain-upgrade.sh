#!/usr/bin/env bash
# Run: bash tests/shell/test-install-brain-upgrade.sh
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
tmp="$(cd "$tmp" && pwd -P)"
test_python="$(python3 -S -c 'import sys; print(sys.executable)')"

# Run the production Python probes without installing packages or loading native code.
mkdir -p "$tmp/python-fixture/hippo_brain" "$tmp/fake-bin"
touch "$tmp/python-fixture/hippo_brain/__init__.py"
printf 'create_app = object()\n' > "$tmp/python-fixture/hippo_brain/server.py"
cat > "$tmp/python-fixture/sqlite_vec.py" <<'PY'
import os
import sqlite3

scenario = os.environ["HIPPO_TEST_SQLITE_SCENARIO"]
original_connect = sqlite3.connect


class ProbeConnection(sqlite3.Connection):
    def __getattribute__(self, name):
        if scenario == "sqlite-api-missing" and name == "enable_load_extension":
            raise AttributeError("fixture SQLite extension loading API unavailable")
        return super().__getattribute__(name)

    def enable_load_extension(self, enabled):
        self.extension_loading = enabled


def connect(database):
    assert database == ":memory:"
    return original_connect(database, factory=ProbeConnection)


sqlite3.connect = connect


def load(connection):
    assert connection.extension_loading
    if scenario == "sqlite-load-fails":
        raise sqlite3.OperationalError("fixture sqlite_vec load failure")

    def vec_version():
        assert not connection.extension_loading
        with open(os.environ["HIPPO_TEST_QUERY_LOG"], "a") as log:
            log.write(os.getcwd() + "\n")
        return "fixture-version"

    connection.create_function("vec_version", 0, vec_version)
PY
cat > "$tmp/fake-bin/uv" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
case "$1" in
    venv|sync) [[ "$HIPPO_TEST_SQLITE_SCENARIO" != sync-fails ]] ;;
    run)
        [[ "$2" == --no-sync && "$3" == python ]]
        shift 3
        exec "$HIPPO_TEST_PYTHON" -S "$@"
        ;;
    *) exit 2 ;;
esac
SH
chmod +x "$tmp/fake-bin/uv"
export PATH="$tmp/fake-bin:$PATH" PYTHONPATH="$tmp/python-fixture" HIPPO_TEST_PYTHON="$test_python"

assert_runtime_failure() {
    local scenario="$1" stderr="$2" diagnostic
    case "$scenario" in
        sqlite-api-missing) diagnostic="fixture SQLite extension loading API unavailable" ;;
        sqlite-load-fails) diagnostic="fixture sqlite_vec load failure" ;;
    esac
    [[ "$(cat "$stderr")" == *"brain runtime probe failed ($test_python): "*"$diagnostic"* ]]
}

mkdir -p "$tmp/release/brain/scripts" "$tmp/release/brain/shell"
printf 'new\n' > "$tmp/release/brain/new-version"
tar -czf "$tmp/hippo-brain-1.2.3.tar.gz" -C "$tmp/release" brain
shasum -a 256 "$tmp/hippo-brain-1.2.3.tar.gz" | sed "s|$tmp/||" > "$tmp/SHA256SUMS.txt"

for scenario in extract-fails sync-fails imports-fail backup-rename-fails relocated-imports-fail receipt-fails cleanup-fails success sqlite-capable sqlite-api-missing sqlite-load-fails; do
    brain_dir="$tmp/$scenario/brain"
    bin_dir="$tmp/$scenario/bin"
    receipts_dir="$tmp/$scenario/receipts"
    mkdir -p "$brain_dir" "$bin_dir" "$receipts_dir"
    printf 'working\n' > "$brain_dir/old-version"
    printf 'old-daemon\n' > "$bin_dir/hippo"
    printf 'old-checksum\n' > "$receipts_dir/brain.sha256"
    printf 'old-daemon-checksum\n' > "$receipts_dir/daemon.sha256"
    export HIPPO_TEST_SQLITE_SCENARIO="$scenario" HIPPO_TEST_QUERY_LOG="$tmp/$scenario/queries"
    if bash -c '
        set -euo pipefail
        repo_root="$1"; release_dir="$2"; brain_dir="$3"; receipts_dir="$4"; scenario="$5"
        set --
        source <(sed "\$d" "$repo_root/scripts/install.sh")
        BRAIN_DIR="$brain_dir"
        BIN_DIR="$(dirname "$brain_dir")/bin"
        RECEIPTS_DIR="$receipts_dir"
        HIPPO_FORCE=1
        download_file() { :; }
        install_daemon() {
            printf "new-daemon\n" > "$BIN_DIR/hippo"
            printf "new-daemon-checksum\n" > "$RECEIPTS_DIR/daemon.sha256"
        }
        if [[ "$scenario" == receipt-fails ]]; then
            write_receipt() { exit 1; }
        fi
        rm() {
            if [[ "$scenario" == cleanup-fails && "${*: -1}" == *.previous ]]; then
                printf "injected backup cleanup failure\n" >&2
                return 1
            fi
            command rm "$@"
        }
        mv() {
            if [[ "$1" == "$BRAIN_DIR" && "$2" == *.previous ]]; then
                : > "$BRAIN_DIR.promotion-attempted"
                if [[ "$scenario" == backup-rename-fails ]]; then
                    printf "injected brain backup rename failure\n" >&2
                    return 1
                fi
            fi
            command mv "$@"
        }
        if [[ "$scenario" == extract-fails ]]; then
            tar() { return 1; }
        fi
        if [[ "$scenario" != sqlite-* ]]; then
            verify_brain_imports() {
                [[ "$scenario" != imports-fail ]] || return 1
                [[ "$scenario" != relocated-imports-fail || "$1" != "$BRAIN_DIR" ]]
            }
        fi
        install_components arm64 v1.2.3 "$release_dir/SHA256SUMS.txt" "$release_dir"
    ' _ "$repo_root" "$tmp" "$brain_dir" "$receipts_dir" "$scenario" > "$tmp/$scenario/stdout" 2> "$tmp/$scenario/stderr"; then
        [[ "$scenario" == success || "$scenario" == cleanup-fails || "$scenario" == sqlite-capable ]]
        if [[ "$scenario" == cleanup-fails ]]; then
            backups=("$brain_dir".new.*.previous)
            test -f "${backups[0]}/old-version"
        fi
        test -f "$brain_dir/new-version"
        test ! -f "$brain_dir/old-version"
        [[ "$(cat "$receipts_dir/brain.sha256")" != old-checksum ]]
        [[ "$(cat "$bin_dir/hippo")" == new-daemon ]]
        [[ "$(cat "$receipts_dir/daemon.sha256")" == new-daemon-checksum ]]
        if [[ "$scenario" == sqlite-capable ]]; then
            test "$(wc -l < "$HIPPO_TEST_QUERY_LOG")" -eq 2
            [[ "$(head -n 1 "$HIPPO_TEST_QUERY_LOG")" == "$brain_dir".new.* ]]
            [[ "$(tail -n 1 "$HIPPO_TEST_QUERY_LOG")" == "$brain_dir" ]]
        fi
    else
        if [[ "$scenario" == success || "$scenario" == cleanup-fails || "$scenario" == sqlite-capable ]]; then
            cat "$tmp/$scenario/stdout" "$tmp/$scenario/stderr" >&2
            exit 1
        fi
        test -f "$brain_dir/old-version"
        test ! -f "$brain_dir/new-version"
        [[ "$(cat "$receipts_dir/brain.sha256")" == old-checksum ]]
        [[ "$(cat "$bin_dir/hippo")" == old-daemon ]]
        [[ "$(cat "$receipts_dir/daemon.sha256")" == old-daemon-checksum ]]
        staging=("$brain_dir".new.*)
        test ! -e "${staging[0]}"
        if [[ "$scenario" == sqlite-* ]]; then
            assert_runtime_failure "$scenario" "$tmp/$scenario/stderr"
            test ! -e "$HIPPO_TEST_QUERY_LOG"
            test ! -e "$brain_dir.promotion-attempted"
        fi
    fi
done

# Exercise the actual mise diagnostic script with the same isolated uv fixture.
"$test_python" -S - "$repo_root/mise.toml" "$tmp/verify-brain.sh" <<'PY'
import pathlib
import sys
import tomllib

config = tomllib.loads(pathlib.Path(sys.argv[1]).read_text())
pathlib.Path(sys.argv[2]).write_text(config["tasks"]["install:verify-brain-imports"]["run"])
PY
for scenario in sqlite-capable sqlite-api-missing sqlite-load-fails; do
    fixture="$tmp/mise-$scenario"
    mkdir -p "$fixture/hippo-brain"
    export HIPPO_TEST_SQLITE_SCENARIO="$scenario" HIPPO_TEST_QUERY_LOG="$fixture/queries"
    if XDG_DATA_HOME="$fixture" bash "$tmp/verify-brain.sh" > "$fixture/stdout" 2> "$fixture/stderr"; then
        [[ "$scenario" == sqlite-capable ]]
        [[ "$(cat "$HIPPO_TEST_QUERY_LOG")" == "$fixture/hippo-brain" ]]
    else
        test "$?" -eq 1
        if [[ "$scenario" == sqlite-capable ]]; then
            cat "$fixture/stdout" "$fixture/stderr" >&2
            exit 1
        fi
        assert_runtime_failure "$scenario" "$fixture/stderr"
        test ! -e "$HIPPO_TEST_QUERY_LOG"
    fi
done

echo 'PASS: runtime probes reject incompatible SQLite; failed upgrades preserve both components and receipts'
