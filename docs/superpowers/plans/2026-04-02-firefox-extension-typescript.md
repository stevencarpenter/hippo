# Firefox Extension TypeScript Rewrite Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite the Firefox extension from vanilla JS to TypeScript with esbuild, eliminating duplicated constants and adding compile-time type safety.

**Architecture:** TypeScript source in `src/`, esbuild bundles to `dist/` as IIFE, shared `config.ts` and `types.ts` eliminate all constant duplication. Readability.js stays vendored and loaded via manifest. Behavior is identical to the JS version.

**Tech Stack:** TypeScript 5.8+, esbuild, web-ext, Firefox MV2 WebExtension APIs

**Spec:** `docs/superpowers/specs/2026-04-02-firefox-extension-typescript-rewrite.md`

---

## Parallel Execution Map

```
Phase 1 (parallel):   [Task 1: Build toolchain]  |  [Task 2: Shared config + types]
Phase 2 (parallel):   [Task 3: content.ts]  |  [Task 4: background.ts]  |  [Task 5: popup.ts]
Phase 3 (sequential): [Task 6: Wire manifest + HTML, delete old JS, build, verify]
```

---

### Task 1: Build toolchain (package.json, tsconfig.json, build.mjs)

**Files:**
- Create: `extension/firefox/package.json`
- Create: `extension/firefox/tsconfig.json`
- Create: `extension/firefox/build.mjs`

No dependencies on other tasks. Can run in parallel with Task 2.

- [ ] **Step 1: Create package.json**

Write `extension/firefox/package.json`:

```json
{
  "private": true,
  "name": "hippo-browser-capture",
  "version": "0.1.0",
  "scripts": {
    "build": "node build.mjs",
    "watch": "node build.mjs --watch",
    "typecheck": "tsc --noEmit",
    "package": "node build.mjs && npx web-ext build --overwrite-dest"
  },
  "devDependencies": {
    "esbuild": "^0.25",
    "typescript": "^5.8"
  }
}
```

- [ ] **Step 2: Create tsconfig.json**

Write `extension/firefox/tsconfig.json`:

```json
{
  "compilerOptions": {
    "strict": true,
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "bundler",
    "outDir": "dist",
    "rootDir": "src",
    "skipLibCheck": true,
    "noEmit": true,
    "forceConsistentCasingInFileNames": true,
    "esModuleInterop": true
  },
  "include": ["src/**/*.ts"]
}
```

- [ ] **Step 3: Create build.mjs**

Write `extension/firefox/build.mjs`:

```javascript
import { build, context } from "esbuild";

const common = {
  bundle: true,
  format: "iife",
  target: "es2022",
  sourcemap: false,
  minify: false,
};

const entries = [
  { entryPoints: ["src/background.ts"], outfile: "dist/background.js" },
  { entryPoints: ["src/content.ts"], outfile: "dist/content.js" },
  { entryPoints: ["src/popup.ts"], outfile: "dist/popup.js" },
];

if (process.argv.includes("--watch")) {
  for (const entry of entries) {
    const ctx = await context({ ...common, ...entry });
    await ctx.watch();
  }
  console.log("watching for changes...");
} else {
  await Promise.all(entries.map((e) => build({ ...common, ...e })));
  console.log("built dist/");
}
```

- [ ] **Step 4: Install dependencies**

Run:
```bash
cd extension/firefox && npm install
```
Expected: `node_modules/` created with esbuild + typescript.

- [ ] **Step 5: Update .gitignore**

Read then rewrite `extension/firefox/.gitignore`:

```
node_modules/
dist/
web-ext-artifacts/
```

- [ ] **Step 6: Commit**

```bash
git add extension/firefox/package.json extension/firefox/tsconfig.json extension/firefox/build.mjs extension/firefox/package-lock.json
git add -f extension/firefox/.gitignore
git commit -m "chore(extension): add TypeScript + esbuild build toolchain"
```

---

### Task 2: Shared config and types (src/config.ts, src/types.ts)

**Files:**
- Create: `extension/firefox/src/config.ts`
- Create: `extension/firefox/src/types.ts`

