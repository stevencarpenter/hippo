/**
 * Hippo Browser Capture — content script.
 *
 * Runs on every page at document_idle. Tracks engagement signals (dwell time,
 * scroll depth) and extracts main content via Readability on page departure.
 * Sends a "page_visit" message to the background script for allowlist
 * filtering and native messaging relay.
 */
(function () {
  "use strict";

  // --- State ---
  let visibleSince = performance.now();
  let totalVisibleMs = 0;
  let isVisible = !document.hidden;
  let maxScrollDepth = 0;
  let sent = false;

  // --- Minimum dwell threshold (ms) ---
  const MIN_DWELL_MS = 3000;

  // --- Max extracted text size (bytes) ---
  const MAX_TEXT_BYTES = 50 * 1024;

  // --- Visibility tracking ---
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      // Page became hidden — accumulate visible time
      if (isVisible) {
        totalVisibleMs += performance.now() - visibleSince;
        isVisible = false;
      }
      maybeSend();
    } else {
      // Page became visible — start a new visible interval
      visibleSince = performance.now();
      isVisible = true;
    }
  });

  // --- Scroll depth tracking ---
  window.addEventListener(
    "scroll",
    function () {
      const docHeight = Math.max(
        document.body.scrollHeight,
        document.documentElement.scrollHeight,
        1
      );
      const viewBottom = window.scrollY + window.innerHeight;
      const depth = Math.min(viewBottom / docHeight, 1.0);
      if (depth > maxScrollDepth) {
        maxScrollDepth = depth;
      }
    },
    { passive: true }
  );

  // --- Before unload ---
  window.addEventListener("beforeunload", function () {
    maybeSend();
  });

  // --- Send page visit data ---
  function maybeSend() {
    if (sent) return;

    // Finalize dwell time
    let dwellMs = totalVisibleMs;
    if (isVisible) {
      dwellMs += performance.now() - visibleSince;
    }

    if (dwellMs < MIN_DWELL_MS) return;

    sent = true;

    // Extract main content via Readability
    let extractedText = null;
    try {
      if (typeof Readability !== "undefined") {
        const docClone = document.cloneNode(true);
        const article = new Readability(docClone).parse();
        if (article && article.textContent) {
          extractedText = article.textContent;
          // Truncate to MAX_TEXT_BYTES
          if (extractedText.length > MAX_TEXT_BYTES) {
            extractedText = extractedText.substring(0, MAX_TEXT_BYTES);
          }
        }
      }
    } catch (_e) {
      // Readability can fail on malformed DOMs — not critical
      extractedText = null;
    }

    const message = {
      type: "page_visit",
      url: location.href,
      title: document.title || "",
      domain: location.hostname,
      dwell_ms: Math.round(dwellMs),
      scroll_depth: parseFloat(maxScrollDepth.toFixed(3)),
      extracted_text: extractedText,
      referrer: document.referrer || null,
      timestamp: Date.now(),
    };

    try {
      browser.runtime.sendMessage(message);
    } catch (_e) {
      // Extension context may be invalidated — nothing we can do
    }
  }
})();
