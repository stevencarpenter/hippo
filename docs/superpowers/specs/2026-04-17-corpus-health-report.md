# Corpus Health Report — 2026-04-17

**Author:** corpus-analyst (hippo-sqlite-vec team, wave 2)
**Source DB:** `~/.local/share/hippo/hippo.db` (schema v5, live; branch `postgres` is v6)
**Method:** read-only SQL against the live corpus + static analysis of `brain/` and `crates/` source.

## TL;DR

The corpus is **structurally sound** (no orphan knowledge nodes, clean KN→event link density, per-source enrichment rates
88 %/100 %/62 % for shell/Claude/browser) but suffers from **three quality gaps that will bite sqlite-vec retrieval**:
(1) **LM Studio is currently broken**, so 213 events are `failed` and 417+ are stuck `processing` behind an active lock —
the pending backlog is *growing* during analysis, confirming enrichment is wedged on `400 Bad Request` from
`localhost:1234`. (2) The `relationships` table has **zero INSERT sites in the codebase** — the graph-edges feature was
schema-provisioned but never wired, which silently disables any graph-traversal queries users might attempt. (3) Entity
canonicalization is **effectively path-verbatim** (just lowercased), so `storage.rs` exists as 8 separate entities across
worktrees/projects and `.gitignore` as 12 — entity-based retrieval will fragment badly as the corpus grows. Redaction is
working correctly (2 marginal false-negatives out of 6,790 events, both in analyst-authored SQL). Per-project coverage
is wildly uneven (hippo-postgres shows 1.74 % KN/event vs hippo at 13.4 %) because the new branch's events are
accumulating *faster than enrichment can drain them* while LM Studio is down.

## 1. Enrichment queue — 417 stuck `processing` rows

```sql
SELECT status, COUNT(*) FROM enrichment_queue GROUP BY status;
-- done 5866 | failed 213 | pending 291 | processing 417
```

**Lock analysis (all 417 rows share one lock timestamp):**

```sql
SELECT locked_by, retry_count, COUNT(*) FROM enrichment_queue WHERE status='processing' GROUP BY locked_by, retry_count;
-- brain-enrichment | 0 | 376
-- brain-enrichment | 1 |  34
-- brain-enrichment | 2 |   7
SELECT datetime(MIN(locked_at)/1000,'unixepoch') FROM enrichment_queue WHERE status='processing';
-- 2026-04-17 10:36:45  (wallclock during analysis: 11:07, i.e. ~31 min old and growing)
```

All 417 rows share identical `locked_at` (a single claim batch, not a stale orphan pool). Combined with the growing
`pending` count (231 → 291 → 417 over a 10-minute observation window) and the `400 Bad Request` failures (§2), this
points to **LM Studio returning 400 for the current model endpoint** rather than orphan processes. The claim batch is
holding the lock while retrying against a dead endpoint.

**Severity: HIGH.** Backlog is draining backwards. Recommend:
- Immediate: restart LM Studio and verify `models.enrichment` is loadable at `http://localhost:1234`.
- Short-term: cap claim-batch size (currently appears ≥400) so one bad batch doesn't wedge the queue. Add a
  watchdog that relinquishes `processing` locks older than N minutes (N = p99 enrichment latency × 3).
- Long-term: reject-batch + model-health preflight before claiming work; the current design assumes the endpoint is
  always healthy.

## 2. Failed enrichments — 213 rows, 94 % single root cause

```sql
SELECT error_message, COUNT(*) FROM enrichment_queue WHERE status='failed' GROUP BY error_message ORDER BY 2 DESC LIMIT 5;
-- "Client error '400 Bad Request' for url 'http://localhost:1234/v1/chat/completions'"  200
-- "ReadTimeout"                                                                           13
```

Only two distinct failure modes. **94 % are HTTP 400 from LM Studio** (stale model reference, malformed prompt,
exceeded context window, or simply no model loaded). 6 % are read timeouts (likely cold-start or large-context enrich).

Failed events span 2026-04-12 → 2026-04-17, so this is *episodic LM-Studio unavailability*, not a permanent corpus issue.
`retry_count=3` across the board (max_retries hit). Sample commands (below) are short, benign shell fragments — no
payload-shape pathology detectable.

```
"opencode" | "ll" | "burp" | "vim ~/.config/opencode/opencode.json" | "read /Users/.../hippo/crates/hippo-core/src/events.rs"
```

**Severity: MED.** All retryable in principle, but the pipeline has no retry-after-cooldown logic. Recommend:
- Add a `retriable_failures` reset path: periodically requeue `status='failed' AND retry_count>=max_retries` rows if
  the error_message matches a transient pattern (400/timeout), with exponential backoff on retry windows.
- Ship a `hippo ingest retry-failed` CLI to make this explicit.

