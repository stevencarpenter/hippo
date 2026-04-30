# Enrichment Pipeline Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the enrichment pipeline to produce high-quality, specific knowledge nodes by adding stdout capture, session-based grouping, a redesigned prompt, and semantic vector search.

**Architecture:** Shell hook captures stdout/stderr and passes to daemon. Brain groups events by session, enriches with richer prompt via qwen3.5-35b-a3b, embeds via nomic v2, and serves semantic search via the /query endpoint.

**Tech Stack:** Rust (clap, serde, reqwest), Python (starlette, lancedb, httpx), zsh, mise

**Spec:** `docs/superpowers/specs/2026-03-29-enrichment-pipeline-redesign.md`

---

## Parallelism Map

```
Phase 1 (parallel):  Task 1 (config) | Task 2 (models.py)
Phase 2 (parallel):  Task 3 (shell capture) | Task 4 (enrichment grouping) | Task 5 (LanceDB schema)
Phase 3 (parallel):  Task 6 (semantic search endpoint) | Task 7 (CLI query display) | Task 8 (mise re-enrich)
Phase 4 (sequential): Task 9 (verification)
```

Each task owns specific files. No file conflicts between parallel tasks in the same phase.

---

## File Map

| File | Task | Responsibility |
|------|------|---------------|
| `crates/hippo-core/src/config.rs` | 1 | Add output capture and session grouping config fields |
| `config/config.default.toml` | 1 | Update defaults for new config keys and models |
| `brain/src/hippo_brain/__init__.py` | 1 | Pass new config fields through to create_app |
| `brain/src/hippo_brain/models.py` | 2 | Add key_decisions, problems_encountered; drop relationships |
| `shell/hippo.zsh` | 3 | Capture stdout/stderr, truncate, pass to CLI |
| `crates/hippo-daemon/src/cli.rs` | 3 | Add --output flag to SendEventSource::Shell |
| `crates/hippo-daemon/src/commands.rs` (send-event area) | 3 | Wire output into CapturedOutput |
| `crates/hippo-daemon/src/main.rs` (send-event area) | 3 | Pass output flag through |
| `brain/src/hippo_brain/enrichment.py` | 4 | Session grouping, chunking, new prompt, stdout in prompt |
| `brain/src/hippo_brain/embeddings.py` | 5 | Add key_decisions, problems_encountered to LanceDB schema |
| `brain/src/hippo_brain/server.py` | 6 | Semantic search in /query, session-based enrichment loop |
| `crates/hippo-daemon/src/main.rs` (query area) | 7 | Semantic result display with scores |
| `mise.toml` | 8 | Add re-enrich task |

---

### Task 1: Config updates

**Files:**
- Modify: `crates/hippo-core/src/config.rs:47-110`
- Modify: `config/config.default.toml`
- Modify: `brain/src/hippo_brain/__init__.py`

**Important context:** The existing `DaemonConfig` struct is at line 47, `BrainConfig` at line 77. Both have `Default` impls. The Python `__init__.py` loads config and passes it to `create_app`.

- [ ] **Step 1: Add daemon output capture config fields**

In `crates/hippo-core/src/config.rs`, add two fields to `DaemonConfig` (line 47):

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DaemonConfig {
    #[serde(default = "default_flush_interval_ms")]
    pub flush_interval_ms: u64,
    #[serde(default = "default_flush_batch_size")]
    pub flush_batch_size: usize,
    #[serde(default = "default_socket_timeout_ms")]
    pub socket_timeout_ms: u64,
    #[serde(default = "default_output_head_lines")]
    pub output_head_lines: usize,
    #[serde(default = "default_output_tail_lines")]
    pub output_tail_lines: usize,
}
```

Add default functions after line 65:

```rust
fn default_output_head_lines() -> usize {
    50
}
fn default_output_tail_lines() -> usize {
    100
}
```

Update the `Default` impl (line 67):

```rust
impl Default for DaemonConfig {
    fn default() -> Self {
        Self {
            flush_interval_ms: default_flush_interval_ms(),
            flush_batch_size: default_flush_batch_size(),
            socket_timeout_ms: default_socket_timeout_ms(),
            output_head_lines: default_output_head_lines(),
            output_tail_lines: default_output_tail_lines(),
        }
    }
}
```

- [ ] **Step 2: Add brain session grouping config fields**

Add two fields to `BrainConfig` (line 77):

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrainConfig {
    #[serde(default = "default_brain_port")]
    pub port: u16,
    #[serde(default = "default_poll_interval_secs")]
    pub poll_interval_secs: u64,
    #[serde(default = "default_enrichment_batch_size")]
    pub enrichment_batch_size: usize,
    #[serde(default = "default_max_queue_depth")]
    pub max_queue_depth: usize,
    #[serde(default = "default_max_events_per_chunk")]
    pub max_events_per_chunk: usize,
    #[serde(default = "default_session_stale_secs")]
    pub session_stale_secs: u64,
}
```

Add defaults:

```rust
fn default_max_events_per_chunk() -> usize {
    30
}
fn default_session_stale_secs() -> u64 {
    120
}
```

Update `Default` impl:

