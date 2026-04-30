# Browser Yield Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix three confirmed pipeline gaps that suppress browser knowledge node yield: aggressive scroll filter, narrow allowlist, and untested error_message coverage for low-engagement skips.

**Architecture:** Fix #1 adds a long-dwell bypass to the Python brain's scroll filter (configurable via `config.toml [browser]`, threaded through `_load_runtime_settings` → `create_app` → `BrainServer` → `claim_pending_browser_events`). Fix #2 expands the domain allowlist in both the TypeScript extension default and the Rust config default. Fix #3 adds a test asserting `error_message` is set when low-engagement events are skipped.

**Tech Stack:** Python 3.14 + SQLite (brain enrichment), Rust (hippo-core config), TypeScript (Firefox extension)

---

### Task 1: Add long-dwell bypass to scroll depth filter

A 38-minute reading session on GitHub was dropped because scroll_depth was 11.7% with no search query. The filter needs a dwell-time escape hatch: if a user spends ≥ `long_dwell_bypass_ms` on a page, enrich it regardless of scroll.

**Files:**
- Modify: `brain/src/hippo_brain/browser_enrichment.py:44-55` (function signature)
- Modify: `brain/src/hippo_brain/browser_enrichment.py:129-140` (filter logic)
- Modify: `brain/src/hippo_brain/server.py:109-140` (`BrainServer.__init__`)
- Modify: `brain/src/hippo_brain/server.py:832-838` (claim call site)
- Modify: `brain/src/hippo_brain/server.py:1338-1365` (`create_app` signature)
- Modify: `brain/src/hippo_brain/__init__.py:51-68` (`_load_runtime_settings` return)
- Modify: `brain/src/hippo_brain/__init__.py:110-123` (`create_app` call)
- Modify: `config/config.default.toml` (document under `[browser]`)
- Test: `brain/tests/test_browser_enrichment.py`

- [ ] **Step 1: Write failing test for long-dwell bypass**

Add to the `TestClaimPendingBrowserEvents` class in `brain/tests/test_browser_enrichment.py`:

```python
def test_long_dwell_bypasses_scroll_filter(self, db):
    """Events with low scroll but dwell >= long_dwell_bypass_ms are kept."""
    stale_ts = int(time.time() * 1000) - 120_000

    # Low scroll, no query, but 3-minute dwell — should be kept
    _insert_browser_event(
        db,
        1,
        stale_ts,
        url="https://github.com/sjcarpenter/hippo/pull/99",
        domain="github.com",
        dwell_ms=180_000,
        scroll_depth=0.05,
    )
    # Low scroll, no query, short dwell — should be skipped
    _insert_browser_event(
        db,
        2,
        stale_ts + 1000,
        url="https://github.com/sjcarpenter/hippo/issues/1",
        domain="github.com",
        dwell_ms=5000,
        scroll_depth=0.05,
    )

    chunks = claim_pending_browser_events(
        db, "test-worker", stale_secs=60, long_dwell_bypass_ms=120_000
    )
    all_events = [e for chunk in chunks for e in chunk]
    assert len(all_events) == 1
    assert all_events[0]["id"] == 1

    row = db.execute(
        "SELECT status FROM browser_enrichment_queue WHERE browser_event_id = 2"
    ).fetchone()
    assert row[0] == "skipped"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd "$(git rev-parse --show-toplevel)"
uv run --project brain --extra dev pytest brain/tests/test_browser_enrichment.py::TestClaimPendingBrowserEvents::test_long_dwell_bypasses_scroll_filter -v
```

Expected: `FAILED` — `TypeError` or `AssertionError` since `long_dwell_bypass_ms` param doesn't exist yet.

- [ ] **Step 3: Add `long_dwell_bypass_ms` parameter to `claim_pending_browser_events`**

In `brain/src/hippo_brain/browser_enrichment.py`, update the function signature and filter logic:

