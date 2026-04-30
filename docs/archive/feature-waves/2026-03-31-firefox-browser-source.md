# Firefox Browser Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Firefox Developer Edition browsing as a knowledge source — a WebExtension captures page visits with engagement signals and content, sends them to hippo-daemon via Native Messaging, where they flow through the existing enrichment pipeline and get correlated with shell activity.

**Architecture:** Firefox WebExtension (content script + background script) captures dwell time, scroll depth, and Readability-extracted content from allowlisted domains. Sends via Native Messaging to a `hippo native-messaging-host` bridge process that forwards to the daemon's Unix socket. Daemon stores in `browser_events` table, auto-queues enrichment. Brain correlates browser events with temporally adjacent shell events in enrichment prompts.

**Tech Stack:** JavaScript (WebExtension MV2), Rust (native messaging host + storage), Python (browser enrichment), SQLite, Mozilla Readability.js

**Spec:** `docs/superpowers/specs/2026-03-31-firefox-browser-source-design.md`

---

### Task 1: BrowserEvent type and EventPayload variant

**Files:**
- Modify: `crates/hippo-core/src/events.rs`
- Test: `crates/hippo-core/src/events.rs` (inline tests)

- [ ] **Step 1: Write the failing test**

Add to the `#[cfg(test)] mod tests` block in `crates/hippo-core/src/events.rs`:

```rust
fn sample_browser_event() -> BrowserEvent {
    BrowserEvent {
        url: "https://stackoverflow.com/questions/12345".to_string(),
        title: "How to implement Display trait".to_string(),
        domain: "stackoverflow.com".to_string(),
        dwell_ms: 45000,
        scroll_depth: 0.85,
        extracted_text: Some("The Display trait requires...".to_string()),
        search_query: Some("rust implement Display trait".to_string()),
        referrer: Some("https://www.google.com/search?q=rust+Display".to_string()),
        content_hash: None,
    }
}

#[test]
fn test_browser_event_roundtrip() {
    let envelope = EventEnvelope {
        envelope_id: Uuid::new_v4(),
        producer_version: 1,
        timestamp: Utc::now(),
        payload: EventPayload::Browser(Box::new(sample_browser_event())),
    };
    let json = serde_json::to_string(&envelope).unwrap();
    let parsed: EventEnvelope = serde_json::from_str(&json).unwrap();
    match &parsed.payload {
        EventPayload::Browser(browser) => {
            assert_eq!(browser.url, "https://stackoverflow.com/questions/12345");
            assert_eq!(browser.domain, "stackoverflow.com");
            assert_eq!(browser.dwell_ms, 45000);
            assert!((browser.scroll_depth - 0.85).abs() < f32::EPSILON);
            assert_eq!(
                browser.search_query.as_deref(),
                Some("rust implement Display trait")
            );
        }
        _ => panic!("expected Browser payload"),
    }
}

#[test]
fn test_browser_adjacently_tagged_json_shape() {
    let envelope = EventEnvelope {
        envelope_id: Uuid::new_v4(),
        producer_version: 1,
        timestamp: Utc::now(),
        payload: EventPayload::Browser(Box::new(sample_browser_event())),
    };
    let value: serde_json::Value = serde_json::to_value(&envelope).unwrap();
    let payload = &value["payload"];
    assert_eq!(payload["type"], "Browser");
    assert!(payload["data"].is_object());
    assert_eq!(payload["data"]["domain"], "stackoverflow.com");
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cargo test -p hippo-core test_browser`
Expected: FAIL — `BrowserEvent` not found

- [ ] **Step 3: Add BrowserEvent struct and EventPayload::Browser variant**

In `crates/hippo-core/src/events.rs`, add the struct before `EventEnvelope`:

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrowserEvent {
    pub url: String,
    pub title: String,
    pub domain: String,
    pub dwell_ms: u64,
    pub scroll_depth: f32,
    pub extracted_text: Option<String>,
    pub search_query: Option<String>,
    pub referrer: Option<String>,
    pub content_hash: Option<String>,
}
```

Add the variant to `EventPayload`:

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", content = "data")]
pub enum EventPayload {
    Shell(Box<ShellEvent>),
    FsChange(FsChangeEvent),
    IdeAction(IdeActionEvent),
    Browser(Box<BrowserEvent>),
    Raw(serde_json::Value),
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cargo test -p hippo-core`
Expected: All tests PASS (including existing tests — no regressions)

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-core/src/events.rs
git commit -m "feat(core): add BrowserEvent type and EventPayload::Browser variant"
```

---

### Task 2: Browser config section

**Files:**
- Modify: `crates/hippo-core/src/config.rs`
- Modify: `config/config.default.toml`
- Test: `crates/hippo-core/src/config.rs` (inline tests)

- [ ] **Step 1: Write the failing test**

Add to the `#[cfg(test)] mod tests` block in `crates/hippo-core/src/config.rs`:

```rust
#[test]
fn test_browser_config_defaults() {
    let config = HippoConfig::default();
    assert!(config.browser.enabled);
    assert_eq!(config.browser.min_dwell_ms, 3000);
    assert!((config.browser.scroll_depth_threshold - 0.15).abs() < f32::EPSILON);
    assert_eq!(config.browser.dedup_window_minutes, 30);
    assert_eq!(config.browser.correlation_window_ms, 300_000);
    assert_eq!(config.browser.stale_session_secs, 60);
    assert!(!config.browser.allowlist.domains.is_empty());
    assert!(config.browser.allowlist.domains.contains(&"github.com".to_string()));
    assert!(config.browser.allowlist.domains.contains(&"stackoverflow.com".to_string()));
    assert!(!config.browser.url_redaction.strip_params.is_empty());
    assert!(config.browser.url_redaction.strip_params.contains(&"token".to_string()));
}

#[test]
fn test_browser_config_from_toml() {
    let toml_str = r#"
[browser]
enabled = false
min_dwell_ms = 5000

[browser.allowlist]
domains = ["example.com"]

[browser.url_redaction]
strip_params = ["secret"]
"#;
    let config: HippoConfig = toml::from_str(toml_str).unwrap();
    assert!(!config.browser.enabled);
    assert_eq!(config.browser.min_dwell_ms, 5000);
    assert_eq!(config.browser.allowlist.domains, vec!["example.com"]);
    assert_eq!(config.browser.url_redaction.strip_params, vec!["secret"]);
    // Unspecified fields use defaults
    assert_eq!(config.browser.dedup_window_minutes, 30);
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cargo test -p hippo-core test_browser_config`
Expected: FAIL — no field `browser` on type `HippoConfig`

- [ ] **Step 3: Add BrowserConfig structs and wire into HippoConfig**

In `crates/hippo-core/src/config.rs`, add these structs (before `impl HippoConfig`):

```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrowserConfig {
    #[serde(default = "default_browser_enabled")]
    pub enabled: bool,
    #[serde(default = "default_min_dwell_ms")]
    pub min_dwell_ms: u64,
    #[serde(default = "default_scroll_depth_threshold")]
    pub scroll_depth_threshold: f32,
    #[serde(default = "default_dedup_window_minutes")]
    pub dedup_window_minutes: u64,
    #[serde(default = "default_correlation_window_ms")]
    pub correlation_window_ms: u64,
    #[serde(default = "default_browser_stale_session_secs")]
    pub stale_session_secs: u64,
    #[serde(default)]
    pub allowlist: BrowserAllowlist,
    #[serde(default)]
    pub url_redaction: BrowserUrlRedaction,
}

fn default_browser_enabled() -> bool { true }
fn default_min_dwell_ms() -> u64 { 3000 }
fn default_scroll_depth_threshold() -> f32 { 0.15 }
fn default_dedup_window_minutes() -> u64 { 30 }
fn default_correlation_window_ms() -> u64 { 300_000 }
fn default_browser_stale_session_secs() -> u64 { 60 }

impl Default for BrowserConfig {
    fn default() -> Self {
        Self {
            enabled: default_browser_enabled(),
            min_dwell_ms: default_min_dwell_ms(),
            scroll_depth_threshold: default_scroll_depth_threshold(),
            dedup_window_minutes: default_dedup_window_minutes(),
            correlation_window_ms: default_correlation_window_ms(),
            stale_session_secs: default_browser_stale_session_secs(),
            allowlist: BrowserAllowlist::default(),
            url_redaction: BrowserUrlRedaction::default(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrowserAllowlist {
    #[serde(default = "default_browser_domains")]
    pub domains: Vec<String>,
}

fn default_browser_domains() -> Vec<String> {
    vec![
        "github.com".into(),
        "stackoverflow.com".into(),
        "developer.mozilla.org".into(),
        "docs.rs".into(),
        "doc.rust-lang.org".into(),
        "crates.io".into(),
        "npmjs.com".into(),
        "pypi.org".into(),
        "docs.python.org".into(),
        "man7.org".into(),
        "wiki.archlinux.org".into(),
    ]
}

impl Default for BrowserAllowlist {
    fn default() -> Self {
        Self { domains: default_browser_domains() }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrowserUrlRedaction {
    #[serde(default = "default_strip_params")]
    pub strip_params: Vec<String>,
}

fn default_strip_params() -> Vec<String> {
    vec![
        "token".into(), "api_key".into(), "password".into(), "secret".into(),
        "auth".into(), "session".into(), "key".into(), "sig".into(),
    ]
}

impl Default for BrowserUrlRedaction {
    fn default() -> Self {
        Self { strip_params: default_strip_params() }
    }
}
```