```rust
impl Default for BrainConfig {
    fn default() -> Self {
        Self {
            port: default_brain_port(),
            poll_interval_secs: default_poll_interval_secs(),
            enrichment_batch_size: default_enrichment_batch_size(),
            max_queue_depth: default_max_queue_depth(),
            max_events_per_chunk: default_max_events_per_chunk(),
            session_stale_secs: default_session_stale_secs(),
        }
    }
}
```

- [ ] **Step 3: Update config.default.toml**

Replace the full contents of `config/config.default.toml`:

```toml
[lmstudio]
base_url = "http://localhost:1234/v1"  # LM Studio local server

[models]
enrichment = "qwen/qwen3.5-35b-a3b"                    # MoE: 35B total, 3B active
query = "qwen/qwen3.5-35b-a3b"                          # Same model for query reformulation
embedding = "text-embedding-nomic-embed-text-v2-moe"     # Purpose-built embedding model, 768d

[daemon]
flush_interval_ms = 100    # Write buffer flush interval (ms)
flush_batch_size = 50      # Max events per flush
socket_timeout_ms = 100    # CLI-to-daemon socket timeout (ms)
output_head_lines = 50     # First N lines of stdout/stderr to capture
output_tail_lines = 100    # Last N lines of stdout/stderr to capture

[brain]
port = 9175                    # Brain query server HTTP port
poll_interval_secs = 5         # Enrichment queue poll interval
enrichment_batch_size = 10     # Deprecated: use max_events_per_chunk
max_events_per_chunk = 30      # Max events per enrichment call
max_queue_depth = 100          # Max pending enrichment items
session_stale_secs = 120       # Wait before enriching a session

[storage]
# data_dir = "~/.local/share/hippo"    # Override data directory
# config_dir = "~/.config/hippo"       # Override config directory
```

- [ ] **Step 4: Update Python __init__.py**

In `brain/src/hippo_brain/__init__.py`, add new config fields to `_load_runtime_settings()` return dict (around line 30):

```python
        "max_events_per_chunk": brain.get("max_events_per_chunk", brain.get("enrichment_batch_size", 10)),
        "session_stale_secs": brain.get("session_stale_secs", 120),
```

Update the `create_app()` call (around line 55):

```python
        app = create_app(
            db_path=settings["db_path"],
            data_dir=settings["data_dir"],
            lmstudio_base_url=settings["lmstudio_base_url"],
            enrichment_model=settings["enrichment_model"],
            embedding_model=settings["embedding_model"],
            poll_interval_secs=settings["poll_interval_secs"],
            enrichment_batch_size=settings["max_events_per_chunk"],
            session_stale_secs=settings["session_stale_secs"],
        )
```

- [ ] **Step 5: Run Rust tests**

Run: `cd ~/projects/hippo && cargo test -p hippo-core && cargo clippy --all-targets -- -D warnings`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add crates/hippo-core/src/config.rs config/config.default.toml brain/src/hippo_brain/__init__.py
git commit -m "feat(config): add output capture and session grouping config fields"
```

---

### Task 2: Update enrichment models

**Files:**
- Modify: `brain/src/hippo_brain/models.py`

**Important context:** `EnrichmentResult` dataclass is at line 4. `validate_enrichment_data` at line 27. `ENRICHMENT_SCHEMA` at line 79. `ENRICHMENT_FIXTURES` at line 112. We're adding `key_decisions` and `problems_encountered`, and removing `relationships`.

- [ ] **Step 1: Update EnrichmentResult dataclass**

Replace the dataclass (line 4):

```python
@dataclass
class EnrichmentResult:
    summary: str
    intent: str
    outcome: str
    entities: dict = field(
        default_factory=lambda: {
            "projects": [],
            "tools": [],
            "files": [],
            "services": [],
            "errors": [],
        }
    )
    tags: list = field(default_factory=list)
    embed_text: str = ""
    key_decisions: list = field(default_factory=list)
    problems_encountered: list = field(default_factory=list)
```

- [ ] **Step 2: Update validate_enrichment_data**

Replace the function (line 27):

```python
def validate_enrichment_data(data: dict) -> EnrichmentResult:
    """Validate raw enrichment JSON and return a typed EnrichmentResult."""
    for field_name in ("summary", "intent", "embed_text"):
        value = data.get(field_name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"required string field '{field_name}' is missing or empty")

    outcome = data.get("outcome")
    if outcome not in _VALID_OUTCOMES:
        raise ValueError(f"outcome must be one of {sorted(_VALID_OUTCOMES)}, got {outcome!r}")

    raw_entities = data.get("entities", {})
    if not isinstance(raw_entities, dict):
        raise ValueError(f"entities must be a dict, got {type(raw_entities).__name__}")

    entities: dict[str, list[str]] = {}
    for key in _ENTITY_KEYS:
        raw_list = raw_entities.get(key, [])
        if not isinstance(raw_list, list):
            raw_list = []
        entities[key] = [item for item in raw_list if isinstance(item, str)]

    raw_tags = data.get("tags", [])
    if not isinstance(raw_tags, list):
        raw_tags = []
    tags = [t for t in raw_tags if isinstance(t, str)]

    raw_decisions = data.get("key_decisions", [])
    if not isinstance(raw_decisions, list):
        raw_decisions = []
    key_decisions = [d for d in raw_decisions if isinstance(d, str)]

    raw_problems = data.get("problems_encountered", [])
    if not isinstance(raw_problems, list):
        raw_problems = []
    problems_encountered = [p for p in raw_problems if isinstance(p, str)]

    return EnrichmentResult(
        summary=data["summary"],
        intent=data["intent"],
        outcome=outcome,
        entities=entities,
        tags=tags,
        embed_text=data["embed_text"],
        key_decisions=key_decisions,
        problems_encountered=problems_encountered,
    )