```python
def claim_pending_browser_events(
    conn,
    worker_id: str,
    stale_secs: int = 60,
    scroll_depth_threshold: float = 0.15,
    max_claim_batch: int | None = None,
    stale_lock_timeout_ms: int = STALE_LOCK_TIMEOUT_MS,
    long_dwell_bypass_ms: int = 120_000,
) -> list[list[dict]]:
    """Atomically claim pending browser events and return them grouped into time-based chunks.

    Only claims events whose timestamp is older than stale_secs (to avoid
    processing events from an active browsing session).

    Events with scroll_depth < scroll_depth_threshold AND no search_query AND
    dwell_ms < long_dwell_bypass_ms are marked 'skipped' and excluded from results.

    `max_claim_batch` caps total events claimed per invocation; `None` disables
    the cap. Enforced as `LIMIT ?` on the UPDATE's inner SELECT.
    """
```

Then change the filter condition at line ~136 from:

```python
        if scroll < scroll_depth_threshold and not has_query:
            skipped.append((ev["id"], f"low engagement: scroll={scroll:.2f} and no search_query"))
```

to:

```python
        dwell_ms = ev.get("dwell_ms") or 0
        if scroll < scroll_depth_threshold and not has_query and dwell_ms < long_dwell_bypass_ms:
            skipped.append(
                (
                    ev["id"],
                    f"low engagement: scroll={scroll:.2f}, no search_query, dwell={dwell_ms}ms",
                )
            )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
uv run --project brain --extra dev pytest brain/tests/test_browser_enrichment.py::TestClaimPendingBrowserEvents::test_long_dwell_bypasses_scroll_filter -v
```

Expected: `PASSED`

- [ ] **Step 5: Add `long_dwell_bypass_ms` to `BrainServer.__init__`**

In `brain/src/hippo_brain/server.py`, update `BrainServer.__init__` signature:

```python
    def __init__(
        self,
        db_path: str = "",
        data_dir: str = "",
        lmstudio_base_url: str = "http://localhost:1234/v1",
        lmstudio_timeout_secs: float = 300.0,
        enrichment_model: str = "",
        embedding_model: str = "",
        query_model: str = "",
        poll_interval_secs: int = 5,
        enrichment_batch_size: int = 30,
        session_stale_secs: int = 120,
        max_claim_batch: int = DEFAULT_MAX_CLAIM_BATCH,
        lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
        long_dwell_bypass_ms: int = 120_000,
    ):
```

And add `self.long_dwell_bypass_ms = long_dwell_bypass_ms` in the body after `self.lock_timeout_ms = lock_timeout_ms`.

- [ ] **Step 6: Pass `long_dwell_bypass_ms` at the claim call site**

In `brain/src/hippo_brain/server.py`, update the `claim_pending_browser_events` call:

```python
                        try:
                            browser_batches = claim_pending_browser_events(
                                conn,
                                worker_id,
                                stale_secs=60,
                                max_claim_batch=self.max_claim_batch,
                                stale_lock_timeout_ms=self.lock_timeout_ms,
                                long_dwell_bypass_ms=self.long_dwell_bypass_ms,
                            )
```

- [ ] **Step 7: Add `long_dwell_bypass_ms` to `create_app`**

In `brain/src/hippo_brain/server.py`, update `create_app` signature and body:

```python
def create_app(
    db_path: str = "",
    data_dir: str = "",
    lmstudio_base_url: str = "http://localhost:1234/v1",
    lmstudio_timeout_secs: float = 300.0,
    enrichment_model: str = "",
    embedding_model: str = "",
    query_model: str = "",
    poll_interval_secs: int = 5,
    enrichment_batch_size: int = 30,
    session_stale_secs: int = 120,
    max_claim_batch: int = DEFAULT_MAX_CLAIM_BATCH,
    lock_timeout_ms: int = DEFAULT_LOCK_TIMEOUT_MS,
    long_dwell_bypass_ms: int = 120_000,
) -> Starlette:
    server = BrainServer(
        db_path=db_path,
        data_dir=data_dir,
        lmstudio_base_url=lmstudio_base_url,
        lmstudio_timeout_secs=lmstudio_timeout_secs,
        enrichment_model=enrichment_model,
        embedding_model=embedding_model,
        query_model=query_model,
        poll_interval_secs=poll_interval_secs,
        enrichment_batch_size=enrichment_batch_size,
        session_stale_secs=session_stale_secs,
        max_claim_batch=max_claim_batch,
        lock_timeout_ms=lock_timeout_ms,
        long_dwell_bypass_ms=long_dwell_bypass_ms,
    )
```

