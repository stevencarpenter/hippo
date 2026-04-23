import type { SearchEngine } from "./types";

/** Default domain allowlist — seeded on first install, overridden by browser.storage.local. */
export const DEFAULT_ALLOWLIST: string[] = [
  // Code forges & sharing
  "github.com",
  "github.io",
  "gitlab.com",
  "bitbucket.org",
  // Package registries
  "crates.io",
  "npmjs.com",
  "pypi.org",
  "mvnrepository.com",
  "pkg.go.dev",
  "rubygems.org",
  // Language & framework docs
  "docs.rs",
  "doc.rust-lang.org",
  "rust-lang.org",
  "docs.python.org",
  "python.org",
  "swift.org",
  "developer.mozilla.org",
  "docs.astral.sh",
  "typescriptlang.org",
  "learn.microsoft.com",
  "kubernetes.io",
  "go.dev",
  "nodejs.org",
  "ziglang.org",
  // AI & ML
  "anthropic.com",
  "openai.com",
  "huggingface.co",
  "arxiv.org",
  "lmstudio.ai",
  // System & OS docs
  "man7.org",
  "wiki.archlinux.org",
  // Database & infra docs
  "sqlite.org",
  "postgresql.org",
  "redis.io",
  "docker.com",
  // Q&A & community
  "stackoverflow.com",
  "stackexchange.com",
  "reddit.com",
  "news.ycombinator.com",
  "lobste.rs",
  // Developer content
  "medium.com",
  "dev.to",
  "hackernoon.com",
  "substack.com",
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
  { domain: "duckduckgo.com", param: "q" },
  { domain: "bing.com", param: "q" },
  { domain: "github.com", param: "q", pathPrefix: "/search" },
];
