# Firefox Browser Source Design

## Context

Hippo currently captures shell commands and Claude Code sessions. Adding Firefox Developer Edition as a knowledge source enables a powerful new dimension: correlating what you **research** in the browser with what you're **doing** in the terminal. The canonical use case is debugging — you hit a cargo error, search Stack Overflow, read docs, then fix the issue. Today, Hippo captures the shell side but loses the research context entirely.

The architecture was designed for this. The existing `EventPayload` enum, enrichment queue pattern, and knowledge node pipeline all generalize to new sources without core changes.

## Design Decisions

- **Capture method**: Firefox WebExtension + Native Messaging to hippo-daemon
- **Privacy model**: Domain allowlist — only capture from explicitly allowed domains
- **Content depth**: Extract main page content via Readability for allowlisted domains
- **Enrichment model**: Correlated enrichment — browser events merged into shell/Claude enrichment prompts within a ±5min temporal window

## Architecture

```
┌─────────────────────────────────┐
│  Firefox Developer Edition      │
│  ┌───────────────────────────┐  │
│  │ Content Script            │  │
│  │  • dwell time (vis API)   │  │
│  │  • scroll depth           │  │
│  │  • Readability extraction │  │
│  └──────────┬────────────────┘  │
│  ┌──────────▼────────────────┐  │
│  │ Background Script         │  │
│  │  • allowlist check        │  │
│  │  • session assembly       │  │
│  │  • Native Messaging port  │  │
│  └──────────┬────────────────┘  │
└─────────────┼───────────────────┘
              │ Native Messaging (stdin/stdout, 4-byte LE length + JSON)
              ▼
┌─────────────────────────────────┐
│  hippo-daemon (Rust)            │
│  • NativeMessagingHost listener │
│  • Allowlist enforcement        │
│  • URL redaction (tokens, etc.) │
│  • Insert → browser_events     │
│  • Auto-queue enrichment        │
└──────────────┬──────────────────┘
               │ SQLite (WAL)
               ▼
┌─────────────────────────────────┐
│  hippo-brain (Python)           │
│  • Poll browser_enrichment_queue│
│  • Correlate with shell events  │
│  •   within ±5min window        │
│  • Build mixed-source prompt    │
│  • Write knowledge nodes        │
│  •   with cross-source links    │
└─────────────────────────────────┘
```

## Components

### 1. Firefox WebExtension (`extension/firefox/`)

Manifest V2 WebExtension (Firefox Dev Edition has full MV2 support; MV3 Native Messaging support in Firefox is still maturing).

**Permissions required:**
- `nativeMessaging` — communicate with hippo-daemon
- `tabs` — detect tab activation/deactivation
- `activeTab` — access page content on user-visited tabs
- `storage` — persist allowlist config and extension state

**Content script** (injected into allowlisted domains):
- Tracks page entry time via `performance.now()`
- Listens to `visibilitychange` events for dwell time (only counts visible time)
- Measures scroll depth: `max(scrollY + innerHeight) / document.body.scrollHeight`
- On page unload or tab deactivation (whichever comes first with dwell > 3s):
  - Runs Mozilla Readability on the DOM to extract main content
  - Sends `{ url, title, domain, dwell_ms, scroll_depth, extracted_text, referrer, timestamp }` to background script

**Background script**:
- Maintains domain allowlist (loaded from extension storage, configurable via popup)
- Filters: skip if domain not in allowlist, skip if dwell < 3s
- Detects search queries: if referrer matches known search engines (google.com, github.com/search, etc.), extract query from URL params
- Assembles `BrowserVisit` message and sends via `browser.runtime.sendNativeMessage("hippo_daemon", message)`
- One-shot messaging (not persistent port) — each visit is independent, no connection state to manage

**Popup UI** (minimal):
- Toggle capture on/off
- View/edit domain allowlist
- Show capture stats (visits captured today, domains seen)
- Link to hippo config for advanced settings

**Default allowlist** (sensible starting set):
```json
[
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
  "wiki.archlinux.org"
]
```

### 2. Native Messaging Host (Rust, in hippo-daemon)

**Host manifest** (`hippo_daemon.json`):
```json
{
  "name": "hippo_daemon",
  "description": "Hippo knowledge capture daemon",
  "path": "/Users/<user>/.local/bin/hippo",
  "type": "stdio",
  "allowed_extensions": ["hippo-browser@local"]
}
```

Installed to: `~/Library/Application Support/Mozilla/NativeMessagingHosts/hippo_daemon.json`

