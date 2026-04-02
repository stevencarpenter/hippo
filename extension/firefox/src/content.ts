/**
 * Hippo Browser Capture — content script.
 *
 * Runs on every page at document_idle. Tracks engagement signals (dwell time,
 * scroll depth) and extracts main content via Readability on page departure.
 * Sends a "page_visit" message to the background script for allowlist
 * filtering and native messaging relay.
 */

import { MIN_DWELL_MS, MAX_TEXT_BYTES } from "./config";
import type { PageVisitMessage } from "./types";

/** Readability is loaded before this script via manifest.json content_scripts. */
declare class Readability {
  constructor(doc: Document);
  parse(): { title: string; textContent: string; content: string } | null;
}

// --- State ---
let visibleSince = performance.now();
let totalVisibleMs = 0;
let isVisible = !document.hidden;
let maxScrollDepth = 0;
let sent = false;

// --- Visibility tracking ---
document.addEventListener("visibilitychange", () => {
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
  () => {
    const docHeight = Math.max(
      document.body.scrollHeight,
      document.documentElement.scrollHeight,
      1,
    );
    const viewBottom = window.scrollY + window.innerHeight;
    const depth = Math.min(viewBottom / docHeight, 1.0);
    if (depth > maxScrollDepth) {
      maxScrollDepth = depth;
    }
  },
  { passive: true },
);

// --- Before unload ---
window.addEventListener("beforeunload", () => {
  maybeSend();
});

// --- Send page visit data ---
function maybeSend(): void {
  if (sent) return;

  // Finalize dwell time
  let dwellMs = totalVisibleMs;
  if (isVisible) {
    dwellMs += performance.now() - visibleSince;
  }

  if (dwellMs < MIN_DWELL_MS) return;

  sent = true;

  // Ask background script if this domain is allowlisted before doing
  // expensive Readability extraction. The background script holds the
  // runtime allowlist and enabled state.
  browser.runtime
    .sendMessage({ type: "check_domain", domain: location.hostname })
    .then((allowed) => {
      if (!allowed) return;

      // Extract main content via Readability
      let extractedText: string | null = null;
      try {
        if (typeof Readability !== "undefined") {
          const docClone = document.cloneNode(true) as Document;
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

      const message: PageVisitMessage = {
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

      browser.runtime.sendMessage(message);
    })
    .catch((_e: unknown) => {
      // Extension context may be invalidated — nothing we can do
    });
}
