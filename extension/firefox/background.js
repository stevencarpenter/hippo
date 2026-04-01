/**
 * Hippo Browser Capture — background script.
 *
 * Receives page_visit messages from content scripts, filters by allowlist,
 * extracts search queries from referrers, and relays to the hippo_daemon
 * native messaging host.
 */
(function () {
  "use strict";

  // --- Default allowlist (matches config/config.default.toml) ---
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

  // --- Search engine patterns ---
  const SEARCH_ENGINES = [
    { domain: "google.com", param: "q" },
    { domain: "www.google.com", param: "q" },
    { domain: "duckduckgo.com", param: "q" },
    { domain: "bing.com", param: "q" },
    { domain: "www.bing.com", param: "q" },
    { domain: "github.com", param: "q", pathPrefix: "/search" },
  ];

  // --- Native messaging host name ---
  const NATIVE_HOST = "hippo_daemon";

  // --- Runtime settings (loaded from storage) ---
  let settings = {
    enabled: true,
    allowlist: DEFAULT_ALLOWLIST.slice(),
    captureCount: 0,
  };

  // --- Load settings from storage ---
  function loadSettings() {
    return browser.storage.local.get(["enabled", "allowlist", "captureCount"]).then((result) => {
      if (typeof result.enabled === "boolean") {
        settings.enabled = result.enabled;
      }
      if (Array.isArray(result.allowlist) && result.allowlist.length > 0) {
        settings.allowlist = result.allowlist;
      }
      if (typeof result.captureCount === "number") {
        settings.captureCount = result.captureCount;
      }
    });
  }

  // --- Persist capture count ---
  function persistCaptureCount() {
    browser.storage.local.set({ captureCount: settings.captureCount });
  }

  // --- Check if a domain is in the allowlist ---
  function isDomainAllowed(domain) {
    const domainLower = domain.toLowerCase();
    return settings.allowlist.some((entry) => {
      const entryLower = entry.toLowerCase();
      return domainLower === entryLower || domainLower.endsWith("." + entryLower);
    });
  }

  // --- Extract search query from a referrer URL ---
  function extractSearchQuery(referrer) {
    if (!referrer) return null;

    let url;
    try {
      url = new URL(referrer);
    } catch (_e) {
      return null;
    }

    const hostname = url.hostname.toLowerCase();
    const pathname = url.pathname;

    for (const engine of SEARCH_ENGINES) {
      const domainMatch =
        hostname === engine.domain || hostname.endsWith("." + engine.domain);
      if (!domainMatch) continue;

      if (engine.pathPrefix && !pathname.startsWith(engine.pathPrefix)) continue;

      const query = url.searchParams.get(engine.param);
      if (query && query.trim().length > 0) {
        return query.trim();
      }
    }

    return null;
  }

  // --- Listen for messages from content scripts ---
  browser.runtime.onMessage.addListener((message, _sender) => {
    // Domain allowlist pre-check — lets content scripts skip expensive work
    if (message.type === "check_domain") {
      return Promise.resolve(settings.enabled && isDomainAllowed(message.domain));
    }

    if (message.type !== "page_visit") return;

    if (!settings.enabled) return;

    if (!isDomainAllowed(message.domain)) return;

    if (message.dwell_ms < 3000) return;

    const searchQuery = extractSearchQuery(message.referrer);

    const visit = {
      url: message.url,
      title: message.title,
      domain: message.domain,
      dwell_ms: message.dwell_ms,
      scroll_depth: message.scroll_depth,
      extracted_text: message.extracted_text || null,
      search_query: searchQuery,
      referrer: message.referrer || null,
      timestamp: message.timestamp,
    };

    browser.runtime.sendNativeMessage(NATIVE_HOST, visit).then(
      (_response) => {
        settings.captureCount++;
        persistCaptureCount();
      },
      (error) => {
        console.error("[hippo] native messaging error:", error);
      }
    );
  });

  // --- Listen for storage changes (settings updated from popup) ---
  browser.storage.onChanged.addListener((changes, area) => {
    if (area !== "local") return;
    if (changes.enabled) {
      settings.enabled = changes.enabled.newValue;
    }
    if (changes.allowlist) {
      settings.allowlist = changes.allowlist.newValue;
    }
    if (changes.captureCount) {
      settings.captureCount = changes.captureCount.newValue;
    }
  });

  // --- Initialize ---
  loadSettings();
})();
