# Firefox Extension TypeScript Rewrite

## Context

The Firefox extension (`extension/firefox/`) is ~500 lines of vanilla JavaScript across 3 files. It works but has maintenance issues:

- The default domain allowlist is duplicated in `background.js`, `popup.js`, and `config.default.toml`
- Constants like `MIN_DWELL_MS`, `MAX_TEXT_BYTES`, `NATIVE_HOST` are scattered across files
- No type safety — field name mismatches between the extension and the Rust `BrowserVisit` struct are caught at runtime (or not at all)
- No build step — changes to shared values require editing multiple files

Rewriting in TypeScript with esbuild fixes all of these while keeping the extension small and fast.

## Design Decisions

- **Language**: TypeScript — catches type drift at compile time
- **Bundler**: esbuild — fast, zero-config, outputs IIFE bundles for Firefox MV2 compatibility
- **Config source of truth**: `src/config.ts` compiled into the bundle, overridden at runtime by `browser.storage.local`
- **Readability**: stays vendored and loaded via manifest (not bundled by esbuild)

## Source Structure

```
extension/firefox/
├── src/
│   ├── config.ts          # Shared constants: DEFAULT_ALLOWLIST, MIN_DWELL_MS, etc.
│   ├── types.ts           # Shared interfaces: BrowserVisit, PageVisitMessage, etc.
│   ├── background.ts      # Message handler, allowlist check, native messaging
│   ├── content.ts         # Dwell/scroll tracking, Readability, domain pre-check
│   └── popup.ts           # Toggle, stats, allowlist editor
├── lib/
│   └── Readability.js     # Vendored Mozilla Readability (unchanged)
├── dist/                  # esbuild output (gitignored)
│   ├── background.js
│   ├── content.js
│   └── popup.js
├── manifest.json          # Points to dist/ for JS, lib/ for Readability
├── popup.html             # Script src updated to dist/popup.js
├── icons/
├── package.json           # esbuild + typescript as devDependencies
├── tsconfig.json          # Strict mode, ES2022 target
├── build.mjs              # esbuild build script
└── .gitignore             # dist/, node_modules/, web-ext-artifacts/
```

## Shared Config (`src/config.ts`)

Single source of truth for all constants:

```typescript
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

export const MIN_DWELL_MS = 3000;
export const MAX_TEXT_BYTES = 50 * 1024;
export const NATIVE_HOST = "hippo_daemon";

export const SEARCH_ENGINES: SearchEngine[] = [
  { domain: "google.com", param: "q" },
  { domain: "www.google.com", param: "q" },
  { domain: "duckduckgo.com", param: "q" },
  { domain: "bing.com", param: "q" },
  { domain: "www.bing.com", param: "q" },
  { domain: "github.com", param: "q", pathPrefix: "/search" },
];
```

Both `background.ts` and `popup.ts` import `DEFAULT_ALLOWLIST` from here. The duplication is eliminated.

## Shared Types (`src/types.ts`)

```typescript
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

export interface CheckDomainMessage {
  type: "check_domain";
  domain: string;
}

export interface SearchEngine {
  domain: string;
  param: string;
  pathPrefix?: string;
}

export interface Settings {
  enabled: boolean;
  allowlist: string[];
  captureCount: number;
}
```

These match the Rust `BrowserVisit` struct field-for-field. A field name typo is a compile error.

## Build System

**package.json** (devDependencies only, no runtime deps):
```json
{
  "private": true,
  "scripts": {
    "build": "node build.mjs",
    "watch": "node build.mjs --watch",
    "package": "node build.mjs && npx web-ext build --overwrite-dest"
  },
  "devDependencies": {
    "esbuild": "^0.25",
    "typescript": "^5.8",
    "@anthropic-ai/sdk": "^0.1"
  }
}
```

Wait — scratch `@anthropic-ai/sdk`, that's not needed. Just:
```json
{
  "private": true,
  "scripts": {
    "build": "node build.mjs",
    "watch": "node build.mjs --watch",
    "package": "node build.mjs && npx web-ext build --overwrite-dest"
  },
  "devDependencies": {
    "esbuild": "^0.25",
    "typescript": "^5.8"
  }
}
```

**build.mjs**:
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
  console.log("watching...");
} else {
  await Promise.all(entries.map((e) => build({ ...common, ...e })));
  console.log("built dist/");
}
```

**tsconfig.json**:
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
    "noEmit": true
  },
  "include": ["src/**/*.ts"]
}
```

`noEmit: true` because esbuild does the actual compilation. `tsc` is only used for type checking (`npx tsc --noEmit`).

## Manifest Changes

```json
"content_scripts": [{
  "matches": ["<all_urls>"],
  "js": ["lib/Readability.js", "dist/content.js"],
  "run_at": "document_idle"
}],
"background": {
  "scripts": ["dist/background.js"],
  "persistent": false
}
```

popup.html script tag: `<script src="dist/popup.js"></script>`

## Readability Integration

Readability.js stays in `lib/` and is loaded before `content.ts` via the manifest. In `content.ts`:

```typescript
declare class Readability {
  constructor(doc: Document);
  parse(): { title: string; textContent: string; content: string } | null;
}
```

This gives type safety without bundling or modifying the vendored library.

## Security

All existing hardening carries over:
- Sender validation (`sender.id === browser.runtime.id`)
- Message structure validation (`isValidPageVisit()`)
- Type coercion before native messaging send
- Explicit CSP in manifest
- Only `nativeMessaging` + `storage` permissions

TypeScript adds compile-time enforcement that message shapes match expectations.

## What Changes Behaviorally

Nothing. The extension does exactly the same thing. The rewrite is purely structural:
- Constants consolidated
- Types enforced
- Build step added
- Old `.js` source files replaced by `.ts` source + compiled `dist/`

## Build and Install Workflow

```bash
cd extension/firefox
npm install          # first time
npm run build        # tsc check + esbuild → dist/
npm run package      # build + web-ext → .zip

# Install: about:addons → gear → Install Add-on From File
```

## File Changes

| Action | File |
|--------|------|
| Delete | `extension/firefox/background.js` |
| Delete | `extension/firefox/content.js` |
| Delete | `extension/firefox/popup.js` |
| Create | `extension/firefox/src/config.ts` |
| Create | `extension/firefox/src/types.ts` |
| Create | `extension/firefox/src/background.ts` |
| Create | `extension/firefox/src/content.ts` |
| Create | `extension/firefox/src/popup.ts` |
| Create | `extension/firefox/package.json` |
| Create | `extension/firefox/tsconfig.json` |
| Create | `extension/firefox/build.mjs` |
| Modify | `extension/firefox/manifest.json` (point to dist/) |
| Modify | `extension/firefox/popup.html` (script src) |
| Modify | `extension/firefox/.gitignore` (add dist/, node_modules/) |
