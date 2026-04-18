use anyhow::{Result, bail};
use chrono::{DateTime, TimeZone, Utc};
use hippo_core::config::HippoConfig;
use hippo_core::events::{BrowserEvent, EventEnvelope, EventPayload};
use serde::{Deserialize, Serialize};
use std::io::{Read, Write};
use tracing::{debug, error, info, warn};
use url::Url;
use uuid::Uuid;

use crate::commands::send_event_fire_and_forget;

/// Maximum native message size (1 MB).
const MAX_MESSAGE_SIZE: u32 = 1_024 * 1_024;

/// Namespace UUID for browser envelope deduplication (v5).
/// Generated once, used as the namespace for `make_envelope_id`.
const BROWSER_NS: Uuid = Uuid::from_bytes([
    0x8a, 0x3b, 0x7c, 0x01, 0xd4, 0xe5, 0x4f, 0x6a, 0x9b, 0x2c, 0x1e, 0x0f, 0xa8, 0x5d, 0x3c, 0x7e,
]);

/// Struct matching what the Firefox extension sends via Native Messaging.
#[derive(Debug, Clone, Deserialize)]
pub struct BrowserVisit {
    pub url: String,
    pub title: String,
    pub domain: String,
    pub dwell_ms: u64,
    pub scroll_depth: f32,
    pub extracted_text: Option<String>,
    pub search_query: Option<String>,
    pub referrer: Option<String>,
    pub timestamp: i64,
}

/// Response sent back to the Firefox extension.
#[derive(Debug, Serialize)]
struct NativeResponse {
    status: &'static str,
    #[serde(skip_serializing_if = "Option::is_none")]
    error: Option<String>,
}

/// Read a single Native Messaging message from stdin.
///
/// The Native Messaging protocol uses 4-byte **native-endian** length prefix,
/// unlike the daemon's big-endian framing.
///
/// Returns `None` on EOF.
pub fn read_native_message() -> Result<Option<Vec<u8>>> {
    let mut len_buf = [0u8; 4];
    let stdin = std::io::stdin();
    let mut handle = stdin.lock();
    match handle.read_exact(&mut len_buf) {
        Ok(()) => {}
        Err(e) if e.kind() == std::io::ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(e.into()),
    }
    let len = u32::from_ne_bytes(len_buf);
    if len > MAX_MESSAGE_SIZE {
        bail!("native message too large: {len} bytes (max {MAX_MESSAGE_SIZE})");
    }
    let mut buf = vec![0u8; len as usize];
    handle.read_exact(&mut buf)?;
    Ok(Some(buf))
}

/// Write a Native Messaging response to stdout.
///
/// Uses 4-byte **native-endian** length prefix.
pub fn write_native_message(data: &[u8]) -> Result<()> {
    let len: u32 = data
        .len()
        .try_into()
        .map_err(|_| anyhow::anyhow!("response too large: {} bytes", data.len()))?;
    let stdout = std::io::stdout();
    let mut handle = stdout.lock();
    handle.write_all(&len.to_ne_bytes())?;
    handle.write_all(data)?;
    handle.flush()?;
    Ok(())
}

/// Strip sensitive query parameters from a URL.
///
/// Matching is case-insensitive. If the URL cannot be parsed, the original
/// string is returned unchanged.
pub fn strip_sensitive_params(url_str: &str, strip_params: &[String]) -> String {
    let Ok(mut parsed) = Url::parse(url_str) else {
        return url_str.to_string();
    };

    let filtered: Vec<(String, String)> = parsed
        .query_pairs()
        .filter(|(name, _)| !strip_params.iter().any(|s| s.eq_ignore_ascii_case(name)))
        .map(|(k, v)| (k.into_owned(), v.into_owned()))
        .collect();

    if filtered.is_empty() {
        parsed.set_query(None);
    } else {
        parsed
            .query_pairs_mut()
            .clear()
            .extend_pairs(filtered.iter().map(|(k, v)| (k.as_str(), v.as_str())));
    }

    parsed.to_string()
}

