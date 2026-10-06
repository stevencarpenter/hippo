#!/usr/bin/env bash
# Run: bash tests/shell/test-install-external-daemon.sh
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
sed '$d' "$repo_root/scripts/install.sh" > "$tmp/install-helpers.sh"

for scenario in path-only local-only local-and-path missing config-fails xdg-config \
    legacy-path legacy-local legacy-config-fails legacy-existing legacy-empty-xdg \
    empty-xdg legacy-unset-xdg legacy-empty-existing; do
    fixture="$tmp/$scenario home"
    mkdir -p "$fixture/.local/bin" "$fixture/external-bin" "$fixture/cwd"
    config_root="$fixture/config-root"
    data_root="$fixture/data-root"
    config_env="$config_root"
    data_env="$data_root"
    case "$scenario" in
        *empty*|legacy-unset-xdg)
            config_root="$fixture/.config"
            data_root="$fixture/.local/share"
            config_env=
            data_env= ;;
    esac
    cat > "$fixture/fake-hippo" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >> "$HIPPO_TEST_INVOCATIONS"
printf '%s\n' "$0" >> "$HIPPO_TEST_BINARIES"
printf '%s|%s\n' "${XDG_CONFIG_HOME-unset}" "${XDG_DATA_HOME-unset}" >> "$HIPPO_TEST_ENVIRONMENTS"
# Historical Rust resolvers distinguish an absent variable from an empty one.
if [[ "$HIPPO_TEST_SCENARIO" == legacy-* ]]; then
    config_root="${XDG_CONFIG_HOME-${HOME}/.config}"
    data_root="${XDG_DATA_HOME-${HOME}/.local/share}"
else
    config_root="${XDG_CONFIG_HOME:-${HOME}/.config}"
    data_root="${XDG_DATA_HOME:-${HOME}/.local/share}"
fi
# PathBuf::join on an empty root produces a relative path.
config_dir="${config_root:+${config_root}/}hippo"
data_dir="${data_root:+${data_root}/}hippo"
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
        mkdir -p "$config_dir" "$data_dir"
        printf '[brain]\nport = 9175\n' > "$config_dir/config.toml"
        if [[ "$*" == 'config edit' ]]; then
            "$EDITOR" "$config_dir/config.toml"
        fi ;;
esac
EOF
    chmod +x "$fixture/fake-hippo"
    case "$scenario" in
        path-only|local-and-path|config-fails|xdg-config|legacy-path|legacy-config-fails|*existing|*xdg) ln -s "$fixture/fake-hippo" "$fixture/external-bin/hippo" ;;
    esac
    case "$scenario" in
        local-only|local-and-path|legacy-local) cp "$fixture/fake-hippo" "$fixture/.local/bin/hippo" ;;
    esac
    if [[ "$scenario" == xdg-config || "$scenario" == *existing ]]; then
        mkdir -p "$config_root/hippo"
        printf '[brain]\nport = 18234\n' > "$config_root/hippo/config.toml"
    fi

    # The child bash expands its fixture environment, not the parent shell.
    # shellcheck disable=SC2016
    if env HOME="$fixture" PATH="$fixture/external-bin:/usr/bin:/bin" \
        XDG_STATE_HOME="$fixture/state" HIPPO_INSTALL_DAEMON=0 HIPPO_INSTALL_SKILLS=0 \
        XDG_CONFIG_HOME="$config_env" XDG_DATA_HOME="$data_env" \
        HIPPO_TEST_HEALTH="$fixture/health" \
        HIPPO_TEST_INVOCATIONS="$fixture/invocations" HIPPO_TEST_BINARIES="$fixture/binaries" \
        HIPPO_TEST_ENVIRONMENTS="$fixture/environments" \
        HIPPO_TEST_SCENARIO="$scenario" \
        bash -c '
            set -euo pipefail
            install_helpers="$1"
            set --
            cd "$HOME/cwd"
            if [[ "$HIPPO_TEST_SCENARIO" == legacy-unset-xdg ]]; then
                unset XDG_CONFIG_HOME XDG_DATA_HOME
            fi
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
        if [[ "$scenario" == xdg-config || "$scenario" == *existing ]]; then
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
        diff -u "$fixture/expected-config" "$config_root/hippo/config.toml"
        expected_binary="$fixture/.local/bin/hippo"
        if [[ "$scenario" == path-only || "$scenario" == xdg-config \
            || "$scenario" == legacy-path || "$scenario" == *existing || "$scenario" == *xdg ]]; then
            expected_binary="$fixture/external-bin/hippo"
        fi
        test -d "$data_root/hippo"
        test ! -e "$fixture/cwd/hippo"
        if [[ "$data_root" != "$fixture/.local/share" ]]; then
            test ! -e "$fixture/.local/share/hippo"
        fi
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
        while IFS= read -r _; do
            printf '%s|%s\n' "$config_root" "$data_root"
        done < "$fixture/expected-invocations" > "$fixture/expected-environments"
        diff -u "$fixture/expected-environments" "$fixture/environments"
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