**New CLI subcommand**: `hippo native-messaging-host`
- Reads from stdin, writes to stdout using Native Messaging protocol (4-byte LE length prefix + JSON)
- For each incoming `BrowserVisit` message:
  - Validates against daemon-side allowlist (defense in depth)
  - Strips sensitive URL query params (token, api_key, password, secret, auth, session, etc.)
  - Converts to `EventEnvelope` with `EventPayload::Browser(Box<BrowserEvent>)`
  - Sends to daemon via Unix socket (reuses existing `DaemonRequest::IngestEvent`)
- Process lifecycle: Firefox spawns one per `sendNativeMessage` call (one-shot mode), or keeps alive for `connectNative` (persistent mode). One-shot is simpler and sufficient for our use case.

**New type in `hippo-core/src/events.rs`**:
```rust
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BrowserEvent {
    pub url: String,
    pub title: String,
    pub domain: String,
    pub dwell_ms: u64,
    pub scroll_depth: f32,         // 0.0–1.0
    pub extracted_text: Option<String>,
    pub search_query: Option<String>,
    pub referrer: Option<String>,
    pub content_hash: Option<String>,  // SHA256 of extracted_text for dedup
}
```

`EventPayload` gains a new variant:
```rust
pub enum EventPayload {
    Shell(Box<ShellEvent>),
    FsChange(FsChangeEvent),
    IdeAction(IdeActionEvent),
    Browser(Box<BrowserEvent>),  // new
    Raw(serde_json::Value),
}
```

### 3. SQLite Schema (migration v3 → v4)

```sql
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

-- Temporal correlation index: fast lookup of browser events near shell events
CREATE INDEX IF NOT EXISTS idx_browser_events_ts_domain ON browser_events(timestamp, domain);

PRAGMA user_version = 4;
```

### 4. Daemon-Side Storage (`hippo-core/src/storage.rs`)

New function `insert_browser_event()`:
- Accepts `BrowserEvent` + `envelope_id` + `timestamp`
- Computes `content_hash` = SHA256 of `extracted_text` if present
- **Dedup logic**: If same `domain + url_path` (ignoring query params) visited within 30 minutes AND content_hash matches → `INSERT OR IGNORE` via envelope_id
  - Envelope ID for browser events: `Uuid::new_v5(BROWSER_NS, format!("{url}:{timestamp_bucket}"))` where `timestamp_bucket = timestamp / (30 * 60 * 1000)` — this makes revisits within the same 30-min window idempotent
- Atomic transaction: insert browser_event + insert browser_enrichment_queue entry

### 5. Enrichment Pipeline (`brain/src/hippo_brain/browser_enrichment.py`)

**New module** following the same structure as `enrichment.py` and `claude_sessions.py`.