```

- [ ] **Step 3: Update ENRICHMENT_SCHEMA and ENRICHMENT_FIXTURES**

Replace `ENRICHMENT_SCHEMA` (line 79):

```python
ENRICHMENT_SCHEMA = {
    "type": "object",
    "required": ["summary", "intent", "outcome", "entities", "tags", "embed_text"],
    "properties": {
        "summary": {"type": "string"},
        "intent": {"type": "string"},
        "outcome": {"type": "string", "enum": ["success", "partial", "failure", "unknown"]},
        "entities": {
            "type": "object",
            "properties": {
                "projects": {"type": "array", "items": {"type": "string"}},
                "tools": {"type": "array", "items": {"type": "string"}},
                "files": {"type": "array", "items": {"type": "string"}},
                "services": {"type": "array", "items": {"type": "string"}},
                "errors": {"type": "array", "items": {"type": "string"}},
            },
        },
        "tags": {"type": "array", "items": {"type": "string"}},
        "embed_text": {"type": "string"},
        "key_decisions": {"type": "array", "items": {"type": "string"}},
        "problems_encountered": {"type": "array", "items": {"type": "string"}},
    },
}

ENRICHMENT_FIXTURES = [
    {
        "input": {
            "command": "cargo test -p hippo-core",
            "exit_code": 0,
            "duration_ms": 3500,
            "cwd": "/Users/dev/projects/hippo",
            "git_branch": "main",
        },
        "expected": EnrichmentResult(
            summary="Ran Rust unit tests for hippo-core crate, all tests passed.",
            intent="testing",
            outcome="success",
            entities={
                "projects": ["hippo"],
                "tools": ["cargo", "rustc"],
                "files": [],
                "services": [],
                "errors": [],
            },
            tags=["rust", "testing", "hippo-core"],
            embed_text="cargo test hippo-core: all tests passed in hippo project on main branch",
            key_decisions=[],
            problems_encountered=[],
        ),
    },
]
```

- [ ] **Step 4: Run Python tests**

Run: `cd ~/projects/hippo && uv run --project brain pytest brain/tests -v`
Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/models.py
git commit -m "feat(models): add key_decisions and problems_encountered; drop relationships"
```

---

### Task 3: Shell stdout/stderr capture

**Files:**
- Modify: `shell/hippo.zsh`
- Modify: `crates/hippo-daemon/src/cli.rs:117-141`
- Modify: `crates/hippo-daemon/src/commands.rs:251-316`
- Modify: `crates/hippo-daemon/src/main.rs:184-206`

**Important context:** The Rust types already have `stdout: Option<CapturedOutput>` in `ShellEvent` and the DB schema has `stdout TEXT`. The `CapturedOutput` struct has `content: String`, `truncated: bool`, `original_bytes: usize`. Currently `handle_send_event_shell` hardcodes `stdout: None, stderr: None`.

- [ ] **Step 1: Add --output flag to CLI**

In `crates/hippo-daemon/src/cli.rs`, add to `SendEventSource::Shell` (after `git_dirty` around line 140):

```rust
        /// Captured stdout+stderr (truncated)
        #[arg(long)]
        output: Option<String>,
```

- [ ] **Step 2: Wire output through main.rs**

In `crates/hippo-daemon/src/main.rs`, update the `SendEventSource::Shell` destructuring (around line 185) to include `output`:

```rust
            SendEventSource::Shell {
                cmd,
                exit,
                cwd,
                duration_ms,
                git_branch,
                git_commit,
                git_dirty,
                output,
            } => {
                commands::handle_send_event_shell(
                    &config,
                    cmd,
                    exit,
                    cwd,
                    duration_ms,
                    git_branch,
                    git_commit,
                    git_dirty,
                    output,
                )
                .await?;
            }
```

- [ ] **Step 3: Update handle_send_event_shell**

In `crates/hippo-daemon/src/commands.rs`, update the function signature (line 251) to accept `output: Option<String>`:

```rust
#[allow(clippy::too_many_arguments)]
pub async fn handle_send_event_shell(
    config: &HippoConfig,
    cmd: String,
    exit: i32,
    cwd: String,
    duration_ms: u64,
    git_branch: Option<String>,
    git_commit: Option<String>,
    git_dirty: bool,
    output: Option<String>,
) -> Result<()> {
```

Where `ShellEvent` is constructed (around line 285), change `stdout: None, stderr: None` to:

```rust
        stdout: output.as_ref().map(|o| CapturedOutput {
            content: o.clone(),
            truncated: false,
            original_bytes: o.len(),
        }),
        stderr: None,
```

Add the import at the top of the file:
```rust
use hippo_core::events::CapturedOutput;
```

- [ ] **Step 4: Update the shell hook**

Replace the contents of `shell/hippo.zsh`:

