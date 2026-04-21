#!/usr/bin/env bash
# Scans shell scripts in this repo for patterns that could leak secrets
# (e.g., hardcoded tokens, secret-like env var assignments). Intended to
# run in pre-commit and CI. Requires ripgrep.
set -euo pipefail

usage() {
    cat <<'EOF'
check-shell-secrets — scan repo shell scripts for secret-leak patterns

USAGE:
    check-shell-secrets.sh [--help]

Takes no arguments. Scans all *.sh files under the repo root (excluding
itself). Exits 0 on clean, non-zero on any suspected leak.

REQUIREMENTS:
    ripgrep (brew install ripgrep / apt install ripgrep)
EOF
}

case "${1:-}" in
    -h|--help) usage; exit 0 ;;
esac

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${ROOT}"

require_cmd() {
  local cmd="$1"
  if ! command -v "${cmd}" >/dev/null 2>&1; then
    echo "[shell-secrets] ERROR: required command '${cmd}' not found." >&2
    if [[ "${cmd}" == "rg" ]]; then
      echo "[shell-secrets] Install with: brew install ripgrep (macOS), apt install ripgrep (Debian/Ubuntu)" >&2
    fi
    exit 1
  fi
}

require_cmd rg

echo "[shell-secrets] scanning shell scripts for secret-leak patterns..."

TARGETS=()
while IFS= read -r file; do
  if [[ "${file}" == "scripts/security/check-shell-secrets.sh" ]]; then
    continue
  fi
  TARGETS+=("${file}")
done < <(rg --files -g '*.sh' -g '*.zsh' scripts shell)

if [[ "${#TARGETS[@]}" -eq 0 ]]; then
  echo "[shell-secrets] no shell scripts found."
  exit 0
fi

failures=0

print_matches() {
  local title="$1"
  local pattern="$2"
  local matches
  matches="$(rg -n --no-heading -S "${pattern}" "${TARGETS[@]}" || true)"
  if [[ -n "${matches}" ]]; then
    echo "[shell-secrets] ${title}"
    echo "${matches}"
    echo ""
    failures=1
  fi
}

# Hard fail on shell debug trace in committed scripts.
print_matches "disallowed shell tracing (set -x / xtrace) found:" '(^|\s)set\s+-x\b|xtrace'

# Hard fail on curl verbose output in committed scripts.
print_matches "disallowed verbose curl flags found:" '\bcurl\b[^\n]*(\s-v\b|\s--verbose\b)'

# Detect direct secret-variable printing to stdout/stderr.
# Lines annotated with '# allow-secret-print' are exempted.
secret_print_matches="$(
  rg -n --no-heading -S \
    '^\s*(echo|printf)\b.*(\$[{(]?[A-Za-z_][A-Za-z0-9_]*(SECRET|TOKEN|PASSWORD|API_KEY|PRIVATE_KEY|ACCESS_KEY|JWT)[A-Za-z0-9_]*[})]?)' \
    "${TARGETS[@]}" | rg -v 'allow-secret-print' || true
)"
if [[ -n "${secret_print_matches}" ]]; then
  echo "[shell-secrets] potential secret variable printing found:"
  echo "${secret_print_matches}"
  echo ""
  failures=1
fi

# Block known-bad static temp token dump patterns.
print_matches "static temp secret artifact patterns found:" '/tmp/.*(token|secret|credential).*\.(json|txt|log)'

if [[ "${failures}" -ne 0 ]]; then
  echo "[shell-secrets] FAIL: secret-leak guardrail violations detected."
  exit 1
fi

echo "[shell-secrets] PASS: no disallowed secret-leak patterns detected."