No dependencies on other tasks. Can run in parallel with Task 1.

- [ ] **Step 1: Create src/types.ts**

Write `extension/firefox/src/types.ts`:

```typescript
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

/** Message sent from content script to check domain allowlist. */
export interface CheckDomainMessage {
  type: "check_domain";
  domain: string;
}

/** Union of all messages the background script handles. */
export type ExtensionMessage = PageVisitMessage | CheckDomainMessage;

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
```

- [ ] **Step 2: Create src/config.ts**

Write `extension/firefox/src/config.ts`:

```typescript
import type { SearchEngine } from "./types";

/** Default domain allowlist — seeded on first install, overridden by browser.storage.local. */
export const DEFAULT_ALLOWLIST: string[] = [
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

/** Minimum visible dwell time (ms) before capturing a page visit. */
export const MIN_DWELL_MS = 3000;

/** Maximum extracted text size (bytes) to avoid oversized native messages. */
export const MAX_TEXT_BYTES = 50 * 1024;

/** Native messaging host name (must match the NM manifest "name" field). */
export const NATIVE_HOST = "hippo_daemon";

/** Search engine patterns for extracting queries from referrer URLs. */
export const SEARCH_ENGINES: SearchEngine[] = [
  { domain: "google.com", param: "q" },
  { domain: "www.google.com", param: "q" },
  { domain: "duckduckgo.com", param: "q" },
  { domain: "bing.com", param: "q" },
  { domain: "www.bing.com", param: "q" },
  { domain: "github.com", param: "q", pathPrefix: "/search" },
];
```

- [ ] **Step 3: Verify types compile**

Run:
```bash
cd extension/firefox && npx tsc --noEmit
```
Expected: No errors. (Requires Task 1 to have installed typescript, but `npx tsc` works standalone too.)

- [ ] **Step 4: Commit**

```bash
git add extension/firefox/src/types.ts extension/firefox/src/config.ts
git commit -m "feat(extension): add shared TypeScript config and types"
```

---

### Task 3: Content script (src/content.ts)

**Files:**
- Create: `extension/firefox/src/content.ts`

Depends on Task 2 (imports from config.ts and types.ts). Can run in parallel with Tasks 4 and 5.

- [ ] **Step 1: Create src/content.ts**

Write `extension/firefox/src/content.ts`:

```typescript
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

// Readability is loaded before this script via manifest.json content_scripts
declare class Readability {
  constructor(doc: Document);
  parse(): { title: string; textContent: string; content: string } | null;
}

let visibleSince = performance.now();
let totalVisibleMs = 0;
let isVisible = !document.hidden;
let maxScrollDepth = 0;
let sent = false;

// --- Visibility tracking ---
document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    if (isVisible) {
      totalVisibleMs += performance.now() - visibleSince;
      isVisible = false;
    }
    maybeSend();
  } else {
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

  let dwellMs = totalVisibleMs;
  if (isVisible) {
    dwellMs += performance.now() - visibleSince;
  }

  if (dwellMs < MIN_DWELL_MS) return;

  sent = true;

  browser.runtime
    .sendMessage({ type: "check_domain", domain: location.hostname })
    .then((allowed: boolean) => {
      if (!allowed) return;

      let extractedText: string | null = null;
      try {
        if (typeof Readability !== "undefined") {
          const docClone = document.cloneNode(true) as Document;
          const article = new Readability(docClone).parse();
          if (article?.textContent) {
            extractedText = article.textContent.substring(0, MAX_TEXT_BYTES);
          }
        }
      } catch {
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
    .catch(() => {
      // Extension context may be invalidated — nothing we can do
    });
}
```

- [ ] **Step 2: Verify it compiles**

Run:
```bash
cd extension/firefox && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add extension/firefox/src/content.ts
git commit -m "feat(extension): rewrite content script in TypeScript"
```

---

### Task 4: Background script (src/background.ts)

**Files:**
- Create: `extension/firefox/src/background.ts`

Depends on Task 2. Can run in parallel with Tasks 3 and 5.

- [ ] **Step 1: Create src/background.ts**