```zsh
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

# Output capture config
typeset -g _HIPPO_OUTPUT_HEAD=${HIPPO_OUTPUT_HEAD_LINES:-50}
typeset -g _HIPPO_OUTPUT_TAIL=${HIPPO_OUTPUT_TAIL_LINES:-100}
typeset -g _HIPPO_OUTPUT_FILE="/tmp/hippo-output.$$"

# Truncate captured output: first N + last M lines
_hippo_truncate_output() {
    local file="$1"
    local head_n="${_HIPPO_OUTPUT_HEAD}"
    local tail_n="${_HIPPO_OUTPUT_TAIL}"

    [[ ! -f "$file" ]] && return
    local total
    total=$(wc -l < "$file" 2>/dev/null)
    total=${total##* }

    if (( total <= head_n + tail_n )); then
        cat "$file"
    else
        local omitted=$(( total - head_n - tail_n ))
        head -n "$head_n" "$file"
        echo "... ($omitted lines omitted) ..."
        tail -n "$tail_n" "$file"
    fi
}

# Preexec: capture command and start time
_hippo_preexec() {
    _HIPPO_CMD="$1"
    _HIPPO_CWD="$PWD"
    _HIPPO_START="${EPOCHREALTIME}"
    : > "${_HIPPO_OUTPUT_FILE}" 2>/dev/null
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

    # Attach truncated output if capture file has content
    if [[ -s "${_HIPPO_OUTPUT_FILE}" ]]; then
        local truncated
        truncated="$(_hippo_truncate_output "${_HIPPO_OUTPUT_FILE}")"
        if [[ -n "${truncated}" ]]; then
            args+=(--output "${truncated}")
        fi
    fi

    # Fire and forget
    hippo "${args[@]}" &>/dev/null &!

    # Clean up temp vars
    unset _HIPPO_CMD _HIPPO_CWD _HIPPO_START
}

add-zsh-hook preexec _hippo_preexec
add-zsh-hook precmd _hippo_precmd
```

- [ ] **Step 5: Build and run Rust tests**

Run: `cd ~/projects/hippo && cargo test && cargo clippy --all-targets -- -D warnings`
Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add shell/hippo.zsh crates/hippo-daemon/src/cli.rs crates/hippo-daemon/src/commands.rs crates/hippo-daemon/src/main.rs
git commit -m "feat(capture): add stdout/stderr output capture to shell hook and CLI"
```

---

### Task 4: Enrichment session grouping and prompt redesign

**Files:**
- Modify: `brain/src/hippo_brain/enrichment.py`

**Important context:** Current `claim_pending_events` is at line 76. `SYSTEM_PROMPT` at line 10. `build_enrichment_prompt` at line 39. `write_knowledge_node` at line 140. The current prompt only sees command, exit code, cwd, git info. Events table already has `stdout` and `stderr` columns.

- [ ] **Step 1: Replace SYSTEM_PROMPT**

Replace `SYSTEM_PROMPT` (line 10):

```python
SYSTEM_PROMPT = """You are a developer activity analyst. You receive a sequence of shell command events from a single work session and produce structured enrichment data.

Events are labeled with who executed them: "developer (human)" for commands the user typed,
or "Claude Code (AI agent)" for commands executed by an AI coding assistant. Reflect this
distinction in your summary — attribute actions to the correct actor.

IMPORTANT: Be specific. Use actual file names, function names, error messages, and outcomes from the event data. Generic descriptions like "edited a Rust file" are unacceptable. Instead say "added build.rs to hippo-daemon that embeds git metadata via cargo:rustc-env".

The embed_text field should read like a developer's work log entry — specific enough that searching for "embedding model configuration" or "clippy warning fix" would find it.

Output a JSON object with these fields:
- summary: Specific description of what was accomplished (not what tools were used)
- intent: The developer's goal (e.g., "testing", "debugging", "deploying", "refactoring")
- outcome: One of "success", "partial", "failure", "unknown"
- key_decisions: List of decisions made and why (e.g., "Chose build.rs over vergen crate for zero dependencies")
- problems_encountered: List of errors/failures and how they were resolved
- entities: An object with lists of extracted entities:
  - projects: Project names mentioned or inferred
  - tools: CLI tools used (cargo, npm, git, docker, etc.)
  - files: Specific files referenced (use actual paths from the events)
  - services: Services interacted with (databases, APIs, etc.)
  - errors: Actual error messages encountered (not generic descriptions)
- tags: Descriptive, specific tags (not "success" or "editing")
- embed_text: A detailed paragraph a developer would write in a work log. Specific file names, error messages, and outcomes. Optimized for semantic search.

Output ONLY valid JSON, no markdown fences or extra text."""
```

- [ ] **Step 2: Update build_enrichment_prompt to include stdout/stderr**

Replace `build_enrichment_prompt` (line 39):

```python
def build_enrichment_prompt(events: list[dict]) -> str:
    """Format events into the user prompt template."""
    lines = []
    for i, ev in enumerate(events, 1):
        actor = _actor_label(ev.get("shell", ""))
        parts = [f"Event {i} (executed by {actor}):"]
        parts.append(f"  command: {ev.get('command', '')}")
        parts.append(f"  exit_code: {ev.get('exit_code', '')}")
        parts.append(f"  duration_ms: {ev.get('duration_ms', '')}")
        parts.append(f"  cwd: {ev.get('cwd', '')}")
        if ev.get("git_branch"):
            parts.append(f"  git_branch: {ev['git_branch']}")
        if ev.get("git_commit"):
            parts.append(f"  git_commit: {ev['git_commit']}")
        if ev.get("git_repo"):
            parts.append(f"  git_repo: {ev['git_repo']}")
        if ev.get("stdout"):
            parts.append(f"  stdout:\n{ev['stdout']}")
        if ev.get("stderr"):
            parts.append(f"  stderr:\n{ev['stderr']}")
        lines.append("\n".join(parts))
    return "\n\n".join(lines)