## 3. Relationships table — 0 rows, 0 INSERT sites

```sql
SELECT COUNT(*) FROM relationships;  -- 0
```

```
$ grep -rn "INSERT INTO relationships\|relationships\b" crates brain --include='*.rs' --include='*.py'
crates/hippo-core/src/storage.rs:898:            "relationships",   -- only mention: a cleanup table list
```

**The only source reference is a table-list string in storage.rs cleanup code.** There is no brain-side enrichment path
that produces graph edges. Schema provides `from_entity_id`, `to_entity_id`, `relationship`, `weight`,
`evidence_count` — all dead.

**Implications for retrieval:**
- MCP tools like `get_entities` surface entities, but any "show related X" traversal returns empty.
- The sqlite-vec consolidation design (docs/2026-04-17-sqlite-vec-consolidation-design.md) presumes entity-link
  graph expansion as a future retrieval leg. That leg is currently 0-dimensional.
- Users running `ask` against the new pipeline will not notice — RAG synthesis works without the graph — but any
  workflow that pivots on "what's related to this entity?" returns no hits.

**Severity: HIGH** (silent feature-gap, not a crash). Recommend either:
- Wire relationships: during enrichment, emit `(from, to, verb)` triples from the LLM JSON (`entities.relationships`
  is already in the enrichment prompt contract), and upsert into `relationships` keyed by the UNIQUE constraint.
- Or **drop the table + MCP endpoints** and document the feature as not-shipped for v1. Keeping empty tables is worse
  than removing them: it creates false expectations for downstream consumers.

## 4. Lessons — 0 rows, expected

```sql
SELECT COUNT(*) FROM lessons, workflow_runs;  -- 0 | 0
```

Lessons are emitted from `workflow_enrichment.py` on CI signals. Personal machine has no `[workflow]` watchlist
configured, so `workflow_runs` is 0 → lessons trivially 0. **Expected-but-empty**, not a bug. Mention in docs: "lessons
require a workflow watchlist to be configured; see config.toml `[workflow.watchlist]`."

**Severity: LOW (documentation-only).**

## 5. Redaction — 1/6,790 events, but config is tight

```sql
SELECT COUNT(*) FROM events;                         -- 6,790 (growing live)
SELECT SUM(redaction_count>0) FROM events;           -- 1
SELECT id, command FROM events WHERE redaction_count>0;
-- 4642 | gh pr edit 38 ... --body "$(cat <<'EOF' ... generic_secret_assignment pattern fired on embedded body
```

**Is the user genuinely clean, or is redaction broken?** Sanity checks against the corpus:

```sql
-- AWS keys, GitHub PATs, JWTs, bearer headers, generic `*=secret`
SELECT COUNT(*) FROM events WHERE redaction_count=0 AND (
  command LIKE '%ghp\_%' ESCAPE '\\' OR command LIKE '%AKIA%'
  OR command LIKE 'export % TOKEN=%' OR command LIKE 'export %SECRET=%'
  OR command LIKE '%PASSWORD=%' OR command LIKE '%API_KEY=%');
-- 2
```

The 2 unredacted hits are **both analyst-generated SQL queries** I ran earlier in this session (my own secret-hunting
query captured by the shell hook — the query text contained the literal tokens `*TOKEN=*` and `*SECRET=*`). No true
secrets leaked into the corpus.

Reading `config/redact.default.toml`: AWS keys, GitHub PATs, JWT, bearer, generic `api_key|secret_key|password=\S{8,}`,
PEM blocks. Coverage is **reasonable but has gaps worth filing**:

- No rule for `sk-*` (OpenAI/Anthropic-style keys)
- No rule for GitLab `glpat-*` or `gho_*` / `ghs_*` / `ghu_*` (non-PAT GitHub tokens)
- No rule for connection strings (`postgres://user:pass@...`, `mysql://`)
- Generic `(api_key|…)=…` requires ≥8 char value — a short password would leak

**Severity: MED.** Redaction isn't miscounting; it's under-scoped. Recommend a follow-up to expand `redact.default.toml`
patterns (track as its own issue; not blocking for sqlite-vec merge).

## 6. Entity distribution & dedup — canonicalization is verbatim

```sql
SELECT type, COUNT(*) FROM entities GROUP BY type ORDER BY 2 DESC;
-- file 2098 | concept 802 | tool 715 | service 697 | project 128
SELECT type, COUNT(*), SUM(canonical IS NULL) FROM entities GROUP BY type;
-- every row has a canonical
```

100 % of entities have a canonical. But `canonical = LOWER(name)` — there is no real normalization:

