#!/usr/bin/env bash
# Run: bash tests/shell/test-install-external-daemon.sh
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
sed '$d' "$repo_root/scripts/install.sh" > "$tmp/install-helpers.sh"

for scenario in path-only local-only local-and-path missing config-fails xdg-config \
    legacy-path legacy-local legacy-config-fails legacy-existing; do
    fixture="$tmp/$scenario home"
    mkdir -p "$fixture/.local/bin" "$fixture/external-bin"
    cat > "$fixture/fake-hippo" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$HIPPO_TEST_INVOCATIONS"
printf '%s\n' "$0" >> "$HIPPO_TEST_BINARIES"
case "$*" in
    'config init --help')
        if [[ "$HIPPO_TEST_SCENARIO" == legacy-* ]]; then
            printf "error: unrecognized subcommand 'init'\n" >&2
            exit 2
        fi
        exit 0 ;;
    'config init'|'config edit')
        if [[ "$HIPPO_TEST_SCENARIO" == legacy-* ]]; then
            [[ "$*" == 'config edit' ]]
            [[ "$EDITOR" == true ]]
        else
            [[ "$*" == 'config init' ]]
        fi
        if [[ "$HIPPO_TEST_SCENARIO" == *config-fails ]]; then
            printf 'injected config initialization failure\n' >&2
            exit 23
        fi
        printf '[brain]\nport = 9175\n' > "$XDG_CONFIG_HOME/hippo/config.toml"
        if [[ "$*" == 'config edit' ]]; then
            "$EDITOR" "$XDG_CONFIG_HOME/hippo/config.toml"
        fi ;;
esac
EOF
    chmod +x "$fixture/fake-hippo"
    case "$scenario" in
        path-only|local-and-path|config-fails|xdg-config|legacy-path|legacy-config-fails|legacy-existing) ln -s "$fixture/fake-hippo" "$fixture/external-bin/hippo" ;;
    esac
    case "$scenario" in
        local-only|local-and-path|legacy-local) cp "$fixture/fake-hippo" "$fixture/.local/bin/hippo" ;;
    esac
    if [[ "$scenario" == xdg-config || "$scenario" == legacy-existing ]]; then
        mkdir -p "$fixture/config-root/hippo"
        printf '[brain]\nport = 18234\n' > "$fixture/config-root/hippo/config.toml"
    fi

    if env HOME="$fixture" PATH="$fixture/external-bin:/usr/bin:/bin" \
        XDG_STATE_HOME="$fixture/state" HIPPO_INSTALL_DAEMON=0 HIPPO_INSTALL_SKILLS=0 \
        XDG_CONFIG_HOME="$fixture/config-root" XDG_DATA_HOME="$fixture/data-root" \
        HIPPO_TEST_HEALTH="$fixture/health" \
        HIPPO_TEST_INVOCATIONS="$fixture/invocations" HIPPO_TEST_BINARIES="$fixture/binaries" \
        HIPPO_TEST_SCENARIO="$scenario" \
        bash -c '
            set -euo pipefail
            install_helpers="$1"
            set --
            source "$install_helpers"
            detect_platform() { printf "arm64\n"; }
            get_latest_release() { printf "v1.2.3\n"; }
            download_file() { :; }
            install_brain() { :; }
            check_dependencies() { :; }
            curl() { printf "%s\n" "$*" >> "$HIPPO_TEST_HEALTH"; }
            main
        ' _ "$tmp/install-helpers.sh" > "$fixture/output" 2>&1; then
        [[ "$scenario" != missing && "$scenario" != *config-fails ]]
        if [[ "$scenario" == xdg-config || "$scenario" == legacy-existing ]]; then
            : > "$fixture/expected-invocations"
            grep -q 'http://127.0.0.1:18234/health$' "$fixture/health"
            printf '[brain]\nport = 18234\n' > "$fixture/expected-config"
        else
            printf 'config init --help\n' > "$fixture/expected-invocations"
            if [[ "$scenario" == legacy-* ]]; then
                printf 'config edit\n' >> "$fixture/expected-invocations"
            else
                printf 'config init\n' >> "$fixture/expected-invocations"
            fi
            printf '[brain]\nport = 9175\n' > "$fixture/expected-config"
        fi
        diff -u "$fixture/expected-config" "$fixture/config-root/hippo/config.toml"
        expected_binary="$fixture/.local/bin/hippo"
        if [[ "$scenario" == path-only || "$scenario" == xdg-config \
            || "$scenario" == legacy-path || "$scenario" == legacy-existing ]]; then
            expected_binary="$fixture/external-bin/hippo"
        fi
        test -d "$fixture/data-root/hippo"
        test ! -e "$fixture/.local/share/hippo"
        cat >> "$fixture/expected-invocations" <<EOF
daemon install --force --binary-path $expected_binary --brain-dir $fixture/.local/share/hippo-brain
daemon start
status
doctor
EOF
        diff -u "$fixture/expected-invocations" "$fixture/invocations"
        while IFS= read -r _; do
            printf '%s\n' "$expected_binary"
        done < "$fixture/expected-invocations" > "$fixture/expected-binaries"
        diff -u "$fixture/expected-binaries" "$fixture/binaries"
    else
        status=$?
        if [[ "$scenario" != missing && "$scenario" != *config-fails ]]; then
            cat "$fixture/output" >&2
            exit 1
        fi
        test "$status" -eq 1
        if [[ "$scenario" == missing ]]; then
            test ! -e "$fixture/invocations"
            grep -q 'HIPPO_INSTALL_DAEMON=0 but no hippo binary is on PATH' "$fixture/output"
        else
            printf 'config init --help\n' > "$fixture/expected-invocations"
            if [[ "$scenario" == legacy-* ]]; then
                printf 'config edit\n' >> "$fixture/expected-invocations"
            else
                printf 'config init\n' >> "$fixture/expected-invocations"
            fi
            diff -u "$fixture/expected-invocations" "$fixture/invocations"
            grep -q 'Failed to initialize configuration at ' "$fixture/output"
            grep -q 'injected config initialization failure' "$fixture/output"
        fi
    fi
done

echo 'PASS: external daemons configure, install, start and verify through the selected binary'