```

- [ ] **Step 3: Add session-aware claiming function**

Add after `claim_pending_events` (keep the old function):

```python
def claim_pending_events_by_session(
    conn, max_per_chunk: int, worker_id: str, stale_secs: int = 120
) -> list[list[dict]]:
    """Claim pending events grouped by session. Returns list of event chunks.

    Only processes sessions where the last event is older than stale_secs.
    Long sessions are split into chunks at time gaps > 60s or at max_per_chunk.
    """
    now_ms = int(time.time() * 1000)
    stale_threshold_ms = now_ms - (stale_secs * 1000)
    stale_lock_ms = now_ms - STALE_LOCK_TIMEOUT_MS

    cursor = conn.execute(
        """
        SELECT e.session_id, COUNT(*) as cnt
        FROM enrichment_queue eq
        JOIN events e ON eq.event_id = e.id
        WHERE eq.status = 'pending'
           OR (eq.status = 'processing' AND COALESCE(eq.locked_at, 0) <= ?)
        GROUP BY e.session_id
        HAVING MAX(e.timestamp) < ?
        ORDER BY MIN(e.timestamp) ASC
        """,
        (stale_lock_ms, stale_threshold_ms),
    )
    sessions = cursor.fetchall()

    all_chunks = []
    for session_id, _ in sessions:
        cursor = conn.execute(
            """
            UPDATE enrichment_queue
            SET status = 'processing', locked_at = ?, locked_by = ?, updated_at = ?
            WHERE id IN (
                SELECT eq.id FROM enrichment_queue eq
                JOIN events e ON eq.event_id = e.id
                WHERE e.session_id = ?
                  AND (eq.status = 'pending'
                       OR (eq.status = 'processing' AND COALESCE(eq.locked_at, 0) <= ?))
            )
            RETURNING event_id
            """,
            (now_ms, worker_id, now_ms, session_id, stale_lock_ms),
        )
        event_ids = [row[0] for row in cursor.fetchall()]
        conn.commit()

        if not event_ids:
            continue

        placeholders = ",".join("?" * len(event_ids))
        cursor = conn.execute(
            f"""
            SELECT id, session_id, timestamp, command, exit_code, duration_ms,
                   cwd, hostname, shell, git_repo, git_branch, git_commit, git_dirty,
                   stdout, stderr
            FROM events
            WHERE id IN ({placeholders})
            ORDER BY timestamp ASC
            """,
            event_ids,
        )

        events = []
        for row in cursor.fetchall():
            events.append({
                "id": row[0], "session_id": row[1], "timestamp": row[2],
                "command": row[3], "exit_code": row[4], "duration_ms": row[5],
                "cwd": row[6], "hostname": row[7], "shell": row[8],
                "git_repo": row[9], "git_branch": row[10], "git_commit": row[11],
                "git_dirty": row[12], "stdout": row[13], "stderr": row[14],
            })

        chunks = _chunk_events(events, max_per_chunk)
        all_chunks.extend(chunks)

    return all_chunks


def _chunk_events(events: list[dict], max_size: int) -> list[list[dict]]:
    """Split events into chunks at time gaps > 60s or at max_size."""
    if len(events) <= max_size:
        return [events]

    TIME_GAP_MS = 60_000
    chunks = []
    current = [events[0]]

    for ev in events[1:]:
        prev_ts = current[-1]["timestamp"]
        gap = ev["timestamp"] - prev_ts
        if gap > TIME_GAP_MS or len(current) >= max_size:
            chunks.append(current)
            current = [ev]
        else:
            current.append(ev)

    if current:
        chunks.append(current)
    return chunks
```

- [ ] **Step 4: Update write_knowledge_node — new fields, drop relationships**

In `write_knowledge_node` (line 140), update the `content` JSON (around line 150):

```python
    content = json.dumps(
        {
            "summary": result.summary,
            "intent": result.intent,
            "outcome": result.outcome,
            "entities": result.entities,
            "tags": result.tags,
            "key_decisions": result.key_decisions,
            "problems_encountered": result.problems_encountered,
        }
    )