```sql
SELECT name FROM entities WHERE type='file' AND name LIKE '%storage.rs';
-- /Users/carpenter/projects/hippo-otel/crates/hippo-core/src/storage.rs
-- /Users/carpenter/projects/hippo/.claude/worktrees/agent-a30fa304/crates/hippo-core/src/storage.rs
-- /Users/carpenter/projects/hippo/.claude/worktrees/agent-aa892054/crates/hippo-core/src/storage.rs
-- /Users/carpenter/projects/hippo/crates/hippo-core/src/storage.rs
-- /Users/carpenter/projects/hippo/crates/hippo-daemon/src/storage.rs
-- /Users/carpenter/projects/whistlepost/backend/src/utils/storage.rs
-- crates/hippo-core/src/storage.rs         (relative from cwd)
-- storage.rs                               (bare basename)
```

**8 separate `storage.rs` entities.** `.gitignore` has 12 variants; `.env`/`.env.example`/`.zshrc` have 6 each. The
UNIQUE(type, canonical) constraint fires on exact-path match, so the LLM's emitted form (relative vs. absolute vs.
basename, agent-worktree copies, different repos) each spawns a fresh row.

Sampled non-file entities also show **raw-error-message pollution** in the `concept` class:

- `"Connection aborted. BadStatusLine('\x00\x00\x06\x04...')"`
- `"browser enrichment failed: %s"` (format string, captured verbatim)
- `"error: unused import: \`BrowserEvent\`"`
- `"prettier: Formatting differences found in e2e/helpers/auth.ts"`

These are *transient noise*, not durable concepts.

**Severity: HIGH for retrieval quality.** Entity-based filter pushdown in the new retrieval leg will bucket queries
unevenly because `storage.rs@hippo` and `storage.rs@whistlepost` are both present but not related. Recommend:

- Short-term: entity resolver normalization pass — for `type='file'`, canonical = `(repo_name, relpath_from_repo_root)`
  when determinable from event context (events carry `cwd` and some carry repo inference).
- Short-term: entity class allowlist in enrichment — reject `concept` entities matching regex for shell error
  fragments, format strings, or punctuation-heavy short strings.
- Long-term: periodic dedup job that merges entities with fuzzy-matching canonicals (Levenshtein or path-suffix) into
  a single canonical with alias list.

## 7. Knowledge-node quality — samples

```sql
-- Random KN sample; check summary↔embed_text alignment
SELECT id, substr(content,1,200), substr(embed_text,1,200) FROM knowledge_nodes ORDER BY RANDOM() LIMIT 3;
```

3 sampled nodes (IDs 1501, 627, 1849):

- **1501** — summary "Conducted a comprehensive audit of the ai_book repository…" ; embed_text "Performed a repository
  audit on 'ai_book' to find hardcoded personal paths…" ✅ coherent, semantically parallel.
- **627** — summary "Implemented Task 6 – added a native-messaging host bridge…" ; embed_text "Added the
  native-messaging host bridge for Firefox by first adding the `url` crate…" ✅ strong alignment.
- **1849** — summary "Bumped workspace version from 0.9.1 to 0.10.0…" ; embed_text "Bumped workspace version from 0.9.1
  to 0.10.0 in root Cargo.toml (line 6)…" ✅ high specificity, good grounding.

**Link density:**

```sql
SELECT AVG(c), MAX(c), MIN(c) FROM (SELECT knowledge_node_id, COUNT(*) c FROM knowledge_node_events GROUP BY knowledge_node_id);
-- 6.43 events/node (max 30, min 1)
SELECT COUNT(*) FROM knowledge_nodes WHERE id NOT IN (SELECT knowledge_node_id FROM knowledge_node_events UNION SELECT ... );
-- 0 orphan nodes
```

Mean 6.4 events per shell node (range 1–30) — healthy narrative aggregation. **Zero orphan knowledge nodes.**

**Model mix** (all enrichment_model values across nodes):

```
gpt-oss-120b-mlx-crack   884
qwen3.5-35b-a3b          763
google/gemma-4-26b-a4b   218
google/gemma-4-31b        15
```