- [ ] **Step 8: Read `long_dwell_bypass_ms` from config.toml in `_load_runtime_settings`**

In `brain/src/hippo_brain/__init__.py`, update `_load_runtime_settings` to read the new field and pass it to `create_app`.

Add to the return dict (after the existing `browser` variable is loaded — add `browser = config.get("browser", {})` if not already present, then add the key):

```python
    browser = config.get("browser", {})

    return {
        ...existing keys...,
        "long_dwell_bypass_ms": browser.get("long_dwell_bypass_ms", 120_000),
    }
```

And update the `create_app` call:

```python
        app = create_app(
            db_path=settings["db_path"],
            data_dir=settings["data_dir"],
            lmstudio_base_url=settings["lmstudio_base_url"],
            lmstudio_timeout_secs=settings["lmstudio_timeout_secs"],
            enrichment_model=settings["enrichment_model"],
            embedding_model=settings["embedding_model"],
            query_model=settings["query_model"],
            poll_interval_secs=settings["poll_interval_secs"],
            enrichment_batch_size=settings["max_events_per_chunk"],
            session_stale_secs=settings["session_stale_secs"],
            max_claim_batch=settings["max_claim_batch"],
            lock_timeout_ms=int(settings["lock_timeout_secs"]) * 1000,
            long_dwell_bypass_ms=settings["long_dwell_bypass_ms"],
        )
```

- [ ] **Step 9: Document the new field in `config/config.default.toml`**

Add under `[browser]`:

```toml
long_dwell_bypass_ms = 120000   # Keep pages with dwell >= this regardless of scroll (default: 2 min)
```

- [ ] **Step 10: Run the full browser enrichment test suite**

```bash
uv run --project brain --extra dev pytest brain/tests/test_browser_enrichment.py -v
```

Expected: all tests pass.

- [ ] **Step 11: Run full brain lint and tests**

```bash
uv run --project brain --extra dev ruff check brain/src brain/tests && \
uv run --project brain --extra dev ruff format --check brain/src brain/tests && \
uv run --project brain --extra dev pytest brain/tests -v --ignore=brain/tests/test_client_real.py --ignore=brain/tests/test_mcp_queries_gh.py --ignore=brain/tests/test_mcp_server_gh.py --ignore=brain/tests/test_lessons_graduation_hippo.py -x -q
```

Expected: lint clean, all tests pass.

- [ ] **Step 12: Commit**

```bash
git add brain/src/hippo_brain/browser_enrichment.py \
        brain/src/hippo_brain/server.py \
        brain/src/hippo_brain/__init__.py \
        config/config.default.toml \
        brain/tests/test_browser_enrichment.py
git commit -m "feat(brain): add long-dwell bypass to browser scroll depth filter

Sessions with dwell_ms >= long_dwell_bypass_ms (default 120s) are kept
even when scroll_depth < threshold and no search_query. Configurable
via config.toml [browser].long_dwell_bypass_ms. Threads through
_load_runtime_settings -> create_app -> BrainServer -> claim."
```

---

### Task 2: Expand domain allowlist