```

Delete the entire "Populate relationships table" block (lines 222-242).

- [ ] **Step 5: Run Python tests and lint**

Run: `cd ~/projects/hippo && uv run --project brain pytest brain/tests -v && uv run --project brain ruff check brain/ && uv run --project brain ruff format --check brain/`
Expected: All pass, clean lint.

- [ ] **Step 6: Commit**

```bash
git add brain/src/hippo_brain/enrichment.py
git commit -m "feat(enrichment): session-based grouping, stdout in prompt, redesigned prompt"
```

---

### Task 5: LanceDB schema update

**Files:**
- Modify: `brain/src/hippo_brain/embeddings.py`

- [ ] **Step 1: Update KNOWLEDGE_SCHEMA**

Replace `KNOWLEDGE_SCHEMA` (line 8):

```python
KNOWLEDGE_SCHEMA = pa.schema(
    [
        pa.field("id", pa.int64()),
        pa.field("session_id", pa.int64()),
        pa.field("captured_at", pa.int64()),
        pa.field("commands_raw", pa.string()),
        pa.field("cwd", pa.string()),
        pa.field("git_branch", pa.string()),
        pa.field("git_repo", pa.string()),
        pa.field("outcome", pa.string()),
        pa.field("tags", pa.string()),
        pa.field("entities_json", pa.string()),
        pa.field("embed_text", pa.string()),
        pa.field("summary", pa.string()),
        pa.field("key_decisions", pa.string()),
        pa.field("problems_encountered", pa.string()),
        pa.field("vec_knowledge", pa.list_(pa.float32(), EMBED_DIM)),
        pa.field("vec_command", pa.list_(pa.float32(), EMBED_DIM)),
        pa.field("enrichment_model", pa.string()),
    ]
)
```

- [ ] **Step 2: Update embed_knowledge_node row dict**

Replace the `row` dict in `embed_knowledge_node` (line 74):

```python
    row = {
        "id": node_dict.get("id", 0),
        "session_id": node_dict.get("session_id", 0),
        "captured_at": node_dict.get("captured_at", 0),
        "commands_raw": commands_raw,
        "cwd": node_dict.get("cwd", ""),
        "git_branch": node_dict.get("git_branch", ""),
        "git_repo": node_dict.get("git_repo", ""),
        "outcome": node_dict.get("outcome", ""),
        "tags": json.dumps(node_dict.get("tags", [])),
        "entities_json": json.dumps(node_dict.get("entities", {})),
        "embed_text": embed_text,
        "summary": node_dict.get("summary", ""),
        "key_decisions": json.dumps(node_dict.get("key_decisions", [])),
        "problems_encountered": json.dumps(node_dict.get("problems_encountered", [])),
        "vec_knowledge": vec_knowledge,
        "vec_command": vec_command,
        "enrichment_model": node_dict.get("enrichment_model", ""),
    }
```

- [ ] **Step 3: Run tests**

Run: `cd ~/projects/hippo && uv run --project brain pytest brain/tests -v`
Expected: All pass.

- [ ] **Step 4: Commit**

```bash
git add brain/src/hippo_brain/embeddings.py
git commit -m "feat(embeddings): add key_decisions and problems_encountered to LanceDB schema"
```

---

### Task 6: Semantic search endpoint and enrichment loop update

**Files:**
- Modify: `brain/src/hippo_brain/server.py`

**Important context:** `BrainServer.__init__` at line 30. `query` method at line 119. `_enrichment_loop` at line 162. `create_app` at line 267.

- [ ] **Step 1: Update BrainServer.__init__ to accept session_stale_secs**

Add `session_stale_secs: int = 120` to the `__init__` signature (line 30) and store it:

```python
        self.session_stale_secs = session_stale_secs
```

- [ ] **Step 2: Replace query method with semantic search**

Replace the `query` method and add `_query_lexical` helper. Remove the old `query` method (lines 119-161) and the comment above it (lines 116-118). Replace with:

```python
    async def query(self, request: Request) -> JSONResponse:
        body = await request.json()
        text = body.get("text", "")
        mode = body.get("mode", "semantic")
        if not text:
            return JSONResponse({"error": "text is required"}, status_code=400)

        if mode == "lexical":
            return await self._query_lexical(text)

        if not self.embedding_model or not self._vector_table:
            logger.warning("semantic search unavailable, falling back to lexical")
            return await self._query_lexical(
                text, warning="semantic search unavailable, using lexical"
            )

        try:
            query_vecs = await self.client.embed([text], model=self.embedding_model)
            from hippo_brain.embeddings import search_similar

            results = search_similar(self._vector_table, query_vecs[0], limit=10)

            hits = []
            for r in results:
                hits.append(
                    {
                        "score": round(1 - r.get("_distance", 0), 3),
                        "summary": r.get("summary", ""),
                        "embed_text": r.get("embed_text", ""),
                        "tags": r.get("tags", "[]"),
                        "key_decisions": r.get("key_decisions", "[]"),
                        "problems_encountered": r.get("problems_encountered", "[]"),
                        "cwd": r.get("cwd", ""),
                        "git_branch": r.get("git_branch", ""),
                        "session_id": r.get("session_id", 0),
                        "commands_raw": r.get("commands_raw", ""),
                    }
                )

            return JSONResponse({"mode": "semantic", "results": hits})
        except Exception as e:
            logger.error("semantic search failed, falling back to lexical: %s", e)
            return await self._query_lexical(text, warning=f"semantic search failed: {e}")

    async def _query_lexical(
        self, text: str, warning: str | None = None
    ) -> JSONResponse:
        try:
            conn = self._get_conn()
            pattern = f"%{text}%"

            cursor = conn.execute(
                """SELECT id, command, cwd, timestamp
                   FROM events
                   WHERE command LIKE ?
                   ORDER BY timestamp DESC LIMIT 10""",
                (pattern,),
            )
            events = [
                {"event_id": r[0], "command": r[1], "cwd": r[2], "timestamp": r[3]}
                for r in cursor.fetchall()
            ]

            cursor = conn.execute(
                """SELECT id, uuid, content, embed_text
                   FROM knowledge_nodes
                   WHERE content LIKE ?
                      OR embed_text LIKE ?
                   ORDER BY created_at DESC LIMIT 10""",
                (pattern, pattern),
            )
            nodes = [
                {"id": r[0], "uuid": r[1], "content": r[2], "embed_text": r[3]}
                for r in cursor.fetchall()
            ]

            conn.close()
            result: dict = {"mode": "lexical", "events": events, "nodes": nodes}
            if warning:
                result["warning"] = warning
            return JSONResponse(result)
        except Exception as e:
            logger.error("query error: %s", e)
            return JSONResponse({"error": str(e)}, status_code=500)