Multiple models in the corpus → retrieval may surface heterogeneous summary styles. Not a bug; worth noting when the
evaluation harness (task #11) measures synthesis groundedness — baseline comparisons should segment by model.

**Severity: LOW.** Quality is good; keep the sampling protocol for the eval harness.

## 8. Per-project coverage gaps

`events.git_repo` is **always NULL** (column provisioned but daemon isn't populating it). Used `cwd` prefix as project proxy:

```sql
WITH proj AS (SELECT id, CASE WHEN cwd LIKE '/Users/carpenter/projects/%' THEN
  substr(cwd,27, instr(substr(cwd,27)||'/', '/')-1) ELSE '(other)' END p FROM events)
SELECT p.p, COUNT(DISTINCT p.id) evs, COUNT(DISTINCT kne.knowledge_node_id) kn,
  ROUND(100.0*COUNT(DISTINCT kne.knowledge_node_id)/COUNT(DISTINCT p.id),2) pct
FROM proj p LEFT JOIN knowledge_node_events kne ON kne.event_id=p.id
GROUP BY p.p ORDER BY evs DESC LIMIT 12;
```

| Project         |   Events |   KN linked |  KN/event % |
|-----------------|---------:|------------:|------------:|
| hippo           |    3,965 |         530 |      13.4 % |
| chezmoi         |    1,041 |         186 |      17.9 % |
| **hippo-postgres** |    **746** |         **13** |       **1.7 %** |
| (other)         |      380 |         133 |      35.0 % |
| nuv             |      155 |          25 |      16.1 % |
| python_playa    |      105 |          31 |      29.5 % |
| tributary       |      102 |          22 |      21.6 % |
| ai_book         |       75 |          11 |      14.7 % |
| whistlepost     |       73 |          15 |      20.6 % |
| hippo-otel      |       48 |           7 |      14.6 % |
| stevectl        |       47 |          12 |      25.5 % |
| pp-bot          |       41 |          10 |      24.4 % |

**Findings:**

- **`hippo-postgres` is catastrophically under-enriched** (1.74 %). This is the branch doing the sqlite-vec work — high
  event volume (746) because the analyst/builders are active in that worktree, but enrichment is wedged (§1), so
  recent events haven't drained to KNs yet. Once LM Studio recovers, coverage should bounce back.
- hippo at 13.4 % and chezmoi at 17.9 % are the "mature" baselines. Treat **~15 %** as the expected equilibrium KN/event
  ratio given the current 6.4-events-per-node aggregation rate.
- `(other)` at 35 % is inflated because it includes sparse shell activity outside project roots — each command often
  becomes its own node.

**Severity: MED.** The `hippo-postgres` deficit is a symptom of §1, not a separate bug. Recommend:

- Backfill enrichment on `postgres` after LM Studio is healthy; verify coverage climbs above 10 %.
- File a follow-up to populate `events.git_repo` from the daemon (observed NULL across all 6,790 rows). Without it,
  per-project MCP filters in the new retrieval layer must fall back to cwd-prefix matching — brittle for non-`projects/`
  layouts.

## 9. Other sources — Claude + browser

```sql
SELECT COUNT(*) FROM claude_sessions;              -- 963
SELECT COUNT(*) FROM knowledge_node_claude_sessions;-- 963 (1:1 mapping)
AVG claude_sessions per KN: 1.0                    -- each session = one KN

SELECT COUNT(*) FROM browser_events;                -- 16
SELECT status, COUNT(*) FROM browser_enrichment_queue GROUP BY status;
-- done 10 | skipped 6
```

Claude source is clean and 1:1. Browser source is tiny (16 events) — 10 enriched, 6 deliberately skipped. No concerns.

## Severity summary

| # | Issue                                                              | Severity | Action owner          |
|---|--------------------------------------------------------------------|:---:|---|
| 1 | Enrichment queue wedged behind live LM Studio 400s                 | **HIGH** | User (restart LM Studio); pipeline (watchdog + claim cap) |
| 2 | 213 failed rows, no retry-after-cooldown logic                     | MED | brain team (CLI + backoff reset) |
| 3 | `relationships` table has 0 rows and 0 INSERT sites codebase-wide  | **HIGH** | brain team (wire or drop) |
| 4 | 0 lessons — expected given no `[workflow]` watchlist on this box   | LOW | docs |
| 5 | Redaction pattern set is under-scoped (`sk-`, `glpat-`, conn strings) | MED | core team (config) |
| 6 | Entity canonical = verbatim lowercased path → 8× `storage.rs` etc. | **HIGH** | brain team (resolver + dedup job) |
| 7 | KN summary/embed quality good; heterogeneous model mix             | LOW | eval harness segments by model |
| 8 | `hippo-postgres` coverage 1.7 % (symptom of #1)                    | MED | will self-heal post-#1; also fill `events.git_repo` |

## Notes for wave-2 teammates

- **metrics-designer (task #10 / #11):** segment qualitative metrics by `enrichment_model`; the corpus has 4 model
  vintages and synthesis-groundedness will vary by model. Also, the 1.7 % coverage on `hippo-postgres` contaminates any
  project-scoped retrieval benchmark — either backfill first or exclude `hippo-postgres` from the labeled Q/A set.
- **pitfall-auditor (task #11 / #12):** entity-dedup failure (§6) is a **10×-corpus time bomb** — at 10× scale
  `storage.rs` alone will have dozens of variants, and the UNIQUE(type, canonical) index grows linearly with no
  merging. Flag this in the risk register. Also flag: `relationships` being unpopulated means any risk analysis
  assuming graph expansion is moot — the feature is aspirational in code only.