Two allowlists need updating: the TypeScript extension default (used on fresh install / reset) and the Rust config default (used by the daemon's native messaging defense-in-depth check when config.toml has no `[browser]` section).

**Files:**
- Modify: `extension/firefox/src/config.ts:4-37`
- Modify: `crates/hippo-core/src/config.rs:241-276` (`default_browser_allowlist_domains`)
- Modify: `crates/hippo-core/src/config.rs:800-821` (test update)
- Modify: `config/config.default.toml` (template domains list)

- [ ] **Step 1: Update `DEFAULT_ALLOWLIST` in the TypeScript extension**

Replace the `DEFAULT_ALLOWLIST` array in `extension/firefox/src/config.ts`:

```typescript
export const DEFAULT_ALLOWLIST: string[] = [
  // Code forges & sharing
  "github.com",
  "github.io",
  "gitlab.com",
  "bitbucket.org",
  // Package registries
  "crates.io",
  "npmjs.com",
  "pypi.org",
  "mvnrepository.com",
  "pkg.go.dev",
  "rubygems.org",
  // Language & framework docs
  "docs.rs",
  "doc.rust-lang.org",
  "rust-lang.org",
  "docs.python.org",
  "python.org",
  "swift.org",
  "developer.mozilla.org",
  "docs.astral.sh",
  "typescriptlang.org",
  "learn.microsoft.com",
  "kubernetes.io",
  "go.dev",
  "nodejs.org",
  "ziglang.org",
  // AI & ML
  "anthropic.com",
  "openai.com",
  "huggingface.co",
  "arxiv.org",
  "lmstudio.ai",
  // System & OS docs
  "man7.org",
  "wiki.archlinux.org",
  // Database & infra docs
  "sqlite.org",
  "postgresql.org",
  "redis.io",
  "docker.com",
  // Q&A & community
  "stackoverflow.com",
  "stackexchange.com",
  "reddit.com",
  "news.ycombinator.com",
  "lobste.rs",
  // Developer content
  "medium.com",
  "dev.to",
  "hackernoon.com",
  "substack.com",
];
```

- [ ] **Step 2: Update `default_browser_allowlist_domains` in Rust config**

Replace the function body in `crates/hippo-core/src/config.rs`:

```rust
fn default_browser_allowlist_domains() -> Vec<String> {
    vec![
        // Code forges & sharing
        "github.com".to_string(),
        "github.io".to_string(),
        "gitlab.com".to_string(),
        "bitbucket.org".to_string(),
        // Package registries
        "crates.io".to_string(),
        "npmjs.com".to_string(),
        "pypi.org".to_string(),
        "mvnrepository.com".to_string(),
        "pkg.go.dev".to_string(),
        "rubygems.org".to_string(),
        // Language & framework docs
        "docs.rs".to_string(),
        "doc.rust-lang.org".to_string(),
        "rust-lang.org".to_string(),
        "docs.python.org".to_string(),
        "python.org".to_string(),
        "swift.org".to_string(),
        "developer.mozilla.org".to_string(),
        "docs.astral.sh".to_string(),
        "typescriptlang.org".to_string(),
        "learn.microsoft.com".to_string(),
        "kubernetes.io".to_string(),
        "go.dev".to_string(),
        "nodejs.org".to_string(),
        "ziglang.org".to_string(),
        // AI & ML
        "anthropic.com".to_string(),
        "openai.com".to_string(),
        "huggingface.co".to_string(),
        "arxiv.org".to_string(),
        "lmstudio.ai".to_string(),
        // System & OS docs
        "man7.org".to_string(),
        "wiki.archlinux.org".to_string(),
        // Database & infra docs
        "sqlite.org".to_string(),
        "postgresql.org".to_string(),
        "redis.io".to_string(),
        "docker.com".to_string(),
        // Q&A & community
        "stackoverflow.com".to_string(),
        "stackexchange.com".to_string(),
        "reddit.com".to_string(),
        "news.ycombinator.com".to_string(),
        "lobste.rs".to_string(),
        // Developer content
        "medium.com".to_string(),
        "dev.to".to_string(),
        "hackernoon.com".to_string(),
        "substack.com".to_string(),
    ]
}
```

- [ ] **Step 3: Update the Rust config test to assert new domains**

In `crates/hippo-core/src/config.rs`, in `test_browser_config_defaults`, add assertions after the existing ones:

```rust
        assert!(config.allowlist.domains.contains(&"rust-lang.org".to_string()));
        assert!(config.allowlist.domains.contains(&"anthropic.com".to_string()));
        assert!(config.allowlist.domains.contains(&"arxiv.org".to_string()));
        assert!(config.allowlist.domains.contains(&"sqlite.org".to_string()));
        assert!(config.allowlist.domains.contains(&"lobste.rs".to_string()));
```

- [ ] **Step 4: Update `config/config.default.toml` domains list**

In `config/config.default.toml`, replace the domains array under `[browser.allowlist]` with the full expanded list matching the Rust defaults above.

- [ ] **Step 5: Run Rust tests**

```bash
cargo test -p hippo-core -- config 2>&1 | tail -20
```

Expected: all config tests pass.

- [ ] **Step 6: Rebuild the Rust binary and reinstall**

```bash
cargo build --release 2>&1 | tail -5 && \
cp target/release/hippo ~/.local/bin/hippo
```

Expected: build succeeds.

- [ ] **Step 7: Rebuild the Firefox extension and install**

```bash
cd /path/to/hippo && mise run install:ext
```

Expected: extension built and XPI updated in Firefox profile. Restart Firefox after this step to pick up the new default allowlist (only matters for a fresh-profile install, but good hygiene).

- [ ] **Step 8: Commit**

```bash
git add extension/firefox/src/config.ts \
        crates/hippo-core/src/config.rs \
        config/config.default.toml
git commit -m "feat: expand browser allowlist with AI, infra, and community domains

Adds anthropic.com, arxiv.org, rust-lang.org, python.org, swift.org,
sqlite.org, postgresql.org, redis.io, docker.com, huggingface.co,
openai.com, lmstudio.ai, lobste.rs, hackernoon.com, substack.com,
bitbucket.org, rubygems.org, ziglang.org to both the TypeScript
extension DEFAULT_ALLOWLIST and the Rust config default.

Note: existing users with a stored extension allowlist will not see
these new domains automatically — they can reset via the popup or
add domains manually."
```

---

### Task 3: Assert error_message is saved for low-engagement skips

The test `test_claim_skips_low_engagement_events` checks `status == "skipped"` but does not assert that `error_message` is populated. This is the path that dropped the 38-minute session. Adding the assertion will catch any future regression where skip reasons are lost.

**Files:**
- Modify: `brain/tests/test_browser_enrichment.py:99-147`

- [ ] **Step 1: Add `error_message` assertion to the existing low-engagement test**

In `brain/tests/test_browser_enrichment.py`, update `test_claim_skips_low_engagement_events` — replace the final assertion block:

```python
        # Verify the skipped event's queue status and error message
        row = db.execute(
            "SELECT status, error_message FROM browser_enrichment_queue WHERE browser_event_id = 1"
        ).fetchone()
        assert row[0] == "skipped"
        assert row[1] is not None and len(row[1]) > 0, (
            "error_message must be set for skipped low-engagement events"
        )
        assert "scroll" in row[1], f"expected 'scroll' in error_message, got: {row[1]!r}"
```

- [ ] **Step 2: Run the test to verify it passes with current code**

```bash
uv run --project brain --extra dev pytest brain/tests/test_browser_enrichment.py::TestClaimPendingBrowserEvents::test_claim_skips_low_engagement_events -v
```

Expected: `PASSED` — confirms current code saves the reason. If it fails, the save is broken and needs a fix in `claim_pending_browser_events` at the skip UPDATE block.

- [ ] **Step 3: Run full browser enrichment suite one final time**

```bash
uv run --project brain --extra dev pytest brain/tests/test_browser_enrichment.py -v
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add brain/tests/test_browser_enrichment.py
git commit -m "test(brain): assert error_message is set for low-engagement browser skips

Closes observability gap: skipped events must always carry a human-readable
reason for diagnosis. Covers the scroll < threshold AND no search_query path."
```

---

## Post-Implementation Verification

After all three tasks are complete:

```bash
# Verify brain restarts cleanly
mise run restart

# Confirm brain is up
curl -s http://localhost:9175/health | python3 -m json.tool

# After visiting a qualifying page in Firefox, check capture:
sqlite3 ~/.local/share/hippo/hippo.db \
  "SELECT datetime(timestamp/1000,'unixepoch','localtime'), domain, dwell_ms, scroll_depth FROM browser_events ORDER BY timestamp DESC LIMIT 5"
```