```

- [ ] **Step 3: Update enrichment loop to use session grouping**

In `_enrichment_loop` (line 162), replace the event claiming section. Change:

```python
                    conn = self._get_conn()
                    events = claim_pending_events(conn, self.enrichment_batch_size, worker_id)
                    if not events:
                        conn.close()
                        continue

                    event_ids = [e["id"] for e in events]
                    logger.info("claimed %d events: %s", len(event_ids), event_ids)
```

To:

```python
                    conn = self._get_conn()
                    chunks = claim_pending_events_by_session(
                        conn, self.enrichment_batch_size, worker_id, self.session_stale_secs
                    )
                    if not chunks:
                        conn.close()
                        continue

                    for events in chunks:
                        event_ids = [e["id"] for e in events]
                        logger.info("claimed %d events (session): %s", len(event_ids), event_ids)
```

Then indent the rest of the enrichment processing (prompt building, LLM call, knowledge node write, embedding) inside the `for events in chunks:` loop.

Update the import at the top of the file to include `claim_pending_events_by_session`:

```python
from hippo_brain.enrichment import (
    SYSTEM_PROMPT,
    build_enrichment_prompt,
    claim_pending_events,
    claim_pending_events_by_session,
    mark_queue_failed,
    parse_enrichment_response,
    write_knowledge_node,
)
```

- [ ] **Step 4: Update create_app**

Add `session_stale_secs` parameter to `create_app` (line 267) and pass through:

```python
def create_app(
    db_path: str = "",
    data_dir: str = "",
    lmstudio_base_url: str = "http://localhost:1234/v1",
    enrichment_model: str = "",
    embedding_model: str = "",
    poll_interval_secs: int = 5,
    enrichment_batch_size: int = 10,
    session_stale_secs: int = 120,
) -> Starlette:
    server = BrainServer(
        db_path=db_path,
        data_dir=data_dir,
        lmstudio_base_url=lmstudio_base_url,
        enrichment_model=enrichment_model,
        embedding_model=embedding_model,
        poll_interval_secs=poll_interval_secs,
        enrichment_batch_size=enrichment_batch_size,
        session_stale_secs=session_stale_secs,
    )
```

- [ ] **Step 5: Also pass key_decisions and problems_encountered in the enrichment loop's node_dict**

In the enrichment loop where `node_dict` is built (around line 200), add the new fields:

```python
                                node_dict = {
                                    "id": node_id,
                                    "session_id": events[0].get("session_id", 0),
                                    "captured_at": int(time.time() * 1000),
                                    "commands_raw": " ; ".join(
                                        e.get("command", "") for e in events
                                    ),
                                    "cwd": events[0].get("cwd", ""),
                                    "git_branch": events[0].get("git_branch", ""),
                                    "git_repo": "",
                                    "outcome": result.outcome,
                                    "tags": result.tags,
                                    "entities": result.entities
                                    if isinstance(result.entities, dict)
                                    else {},
                                    "embed_text": result.embed_text,
                                    "summary": result.summary,
                                    "key_decisions": result.key_decisions,
                                    "problems_encountered": result.problems_encountered,
                                    "enrichment_model": self.enrichment_model,
                                }
