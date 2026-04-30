# CI/CD Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add comprehensive CI/CD workflows to validate PRs and main pushes across all hippo components (Rust, Python, Firefox extension, shell scripts).

**Architecture:** Component-split GitHub Actions workflows with path filters, running on Blacksmith runners. Each component gets its own workflow file. A shell secret-leak scanner adapted from sjcarpenter/whistlepost provides security guardrails.

**Tech Stack:** GitHub Actions, Blacksmith runners, `dtolnay/rust-toolchain`, `useblacksmith/rust-cache@v3`, `taiki-e/install-action`, uv (Python), npm, ripgrep

**Spec:** `docs/superpowers/specs/2026-04-08-cicd-design.md`

---

## File Map

| Action | Path | Purpose |
|--------|------|---------|
| Create | `.github/workflows/rust.yml` | Rust format, clippy, test, audit |
| Create | `.github/workflows/python.yml` | Python ruff, pytest, pip-audit |
| Create | `.github/workflows/extension.yml` | Firefox extension typecheck + build |
| Create | `.github/workflows/security.yml` | Shell secret leak scanning |
| Create | `scripts/security/check-shell-secrets.sh` | Secret leak detection script |

Existing files (`claude.yml`, `claude-code-review.yml`) are not modified.

---

### Task 1: Security guardrails script

**Files:**
- Create: `scripts/security/check-shell-secrets.sh`

- [ ] **Step 1: Create the security directory**

```bash
mkdir -p scripts/security
```

- [ ] **Step 2: Write the check-shell-secrets.sh script**

Create `scripts/security/check-shell-secrets.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

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
done < <(rg --files -g '*.sh' scripts shell)

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
```

- [ ] **Step 3: Make it executable**

```bash
chmod +x scripts/security/check-shell-secrets.sh
```

- [ ] **Step 4: Run it locally to verify it passes on existing scripts**

Run: `./scripts/security/check-shell-secrets.sh`
Expected: `[shell-secrets] PASS: no disallowed secret-leak patterns detected.`

- [ ] **Step 5: Commit**

```bash
git add scripts/security/check-shell-secrets.sh
git commit -m "feat: add shell secret-leak scanner adapted from whistlepost"
```

---

### Task 2: Rust CI workflow

**Files:**
- Create: `.github/workflows/rust.yml`

- [ ] **Step 1: Write the workflow file**

Create `.github/workflows/rust.yml`:

```yaml
name: Rust CI

on:
  pull_request:
    paths:
      - "crates/**"
      - "Cargo.toml"
      - "Cargo.lock"
      - ".github/workflows/rust.yml"
  push:
    branches: [main]
    paths:
      - "crates/**"
      - "Cargo.toml"
      - "Cargo.lock"
      - ".github/workflows/rust.yml"

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

env:
  CARGO_TERM_COLOR: always
  CARGO_INCREMENTAL: 0
  CARGO_NET_RETRY: 10
  RUST_BACKTRACE: short
  RUSTUP_MAX_RETRIES: 10

permissions:
  contents: read

jobs:
  format:
    name: Format
    runs-on: blacksmith-2vcpu-ubuntu-2404
    if: github.event_name == 'pull_request'
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4

      - name: Install Rust toolchain
        uses: dtolnay/rust-toolchain@stable
        with:
          components: rustfmt

      - name: Check formatting
        run: cargo fmt --check

  build-and-test:
    name: Build & Test
    runs-on: blacksmith-4vcpu-ubuntu-2404
    timeout-minutes: 20
    steps:
      - uses: actions/checkout@v4

      - name: Install Rust toolchain
        uses: dtolnay/rust-toolchain@stable
        with:
          components: clippy

      - name: Setup Rust cache
        uses: useblacksmith/rust-cache@v3
        with:
          shared-key: rust-ci-${{ github.base_ref || github.ref_name }}
          cache-on-failure: true

      - name: Build all targets
        run: cargo build --all-targets --locked

      - name: Run clippy
        run: cargo clippy --all-targets --locked --no-deps -- -D warnings

      - name: Run tests
        run: cargo test --locked --no-fail-fast

      - name: Install cargo-audit
        uses: taiki-e/install-action@cargo-audit

      - name: Audit Rust dependencies
        run: cargo audit --file Cargo.lock
```

- [ ] **Step 2: Validate YAML syntax**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/rust.yml'))"`
Expected: No error (exits 0).

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/rust.yml
git commit -m "feat: add Rust CI workflow (format, clippy, test, audit)"
```

---

### Task 3: Python CI workflow

**Files:**
- Create: `.github/workflows/python.yml`

- [ ] **Step 1: Write the workflow file**

Create `.github/workflows/python.yml`:

```yaml
name: Python CI

