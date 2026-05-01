import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const FALLBACK_FILE = path.resolve(__dirname, "..", "data", "github-fallback.json");

const REPO = "stevencarpenter/hippo";
const API = "https://api.github.com";

export interface Release {
  tag: string;
  name: string;
  date: string;       // YYYY-MM-DD
  url: string;
  body: string;       // raw markdown
  draft: boolean;
  prerelease: boolean;
}

export interface CommitInfo {
  sha: string;
  shortSha: string;
  message: string;
  date: string;
  url: string;
}

export interface CIRun {
  status: string;
  conclusion: string | null;
  date: string;
  url: string;
  name: string;
}

export interface GithubFallback {
  releases: Release[];
  latestCommit: CommitInfo | null;
  latestCIRun: CIRun | null;
  openIssueCount: number | null;
  fetchedAt: string;
}

function authHeaders(): Record<string, string> {
  const token = process.env.GITHUB_TOKEN;
  const headers: Record<string, string> = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "hippobrain-site-build",
    "X-GitHub-Api-Version": "2022-11-28",
  };
  if (token) headers["Authorization"] = `Bearer ${token}`;
  else if (process.env.CI) {
    console.warn("[github] no GITHUB_TOKEN; rate-limited fetches (60/h per IP)");
  }
  return headers;
}

async function fetchWithRetry(url: string, retries = 3): Promise<Response> {
  let lastErr: unknown;
  let delay = 1000;
  for (let attempt = 0; attempt < retries; attempt++) {
    try {
      const res = await fetch(url, { headers: authHeaders() });
      if (res.ok) return res;
      // Treat 4xx as terminal, 5xx as retryable.
      if (res.status >= 500 || res.status === 403) {
        lastErr = new Error(`fetch ${url} -> ${res.status}`);
        await new Promise((r) => setTimeout(r, delay));
        delay *= 4;
        continue;
      }
      return res;
    } catch (e) {
      lastErr = e;
      await new Promise((r) => setTimeout(r, delay));
      delay *= 4;
    }
  }
  throw lastErr instanceof Error ? lastErr : new Error(String(lastErr));
}

function loadFallback(): GithubFallback {
  if (existsSync(FALLBACK_FILE)) {
    try {
      return JSON.parse(readFileSync(FALLBACK_FILE, "utf8")) as GithubFallback;
    } catch {
      // fall through to empty
    }
  }
  return {
    releases: [],
    latestCommit: null,
    latestCIRun: null,
    openIssueCount: null,
    fetchedAt: "",
  };
}

function saveFallback(data: GithubFallback): void {
  try {
    mkdirSync(path.dirname(FALLBACK_FILE), { recursive: true });
    writeFileSync(FALLBACK_FILE, JSON.stringify(data, null, 2));
  } catch (e) {
    console.warn(`[github] could not persist fallback: ${e}`);
  }
}

let cache: GithubFallback | null = null;

async function loadGithubState(): Promise<GithubFallback> {
  if (cache) return cache;
  const fallback = loadFallback();
  // If GITHUB_TOKEN is missing AND we already have some fallback, prefer fallback
  // (avoid burning 60/h budget during local dev).
  const hasFallback = fallback.releases.length > 0 && !!fallback.fetchedAt;
  if (!process.env.CI && !process.env.GITHUB_TOKEN && hasFallback) {
    cache = fallback;
    return fallback;
  }
  try {
    const [relsRes, commitRes, runsRes, issuesRes] = await Promise.all([
      fetchWithRetry(`${API}/repos/${REPO}/releases?per_page=20`),
      fetchWithRetry(`${API}/repos/${REPO}/commits?per_page=1`),
      fetchWithRetry(`${API}/repos/${REPO}/actions/runs?per_page=1&branch=main`),
      fetchWithRetry(`${API}/search/issues?q=repo:${REPO}+is:issue+is:open&per_page=1`),
    ]);
    const releases = relsRes.ok
      ? ((await relsRes.json()) as Array<Record<string, unknown>>).filter((r) => !r.draft).map((r) => ({
          tag: String(r.tag_name ?? ""),
          name: String(r.name || r.tag_name || ""),
          date: String(r.published_at ?? r.created_at ?? "").slice(0, 10),
          url: String(r.html_url ?? ""),
          body: String(r.body ?? ""),
          draft: Boolean(r.draft),
          prerelease: Boolean(r.prerelease),
        }))
      : fallback.releases;
    let latestCommit: CommitInfo | null = fallback.latestCommit;
    if (commitRes.ok) {
      const arr = (await commitRes.json()) as Array<Record<string, unknown>>;
      const c = arr[0];
      if (c) {
        const commit = c.commit as Record<string, unknown>;
        const author = (commit?.author ?? {}) as Record<string, unknown>;
        const sha = String(c.sha ?? "");
        latestCommit = {
          sha,
          shortSha: sha.slice(0, 7),
          message: String(commit?.message ?? "").split("\n")[0] ?? "",
          date: String(author?.date ?? "").slice(0, 10),
          url: String(c.html_url ?? ""),
        };
      }
    }
    let latestCIRun: CIRun | null = fallback.latestCIRun;
    if (runsRes.ok) {
      const data = (await runsRes.json()) as { workflow_runs?: Array<Record<string, unknown>> };
      const r = data.workflow_runs?.[0];
      if (r) {
        latestCIRun = {
          status: String(r.status ?? ""),
          conclusion: r.conclusion ? String(r.conclusion) : null,
          date: String(r.run_started_at ?? r.created_at ?? "").slice(0, 10),
          url: String(r.html_url ?? ""),
          name: String(r.name ?? ""),
        };
      }
    }
    let openIssueCount: number | null = fallback.openIssueCount;
    if (issuesRes.ok) {
      const data = (await issuesRes.json()) as { total_count?: number };
      if (typeof data.total_count === "number") openIssueCount = data.total_count;
    }
    const next: GithubFallback = {
      releases,
      latestCommit,
      latestCIRun,
      openIssueCount,
      fetchedAt: new Date().toISOString(),
    };
    saveFallback(next);
    cache = next;
    return next;
  } catch (e) {
    console.warn(`[github] live fetch failed (${e}); using fallback`);
    cache = fallback;
    return fallback;
  }
}

export async function listReleases(): Promise<Release[]> {
  return (await loadGithubState()).releases;
}
export async function latestCommit(): Promise<CommitInfo | null> {
  return (await loadGithubState()).latestCommit;
}
export async function latestCIRun(): Promise<CIRun | null> {
  return (await loadGithubState()).latestCIRun;
}
export async function openIssueCount(): Promise<number | null> {
  return (await loadGithubState()).openIssueCount;
}