**Queue claiming**: `claim_pending_browser_events()`
- Groups browser events by temporal proximity (events within 5 minutes of each other form a "browsing session")
- Only processes browsing sessions where last event is > 60s old (stale threshold, shorter than shell's 120s since browsing sessions are more transient)

**Correlated enrichment** (the key differentiator):

When building the enrichment prompt for shell events (`enrichment.py`), query for temporally correlated browser events:

```python
def get_correlated_browser_events(db, session_start_ms, session_end_ms, window_ms=300_000):
    """Fetch browser events within ±5min of a shell session's time range."""
    return db.execute("""
        SELECT url, title, domain, dwell_ms, scroll_depth, 
               extracted_text, search_query, timestamp
        FROM browser_events
        WHERE timestamp BETWEEN ? AND ?
        ORDER BY timestamp
    """, (session_start_ms - window_ms, session_end_ms + window_ms)).fetchall()
```

**Mixed-source enrichment prompt**:

```
Events from a developer work session. Shell commands show what was executed.
Browser activity shows what the developer was researching at the same time.

Shell Events:
  Event 1 (developer): cargo build → exit_code: 1, stderr: "error[E0277]: ..."
  Event 2 (developer): cargo build → exit_code: 0

Browser Activity (concurrent):
  [12:34] stackoverflow.com - "How to implement Display trait for custom error" (read 4.2min, 85% scroll)
  [12:38] doc.rust-lang.org - "std::fmt::Display" (read 1.8min, 60% scroll)
  Search query: "rust implement Display trait custom error type"
```

The LLM sees the full picture: the developer hit an error, searched for a solution, read relevant docs, and fixed it.

**Standalone browser enrichment**:

Browser events NOT correlated with shell activity (e.g., a pure research session reading docs) get their own enrichment pass with a browser-specific system prompt:

```
You are analyzing a developer's web browsing session. Extract what they were 
researching, learning, or investigating. Focus on the technical topics and 
how pages relate to each other (e.g., a search query leading to specific docs).
```

### 6. Configuration (`config/config.default.toml`)

```toml
[browser]
enabled = true
min_dwell_ms = 3000                 # Ignore visits shorter than 3 seconds
scroll_depth_threshold = 0.15       # Skip events with scroll < 15% AND no search query
dedup_window_minutes = 30           # Same URL within this window = one logical visit
correlation_window_ms = 300000      # ±5min window for shell/browser correlation
stale_session_secs = 60             # Process browsing sessions older than 60s

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
# Query parameter names to strip from URLs before storage
strip_params = ["token", "api_key", "password", "secret", "auth", "session", "key", "sig"]
```

### 7. Dedup & Noise Filtering Summary

| Layer | Filter | Effect |
|-------|--------|--------|
| Extension content script | `dwell_ms < 3000` | Skip accidental tab opens |
| Extension background | Domain not in allowlist | Skip non-work browsing |
| Daemon (native msg host) | Domain allowlist (defense in depth) | Redundant safety check |
| Daemon (native msg host) | Strip sensitive URL params | Privacy: remove tokens from URLs |
| Daemon storage | `envelope_id` = `v5_uuid(url, 30min_bucket)` | Same URL within 30min = one event |
| Daemon storage | `content_hash` (SHA256 of extracted text) | Detect identical content on revisit |
| Brain enrichment | `scroll_depth < 0.15 AND no search_query` | Skip skimmed pages with no search context |

### 8. Entity Types

Browser events introduce new entity types to the `entities` table:

- `type = 'domain'`: Frequently visited domains (stackoverflow.com, docs.rs)
- `type = 'concept'` (existing): Technical topics extracted from search queries and page content

**Note**: The `entities` table CHECK constraint must be updated to include `'domain'` as part of the v3→v4 migration:
```sql
ALTER TABLE entities DROP CONSTRAINT ... -- SQLite doesn't support this; 
-- migration must recreate the table or use the existing pattern of 
-- adding the new type to the CHECK list in schema.sql
```
In practice: update the CHECK constraint in `schema.sql` to include `'domain'` and handle via the migration path already established for v3→v4.

New relationship types in `relationships`:
- `researched_on`: links a `project` entity to a `domain` entity
- `searched_for`: links a `concept` entity to search query context

## File Changes Summary

| File | Change |
|------|--------|
| `extension/firefox/` | **New directory** — WebExtension (manifest, background, content script, popup) |
| `crates/hippo-core/src/events.rs` | Add `BrowserEvent` struct + `EventPayload::Browser` variant |
| `crates/hippo-core/src/protocol.rs` | No changes — `IngestEvent` already accepts any `EventPayload` |
| `crates/hippo-core/src/storage.rs` | Add `insert_browser_event()`, schema migration v3→v4 |
| `crates/hippo-core/src/schema.sql` | Add `browser_events`, `browser_enrichment_queue`, `knowledge_node_browser_events` tables |
| `crates/hippo-daemon/src/cli.rs` | Add `native-messaging-host` subcommand |
| `crates/hippo-daemon/src/native_messaging.rs` | **New file** — stdin/stdout Native Messaging protocol handler |
| `crates/hippo-daemon/src/daemon.rs` | Handle `EventPayload::Browser` in flush path |
| `brain/src/hippo_brain/browser_enrichment.py` | **New file** — browser queue claiming, standalone browser enrichment |
| `brain/src/hippo_brain/enrichment.py` | Add `get_correlated_browser_events()`, inject browser context into shell enrichment prompts |
| `config/config.default.toml` | Add `[browser]` section |

## Verification

1. **Extension loads in Firefox Dev Edition**: `about:debugging` → Load Temporary Add-on → verify permissions granted
2. **Native Messaging works**: Visit an allowlisted domain → check `hippo doctor` shows browser events count > 0
3. **Allowlist enforcement**: Visit a non-allowlisted domain → verify no event captured (check `browser_events` table)
4. **Dwell filtering**: Quick tab open/close (< 3s) → verify no event captured
5. **Content extraction**: Visit a Stack Overflow page, stay > 5s, scroll → verify `extracted_text` is populated and contains the answer content (not sidebar/nav)
6. **Dedup**: Refresh the same page within 30min → verify only one `browser_events` row
7. **URL redaction**: Visit a URL with `?token=abc123` → verify the token is stripped in stored URL
8. **Correlated enrichment**: Run `cargo build` (fail), search SO for the error, fix it, run `cargo build` (success) → verify the resulting knowledge node references both the shell commands AND the browser research
9. **Standalone enrichment**: Browse docs without any shell activity → verify browser-only knowledge node created
10. **Entity extraction**: After enrichment, verify `entities` table has domain entries and concept entries from search queries