Write `extension/firefox/src/background.ts`:

```typescript
/**
 * Hippo Browser Capture — background script.
 *
 * Receives page_visit messages from content scripts, filters by allowlist,
 * extracts search queries from referrers, and relays to the hippo_daemon
 * native messaging host.
 */

import {
  DEFAULT_ALLOWLIST,
  MIN_DWELL_MS,
  NATIVE_HOST,
  SEARCH_ENGINES,
} from "./config";
import type {
  BrowserVisit,
  ExtensionMessage,
  PageVisitMessage,
  Settings,
} from "./types";

// --- Runtime settings (loaded from browser.storage.local) ---
const settings: Settings = {
  enabled: true,
  allowlist: [...DEFAULT_ALLOWLIST],
  captureCount: 0,
};

// --- Load settings from storage ---
function loadSettings(): Promise<void> {
  return browser.storage.local
    .get(["enabled", "allowlist", "captureCount"])
    .then((result: Record<string, unknown>) => {
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
function persistCaptureCount(): void {
  browser.storage.local.set({ captureCount: settings.captureCount });
}

// --- Check if a domain is in the allowlist ---
function isDomainAllowed(domain: string): boolean {
  const domainLower = domain.toLowerCase();
  return settings.allowlist.some((entry) => {
    const entryLower = entry.toLowerCase();
    return domainLower === entryLower || domainLower.endsWith("." + entryLower);
  });
}

// --- Extract search query from a referrer URL ---
function extractSearchQuery(referrer: string | null): string | null {
  if (!referrer) return null;

  let url: URL;
  try {
    url = new URL(referrer);
  } catch {
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

// --- Validate message sender is our own extension ---
function isOwnExtension(sender: browser.runtime.MessageSender): boolean {
  return sender?.id === browser.runtime.id;
}

// --- Validate page_visit message structure ---
function isValidPageVisit(msg: PageVisitMessage): boolean {
  return (
    typeof msg.url === "string" &&
    typeof msg.domain === "string" &&
    typeof msg.dwell_ms === "number" &&
    typeof msg.scroll_depth === "number" &&
    typeof msg.timestamp === "number" &&
    msg.url.length > 0 &&
    msg.domain.length > 0 &&
    msg.dwell_ms >= 0 &&
    msg.scroll_depth >= 0 &&
    msg.scroll_depth <= 1.0
  );
}

// --- Listen for messages from content scripts ---
browser.runtime.onMessage.addListener(
  (message: ExtensionMessage, sender: browser.runtime.MessageSender) => {
    if (!isOwnExtension(sender)) return;

    if (message.type === "check_domain") {
      if (typeof message.domain !== "string") return Promise.resolve(false);
      return Promise.resolve(settings.enabled && isDomainAllowed(message.domain));
    }

    if (message.type !== "page_visit") return;
    if (!settings.enabled) return;
    if (!isValidPageVisit(message)) return;
    if (!isDomainAllowed(message.domain)) return;
    if (message.dwell_ms < MIN_DWELL_MS) return;

    const searchQuery = extractSearchQuery(message.referrer);

    const visit: BrowserVisit = {
      url: String(message.url),
      title: String(message.title || ""),
      domain: String(message.domain),
      dwell_ms: Math.round(message.dwell_ms),
      scroll_depth: parseFloat(message.scroll_depth.toFixed(3)),
      extracted_text:
        typeof message.extracted_text === "string"
          ? message.extracted_text
          : null,
      search_query: searchQuery,
      referrer:
        typeof message.referrer === "string" ? message.referrer : null,
      timestamp: Math.round(message.timestamp),
    };

    browser.runtime.sendNativeMessage(NATIVE_HOST, visit).then(
      () => {
        settings.captureCount++;
        persistCaptureCount();
      },
      (error: unknown) => {
        console.error("[hippo] native messaging error:", error);
      },
    );
  },
);

// --- Listen for storage changes (settings updated from popup) ---
browser.storage.onChanged.addListener(
  (
    changes: Record<string, browser.storage.StorageChange>,
    area: string,
  ) => {
    if (area !== "local") return;
    if (changes.enabled) {
      settings.enabled = changes.enabled.newValue as boolean;
    }
    if (changes.allowlist) {
      settings.allowlist = changes.allowlist.newValue as string[];
    }
    if (changes.captureCount) {
      settings.captureCount = changes.captureCount.newValue as number;
    }
  },
);

// --- Initialize ---
loadSettings();
```

