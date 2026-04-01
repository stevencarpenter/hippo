# Hippo Browser Capture — Firefox Extension

Captures browsing activity from allowlisted domains and sends it to the Hippo daemon via Native Messaging.

## Setup

### 1. Install Native Messaging Host

```bash
cargo build --release
hippo daemon install --force
```

This creates:
- Wrapper script: `~/Library/Application Support/Mozilla/NativeMessagingHosts/hippo-native-messaging`
- Host manifest: `~/Library/Application Support/Mozilla/NativeMessagingHosts/hippo_daemon.json`

### 2. Load Extension in Firefox Developer Edition

1. Navigate to `about:debugging#/runtime/this-firefox`
2. Click **Load Temporary Add-on**
3. Select `extension/firefox/manifest.json`

The extension icon appears in the toolbar. Click it to toggle capture or edit the domain allowlist.

### 3. Verify

1. Visit an allowlisted domain (github.com, stackoverflow.com, etc.)
2. Stay on the page for at least 3 seconds
3. Navigate away or switch tabs
4. Check: `sqlite3 ~/.local/share/hippo/hippo.db "SELECT domain, title, dwell_ms FROM browser_events ORDER BY timestamp DESC LIMIT 5"`

## How It Works

**Content script** (`content.js`) — injected into all pages:
- Tracks visible dwell time via Page Visibility API
- Measures max scroll depth
- Extracts main content via Mozilla Readability on page departure
- Only sends if dwell > 3 seconds

**Background script** (`background.js`):
- Filters by domain allowlist
- Extracts search queries from referrer URLs (Google, DuckDuckGo, Bing, GitHub)
- Sends to `hippo_daemon` native messaging host

**Native Messaging host** (`hippo native-messaging-host`):
- Validates domain against daemon-side allowlist (defense in depth)
- Strips sensitive URL query params (tokens, API keys)
- Creates deterministic envelope IDs for dedup
- Forwards to daemon via Unix socket

## Configuration

Edit `~/.config/hippo/config.toml`:

```toml
[browser]
enabled = true
min_dwell_ms = 3000          # Skip visits shorter than 3s
dedup_window_minutes = 30    # Same URL within 30min = one event

[browser.allowlist]
domains = ["github.com", "stackoverflow.com", ...]

[browser.url_redaction]
strip_params = ["token", "api_key", "password", "secret"]
```

Or edit the allowlist directly from the extension popup.

## Files

| File | Purpose |
|------|---------|
| `manifest.json` | WebExtension manifest (MV2) |
| `content.js` | Page-level dwell/scroll/content capture |
| `background.js` | Allowlist filtering, search query extraction, Native Messaging |
| `popup.html/js` | Extension popup UI (toggle, stats, allowlist editor) |
| `lib/Readability.js` | Mozilla Readability v0.6.0 (vendored) |