on:
  pull_request:
    paths:
      - "brain/**"
      - ".github/workflows/python.yml"
  push:
    branches: [main]
    paths:
      - "brain/**"
      - ".github/workflows/python.yml"

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  lint-and-test:
    name: Lint & Test
    runs-on: blacksmith-4vcpu-ubuntu-2404
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0  # needed for git describe in version stamp

      - name: Install uv
        uses: astral-sh/setup-uv@v6

      - name: Install Python
        run: uv python install 3.14

      - name: Sync dependencies
        run: uv sync --project brain

      - name: Stamp version
        run: |
          set -euo pipefail
          BASE_VERSION=$(grep '^version' Cargo.toml | head -1 | cut -d'"' -f2)
          RAW=$(git describe --tags --always --dirty --match 'v*' 2>/dev/null || echo "unknown")

          if [ "$RAW" = "v${BASE_VERSION}" ]; then
              FULL_VERSION="$BASE_VERSION"
          elif [ "$RAW" = "v${BASE_VERSION}-dirty" ]; then
              FULL_VERSION="${BASE_VERSION}+dirty"
          elif [[ "$RAW" == v* ]]; then
              DIRTY=""
              CLEAN="$RAW"
              if [[ "$RAW" == *-dirty ]]; then
                  DIRTY=".dirty"
                  CLEAN="${RAW%-dirty}"
              fi
              COUNT=$(echo "$CLEAN" | sed 's/v[^-]*-\([0-9]*\)-.*/\1/')
              SHA=$(echo "$CLEAN" | sed 's/v[^-]*-[0-9]*-//')
              FULL_VERSION="${BASE_VERSION}-dev.${COUNT}+${SHA}${DIRTY}"
          else
              DIRTY=""
              SHA="$RAW"
              if [[ "$RAW" == *-dirty ]]; then
                  DIRTY=".dirty"
                  SHA="${RAW%-dirty}"
              fi
              FULL_VERSION="${BASE_VERSION}-dev+g${SHA}${DIRTY}"
          fi

          mkdir -p brain/src/hippo_brain
          echo "__version__ = \"${FULL_VERSION}\"" > brain/src/hippo_brain/_version.py
          echo "Stamped brain version: ${FULL_VERSION}"

      - name: Ruff check
        run: uv run --project brain ruff check brain/

      - name: Ruff format check
        run: uv run --project brain ruff format --check brain/src brain/tests

      - name: Run tests
        run: uv run --project brain pytest brain/tests -v --cov=hippo_brain --cov-report=term-missing

      - name: Install pip-audit
        run: uv tool install pip-audit

      - name: Audit Python dependencies
        run: uv run --project brain pip-audit
```

- [ ] **Step 2: Validate YAML syntax**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/python.yml'))"`
Expected: No error (exits 0).

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/python.yml
git commit -m "feat: add Python CI workflow (ruff, pytest, pip-audit)"
```

---

### Task 4: Firefox extension CI workflow

**Files:**
- Create: `.github/workflows/extension.yml`

- [ ] **Step 1: Write the workflow file**

Create `.github/workflows/extension.yml`:

```yaml
name: Extension CI

on:
  pull_request:
    paths:
      - "extension/firefox/**"
      - ".github/workflows/extension.yml"
  push:
    branches: [main]
    paths:
      - "extension/firefox/**"
      - ".github/workflows/extension.yml"

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  typecheck:
    name: Typecheck & Build
    runs-on: blacksmith-2vcpu-ubuntu-2404
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4

      - name: Install dependencies
        working-directory: extension/firefox
        run: npm ci

      - name: Typecheck and build
        working-directory: extension/firefox
        run: npm run build
```

- [ ] **Step 2: Validate YAML syntax**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/extension.yml'))"`
Expected: No error (exits 0).

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/extension.yml
git commit -m "feat: add Firefox extension CI workflow (typecheck + build)"
```

---

### Task 5: Security guardrails workflow

**Files:**
- Create: `.github/workflows/security.yml`

- [ ] **Step 1: Write the workflow file**

Create `.github/workflows/security.yml`:

```yaml
name: Security Guardrails

on:
  pull_request:
    paths:
      - "scripts/**"
      - "shell/**"
      - ".github/workflows/**"
  push:
    branches: [main]
    paths:
      - "scripts/**"
      - "shell/**"
      - ".github/workflows/**"

concurrency:
  group: ${{ github.workflow }}-${{ github.ref }}
  cancel-in-progress: true

permissions:
  contents: read

jobs:
  shell-secret-leak-check:
    name: Shell Secret Leak Check
    runs-on: blacksmith-2vcpu-ubuntu-2404
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4

      - name: Install ripgrep
        run: |
          sudo apt-get update
          sudo apt-get install -y ripgrep

      - name: Check shell scripts for secret leakage patterns
        run: ./scripts/security/check-shell-secrets.sh
```

- [ ] **Step 2: Validate YAML syntax**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/security.yml'))"`
Expected: No error (exits 0).

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/security.yml
git commit -m "feat: add security guardrails workflow (shell secret leak check)"
```

---

### Task 6: Validation — push a test branch and verify workflows trigger

- [ ] **Step 1: Create a test branch and push**

```bash
git checkout -b ci/add-workflows
git push -u origin ci/add-workflows
```

- [ ] **Step 2: Open a draft PR to main**

```bash
gh pr create --draft --title "feat: add CI/CD workflows" --body "Adds Rust, Python, extension, and security CI workflows on Blacksmith runners."
```

- [ ] **Step 3: Verify all 4 new workflows trigger**

Check the PR checks tab. Expected workflows:
- Rust CI (format + build-and-test)
- Python CI (lint-and-test)
- Extension CI (typecheck)
- Security Guardrails (shell-secret-leak-check)

Run: `gh pr checks` to see status.

- [ ] **Step 4: Fix any failures**

If any workflow fails, read the logs:
```bash
gh run view <run-id> --log-failed
```

Fix the issue, commit, push. The concurrency group will cancel the old run.

- [ ] **Step 5: Mark PR ready and merge once all checks pass**

```bash
gh pr ready
gh pr merge --merge
```