- [ ] **Step 2: Verify it compiles**

Run:
```bash
cd extension/firefox && npx tsc --noEmit
```

Note: This will likely fail because `browser.*` types are not installed. We need `@anthropic-ai/sdk` — no wait, we need `@types/webextension-polyfill` or just declare the browser global. Since Firefox MV2 uses the `browser` namespace natively, add a minimal type declaration.

Create `extension/firefox/src/browser.d.ts`:

```typescript
/** Minimal Firefox WebExtension API type declarations. */
declare namespace browser {
  namespace runtime {
    const id: string;
    interface MessageSender {
      id?: string;
      tab?: { id: number; url?: string };
    }
    function sendMessage(message: unknown): Promise<unknown>;
    function sendNativeMessage(host: string, message: unknown): Promise<unknown>;
    const onMessage: {
      addListener(
        callback: (
          message: any,
          sender: MessageSender,
        ) => void | Promise<unknown>,
      ): void;
    };
  }
  namespace storage {
    interface StorageChange {
      oldValue?: unknown;
      newValue?: unknown;
    }
    const local: {
      get(keys: string[]): Promise<Record<string, unknown>>;
      set(items: Record<string, unknown>): Promise<void>;
    };
    const onChanged: {
      addListener(
        callback: (
          changes: Record<string, StorageChange>,
          area: string,
        ) => void,
      ): void;
    };
  }
}
```

- [ ] **Step 3: Verify it compiles with browser types**

Run:
```bash
cd extension/firefox && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 4: Commit**

```bash
git add extension/firefox/src/background.ts extension/firefox/src/browser.d.ts
git commit -m "feat(extension): rewrite background script in TypeScript"
```

---

### Task 5: Popup script (src/popup.ts)

**Files:**
- Create: `extension/firefox/src/popup.ts`

Depends on Task 2. Can run in parallel with Tasks 3 and 4.

- [ ] **Step 1: Create src/popup.ts**

Write `extension/firefox/src/popup.ts`:

```typescript
/**
 * Hippo Browser Capture — popup script.
 *
 * Manages the enable/disable toggle, capture count display, and domain
 * allowlist editing. All state persists in browser.storage.local.
 */

import { DEFAULT_ALLOWLIST } from "./config";

const enabledCheckbox = document.getElementById("enabled") as HTMLInputElement;
const countDisplay = document.getElementById("count") as HTMLElement;
const allowlistTextarea = document.getElementById("allowlist") as HTMLTextAreaElement;
const saveButton = document.getElementById("save") as HTMLButtonElement;
const savedIndicator = document.getElementById("saved") as HTMLElement;

// --- Load current settings ---
browser.storage.local
  .get(["enabled", "allowlist", "captureCount"])
  .then((result: Record<string, unknown>) => {
    enabledCheckbox.checked =
      typeof result.enabled === "boolean" ? result.enabled : true;

    const domains =
      Array.isArray(result.allowlist) && result.allowlist.length > 0
        ? (result.allowlist as string[])
        : DEFAULT_ALLOWLIST;
    allowlistTextarea.value = domains.join("\n");

    countDisplay.textContent = String(
      typeof result.captureCount === "number" ? result.captureCount : 0,
    );
  });

// --- Toggle enabled state immediately on change ---
enabledCheckbox.addEventListener("change", () => {
  browser.storage.local.set({ enabled: enabledCheckbox.checked });
});

// --- Save allowlist ---
saveButton.addEventListener("click", () => {
  const lines = allowlistTextarea.value
    .split("\n")
    .map((line) => line.trim().toLowerCase())
    .filter((line) => line.length > 0);

  const unique = [...new Set(lines)];

  browser.storage.local.set({ allowlist: unique }).then(() => {
    allowlistTextarea.value = unique.join("\n");

    savedIndicator.classList.add("show");
    setTimeout(() => {
      savedIndicator.classList.remove("show");
    }, 1500);
  });
});
```

- [ ] **Step 2: Verify it compiles**

Run:
```bash
cd extension/firefox && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 3: Commit**