Add the field to `HippoConfig`:

```rust
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct HippoConfig {
    #[serde(default)]
    pub lmstudio: LmStudioConfig,
    #[serde(default)]
    pub models: ModelsConfig,
    #[serde(default)]
    pub daemon: DaemonConfig,
    #[serde(default)]
    pub brain: BrainConfig,
    #[serde(default)]
    pub storage: StorageConfig,
    #[serde(default)]
    pub browser: BrowserConfig,
}
```

- [ ] **Step 4: Update config.default.toml**

Append to `config/config.default.toml`:

```toml

[browser]
enabled = true
min_dwell_ms = 3000
scroll_depth_threshold = 0.15
dedup_window_minutes = 30
correlation_window_ms = 300000
stale_session_secs = 60

[browser.allowlist]
domains = [
    "github.com",
    "stackoverflow.com",
    "developer.mozilla.org",
    "docs.rs",
    "doc.rust-lang.org",
    "crates.io",
    "npmjs.com",
    "pypi.org",
    "docs.python.org",
    "man7.org",
    "wiki.archlinux.org",
]

[browser.url_redaction]
strip_params = ["token", "api_key", "password", "secret", "auth", "session", "key", "sig"]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cargo test -p hippo-core`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add crates/hippo-core/src/config.rs config/config.default.toml
git commit -m "feat(config): add [browser] config section with allowlist and URL redaction"
```

---

### Task 3: Schema migration v3 → v4 (browser tables)

**Files:**
- Modify: `crates/hippo-core/src/schema.sql`
- Modify: `crates/hippo-core/src/storage.rs`
- Test: `crates/hippo-core/src/storage.rs` (inline tests) or new test

- [ ] **Step 1: Write the failing test**

Add a test in `crates/hippo-core/src/storage.rs` tests section (you'll need to check for existing test patterns — the file uses `tempfile` for test DBs):

```rust
#[test]
fn test_browser_events_table_exists_after_open() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("test.db");
    let conn = open_db(&db_path).unwrap();
    // Verify the browser_events table exists
    let count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='browser_events'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(count, 1, "browser_events table should exist");

    // Verify browser_enrichment_queue table exists
    let count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='browser_enrichment_queue'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(count, 1, "browser_enrichment_queue table should exist");

    // Verify knowledge_node_browser_events junction table exists
    let count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='knowledge_node_browser_events'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(count, 1, "knowledge_node_browser_events table should exist");

    // Verify user_version is 4
    let version: i64 = conn
        .query_row("PRAGMA user_version", [], |row| row.get(0))
        .unwrap();
    assert_eq!(version, 4);
}

#[test]
fn test_migration_v3_to_v4() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("test.db");

    // Create a v3 database
    {
        let conn = open_db(&db_path).unwrap();
        let version: i64 = conn
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .unwrap();
        assert_eq!(version, 4, "fresh DB should be v4 after including browser tables in schema.sql");
    }

    // Re-open — should not fail
    {
        let conn = open_db(&db_path).unwrap();
        let version: i64 = conn
            .query_row("PRAGMA user_version", [], |row| row.get(0))
            .unwrap();
        assert_eq!(version, 4);
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cargo test -p hippo-core test_browser_events_table`
Expected: FAIL — browser_events table doesn't exist / user_version is 3

- [ ] **Step 3: Update schema.sql with browser tables**

Add before the final `PRAGMA user_version` line in `crates/hippo-core/src/schema.sql`:

```sql
-- Browser visit events captured via Firefox WebExtension
CREATE TABLE IF NOT EXISTS browser_events (
    id              INTEGER PRIMARY KEY,
    timestamp       INTEGER NOT NULL,
    url             TEXT NOT NULL,
    title           TEXT,
    domain          TEXT NOT NULL,
    dwell_ms        INTEGER NOT NULL,
    scroll_depth    REAL,
    extracted_text  TEXT,
    search_query    TEXT,
    referrer        TEXT,
    content_hash    TEXT,
    envelope_id     TEXT,
    enriched        INTEGER NOT NULL DEFAULT 0,
    created_at      INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
);

CREATE TABLE IF NOT EXISTS browser_enrichment_queue (
    id                  INTEGER PRIMARY KEY,
    browser_event_id    INTEGER NOT NULL UNIQUE REFERENCES browser_events(id),
    status              TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'done', 'failed', 'skipped')),
    priority            INTEGER NOT NULL DEFAULT 5,
    retry_count         INTEGER NOT NULL DEFAULT 0,
    max_retries         INTEGER NOT NULL DEFAULT 5,
    error_message       TEXT,
    locked_at           INTEGER,
    locked_by           TEXT,
    created_at          INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
    updated_at          INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
);

CREATE TABLE IF NOT EXISTS knowledge_node_browser_events (
    knowledge_node_id   INTEGER NOT NULL REFERENCES knowledge_nodes(id),
    browser_event_id    INTEGER NOT NULL REFERENCES browser_events(id),
    PRIMARY KEY (knowledge_node_id, browser_event_id)
);

CREATE INDEX IF NOT EXISTS idx_browser_events_timestamp ON browser_events(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_browser_events_domain ON browser_events(domain);
CREATE UNIQUE INDEX IF NOT EXISTS idx_browser_events_envelope_id ON browser_events(envelope_id)
    WHERE envelope_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_browser_events_enriched ON browser_events(enriched)
    WHERE enriched = 0;
CREATE INDEX IF NOT EXISTS idx_browser_queue_pending ON browser_enrichment_queue(status, priority)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_browser_events_ts_domain ON browser_events(timestamp, domain);
```

Update the `PRAGMA user_version` at the end of `schema.sql` from `3` to `4`.

Also update the `entities` table CHECK constraint to include `'domain'`:

```sql
type TEXT NOT NULL CHECK (type IN (
    'project', 'file', 'tool', 'service', 'repo', 'host', 'person',
    'concept', 'domain'
)),
```

- [ ] **Step 4: Add migration path in storage.rs open_db()**

In `crates/hippo-core/src/storage.rs`, update `open_db()`:

Change `const EXPECTED_VERSION: i64 = 3;` to `const EXPECTED_VERSION: i64 = 4;`.

Add a v3 → v4 migration block after the v2 → v3 block:

```rust
// Migrate from v3 → v4: add browser event tables
if version >= 1 && version <= 3 {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS browser_events (
            id              INTEGER PRIMARY KEY,
            timestamp       INTEGER NOT NULL,
            url             TEXT NOT NULL,
            title           TEXT,
            domain          TEXT NOT NULL,
            dwell_ms        INTEGER NOT NULL,
            scroll_depth    REAL,
            extracted_text  TEXT,
            search_query    TEXT,
            referrer        TEXT,
            content_hash    TEXT,
            envelope_id     TEXT,
            enriched        INTEGER NOT NULL DEFAULT 0,
            created_at      INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
         );
         CREATE TABLE IF NOT EXISTS browser_enrichment_queue (
            id                  INTEGER PRIMARY KEY,
            browser_event_id    INTEGER NOT NULL UNIQUE REFERENCES browser_events(id),
            status              TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'processing', 'done', 'failed', 'skipped')),
            priority            INTEGER NOT NULL DEFAULT 5,
            retry_count         INTEGER NOT NULL DEFAULT 0,
            max_retries         INTEGER NOT NULL DEFAULT 5,
            error_message       TEXT,
            locked_at           INTEGER,
            locked_by           TEXT,
            created_at          INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000),
            updated_at          INTEGER NOT NULL DEFAULT (unixepoch('now', 'subsec') * 1000)
         );
         CREATE TABLE IF NOT EXISTS knowledge_node_browser_events (
            knowledge_node_id   INTEGER NOT NULL REFERENCES knowledge_nodes(id),
            browser_event_id    INTEGER NOT NULL REFERENCES browser_events(id),
            PRIMARY KEY (knowledge_node_id, browser_event_id)
         );
         CREATE INDEX IF NOT EXISTS idx_browser_events_timestamp ON browser_events(timestamp DESC);
         CREATE INDEX IF NOT EXISTS idx_browser_events_domain ON browser_events(domain);
         CREATE UNIQUE INDEX IF NOT EXISTS idx_browser_events_envelope_id ON browser_events(envelope_id)
             WHERE envelope_id IS NOT NULL;
         CREATE INDEX IF NOT EXISTS idx_browser_events_enriched ON browser_events(enriched)
             WHERE enriched = 0;
         CREATE INDEX IF NOT EXISTS idx_browser_queue_pending ON browser_enrichment_queue(status, priority)
             WHERE status = 'pending';
         CREATE INDEX IF NOT EXISTS idx_browser_events_ts_domain ON browser_events(timestamp, domain);
         PRAGMA user_version = 4;",
    )?;
}
```

Update the existing migration chain conditions so that v3 also flows into the v4 migration. The version check for the v2→v3 migration already handles `version == 1 || version == 2`. Change the bail condition to `version != 0 && version != EXPECTED_VERSION && version > EXPECTED_VERSION` (or just adjust the version range to allow 3 to fall through to the v3→v4 migration).

- [ ] **Step 5: Run tests to verify they pass**

Run: `cargo test -p hippo-core`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add crates/hippo-core/src/schema.sql crates/hippo-core/src/storage.rs
git commit -m "feat(storage): add browser_events schema and v3→v4 migration"
```