```

- [ ] **Step 6: Run tests and lint**

Run: `cd ~/projects/hippo && uv run --project brain pytest brain/tests -v && uv run --project brain ruff check brain/ && uv run --project brain ruff format --check brain/`
Expected: All pass. Some query tests may need updating for the new response format.

- [ ] **Step 7: Commit**

```bash
git add brain/src/hippo_brain/server.py
git commit -m "feat(search): semantic vector search in /query; session-based enrichment loop"
```

---

### Task 7: CLI semantic query display

**Files:**
- Modify: `crates/hippo-daemon/src/main.rs:220-244`

- [ ] **Step 1: Replace query handler with semantic-aware display**

Replace the `Commands::Query` match arm (line 220):

```rust
        Commands::Query { text, raw } => {
            if raw {
                commands::handle_query_raw(&config, &text).await?;
            } else {
                let brain_url = format!("http://localhost:{}/query", config.brain.port);
                let client = reqwest::Client::new();
                match client
                    .post(&brain_url)
                    .json(&serde_json::json!({"text": text, "mode": "semantic"}))
                    .timeout(std::time::Duration::from_secs(10))
                    .send()
                    .await
                {
                    Ok(resp) if resp.status().is_success() => {
                        let body: serde_json::Value = resp.json().await?;
                        if let Some(warning) = body.get("warning").and_then(|w| w.as_str()) {
                            eprintln!("Warning: {}", warning);
                        }
                        let mode =
                            body.get("mode").and_then(|m| m.as_str()).unwrap_or("unknown");
                        if mode == "semantic" {
                            if let Some(results) =
                                body.get("results").and_then(|r| r.as_array())
                            {
                                if results.is_empty() {
                                    println!("No results found.");
                                } else {
                                    for r in results {
                                        let score = r
                                            .get("score")
                                            .and_then(|s| s.as_f64())
                                            .unwrap_or(0.0);
                                        let summary = r
                                            .get("summary")
                                            .and_then(|s| s.as_str())
                                            .unwrap_or("");
                                        let tags = r
                                            .get("tags")
                                            .and_then(|t| t.as_str())
                                            .unwrap_or("[]");
                                        let cwd = r
                                            .get("cwd")
                                            .and_then(|c| c.as_str())
                                            .unwrap_or("");
                                        let branch = r
                                            .get("git_branch")
                                            .and_then(|b| b.as_str())
                                            .unwrap_or("");

                                        println!("[{:.2}] {}", score, summary);
                                        if !cwd.is_empty() {
                                            print!("       {}", cwd);
                                            if !branch.is_empty() {
                                                print!(" ({})", branch);
                                            }
                                            println!();
                                        }
                                        if tags != "[]" {
                                            println!("       tags={}", tags);
                                        }
                                        println!();
                                    }
                                }
                            }
                        } else {
                            println!("{}", serde_json::to_string_pretty(&body)?);
                        }
                    }
                    _ => {
                        eprintln!(
                            "Brain server unavailable, falling back to raw query..."
                        );
                        commands::handle_query_raw(&config, &text).await?;
                    }
                }
            }
        }
```

- [ ] **Step 2: Build and test**

Run: `cd ~/projects/hippo && cargo build -p hippo-daemon && cargo clippy --all-targets -- -D warnings`
Expected: Clean build.

- [ ] **Step 3: Commit**

```bash
git add crates/hippo-daemon/src/main.rs
git commit -m "feat(query): display semantic search results with scores and metadata"
```

---

### Task 8: mise re-enrich task

**Files:**
- Modify: `mise.toml`

- [ ] **Step 1: Add re-enrich task**

Add after the `[tasks."vectors:search"]` section in `mise.toml`:

```toml
[tasks.re-enrich]
description = "Nuke vectors and knowledge nodes, re-queue all events for enrichment"
run = """
#!/usr/bin/env bash
set -euo pipefail

DATA_DIR=~/.local/share/hippo
DB="$DATA_DIR/hippo.db"

echo "==> Stopping brain..."
pkill -f 'hippo-brain' 2>/dev/null && sleep 1 || true

echo "==> Deleting vector store..."
rm -rf "$DATA_DIR/vectors"
echo "  Removed $DATA_DIR/vectors/"

echo "==> Resetting knowledge nodes and enrichment queue..."
sqlite3 "$DB" 'DELETE FROM knowledge_node_events'
sqlite3 "$DB" 'DELETE FROM knowledge_node_entities'
sqlite3 "$DB" 'DELETE FROM knowledge_nodes'
sqlite3 "$DB" "UPDATE enrichment_queue SET status = 'pending', retry_count = 0, error_message = NULL, locked_at = NULL, locked_by = NULL WHERE status IN ('done', 'failed', 'processing')"

PENDING=$(sqlite3 "$DB" "SELECT COUNT(*) FROM enrichment_queue WHERE status = 'pending'")
echo "  Re-enrichment queued: $PENDING events pending"

echo ""
echo "==> Restart brain to begin re-enrichment:"
echo "  mise run start"
"""
```

- [ ] **Step 2: Commit**

```bash
git add mise.toml
git commit -m "feat(mise): add re-enrich task to reset and re-process all events"
```

---

### Task 9: Full verification

**Files:** None (verification only)

- [ ] **Step 1: Run all Rust checks**

Run: `cd ~/projects/hippo && cargo clippy --all-targets -- -D warnings && cargo fmt --check && cargo test`
Expected: All clean, all pass.

- [ ] **Step 2: Run all Python checks**

Run: `cd ~/projects/hippo && uv run --project brain ruff check brain/ && uv run --project brain ruff format --check brain/ && uv run --project brain pytest brain/tests -v`
Expected: All clean, all pass.

- [ ] **Step 3: Run re-enrich**

Run: `cd ~/projects/hippo && mise run re-enrich`
Expected: Vectors deleted, enrichment queue reset, pending count shown.

- [ ] **Step 4: Start services and verify enrichment**

Run: `cd ~/projects/hippo && mise run start`
Wait ~30 seconds, then: `mise run vectors`
Expected: New rows with specific summaries.

- [ ] **Step 5: Test semantic search**

Run: `cd ~/projects/hippo && hippo query "version embedding"`
Expected: Semantic results with similarity scores.

- [ ] **Step 6: Test raw query fallback**

Run: `cd ~/projects/hippo && hippo query --raw "cargo"`
Expected: Lexical substring results.