```bash
git add extension/firefox/src/popup.ts
git commit -m "feat(extension): rewrite popup script in TypeScript"
```

---

### Task 6: Wire manifest, update HTML, delete old JS, build, verify

**Files:**
- Modify: `extension/firefox/manifest.json`
- Modify: `extension/firefox/popup.html`
- Delete: `extension/firefox/background.js`
- Delete: `extension/firefox/content.js`
- Delete: `extension/firefox/popup.js`

Depends on all previous tasks.

- [ ] **Step 1: Update manifest.json to point to dist/**

Edit `extension/firefox/manifest.json`:

```json
{
  "manifest_version": 2,
  "name": "Hippo Browser Capture",
  "version": "0.2.0",
  "description": "Captures browsing activity for Hippo knowledge base",
  "permissions": [
    "nativeMessaging",
    "storage"
  ],
  "content_security_policy": "script-src 'self'; object-src 'self'",
  "background": {
    "scripts": ["dist/background.js"],
    "persistent": false
  },
  "content_scripts": [
    {
      "matches": ["<all_urls>"],
      "js": ["lib/Readability.js", "dist/content.js"],
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

Note: version bumped to `0.2.0` for the TypeScript rewrite.

- [ ] **Step 2: Update popup.html script src**

In `extension/firefox/popup.html`, change the script tag from:
```html
<script src="popup.js"></script>
```
to:
```html
<script src="dist/popup.js"></script>
```

- [ ] **Step 3: Build dist/**

Run:
```bash
cd extension/firefox && npm run build
```
Expected: `dist/background.js`, `dist/content.js`, `dist/popup.js` created.

- [ ] **Step 4: Type check**

Run:
```bash
cd extension/firefox && npx tsc --noEmit
```
Expected: No errors.

- [ ] **Step 5: Delete old JS source files**

```bash
rm extension/firefox/background.js extension/firefox/content.js extension/firefox/popup.js
```

- [ ] **Step 6: Package extension**

Run:
```bash
cd extension/firefox && npm run package
```
Expected: `web-ext-artifacts/hippo_browser_capture-0.2.0.zip` created.

- [ ] **Step 7: Verify zip contents**

Run:
```bash
unzip -l extension/firefox/web-ext-artifacts/hippo_browser_capture-0.2.0.zip
```
Expected: Contains `dist/background.js`, `dist/content.js`, `dist/popup.js`, `lib/Readability.js`, `manifest.json`, `popup.html`. Does NOT contain `src/` or `node_modules/`.

- [ ] **Step 8: Commit**

```bash
git add extension/firefox/manifest.json extension/firefox/popup.html
git rm extension/firefox/background.js extension/firefox/content.js extension/firefox/popup.js
git commit -m "feat(extension): complete TypeScript rewrite — wire manifest, delete old JS

Bumped extension version to 0.2.0. All JS now compiled from TypeScript
source in src/ via esbuild. Constants consolidated in src/config.ts.
Types shared via src/types.ts. Zero behavior changes."
```

---

## Parallel Execution Reference

| Task | Dependencies | Can run with | Agent |
|------|-------------|--------------|-------|
| Task 1 (toolchain) | None | Task 2 | Agent A |
| Task 2 (config + types) | None | Task 1 | Agent B |
| Task 3 (content.ts) | Task 1, 2 | Task 4, 5 | Agent A |
| Task 4 (background.ts) | Task 1, 2 | Task 3, 5 | Agent B |
| Task 5 (popup.ts) | Task 1, 2 | Task 3, 4 | Agent C |
| Task 6 (wiring + cleanup) | Task 1-5 | None | Any |

**Optimal with 2 agents:** A does 1→3→6, B does 2→4→5
**Optimal with 3 agents:** A=1→3, B=2→4, C=5, then any agent does 6
