/** Message sent from content script to background on page departure. */
export interface PageVisitMessage {
  type: "page_visit";
  url: string;
  title: string;
  domain: string;
  dwell_ms: number;
  scroll_depth: number;
  extracted_text: string | null;
  referrer: string | null;
  timestamp: number;
}

/** Payload sent to the hippo_daemon native messaging host. */
export interface BrowserVisit {
  url: string;
  title: string;
  domain: string;
  dwell_ms: number;
  scroll_depth: number;
  extracted_text: string | null;
  search_query: string | null;
  referrer: string | null;
  timestamp: number;
}

/** Search engine pattern for extracting queries from referrer URLs. */
export interface SearchEngine {
  domain: string;
  param: string;
  pathPrefix?: string;
}

/** Runtime settings persisted in browser.storage.local. */
export interface Settings {
  enabled: boolean;
  allowlist: string[];
  captureCount: number;
}