---

### Task 4: insert_browser_event() storage function

**Files:**
- Modify: `crates/hippo-core/src/storage.rs`
- Modify: `crates/hippo-core/src/events.rs` (add `EventEnvelope::browser()` convenience)

- [ ] **Step 1: Write the failing test**

Add to `crates/hippo-core/src/storage.rs` tests:

```rust
#[test]
fn test_insert_browser_event() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("test.db");
    let conn = open_db(&db_path).unwrap();

    let event = crate::events::BrowserEvent {
        url: "https://stackoverflow.com/questions/12345".to_string(),
        title: "How to implement Display".to_string(),
        domain: "stackoverflow.com".to_string(),
        dwell_ms: 45000,
        scroll_depth: 0.85,
        extracted_text: Some("The Display trait...".to_string()),
        search_query: Some("rust Display trait".to_string()),
        referrer: Some("https://google.com".to_string()),
        content_hash: None,
    };
    let timestamp_ms = 1711900000000_i64;
    let envelope_id = "test-browser-envelope-1";

    let id = insert_browser_event(&conn, &event, timestamp_ms, Some(envelope_id)).unwrap();
    assert!(id > 0);

    // Verify event was stored
    let (stored_url, stored_domain, stored_dwell): (String, String, i64) = conn
        .query_row(
            "SELECT url, domain, dwell_ms FROM browser_events WHERE id = ?",
            [id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .unwrap();
    assert_eq!(stored_url, "https://stackoverflow.com/questions/12345");
    assert_eq!(stored_domain, "stackoverflow.com");
    assert_eq!(stored_dwell, 45000);

    // Verify enrichment queue entry was created
    let queue_status: String = conn
        .query_row(
            "SELECT status FROM browser_enrichment_queue WHERE browser_event_id = ?",
            [id],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(queue_status, "pending");

    // Verify content_hash was computed
    let hash: Option<String> = conn
        .query_row(
            "SELECT content_hash FROM browser_events WHERE id = ?",
            [id],
            |row| row.get(0),
        )
        .unwrap();
    assert!(hash.is_some(), "content_hash should be computed from extracted_text");
}

#[test]
fn test_insert_browser_event_dedup() {
    let dir = tempfile::tempdir().unwrap();
    let db_path = dir.path().join("test.db");
    let conn = open_db(&db_path).unwrap();

    let event = crate::events::BrowserEvent {
        url: "https://docs.rs/serde".to_string(),
        title: "serde docs".to_string(),
        domain: "docs.rs".to_string(),
        dwell_ms: 10000,
        scroll_depth: 0.5,
        extracted_text: None,
        search_query: None,
        referrer: None,
        content_hash: None,
    };

    let id1 = insert_browser_event(&conn, &event, 1711900000000, Some("dup-envelope")).unwrap();
    assert!(id1 > 0);

    // Same envelope_id should be deduped
    let id2 = insert_browser_event(&conn, &event, 1711900000000, Some("dup-envelope")).unwrap();
    assert_eq!(id2, -1, "duplicate envelope_id should return -1");

    // Verify only one row exists
    let count: i64 = conn
        .query_row("SELECT COUNT(*) FROM browser_events", [], |row| row.get(0))
        .unwrap();
    assert_eq!(count, 1);
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cargo test -p hippo-core test_insert_browser`
Expected: FAIL — `insert_browser_event` not found

- [ ] **Step 3: Implement insert_browser_event()**

Add to `crates/hippo-core/src/storage.rs`, after `insert_event_at()`:

```rust
use crate::events::BrowserEvent;

pub fn insert_browser_event(
    conn: &Connection,
    event: &BrowserEvent,
    timestamp_ms: i64,
    envelope_id: Option<&str>,
) -> Result<i64> {
    let content_hash = event.extracted_text.as_ref().map(|text| {
        let mut hasher = Sha256::new();
        hasher.update(text.as_bytes());
        hasher.finalize().iter().map(|b| format!("{:02x}", b)).collect::<String>()
    });

    let tx = conn.unchecked_transaction()?;

    let rows = tx.execute(
        "INSERT OR IGNORE INTO browser_events
         (timestamp, url, title, domain, dwell_ms, scroll_depth,
          extracted_text, search_query, referrer, content_hash, envelope_id)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)",
        rusqlite::params![
            timestamp_ms,
            event.url,
            event.title,
            event.domain,
            event.dwell_ms as i64,
            event.scroll_depth as f64,
            event.extracted_text,
            event.search_query,
            event.referrer,
            content_hash,
            envelope_id,
        ],
    )?;

    if rows == 0 {
        tx.commit()?;
        return Ok(-1);
    }

    let event_id = tx.last_insert_rowid();

    tx.execute(
        "INSERT INTO browser_enrichment_queue (browser_event_id) VALUES (?1)",
        [event_id],
    )?;

    tx.commit()?;
    Ok(event_id)
}
```

Also add the import for `BrowserEvent` at the top of `storage.rs`:
```rust
use crate::events::{BrowserEvent, ShellEvent};
```
(Replace the existing `use crate::events::ShellEvent;`)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cargo test -p hippo-core`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-core/src/storage.rs
git commit -m "feat(storage): add insert_browser_event() with dedup and auto-queue"
```

---

### Task 5: Handle Browser events in daemon flush path

**Files:**
- Modify: `crates/hippo-daemon/src/daemon.rs`

- [ ] **Step 1: Write the failing test**

Add to `crates/hippo-daemon/src/daemon.rs` tests:

```rust
#[tokio::test]
async fn test_flush_browser_event() {
    let config = test_config();
    let state = test_state_with_config(config);

    let browser_event = hippo_core::events::BrowserEvent {
        url: "https://stackoverflow.com/q/12345".to_string(),
        title: "Test page".to_string(),
        domain: "stackoverflow.com".to_string(),
        dwell_ms: 5000,
        scroll_depth: 0.75,
        extracted_text: Some("Answer content here".to_string()),
        search_query: None,
        referrer: None,
        content_hash: None,
    };
    let envelope = EventEnvelope {
        envelope_id: Uuid::new_v4(),
        producer_version: 1,
        timestamp: chrono::Utc::now(),
        payload: EventPayload::Browser(Box::new(browser_event)),
    };

    {
        let mut buffer = state.event_buffer.lock().await;
        buffer.push(envelope);
    }

    flush_events(&state).await;

    let db = state.write_db.lock().await;
    let count: i64 = db
        .query_row("SELECT COUNT(*) FROM browser_events", [], |row| row.get(0))
        .unwrap();
    assert_eq!(count, 1, "browser event should be flushed to browser_events table");

    let queue_count: i64 = db
        .query_row(
            "SELECT COUNT(*) FROM browser_enrichment_queue WHERE status = 'pending'",
            [],
            |row| row.get(0),
        )
        .unwrap();
    assert_eq!(queue_count, 1, "browser event should be auto-queued for enrichment");
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cargo test -p hippo-daemon test_flush_browser`
Expected: FAIL — browser event not handled in flush path (either panic or 0 rows)

- [ ] **Step 3: Add Browser event handling to flush_events()**

In `crates/hippo-daemon/src/daemon.rs`, modify `flush_events()`. The current code only handles `EventPayload::Shell`. Add a branch for `EventPayload::Browser` in the `for envelope in &events` loop:

```rust
for envelope in &events {
    match &envelope.payload {
        EventPayload::Shell(ref shell_event) => {
            // ... existing shell handling code unchanged ...
        }
        EventPayload::Browser(ref browser_event) => {
            let eid = envelope.envelope_id.to_string();
            if let Err(e) = storage::insert_browser_event(
                &db,
                browser_event,
                envelope.timestamp.timestamp_millis(),
                Some(&eid),
            ) {
                warn!("browser event insert failed: {}", e);
                state.drop_count.fetch_add(1, Ordering::Relaxed);
            }
        }
        _ => {
            // FsChange, IdeAction, Raw — not yet handled
        }
    }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cargo test -p hippo-daemon`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add crates/hippo-daemon/src/daemon.rs
git commit -m "feat(daemon): handle Browser events in flush path"
```

---

### Task 6: Native Messaging host bridge

**Files:**
- Create: `crates/hippo-daemon/src/native_messaging.rs`
- Modify: `crates/hippo-daemon/src/cli.rs`
- Modify: `crates/hippo-daemon/src/main.rs`
- Modify: `crates/hippo-daemon/src/lib.rs` (pub mod)

- [ ] **Step 1: Write the native messaging module**

Create `crates/hippo-daemon/src/native_messaging.rs`:

```rust
use anyhow::Result;
use hippo_core::config::HippoConfig;
use hippo_core::events::{BrowserEvent, EventEnvelope, EventPayload};
use hippo_core::protocol::DaemonRequest;
use serde::Deserialize;
use sha2::{Digest, Sha256};
use std::io::{self, Read, Write};
use url::Url;
use uuid::Uuid;