/// Create a deterministic v5 UUID for deduplication.
///
/// The UUID is derived from the URL and a time bucket (url + timestamp
/// truncated to `dedup_window_minutes` intervals). Same URL visited within
/// the same time window produces the same envelope ID.
pub fn make_envelope_id(url: &str, dedup_window_minutes: u64, timestamp_ms: i64) -> Uuid {
    let visit_minutes = (timestamp_ms / 1000) as u64 / 60;
    let bucket = visit_minutes.checked_div(dedup_window_minutes).unwrap_or(visit_minutes);
    let key = format!("{url}:{bucket}");
    Uuid::new_v5(&BROWSER_NS, key.as_bytes())
}

/// Send a response back to the Firefox extension.
fn send_response(status: &'static str, error: Option<String>) {
    let resp = NativeResponse { status, error };
    match serde_json::to_vec(&resp) {
        Ok(data) => {
            if let Err(e) = write_native_message(&data) {
                error!(%e, "failed to write native response");
            }
        }
        Err(e) => {
            error!(%e, "failed to serialize native response");
        }
    }
}

/// Main loop: read Native Messaging messages from stdin, validate, and
/// forward to the hippo daemon via Unix socket.
pub async fn run(config: &HippoConfig) -> Result<()> {
    info!("native-messaging-host starting");

    if !config.browser.enabled {
        warn!("browser capture is disabled in config — exiting");
        send_response("error", Some("browser capture disabled".into()));
        return Ok(());
    }

    let socket_path = config.socket_path();
    let timeout_ms = config.daemon.socket_timeout_ms;
    let strip_params = &config.browser.url_redaction.strip_params;
    let allowed_domains = &config.browser.allowlist.domains;
    let dedup_window = config.browser.dedup_window_minutes;

    loop {
        let raw = match read_native_message() {
            Ok(Some(data)) => data,
            Ok(None) => {
                info!("stdin closed — exiting");
                break;
            }
            Err(e) => {
                error!(%e, "failed to read native message");
                send_response("error", Some(format!("read error: {e}")));
                continue;
            }
        };

        let visit: BrowserVisit = match serde_json::from_slice(&raw) {
            Ok(v) => v,
            Err(e) => {
                warn!(%e, "failed to parse BrowserVisit");
                send_response("error", Some(format!("parse error: {e}")));
                continue;
            }
        };

        // Defense-in-depth: check domain allowlist
        let domain_lower = visit.domain.to_lowercase();
        let allowed = allowed_domains.iter().any(|d| {
            domain_lower == d.to_lowercase()
                || domain_lower.ends_with(&format!(".{}", d.to_lowercase()))
        });
        if !allowed {
            debug!(domain = %visit.domain, "domain not in allowlist — dropping");
            send_response("filtered", None);
            continue;
        }

        // Strip sensitive params from URL and referrer
        let clean_url = strip_sensitive_params(&visit.url, strip_params);
        let clean_referrer = visit
            .referrer
            .as_deref()
            .map(|r| strip_sensitive_params(r, strip_params));

        let envelope_id = make_envelope_id(&clean_url, dedup_window, visit.timestamp);

        let timestamp: DateTime<Utc> = Utc
            .timestamp_millis_opt(visit.timestamp)
            .single()
            .unwrap_or_else(Utc::now);

        let browser_event = BrowserEvent {
            url: clean_url,
            title: visit.title,
            domain: visit.domain,
            dwell_ms: visit.dwell_ms,
            scroll_depth: visit.scroll_depth,
            extracted_text: visit.extracted_text,
            search_query: visit.search_query,
            referrer: clean_referrer,
            content_hash: None,
        };

        let envelope = EventEnvelope {
            envelope_id,
            producer_version: 1,
            timestamp,
            payload: EventPayload::Browser(Box::new(browser_event)),
        };

        match send_event_fire_and_forget(&socket_path, &envelope, timeout_ms).await {
            Ok(()) => {
                debug!(id = %envelope_id, "event sent to daemon");
                send_response("ok", None);
            }
            Err(e) => {
                error!(%e, "failed to send event to daemon");
                send_response("error", Some(format!("daemon send failed: {e}")));
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
        let url = "https://example.com/search?q=rust&token=secret123&page=2";
        let strip = vec!["token".to_string()];
        let result = strip_sensitive_params(url, &strip);
        assert!(result.contains("q=rust"));
        assert!(result.contains("page=2"));
        assert!(!result.contains("token"));
        assert!(!result.contains("secret123"));
    }

    #[test]
    fn test_strip_sensitive_params_all_removed() {
        let url = "https://example.com/path?token=abc";
        let strip = vec!["token".to_string()];
        let result = strip_sensitive_params(url, &strip);
        assert_eq!(result, "https://example.com/path");
        assert!(!result.contains('?'));
    }

    #[test]
    fn test_strip_sensitive_params_no_query() {
        let url = "https://example.com/path";
        let strip = vec!["token".to_string()];
        let result = strip_sensitive_params(url, &strip);
        assert_eq!(result, "https://example.com/path");
    }

    #[test]
    fn test_strip_sensitive_params_case_insensitive() {
        let url = "https://example.com/?TOKEN=secret&keep=yes";
        let strip = vec!["token".to_string()];
        let result = strip_sensitive_params(url, &strip);
        assert!(!result.contains("TOKEN"));
        assert!(!result.contains("secret"));
        assert!(result.contains("keep=yes"));
    }

    #[test]
    fn test_make_envelope_id_deterministic() {
        let ts = 1711900000000i64;
        let id1 = make_envelope_id("https://example.com/page", 30, ts);
        let id2 = make_envelope_id("https://example.com/page", 30, ts);
        assert_eq!(id1, id2, "same URL and window should produce same UUID");

        let id3 = make_envelope_id("https://example.com/other", 30, ts);
        assert_ne!(id1, id3, "different URL should produce different UUID");
    }

    #[test]
    fn test_strip_sensitive_params_unparseable_url() {
        let url = "not a url at all";
        let strip = vec!["token".to_string()];
        let result = strip_sensitive_params(url, &strip);
        assert_eq!(result, url);
    }

    #[test]
    fn test_strip_sensitive_params_empty_strip_list() {
        let url = "https://example.com/?a=1&b=2";
        let strip: Vec<String> = vec![];
        let result = strip_sensitive_params(url, &strip);
        assert!(result.contains("a=1"));
        assert!(result.contains("b=2"));
    }

    #[test]
    fn test_domain_allowlist_matching() {
        let allowed = ["github.com".to_string(), "stackoverflow.com".to_string()];

        // Helper that matches the logic in run()
        let is_allowed = |domain: &str| -> bool {
            let domain_lower = domain.to_lowercase();
            allowed.iter().any(|d| {
                domain_lower == d.to_lowercase()
                    || domain_lower.ends_with(&format!(".{}", d.to_lowercase()))
            })
        };

        assert!(is_allowed("github.com"));
        assert!(is_allowed("www.github.com"));
        assert!(is_allowed("docs.github.com"));
        assert!(is_allowed("GITHUB.COM"));
        assert!(!is_allowed("notgithub.com"));
        assert!(!is_allowed("evil-github.com"));
        assert!(is_allowed("stackoverflow.com"));
        assert!(!is_allowed("example.com"));
    }

    #[test]
    fn test_browser_visit_deserialize() {
        let json = r#"{
            "url": "https://docs.rs/serde/latest/serde/",
            "title": "serde - Rust",
            "domain": "docs.rs",
            "dwell_ms": 45000,
            "scroll_depth": 0.75,
            "extracted_text": "Serde is a framework",
            "search_query": null,
            "referrer": "https://google.com",
            "timestamp": 1711900000000
        }"#;
        let visit: BrowserVisit = serde_json::from_str(json).unwrap();
        assert_eq!(visit.domain, "docs.rs");
        assert_eq!(visit.dwell_ms, 45000);
        assert!((visit.scroll_depth - 0.75).abs() < f32::EPSILON);
        assert!(visit.search_query.is_none());
        assert_eq!(visit.referrer.as_deref(), Some("https://google.com"));
    }
}
