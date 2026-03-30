# hippo.zsh — Shell hook for command capture
# Source from .zshrc after hippo binary is on PATH.

# Guard against double-sourcing
[[ -n "${_HIPPO_HOOK_LOADED}" ]] && return
_HIPPO_HOOK_LOADED=1

autoload -Uz add-zsh-hook

# Git state cache
typeset -g _HIPPO_LAST_GIT_CWD=""
typeset -g _HIPPO_LAST_GIT_TS=0
typeset -g _HIPPO_GIT_BRANCH=""
typeset -g _HIPPO_GIT_COMMIT=""
typeset -g _HIPPO_GIT_DIRTY=""

# Output capture config (lines to keep from head/tail of output)
typeset -g _HIPPO_OUTPUT_HEAD=${_HIPPO_OUTPUT_HEAD:-50}
typeset -g _HIPPO_OUTPUT_TAIL=${_HIPPO_OUTPUT_TAIL:-100}
typeset -g _HIPPO_OUTPUT_FILE="/tmp/hippo-output.$$"

# Truncate captured output using head+tail with omission marker
_hippo_truncate_output() {
    local file="$1"
    [[ -f "$file" ]] || return
    [[ -s "$file" ]] || return

    local total head_n tail_n
    total=$(wc -l < "$file")
    total=${total##* }
    head_n=${_HIPPO_OUTPUT_HEAD}
    tail_n=${_HIPPO_OUTPUT_TAIL}

    local max_lines=$(( head_n + tail_n ))
    if (( total <= max_lines )); then
        cat "$file"
    else
        local omitted=$(( total - head_n - tail_n ))
        head -n "${head_n}" "$file"
        printf '... (%d lines omitted) ...\n' "${omitted}"
        tail -n "${tail_n}" "$file"
    fi
}

# Preexec: capture command and start time
_hippo_preexec() {
    _HIPPO_CMD="$1"
    _HIPPO_CWD="$PWD"
    _HIPPO_START="${EPOCHREALTIME}"
    # Clear previous output capture
    : > "${_HIPPO_OUTPUT_FILE}"
}

# Precmd: send captured command to daemon
_hippo_precmd() {
    local exit_code=$?

    # Skip if no command was captured
    [[ -z "${_HIPPO_CMD}" ]] && return

    # Calculate duration in milliseconds
    local end="${EPOCHREALTIME}"
    local duration_ms=$(( (${end} - ${_HIPPO_START}) * 1000 ))
    duration_ms=${duration_ms%.*}
    [[ -z "${duration_ms}" ]] && duration_ms=0

    # Refresh git state if cwd changed or 5+ seconds elapsed
    local now="${EPOCHREALTIME}"
    now=${now%.*}
    if [[ "${_HIPPO_CWD}" != "${_HIPPO_LAST_GIT_CWD}" ]] || (( now - _HIPPO_LAST_GIT_TS >= 5 )); then
        _HIPPO_LAST_GIT_CWD="${_HIPPO_CWD}"
        _HIPPO_LAST_GIT_TS="${now}"
        if git -C "${_HIPPO_CWD}" rev-parse --is-inside-work-tree &>/dev/null; then
            _HIPPO_GIT_BRANCH="$(git -C "${_HIPPO_CWD}" rev-parse --abbrev-ref HEAD 2>/dev/null)"
            _HIPPO_GIT_COMMIT="$(git -C "${_HIPPO_CWD}" rev-parse --short HEAD 2>/dev/null)"
            if [[ -n "$(git -C "${_HIPPO_CWD}" status --porcelain 2>/dev/null | head -1)" ]]; then
                _HIPPO_GIT_DIRTY=1
            else
                _HIPPO_GIT_DIRTY=0
            fi
        else
            _HIPPO_GIT_BRANCH=""
            _HIPPO_GIT_COMMIT=""
            _HIPPO_GIT_DIRTY=""
        fi
    fi

    # Build args
    local -a args=(
        send-event shell
        --cmd "${_HIPPO_CMD}"
        --exit "${exit_code}"
        --cwd "${_HIPPO_CWD}"
        --duration-ms "${duration_ms}"
    )

    if [[ -n "${_HIPPO_GIT_BRANCH}" ]]; then
        args+=(--git-branch "${_HIPPO_GIT_BRANCH}")
    fi
    if [[ -n "${_HIPPO_GIT_COMMIT}" ]]; then
        args+=(--git-commit "${_HIPPO_GIT_COMMIT}")
    fi
    if [[ "${_HIPPO_GIT_DIRTY}" == "1" ]]; then
        args+=(--git-dirty)
    fi

    # Attach captured output if available
    if [[ -s "${_HIPPO_OUTPUT_FILE}" ]]; then
        local captured
        captured="$(_hippo_truncate_output "${_HIPPO_OUTPUT_FILE}")"
        if [[ -n "${captured}" ]]; then
            args+=(--output "${captured}")
        fi
    fi

    # Fire and forget — backgrounded, disowned, silenced
    hippo "${args[@]}" &>/dev/null &!

    # Clean up temp vars
    unset _HIPPO_CMD _HIPPO_CWD _HIPPO_START
}

add-zsh-hook preexec _hippo_preexec
add-zsh-hook precmd _hippo_precmd