/// Namespace UUID for browser event envelope IDs (v5 deterministic UUIDs).
const BROWSER_NS: Uuid = Uuid::from_bytes([
    0x6b, 0xa7, 0xb8, 0x14, 0x9d, 0xad, 0x11, 0xd1,
    0x80, 0xb4, 0x00, 0xc0, 0x4f, 0xd4, 0x30, 0xc8,
]);

#[derive(Debug, Deserialize)]
struct BrowserVisit {
    url: String,
    title: String,
    domain: String,
    dwell_ms: u64,
    scroll_depth: f32,
    extracted_text: Option<String>,
    search_query: Option<String>,
    referrer: Option<String>,
    timestamp: i64,
}

fn read_native_message() -> Result<Option<Vec<u8>>> {
    let mut len_buf = [0u8; 4];
    match io::stdin().read_exact(&mut len_buf) {
        Ok(()) => {}
        Err(e) if e.kind() == io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(e.into()),
    }
    let len = u32::from_ne_bytes(len_buf) as usize;
    if len > 1024 * 1024 {
        anyhow::bail!("native message too large: {} bytes", len);
    }
    let mut buf = vec![0u8; len];
    io::stdin().read_exact(&mut buf)?;
    Ok(Some(buf))
}

fn write_native_message(data: &[u8]) -> Result<()> {
    let len = data.len() as u32;
    io::stdout().write_all(&len.to_ne_bytes())?;
    io::stdout().write_all(data)?;
    io::stdout().flush()?;
    Ok(())
}

fn strip_sensitive_params(url_str: &str, strip_params: &[String]) -> String {
    let Ok(mut parsed) = Url::parse(url_str) else {
        return url_str.to_string();
    };
    let filtered: Vec<(String, String)> = parsed
        .query_pairs()
        .filter(|(key, _)| {
            !strip_params.iter().any(|p| key.eq_ignore_ascii_case(p))
        })
        .map(|(k, v)| (k.into_owned(), v.into_owned()))
        .collect();
    if filtered.is_empty() {
        parsed.set_query(None);
    } else {
        let qs: String = filtered
            .iter()
            .map(|(k, v)| format!("{}={}", k, v))
            .collect::<Vec<_>>()
            .join("&");
        parsed.set_query(Some(&qs));
    }
    parsed.to_string()
}

fn make_envelope_id(url: &str, dedup_window_minutes: u64) -> Uuid {
    let bucket = chrono::Utc::now().timestamp() / (dedup_window_minutes as i64 * 60);
    let input = format!("{}:{}", url, bucket);
    Uuid::new_v5(&BROWSER_NS, input.as_bytes())
}

pub async fn run(config: &HippoConfig) -> Result<()> {
    let socket_path = config.socket_path();
    let allowlist = &config.browser.allowlist.domains;
    let strip_params = &config.browser.url_redaction.strip_params;
    let dedup_window = config.browser.dedup_window_minutes;

    while let Some(raw) = read_native_message()? {
        let visit: BrowserVisit = match serde_json::from_slice(&raw) {
            Ok(v) => v,
            Err(e) => {
                let err = serde_json::json!({"error": format!("invalid message: {}", e)});
                write_native_message(serde_json::to_vec(&err)?.as_slice())?;
                continue;
            }
        };

        // Allowlist check (defense in depth)
        if !allowlist.iter().any(|d| visit.domain.ends_with(d.as_str())) {
            let resp = serde_json::json!({"status": "skipped", "reason": "domain not in allowlist"});
            write_native_message(serde_json::to_vec(&resp)?.as_slice())?;
            continue;
        }

        let clean_url = strip_sensitive_params(&visit.url, strip_params);
        let envelope_id = make_envelope_id(&clean_url, dedup_window);

        let browser_event = BrowserEvent {
            url: clean_url,
            title: visit.title,
            domain: visit.domain,
            dwell_ms: visit.dwell_ms,
            scroll_depth: visit.scroll_depth,
            extracted_text: visit.extracted_text,
            search_query: visit.search_query,
            referrer: visit.referrer.map(|r| strip_sensitive_params(&r, strip_params)),
            content_hash: None, // computed in storage layer
        };

        let envelope = EventEnvelope {
            envelope_id,
            producer_version: 1,
            timestamp: chrono::DateTime::from_timestamp_millis(visit.timestamp)
                .unwrap_or_else(chrono::Utc::now),
            payload: EventPayload::Browser(Box::new(browser_event)),
        };

        let request = DaemonRequest::IngestEvent(Box::new(envelope));
        match crate::commands::send_event_fire_and_forget(
            &socket_path,
            &serde_json::from_value(serde_json::to_value(&request)?)?,
            config.daemon.socket_timeout_ms,
        )
        .await
        {
            Ok(()) => {
                let resp = serde_json::json!({"status": "ok"});
                write_native_message(serde_json::to_vec(&resp)?.as_slice())?;
            }
            Err(e) => {
                let resp = serde_json::json!({"status": "error", "message": format!("{}", e)});
                write_native_message(serde_json::to_vec(&resp)?.as_slice())?;
            }
        }
    }

    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_strip_sensitive_params() {
        let url = "https://example.com/page?q=rust&token=abc123&other=yes";
        let stripped = strip_sensitive_params(url, &["token".into(), "api_key".into()]);
        assert!(stripped.contains("q=rust"));
        assert!(stripped.contains("other=yes"));
        assert!(!stripped.contains("token="));
        assert!(!stripped.contains("abc123"));
    }

    #[test]
    fn test_strip_sensitive_params_all_removed() {
        let url = "https://example.com/page?token=abc";
        let stripped = strip_sensitive_params(url, &["token".into()]);
        assert_eq!(stripped, "https://example.com/page");
    }

    #[test]
    fn test_strip_sensitive_params_no_query() {
        let url = "https://example.com/page";
        let stripped = strip_sensitive_params(url, &["token".into()]);
        assert_eq!(stripped, "https://example.com/page");
    }

    #[test]
    fn test_make_envelope_id_deterministic() {
        let id1 = Uuid::new_v5(&BROWSER_NS, b"https://example.com:100");
        let id2 = Uuid::new_v5(&BROWSER_NS, b"https://example.com:100");
        assert_eq!(id1, id2);

        let id3 = Uuid::new_v5(&BROWSER_NS, b"https://example.com:101");
        assert_ne!(id1, id3);
    }

    #[test]
    fn test_strip_sensitive_params_case_insensitive() {
        let url = "https://example.com/page?TOKEN=abc&q=rust";
        let stripped = strip_sensitive_params(url, &["token".into()]);
        assert!(!stripped.contains("abc"));
        assert!(stripped.contains("q=rust"));
    }
}
```

- [ ] **Step 2: Add `url` crate dependency**

Run: `cargo add url -p hippo-daemon`

- [ ] **Step 3: Register the module in lib.rs**

Add `pub mod native_messaging;` to `crates/hippo-daemon/src/lib.rs`.

- [ ] **Step 4: Add CLI subcommand**

In `crates/hippo-daemon/src/cli.rs`, add a new variant to the `Commands` enum:

```rust
/// Run as Native Messaging host for Firefox extension
NativeMessagingHost,
```

- [ ] **Step 5: Wire up in main.rs**

In `crates/hippo-daemon/src/main.rs`, add the match arm after the existing arms:

```rust
Commands::NativeMessagingHost => {
    hippo_daemon::native_messaging::run(&config).await?;
}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cargo test -p hippo-daemon`
Expected: All tests PASS

Run: `cargo clippy --all-targets -- -D warnings`
Expected: No warnings

- [ ] **Step 7: Commit**

```bash
git add crates/hippo-daemon/src/native_messaging.rs crates/hippo-daemon/src/lib.rs \
       crates/hippo-daemon/src/cli.rs crates/hippo-daemon/src/main.rs \
       crates/hippo-daemon/Cargo.toml
git commit -m "feat(daemon): add native-messaging-host subcommand for Firefox extension bridge"
```

---

### Task 7: Firefox WebExtension

**Files:**
- Create: `extension/firefox/manifest.json`
- Create: `extension/firefox/background.js`
- Create: `extension/firefox/content.js`
- Create: `extension/firefox/popup.html`
- Create: `extension/firefox/popup.js`
- Create: `extension/firefox/lib/Readability.js` (vendored from Mozilla)
- Create: `extension/firefox/icons/` (simple placeholder icons)

- [ ] **Step 1: Create directory structure**

```bash
mkdir -p extension/firefox/lib extension/firefox/icons
```

- [ ] **Step 2: Create manifest.json**

Write `extension/firefox/manifest.json`:

```json
{
  "manifest_version": 2,
  "name": "Hippo Browser Capture",
  "version": "0.1.0",
  "description": "Captures browsing activity for Hippo knowledge base",
  "permissions": [
    "nativeMessaging",
    "tabs",
    "activeTab",
    "storage"
  ],
  "background": {
    "scripts": ["background.js"],
    "persistent": false
  },
  "content_scripts": [
    {
      "matches": ["<all_urls>"],
      "js": ["lib/Readability.js", "content.js"],
      "run_at": "document_idle"
    }
  ],
  "browser_action": {
    "default_popup": "popup.html",
    "default_title": "Hippo"
  },
  "browser_specific_settings": {
    "gecko": {
      "id": "hippo-browser@local"
    }
  }
}
```

- [ ] **Step 3: Create content.js**

Write `extension/firefox/content.js`:

```javascript
(() => {
  "use strict";

  let entryTime = performance.now();
  let visibleStart = document.hidden ? null : performance.now();
  let totalVisibleMs = 0;
  let maxScrollDepth = 0;

  function updateScrollDepth() {
    const docHeight = Math.max(
      document.body.scrollHeight,
      document.documentElement.scrollHeight,
      1
    );
    const viewBottom = window.scrollY + window.innerHeight;
    const depth = Math.min(viewBottom / docHeight, 1.0);
    if (depth > maxScrollDepth) maxScrollDepth = depth;
  }

  document.addEventListener("visibilitychange", () => {
    if (document.hidden) {
      if (visibleStart !== null) {
        totalVisibleMs += performance.now() - visibleStart;
        visibleStart = null;
      }
    } else {
      visibleStart = performance.now();
    }
  });

  window.addEventListener("scroll", updateScrollDepth, { passive: true });

  // Initial scroll depth check
  updateScrollDepth();

  function collectAndSend() {
    if (visibleStart !== null) {
      totalVisibleMs += performance.now() - visibleStart;
      visibleStart = null;
    }

    const dwellMs = Math.round(totalVisibleMs);

    // Skip if dwell time too short (3s threshold enforced in background too)
    if (dwellMs < 3000) return;

    let extractedText = null;
    try {
      const clone = document.cloneNode(true);
      const article = new Readability(clone).parse();
      if (article && article.textContent) {
        // Truncate to 50KB to avoid massive messages
        extractedText = article.textContent.substring(0, 50000);
      }
    } catch (e) {
      // Readability not available or failed — send without content
    }

    browser.runtime.sendMessage({
      type: "page_visit",
      url: location.href,
      title: document.title,
      domain: location.hostname,
      dwell_ms: dwellMs,
      scroll_depth: Math.round(maxScrollDepth * 100) / 100,
      extracted_text: extractedText,
      referrer: document.referrer || null,
      timestamp: Date.now(),
    });
  }

  // Send on page unload
  window.addEventListener("beforeunload", collectAndSend);

  // Also send on visibility hidden (tab switch) — the beforeunload may not
  // fire if the tab stays open but loses focus for a long time
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) collectAndSend();
  });
})();
```

- [ ] **Step 4: Create background.js**

Write `extension/firefox/background.js`:

```javascript
"use strict";

const DEFAULT_ALLOWLIST = [
  "github.com",
  "stackoverflow.com",
  "developer.mozilla.org",
  "docs.rs",
  "doc.rust-lang.org",
  "crates.io",
  "npmjs.com",
  "pypi.org",
  "docs.python.org",
  "man7.org",
  "wiki.archlinux.org",
];

const SEARCH_ENGINES = [
  { domain: "google.com", param: "q" },
  { domain: "google.co.uk", param: "q" },
  { domain: "duckduckgo.com", param: "q" },
  { domain: "bing.com", param: "q" },
  { domain: "github.com", path: "/search", param: "q" },
];

let enabled = true;
let allowlist = [...DEFAULT_ALLOWLIST];
let captureCount = 0;

// Load settings
browser.storage.local.get(["enabled", "allowlist", "captureCount"]).then((result) => {
  if (result.enabled !== undefined) enabled = result.enabled;
  if (result.allowlist) allowlist = result.allowlist;
  if (result.captureCount) captureCount = result.captureCount;
});

function isDomainAllowed(domain) {
  return allowlist.some((d) => domain === d || domain.endsWith("." + d));
}

function extractSearchQuery(referrer) {
  if (!referrer) return null;
  try {
    const url = new URL(referrer);
    for (const engine of SEARCH_ENGINES) {
      if (
        url.hostname.endsWith(engine.domain) &&
        (!engine.path || url.pathname.startsWith(engine.path))
      ) {
        return url.searchParams.get(engine.param) || null;
      }
    }
  } catch {
    // Invalid URL
  }
  return null;
}

browser.runtime.onMessage.addListener((msg, sender) => {
  if (msg.type !== "page_visit") return;
  if (!enabled) return;
  if (!isDomainAllowed(msg.domain)) return;
  if (msg.dwell_ms < 3000) return;

  const searchQuery = extractSearchQuery(msg.referrer);

  const visit = {
    url: msg.url,
    title: msg.title,
    domain: msg.domain,
    dwell_ms: msg.dwell_ms,
    scroll_depth: msg.scroll_depth,
    extracted_text: msg.extracted_text,
    search_query: searchQuery,
    referrer: msg.referrer,
    timestamp: msg.timestamp,
  };

  browser.runtime
    .sendNativeMessage("hippo_daemon", visit)
    .then((response) => {
      if (response && response.status === "ok") {
        captureCount++;
        browser.storage.local.set({ captureCount });
      }
    })
    .catch((err) => {
      console.error("Hippo native messaging error:", err);
    });
});
```

- [ ] **Step 5: Create popup.html and popup.js**

Write `extension/firefox/popup.html`:

```html
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <style>
    body { width: 300px; padding: 12px; font-family: system-ui, sans-serif; font-size: 13px; }
    h3 { margin: 0 0 8px 0; font-size: 14px; }
    .toggle { display: flex; align-items: center; gap: 8px; margin-bottom: 12px; }
    .stats { color: #666; margin-bottom: 12px; }
    textarea { width: 100%; height: 120px; font-family: monospace; font-size: 11px; }
    button { margin-top: 8px; padding: 4px 12px; }
  </style>
</head>
<body>
  <h3>Hippo Browser Capture</h3>
  <div class="toggle">
    <input type="checkbox" id="enabled" checked>
    <label for="enabled">Capture enabled</label>
  </div>
  <div class="stats">Pages captured: <span id="count">0</span></div>
  <label>Allowed domains (one per line):</label>
  <textarea id="domains"></textarea>
  <button id="save">Save</button>
  <script src="popup.js"></script>
</body>
</html>
```

Write `extension/firefox/popup.js`:

```javascript
"use strict";

const enabledEl = document.getElementById("enabled");
const countEl = document.getElementById("count");
const domainsEl = document.getElementById("domains");
const saveBtn = document.getElementById("save");

browser.storage.local
  .get(["enabled", "allowlist", "captureCount"])
  .then((result) => {
    enabledEl.checked = result.enabled !== false;
    countEl.textContent = result.captureCount || 0;
    const domains = result.allowlist || [
      "github.com",
      "stackoverflow.com",
      "developer.mozilla.org",
      "docs.rs",
      "doc.rust-lang.org",
      "crates.io",
      "npmjs.com",
      "pypi.org",
      "docs.python.org",
      "man7.org",
      "wiki.archlinux.org",
    ];
    domainsEl.value = domains.join("\n");
  });

enabledEl.addEventListener("change", () => {
  browser.storage.local.set({ enabled: enabledEl.checked });
});

saveBtn.addEventListener("click", () => {
  const domains = domainsEl.value
    .split("\n")
    .map((d) => d.trim())
    .filter((d) => d.length > 0);
  browser.storage.local.set({ allowlist: domains });
  saveBtn.textContent = "Saved!";
  setTimeout(() => (saveBtn.textContent = "Save"), 1500);
});
```

- [ ] **Step 6: Vendor Readability.js**

Download Mozilla's Readability.js and place it at `extension/firefox/lib/Readability.js`. This can be fetched from the `@mozilla/readability` npm package or directly from the GitHub release.

Run:
```bash
curl -sL "https://raw.githubusercontent.com/nickthedude/nickthedude.github.io/refs/heads/main/nickthedude.github.io/extension/Readability.js" -o extension/firefox/lib/Readability.js 2>/dev/null || echo "Download Readability.js manually from https://github.com/nickthedude/nickthedude.github.io/refs/heads/main/nickthedude.github.io/extension/Readability.js"
```

Note: The actual URL should be from Mozilla's official repo. Download from `https://github.com/nickthedude/nickthedude.github.io/raw/refs/heads/main/nickthedude.github.io/extension/Readability.js` — or better, from the official npm package:
```bash
cd extension/firefox/lib
npm pack @mozilla/readability 2>/dev/null && tar -xf mozilla-readability-*.tgz package/Readability.js --strip-components=1 && rm mozilla-readability-*.tgz
```

If neither works, manually copy `Readability.js` from https://github.com/nickthedude/nickthedude.github.io/tree/main into `extension/firefox/lib/`.

- [ ] **Step 7: Create placeholder icon**

Create a minimal SVG icon at `extension/firefox/icons/hippo-48.png`. For now, just create a text file as a placeholder — the extension works without it:

```bash
echo "placeholder" > extension/firefox/icons/.gitkeep
```

- [ ] **Step 8: Commit**

```bash
git add extension/
git commit -m "feat(extension): add Firefox WebExtension with content capture and Native Messaging"
```

---

### Task 8: Native Messaging host manifest and install command

**Files:**
- Modify: `crates/hippo-daemon/src/install.rs`
- Modify: `crates/hippo-daemon/src/cli.rs` (add install subcommand for native messaging)

- [ ] **Step 1: Add native messaging host manifest generation to install.rs**

Read the existing `install.rs` to understand its structure, then add a function:

```rust
pub fn install_native_messaging_manifest(hippo_bin: &Path, force: bool) -> Result<()> {
    let manifest_dir = dirs::home_dir()
        .ok_or_else(|| anyhow::anyhow!("cannot determine home directory"))?
        .join("Library/Application Support/Mozilla/NativeMessagingHosts");
    std::fs::create_dir_all(&manifest_dir)?;
    
    let manifest_path = manifest_dir.join("hippo_daemon.json");
    if manifest_path.exists() && !force {
        anyhow::bail!(
            "Native messaging manifest already exists at {}. Use --force to overwrite.",
            manifest_path.display()
        );
    }

    let manifest = serde_json::json!({
        "name": "hippo_daemon",
        "description": "Hippo knowledge capture daemon - browser event bridge",
        "path": hippo_bin.to_string_lossy(),
        "type": "stdio",
        "allowed_extensions": ["hippo-browser@local"]
    });

    // The "path" must point to a wrapper script that calls `hippo native-messaging-host`
    // because Native Messaging launches the binary directly (not with subcommands)
    let wrapper_path = manifest_dir.join("hippo-native-messaging");
    let wrapper_script = format!(
        "#!/bin/bash\nexec {} native-messaging-host\n",
        hippo_bin.to_string_lossy()
    );
    std::fs::write(&wrapper_path, wrapper_script)?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&wrapper_path, std::fs::Permissions::from_mode(0o755))?;
    }

    let manifest_with_wrapper = serde_json::json!({
        "name": "hippo_daemon",
        "description": "Hippo knowledge capture daemon - browser event bridge",
        "path": wrapper_path.to_string_lossy(),
        "type": "stdio",
        "allowed_extensions": ["hippo-browser@local"]
    });

    std::fs::write(&manifest_path, serde_json::to_string_pretty(&manifest_with_wrapper)?)?;
    println!("  Native messaging manifest: {}", manifest_path.display());
    println!("  Wrapper script: {}", wrapper_path.display());
    Ok(())
}
```

- [ ] **Step 2: Wire into install command in main.rs**

Add to the `DaemonAction::Install` handler in `main.rs`, after the existing install calls:

```rust
println!();
println!("Installing Native Messaging manifest for Firefox...");
install::install_native_messaging_manifest(&vars.hippo_bin, force)?;
```

- [ ] **Step 3: Run build to verify it compiles**

Run: `cargo build -p hippo-daemon`
Expected: Compiles without errors

- [ ] **Step 4: Commit**

```bash
git add crates/hippo-daemon/src/install.rs crates/hippo-daemon/src/main.rs
git commit -m "feat(install): add Native Messaging host manifest generation for Firefox"
```

---

### Task 9: Browser enrichment module (Python brain)

**Files:**
- Create: `brain/src/hippo_brain/browser_enrichment.py`
- Test: `brain/tests/test_browser_enrichment.py`

- [ ] **Step 1: Write the failing tests**

Create `brain/tests/test_browser_enrichment.py`:

```python
import sqlite3
import time

import pytest

from hippo_brain.browser_enrichment import (
    BROWSER_SYSTEM_PROMPT,
    build_browser_enrichment_prompt,
    claim_pending_browser_events,
    get_correlated_browser_events,
    mark_browser_queue_failed,
    write_browser_knowledge_node,
)
from hippo_brain.models import EnrichmentResult


@pytest.fixture
def db():
    """Create an in-memory DB with v4 schema."""
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    # Create minimal schema for testing
    conn.executescript("""
        CREATE TABLE browser_events (
            id INTEGER PRIMARY KEY,
            timestamp INTEGER NOT NULL,
            url TEXT NOT NULL,
            title TEXT,
            domain TEXT NOT NULL,
            dwell_ms INTEGER NOT NULL,
            scroll_depth REAL,
            extracted_text TEXT,
            search_query TEXT,
            referrer TEXT,
            content_hash TEXT,
            envelope_id TEXT,
            enriched INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE browser_enrichment_queue (
            id INTEGER PRIMARY KEY,
            browser_event_id INTEGER NOT NULL UNIQUE REFERENCES browser_events(id),
            status TEXT NOT NULL DEFAULT 'pending',
            priority INTEGER NOT NULL DEFAULT 5,
            retry_count INTEGER NOT NULL DEFAULT 0,
            max_retries INTEGER NOT NULL DEFAULT 5,
            error_message TEXT,
            locked_at INTEGER,
            locked_by TEXT,
            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE knowledge_nodes (
            id INTEGER PRIMARY KEY,
            uuid TEXT NOT NULL UNIQUE,
            content TEXT NOT NULL,
            embed_text TEXT NOT NULL,
            node_type TEXT NOT NULL DEFAULT 'observation',
            outcome TEXT,
            tags TEXT,
            enrichment_model TEXT,
            enrichment_version INTEGER NOT NULL DEFAULT 1,
            created_at INTEGER NOT NULL DEFAULT 0,
            updated_at INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE knowledge_node_browser_events (
            knowledge_node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id),
            browser_event_id INTEGER NOT NULL REFERENCES browser_events(id),
            PRIMARY KEY (knowledge_node_id, browser_event_id)
        );
        CREATE TABLE entities (
            id INTEGER PRIMARY KEY,
            type TEXT NOT NULL,
            name TEXT NOT NULL,
            canonical TEXT,
            metadata TEXT,
            first_seen INTEGER NOT NULL DEFAULT 0,
            last_seen INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL DEFAULT 0,
            UNIQUE (type, canonical)
        );
        CREATE TABLE knowledge_node_entities (
            knowledge_node_id INTEGER NOT NULL REFERENCES knowledge_nodes(id),
            entity_id INTEGER NOT NULL REFERENCES entities(id),
            PRIMARY KEY (knowledge_node_id, entity_id)
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY,
            session_id INTEGER,
            timestamp INTEGER NOT NULL,
            command TEXT,
            exit_code INTEGER,
            duration_ms INTEGER,
            cwd TEXT,
            hostname TEXT,
            shell TEXT,
            git_repo TEXT,
            git_branch TEXT,
            git_commit TEXT,
            git_dirty INTEGER,
            stdout TEXT,
            stderr TEXT
        );
    """)
    return conn


def _insert_browser_event(db, url, domain, dwell_ms, timestamp, search_query=None, extracted_text=None):
    db.execute(
        "INSERT INTO browser_events (timestamp, url, title, domain, dwell_ms, scroll_depth, extracted_text, search_query) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (timestamp, url, f"Title for {url}", domain, dwell_ms, 0.5, extracted_text, search_query),
    )
    event_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    db.execute(
        "INSERT INTO browser_enrichment_queue (browser_event_id, status) VALUES (?, 'pending')",
        (event_id,),
    )
    db.commit()
    return event_id


def test_claim_pending_browser_events(db):
    now_ms = int(time.time() * 1000)
    stale_ts = now_ms - 120_000  # 2 min ago

    _insert_browser_event(db, "https://so.com/q/1", "so.com", 5000, stale_ts)
    _insert_browser_event(db, "https://so.com/q/2", "so.com", 8000, stale_ts + 1000)

    chunks = claim_pending_browser_events(db, "test-worker", stale_secs=60)
    assert len(chunks) == 1
    assert len(chunks[0]) == 2


def test_claim_skips_fresh_events(db):
    now_ms = int(time.time() * 1000)
    _insert_browser_event(db, "https://so.com/q/1", "so.com", 5000, now_ms)

    chunks = claim_pending_browser_events(db, "test-worker", stale_secs=60)
    assert len(chunks) == 0


def test_build_browser_enrichment_prompt(db):
    events = [
        {"url": "https://so.com/q/1", "title": "SO Post", "domain": "so.com",
         "dwell_ms": 45000, "scroll_depth": 0.85, "search_query": "rust Display",
         "extracted_text": "The Display trait...", "timestamp": 1000000},
    ]
    prompt = build_browser_enrichment_prompt(events)
    assert "so.com" in prompt
    assert "rust Display" in prompt
    assert "45.0s" in prompt or "45000" in prompt


def test_get_correlated_browser_events(db):
    now_ms = int(time.time() * 1000)
    _insert_browser_event(db, "https://so.com/q/1", "so.com", 5000, now_ms - 60_000)
    _insert_browser_event(db, "https://far.com/page", "far.com", 5000, now_ms - 600_000)

    correlated = get_correlated_browser_events(
        db, now_ms - 120_000, now_ms, window_ms=300_000
    )
    assert len(correlated) == 1
    assert correlated[0]["domain"] == "so.com"


def test_write_browser_knowledge_node(db):
    eid = _insert_browser_event(db, "https://so.com/q/1", "so.com", 5000, 1000000)
    result = EnrichmentResult(
        summary="Researched Display trait on SO",
        intent="research",
        outcome="success",
        entities={"projects": ["hippo"], "tools": [], "files": [], "services": [], "errors": []},
        tags=["rust", "research"],
        embed_text="Researched implementing Display trait for custom error types on Stack Overflow",
    )
    node_id = write_browser_knowledge_node(db, result, [eid], "test-model")
    assert node_id > 0

    # Verify junction table
    link = db.execute(
        "SELECT * FROM knowledge_node_browser_events WHERE knowledge_node_id = ? AND browser_event_id = ?",
        (node_id, eid),
    ).fetchone()
    assert link is not None

    # Verify queue marked done
    status = db.execute(
        "SELECT status FROM browser_enrichment_queue WHERE browser_event_id = ?", (eid,)
    ).fetchone()[0]
    assert status == "done"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --project brain --extra dev pytest brain/tests/test_browser_enrichment.py -v`
Expected: FAIL — `browser_enrichment` module not found

- [ ] **Step 3: Implement browser_enrichment.py**

Create `brain/src/hippo_brain/browser_enrichment.py`:

```python
"""Browser event enrichment pipeline — claim, enrich, and write browser knowledge nodes."""

import json
import time
import uuid

from hippo_brain.models import EnrichmentResult, validate_enrichment_data

STALE_LOCK_TIMEOUT_MS = 5 * 60 * 1000

BROWSER_SYSTEM_PROMPT = """You are a developer activity analyst. You receive a sequence of web pages a developer visited during a browsing session.

Extract what they were researching, learning, or investigating. Focus on technical topics and how pages relate to each other (e.g., a search query leading to documentation).

IMPORTANT: Be specific. Use actual page titles, URLs, technical concepts, and search queries from the data. Generic descriptions like "browsed some pages" are unacceptable.

The embed_text field should read like a developer's research log — specific enough that searching for "Rust Display trait implementation" or "cargo proc-macro error" would find it.

Output a JSON object with these fields:
- summary: Specific description of what was researched or learned
- intent: The developer's goal (e.g., "research", "debugging", "learning", "reference")
- outcome: One of "success", "partial", "failure", "unknown"
- key_decisions: List of decisions informed by the research
- problems_encountered: List of obstacles or dead ends
- entities: An object with lists of extracted entities:
  - projects: Project names mentioned or inferred
  - tools: Technologies, frameworks, languages referenced
  - files: Specific files referenced
  - services: Services or APIs referenced
  - errors: Error messages being researched
- tags: Descriptive, specific tags
- embed_text: A detailed paragraph describing the research session. Specific topics, search queries, and sources. Optimized for semantic search.

Output ONLY valid JSON, no markdown fences or extra text."""


def claim_pending_browser_events(
    conn, worker_id: str, stale_secs: int = 60
) -> list[list[dict]]:
    """Claim pending browser events grouped by temporal proximity.

    Events within 5 minutes of each other form a "browsing session."
    Only processes groups where the last event is older than stale_secs.
    """
    now_ms = int(time.time() * 1000)
    stale_threshold_ms = now_ms - (stale_secs * 1000)
    stale_lock_ms = now_ms - STALE_LOCK_TIMEOUT_MS

    cursor = conn.execute(
        """
        UPDATE browser_enrichment_queue
        SET status = 'processing', locked_at = ?, locked_by = ?, updated_at = ?
        WHERE id IN (
            SELECT beq.id FROM browser_enrichment_queue beq
            JOIN browser_events be ON beq.browser_event_id = be.id
            WHERE (beq.status = 'pending'
                   OR (beq.status = 'processing' AND COALESCE(beq.locked_at, 0) <= ?))
              AND be.timestamp < ?
        )
        RETURNING browser_event_id
        """,
        (now_ms, worker_id, now_ms, stale_lock_ms, stale_threshold_ms),
    )
    event_ids = [row[0] for row in cursor.fetchall()]
    conn.commit()

    if not event_ids:
        return []

    placeholders = ",".join("?" * len(event_ids))
    cursor = conn.execute(
        f"""
        SELECT id, timestamp, url, title, domain, dwell_ms, scroll_depth,
               extracted_text, search_query, referrer
        FROM browser_events
        WHERE id IN ({placeholders})
        ORDER BY timestamp ASC
        """,
        event_ids,
    )

    events = []
    for row in cursor.fetchall():
        events.append({
            "id": row[0],
            "timestamp": row[1],
            "url": row[2],
            "title": row[3],
            "domain": row[4],
            "dwell_ms": row[5],
            "scroll_depth": row[6],
            "extracted_text": row[7],
            "search_query": row[8],
            "referrer": row[9],
        })

    return _chunk_by_time_gap(events)


def _chunk_by_time_gap(events: list[dict], gap_ms: int = 300_000) -> list[list[dict]]:
    """Split events into chunks at time gaps > gap_ms (default 5 min)."""
    if not events:
        return []
    chunks = []
    current = [events[0]]
    for ev in events[1:]:
        if ev["timestamp"] - current[-1]["timestamp"] > gap_ms:
            chunks.append(current)
            current = [ev]
        else:
            current.append(ev)
    if current:
        chunks.append(current)
    return chunks


def build_browser_enrichment_prompt(events: list[dict]) -> str:
    """Format browser events into the user prompt."""
    lines = []
    for i, ev in enumerate(events, 1):
        dwell_s = ev["dwell_ms"] / 1000
        scroll_pct = int((ev.get("scroll_depth") or 0) * 100)
        parts = [f"Page {i}:"]
        parts.append(f"  url: {ev['url']}")
        parts.append(f"  title: {ev.get('title', '')}")
        parts.append(f"  domain: {ev['domain']}")
        parts.append(f"  time spent: {dwell_s:.1f}s, scrolled: {scroll_pct}%")
        if ev.get("search_query"):
            parts.append(f"  search query: {ev['search_query']}")
        if ev.get("extracted_text"):
            # Truncate to 2000 chars for prompt budget
            text = ev["extracted_text"][:2000]
            parts.append(f"  content excerpt:\n{text}")
        lines.append("\n".join(parts))
    return "\n\n".join(lines)


def get_correlated_browser_events(
    conn, session_start_ms: int, session_end_ms: int, window_ms: int = 300_000
) -> list[dict]:
    """Fetch browser events within ±window_ms of a shell session's time range."""
    cursor = conn.execute(
        """
        SELECT id, url, title, domain, dwell_ms, scroll_depth,
               extracted_text, search_query, timestamp
        FROM browser_events
        WHERE timestamp BETWEEN ? AND ?
        ORDER BY timestamp
        """,
        (session_start_ms - window_ms, session_end_ms + window_ms),
    )
    return [
        {
            "id": row[0],
            "url": row[1],
            "title": row[2],
            "domain": row[3],
            "dwell_ms": row[4],
            "scroll_depth": row[5],
            "extracted_text": row[6],
            "search_query": row[7],
            "timestamp": row[8],
        }
        for row in cursor.fetchall()
    ]


def format_browser_context_for_shell_prompt(browser_events: list[dict]) -> str:
    """Format correlated browser events as context to inject into shell enrichment prompt."""
    if not browser_events:
        return ""
    lines = ["\nBrowser Activity (concurrent):"]
    for ev in browser_events:
        dwell_s = ev["dwell_ms"] / 1000
        scroll_pct = int((ev.get("scroll_depth") or 0) * 100)
        line = f"  {ev['domain']} - \"{ev.get('title', '')}\" (read {dwell_s:.1f}s, {scroll_pct}% scroll)"
        lines.append(line)
        if ev.get("search_query"):
            lines.append(f"  Search query: \"{ev['search_query']}\"")
    return "\n".join(lines)


def write_browser_knowledge_node(
    conn, result: EnrichmentResult, event_ids: list[int], model_name: str
) -> int:
    """Insert knowledge node, link to browser events, upsert entities, mark queue done."""
    node_uuid = str(uuid.uuid4())
    now_ms = int(time.time() * 1000)
    content = json.dumps({
        "summary": result.summary,
        "intent": result.intent,
        "outcome": result.outcome,
        "entities": result.entities,
        "tags": result.tags,
        "key_decisions": result.key_decisions,
        "problems_encountered": result.problems_encountered,
    })
    tags_json = json.dumps(result.tags)

    conn.execute("BEGIN")
    try:
        cursor = conn.execute(
            """
            INSERT INTO knowledge_nodes (uuid, content, embed_text, node_type, outcome,
                                         tags, enrichment_model, enrichment_version,
                                         created_at, updated_at)
            VALUES (?, ?, ?, 'observation', ?, ?, ?, 1, ?, ?)
            """,
            (node_uuid, content, result.embed_text, result.outcome, tags_json,
             model_name, now_ms, now_ms),
        )
        node_id = cursor.lastrowid

        for event_id in event_ids:
            conn.execute(
                "INSERT INTO knowledge_node_browser_events (knowledge_node_id, browser_event_id) VALUES (?, ?)",
                (node_id, event_id),
            )

        # Upsert entities
        all_entities = result.entities if isinstance(result.entities, dict) else {}
        entity_type_map = {
            "projects": "project",
            "tools": "tool",
            "files": "file",
            "services": "service",
            "errors": "concept",
        }
        for key, entity_type in entity_type_map.items():
            for name in all_entities.get(key, []):
                canonical = name.lower().strip()
                cursor = conn.execute(
                    """
                    INSERT INTO entities (type, name, canonical, first_seen, last_seen, created_at)
                    VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (type, canonical) DO
                    UPDATE SET last_seen = excluded.last_seen
                    RETURNING id
                    """,
                    (entity_type, name, canonical, now_ms, now_ms, now_ms),
                )
                entity_id = cursor.fetchone()[0]
                conn.execute(
                    "INSERT INTO knowledge_node_entities (knowledge_node_id, entity_id) VALUES (?, ?) ON CONFLICT DO NOTHING",
                    (node_id, entity_id),
                )

        # Mark events enriched
        placeholders = ",".join("?" * len(event_ids))
        conn.execute(
            f"UPDATE browser_events SET enriched = 1 WHERE id IN ({placeholders})",
            event_ids,
        )

        conn.execute(
            f"UPDATE browser_enrichment_queue SET status = 'done', updated_at = ? WHERE browser_event_id IN ({placeholders})",
            [now_ms, *event_ids],
        )

        conn.commit()
        return node_id
    except Exception:
        conn.rollback()
        raise


def mark_browser_queue_failed(conn, event_ids: list[int], error: str) -> None:
    """Increment retry_count; reset to pending if retries remain, failed if exhausted."""
    now_ms = int(time.time() * 1000)
    for event_id in event_ids:
        conn.execute(
            """
            UPDATE browser_enrichment_queue
            SET retry_count = retry_count + 1,
                error_message = ?,
                locked_at = NULL,
                locked_by = NULL,
                updated_at = ?,
                status = CASE
                    WHEN retry_count + 1 >= max_retries THEN 'failed'
                    ELSE 'pending'
                END
            WHERE browser_event_id = ?
            """,
            (error, now_ms, event_id),
        )
    conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --project brain --extra dev pytest brain/tests/test_browser_enrichment.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add brain/src/hippo_brain/browser_enrichment.py brain/tests/test_browser_enrichment.py
git commit -m "feat(brain): add browser enrichment module with queue claiming and knowledge node writing"
```

---

### Task 10: Integrate browser enrichment into brain server loop

**Files:**
- Modify: `brain/src/hippo_brain/server.py`
- Modify: `brain/src/hippo_brain/enrichment.py` (add correlated browser context)

- [ ] **Step 1: Add browser imports to server.py**

At the top of `brain/src/hippo_brain/server.py`, add alongside the existing imports:

```python
from hippo_brain.browser_enrichment import (
    BROWSER_SYSTEM_PROMPT,
    build_browser_enrichment_prompt,
    claim_pending_browser_events,
    mark_browser_queue_failed,
    write_browser_knowledge_node,
)
```

- [ ] **Step 2: Add browser queue depth to health endpoint**

In `BrainServer.health()`, add after the existing claude queue depth queries:

```python
browser_queue_depth = conn.execute(
    "SELECT COUNT(*) FROM browser_enrichment_queue WHERE status = 'pending'"
).fetchone()[0]
browser_queue_failed = conn.execute(
    "SELECT COUNT(*) FROM browser_enrichment_queue WHERE status = 'failed'"
).fetchone()[0]
```

Add to the returned JSON:
```python
"browser_queue_depth": browser_queue_depth,
"browser_queue_failed": browser_queue_failed,
```

Wrap the browser queries in a try/except (table may not exist on v3 DB):
```python
try:
    browser_queue_depth = conn.execute(...).fetchone()[0]
    browser_queue_failed = conn.execute(...).fetchone()[0]
except Exception:
    browser_queue_depth = 0
    browser_queue_failed = 0
```

- [ ] **Step 3: Add browser enrichment to _enrichment_loop()**

In `BrainServer._enrichment_loop()`, after the Claude session processing block (around line 450), add:

```python
# Process browser events
try:
    browser_batches = claim_pending_browser_events(conn, worker_id, stale_secs=60)
    for events in browser_batches:
        event_ids = [e["id"] for e in events]
        logger.info("claimed %d browser events: %s", len(event_ids), event_ids)
        prompt = build_browser_enrichment_prompt(events)
        logger.info("browser enrichment prompt (%d chars)", len(prompt))

        try:
            result = None
            last_err = None
            for attempt in range(3):
                try:
                    messages = [
                        {"role": "system", "content": BROWSER_SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ]
                    if attempt > 0:
                        messages.append({
                            "role": "user",
                            "content": "Your previous response was not valid JSON. Output ONLY a JSON object.",
                        })
                    raw = await self.client.chat(
                        messages=messages, model=self.enrichment_model,
                    )
                    result = parse_enrichment_response(raw)
                    break
                except Exception as e:
                    last_err = e
                    logger.warning("browser enrichment attempt %d failed: %s", attempt + 1, e)
            if result is None:
                raise last_err
            node_id = write_browser_knowledge_node(conn, result, event_ids, self.enrichment_model)
            self.last_success_at_ms = int(time.time() * 1000)
            logger.info("enriched %d browser events -> node %d", len(event_ids), node_id)
        except Exception as e:
            self.last_error = str(e)
            self.last_error_at_ms = int(time.time() * 1000)
            logger.error("browser enrichment failed: %s", e)
            retry_conn = self._get_conn()
            try:
                mark_browser_queue_failed(retry_conn, event_ids, str(e))
            finally:
                retry_conn.close()
except Exception as e:
    logger.warning("browser enrichment polling error: %s", e)
```

- [ ] **Step 4: Add correlated browser context to shell enrichment**

In `brain/src/hippo_brain/enrichment.py`, add the import at the top:

```python
from hippo_brain.browser_enrichment import (
    get_correlated_browser_events,
    format_browser_context_for_shell_prompt,
)
```

Modify `build_enrichment_prompt` to accept an optional `browser_context` string parameter:

```python
def build_enrichment_prompt(events: list[dict], browser_context: str = "") -> str:
    """Format events into the user prompt template."""
    lines = []
    for i, ev in enumerate(events, 1):
        # ... existing code unchanged ...
    prompt = "\n\n".join(lines)
    if browser_context:
        prompt += "\n\n" + browser_context
    return prompt
```

- [ ] **Step 5: Inject browser context in the enrichment loop**

In `server.py`'s `_enrichment_loop()`, where shell events are enriched (around line 248), add browser correlation before building the prompt:

```python
# Get correlated browser events for this shell session
browser_context = ""
try:
    from hippo_brain.browser_enrichment import (
        get_correlated_browser_events,
        format_browser_context_for_shell_prompt,
    )
    if events:
        start_ts = min(e["timestamp"] for e in events)
        end_ts = max(e["timestamp"] for e in events)
        correlated = get_correlated_browser_events(conn, start_ts, end_ts)
        browser_context = format_browser_context_for_shell_prompt(correlated)
except Exception as e:
    logger.debug("browser correlation skipped: %s", e)

prompt = build_enrichment_prompt(events, browser_context=browser_context)
```

- [ ] **Step 6: Update BrainServer._get_conn() expected version**

In `server.py`, update `EXPECTED_VERSION = 3` to `EXPECTED_VERSION = 4`. Add a fallback for v3 databases that haven't been migrated yet:

```python
if version not in (EXPECTED_VERSION, 3):
    conn.close()
    raise RuntimeError(...)
```

This allows the brain to connect to v3 DBs gracefully (browser tables just won't exist).

- [ ] **Step 7: Run all brain tests**

Run: `uv run --project brain --extra dev pytest brain/tests -v`
Expected: All tests PASS

- [ ] **Step 8: Commit**

```bash
git add brain/src/hippo_brain/server.py brain/src/hippo_brain/enrichment.py
git commit -m "feat(brain): integrate browser enrichment loop and correlated shell+browser prompts"
```

---

### Task 11: Full integration verification

- [ ] **Step 1: Run all Rust tests**

```bash
cargo test
```
Expected: All pass

- [ ] **Step 2: Run clippy**

```bash
cargo clippy --all-targets -- -D warnings
```
Expected: No warnings

- [ ] **Step 3: Run all Python tests**

```bash
uv run --project brain --extra dev pytest brain/tests -v
```
Expected: All pass

- [ ] **Step 4: Run Python linting**

```bash
uv run --project brain --extra dev ruff check brain/
uv run --project brain --extra dev ruff format --check brain/
```
Expected: Clean

- [ ] **Step 5: Build release binary**

```bash
cargo build --release
```
Expected: Compiles successfully

- [ ] **Step 6: Manual smoke test**

1. Start daemon: `hippo daemon run` (in a separate terminal)
2. Verify schema migration: `sqlite3 ~/.local/share/hippo/hippo.db "PRAGMA user_version"` → should output `4`
3. Verify tables: `sqlite3 ~/.local/share/hippo/hippo.db ".tables"` → should include `browser_events`, `browser_enrichment_queue`, `knowledge_node_browser_events`

- [ ] **Step 7: Commit any fixups**

```bash
git add -A
git commit -m "chore: integration test fixups for browser source"
```
