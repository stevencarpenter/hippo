# Provenance Baseline for Hippo Expansion — Research & Decisions

> Date: 2026-07-21. Method: internal evidence (live DB queries, source audit) + external landscape scan (all external claims carry URLs; star counts verified 2026-07-21).
> Purpose: fix the **provenance baseline** — what must be true about lineage, capture integrity, and trust in Hippo's data — before expanding capability (consolidation layer, more sources, proactive agents) and before any userbase beyond the author.
> Related: `docs/superpowers/specs/2026-07-21-brain-consolidation-layer-design.md`, `docs/superpowers/plans/2026-07-21-brain-consolidation-phases.md`.

## 1. Internal evidence: four months of real operation

Live DB (`~/.local/share/hippo/hippo.db`, schema v23), queried 2026-07-21:

| Measure | Value |
|---|---|
| Capture span | 2026-03-20 → 2026-07-21 (~4 months continuous) |
| `events` | 53,654 (shell 35,830 · claude-tool 17,824) |
| `browser_events` | 11,579 |
| `agentic_sessions` | 11,193 |
| `sessions` | 27,840 |
| `knowledge_nodes` | 19,187 (observation 16.6k · change_outcome 2.5k) |
| `entities` | 39,833 · `relationships` **0** |
| `lessons` | 18 · `capture_alarms` 3,898 |
| Node growth (last 14d) | 2–432 nodes/day, bursty |

Interpretation: capture is high-volume and durable; the derived layer is flat (see consolidation spec). 3,898 capture alarms against 18 lessons shows the graduation path works but is narrowly fed.

## 2. Provenance mechanisms Hippo already has (the baseline)

Audited against source; each is load-bearing today:

| Mechanism | Where | Strength |
|---|---|---|
| Idempotent ingest via `envelope_id` | `crates/hippo-daemon/src/daemon.rs` ("duplicate envelope_id, already stored") | Re-processing is safe; at-least-once producers can't double-write |
| Redaction before persist | `crates/hippo-core` redaction engine; daemon redacts before insert | Raw secrets never reach disk — the core privacy guarantee |
| Source→node link tables | `knowledge_node_events`, `knowledge_node_agentic_sessions`, browser/workflow variants | Every knowledge node traces to source rows (doc-level) |
| Evidence packets | `brain/src/hippo_brain/evidence_packets.py` | Query results carry `{ref, table, row_id, timestamp_ms, excerpt≤500c}` — inspectable citations |
| Capture integrity stack | `source_health`, `capture_alarms`, watchdog invariants I-1..I-10, synthetic probes (`docs/capture/`) | Capture paths are monitored and alarmed independently of the daemon |
| Versioned derivation | `enrichment_version`, `enrichment_model` on `knowledge_nodes`; `re-enrich-knowledge-nodes.py` | Derived rows know which pipeline vintage made them; re-derivation is rehearsed |
| Immutable revisions + last-known-good projection | auto-memory subsystem (`memory_documents`/revisions/tombstones) | The strongest provenance pattern in the codebase — but only applied to auto-memory |

## 3. External evidence: landscape scan

### 3.1 Agent-memory frameworks (capability comparables)

- **Graphiti (Zep's OSS core)** — [github.com/getzep/graphiti](https://github.com/getzep/graphiti), 29k stars, Apache-2.0. The strongest provenance model in the field: **episodes** (raw ingested data, ground truth) → **facts** (edges with temporal validity windows `valid_from`/`invalidated_at`). Invalidated facts are kept, never deleted, enabling "what was true at time T." Hybrid retrieval semantic+BM25+graph. Ships **opt-out PostHog telemetry**. Paper: [arXiv:2501.13956](https://arxiv.org/abs/2501.13956).
- **Mem0** — [github.com/mem0ai/mem0](https://github.com/mem0ai/mem0), 61.4k stars, Apache-2.0. April 2026 algorithm: **single-pass ADD-only extraction** — memories accumulate, no UPDATE/DELETE. Multi-signal retrieval, pluggable vector stores. Fact-level provenance is thin (source conversation context, no lineage graph).
- **Supermemory** — [github.com/supermemoryai/supermemory](https://github.com/supermemoryai/supermemory), 28.5k stars, MIT. Fact extraction with **contradiction handling and automatic expiry** ("moved to SF" supersedes "lives in NYC"). One-binary local mode with Ollama.
- **Letta (MemGPT)** — [github.com/letta-ai/letta](https://github.com/letta-ai/letta), 23.9k stars, Apache-2.0. Tiered memory (core in-context vs archival), self-editing memory blocks. Active dev moved to letta-code.
- **Cognee** — [github.com/topoteretes/cognee](https://github.com/topoteretes/cognee), 28.9k stars, Apache-2.0. remember/recall/forget/improve over graph+vectors; MCP server.
- **LangGraph memory store** — [docs.langchain.com/oss/python/langgraph/memory](https://docs.langchain.com/oss/python/langgraph/memory). The field's shared vocabulary: semantic/episodic/procedural memory; writes "hot path" or background.

Field convergence: fact extraction + temporal validity + hybrid retrieval + MCP, all Apache/MIT, benchmark-driven (LoCoMo, LongMemEval, MemoryBench). **Competing head-on here is a losing game.** None of them capture raw shell/session/browser activity — they all start from chat transcripts. Capture is Hippo's uncontested ground.

### 3.2 Personal activity capture (userbase comparables)

- **Rewind/Limitless** — acquired by Meta; macOS screen/audio capture **disabled Dec 19, 2025** ([limitless.ai](https://www.limitless.ai)). The category's proprietary flagship exited — the vendor-risk argument for local-first capture now has a canonical example.
- **Screenpipe** — [github.com/screenpipe/screenpipe](https://github.com/screenpipe/screenpipe), 20.4k stars, YC S26, **source-available commercial license** (June 2026). Rust core, local Whisper, SQLite+FTS5, MCP server, "pipes" (scheduled agents) with **deterministic per-pipe data permissions enforced at OS level**. Telemetry (PostHog/Sentry) on by default, opt-out. $25/mo tier.
- **OpenRecall** — [github.com/openrecall/openrecall](https://github.com/openrecall/openrecall), 2.9k stars, AGPL-3.0. Fully offline screenshots+OCR.
- **Windrecorder** — [github.com/yuka-friends/Windrecorder](https://github.com/yuka-friends/Windrecorder), 3.9k stars, GPL-2.0, Windows-only.

Screenpipe is nearly isomorphic to Hippo (local SQLite+FTS5+MCP+localhost API) minus shell/AI-session capture — architecture validated; its **pipe-level permission model is the reference design** for scoped agent access to captured data.

### 3.3 Provenance standards & practice

- **W3C PROV-DM** — [w3.org/TR/prov-dm](https://www.w3.org/TR/prov-dm/). Provenance as **Entity** (thing), **Activity** (process over time), **Agent** (bearer of responsibility), with `wasGeneratedBy` / `used` / `wasDerivedFrom` / `wasAttributedTo`. Full RDF is unnecessary at Hippo's scale; the three-type vocabulary is the valuable part. Direct mapping: events=entities, enrichment runs=activities, the local LLM=a software agent.
- **Anthropic Citations API** — [docs.anthropic.com/en/docs/build-with-claude/citations](https://docs.anthropic.com/en/docs/build-with-claude/citations). The reference implementation of claim-level citation: responses carry `cited_text` plus **char/block offsets into the source**, validated to exist. Strictly stronger than doc-level pointers — and cheap for Hippo, which owns the raw event text.
- **Transparency logs** — [transparency.dev](https://transparency.dev/). Append-only Merkle-tree history (Certificate Transparency, Sigstore): any modification cryptographically detectable. At single-user scale a per-source **hash chain over event rows** (each row's hash includes its predecessor) yields the same tamper evidence with ~zero infrastructure.

### 3.4 Local-first & privacy positioning

- **Ink & Switch, "Local-first software"** (Kleppmann et al., 2019 — [inkandswitch.com/essay/local-first](https://www.inkandswitch.com/essay/local-first)). Seven ideals; notably "the Long Now" — data outlives the company, and the US Library of Congress recommends **SQLite as an archival format**. Hippo's single-file SQLite design already satisfies ideals 1, 3, 5, 6, 7 for one user.
- Verified telemetry postures: Graphiti = opt-out PostHog; screenpipe = opt-out PostHog+Sentry; OpenRecall/Windrecorder = fully offline; Supermemory-local = "nothing leaves your machine." **"No telemetry, not even opt-out" is an unoccupied position** among the high-traction players.

## 4. Gap analysis: baseline vs. what expansion needs

| # | Gap | Why it blocks expansion | Evidence |
|---|---|---|---|
| G1 | No temporal validity on facts | Consolidation (Phase 3 supersession) and any "current state of X" answer needs valid-from/invalidated-at semantics | Graphiti, Mem0, Supermemory all converged on this; consolidation spec §supersession |
| G2 | Evidence packets are doc-level, no char offsets | `/ask` answers can cite a row but not the *span* that supports a claim; hallucination auditing stays coarse | Anthropic citations pattern (§3.3); Hippo owns the raw text, so offsets are cheap |
| G3 | No derivation-run identity | A node records `enrichment_model`+`enrichment_version` but not *which run*, prompt hash, or input batch — can't audit or reproduce a specific derivation | PROV Activity type (§3.3); needed for consolidation provenance (`synthesis_sources` covers inputs, not the run) |
| G4 | No tamper evidence on raw events | For a second user, "the daemon wrote this and it wasn't altered" is asserted, not verifiable | transparency.dev (§3.3); single-user can defer, multi-user cannot |
| G5 | No scoped access model for agents | MCP consumers currently see everything redaction lets through; userbase expansion needs per-consumer scoping | screenpipe pipes (§3.2) |
| G6 | Privacy posture undocumented as a guarantee | Differentiator ("no telemetry, not even opt-out") exists in fact but not as a stated, tested contract | §3.4 telemetry postures |
| G7 | `relationships` empty | Entity graph is prerequisite for salience + graph-expanded retrieval | Consolidation plan Phase 0 |

## 5. Decisions

Each decision is scoped to be evidence-backed and reversible; none blocks the consolidation plan's Phase 0.

**D1 — Adopt PROV's Entity/Activity/Agent as Hippo's provenance vocabulary.** Events/browser rows/agentic segments are Entities; enrichment and consolidation runs are Activities; capture producers and the local LLM are Agents. Schema columns and doc language use these terms. *Evidence: W3C PROV-DM (§3.3); zero-cost conceptual alignment before the derived layer grows.*

**D2 — Episode→fact with validity windows; supersede-never-delete, store-wide.** The auto-memory subsystem's immutable-revision pattern generalizes to all derived rows: `valid_from`/`invalidated_at` semantics via `superseded_by` (consolidation schema v24), raw events remain the episodes/ground truth. *Evidence: independent convergence of Graphiti (validity windows), Mem0 (ADD-only), Supermemory (auto-expiry), plus Hippo's own auto-memory design — four independent implementations of the same idea.*

**D3 — Upgrade evidence packets to claim-level citations.** Add optional `char_start`/`char_end` (+ quoted `cited_text` for verification) into the source row's text, validated on read. Doc-level packets remain the fallback. *Evidence: Anthropic Citations API is the reference pattern (§3.3); cost is low because Hippo owns raw event text; directly raises the auditability of every `/ask` answer and every future synthesis node.*

**D4 — Record derivation-run identity on every derived row.** New `derivation_runs` table (run_id, activity type, model, prompt version/hash, started/finished ts, input watermark range); `knowledge_nodes.derivation_run_id` FK going forward. *Evidence: PROV Activity (§3.3); re-enrich script already demonstrated re-derivation pain without run identity; consolidation Phase 1 needs this for `synthesis_sources` to be auditable.*

**D5 — Defer tamper-evident hash chaining; note it as the multi-user gate.** A per-source hash chain over `events` (row hash includes predecessor) with a `hippo verify` command is the designed-but-deferred answer. Single-user scale doesn't need it; the day a second user captures, it becomes mandatory. *Evidence: transparency.dev model (§3.3); deferral keeps Phase 0–1 scope intact.*

**D6 — State the privacy contract explicitly and test it.** "No telemetry, not even opt-out; redaction before persist; on-device inference only; your data is one SQLite file you can delete" becomes a documented guarantee with a doctor/CI check that fails if any network egress beyond configured inference appears. *Evidence: Graphiti and screenpipe both ship opt-out telemetry (§3.4) — the strict position is unoccupied; Rewind's Meta acquisition (§3.2) makes vendor-independence a live concern for this user category.*

**D7 — Do not enter the memory-framework benchmark race; compete on capture + provenance.** No LoCoMo/LongMemEval chase. Hippo's wedge: raw activity capture (shell/agentic/browser/workflow) + verifiable lineage + local-first. Bench investment stays on faithfulness-of-derivation gates (synthesis Q/A answerable only from evidence) rather than public memory benchmarks. *Evidence: §3.1 convergence table — five well-funded Apache/MIT frameworks already fight that war; capture has exactly one high-traction survivor (screenpipe) and it just went source-available with subscriptions.*

**D8 — Scoped agent access is a prerequisite for userbase expansion, modeled on pipes.** Before any second user: per-MCP-consumer permission scopes (source kinds, time ranges, projects) enforced at query time, not in prompts. *Evidence: screenpipe's deterministic per-pipe permissions (§3.2) are the validated reference design.*

## 6. Impact on the consolidation plan

The phased plan (`2026-07-21-brain-consolidation-phases.md`) absorbs D2–D4 with minimal scope change:

- **Phase 0** — unchanged (G7). Add G6's doctor check (cheap, no inference).
- **Phase 1** — add `derivation_runs` (D4) to schema v24; `synthesis_sources` gains `run_id`; supersession columns implement D2's validity windows.
- **Phase 2** — evidence packets gain claim-level offsets (D3); `/ask` answers cite spans.
- **Phase 3** — contradiction pass = D2's invalidation semantics in action; D5/D8 remain explicitly out of scope, tracked as the userbase gate.

## 7. Open questions

1. Should `derivation_runs` also cover the *embedding* step (embed model identity) or is `enrichment_model`+vec reaper coverage enough? Lean: include — embeddings are derivations too.
2. Claim-level offsets for agentic sessions: offsets into the segment summary or the raw JSONL line? Raw is truer; summary is what's embedded. Start with summary spans.
3. When D5 lands, per-source chains or one global chain? Per-source (matches `source_health` keying).
4. D6's egress check: static (config audit) or runtime (socket counter)? Start static; runtime is a watchdog invariant later.

## Appendix: adoption signals (verified 2026-07-21)

| Project | Stars | License | Cloud/Local | Telemetry |
|---|---|---|---|---|
| mem0 | 61.4k | Apache-2.0 | Both | cloud product |
| graphiti (Zep OSS) | 29k | Apache-2.0 | Local core; Zep=SaaS | opt-out PostHog |
| cognee | 28.9k | Apache-2.0 | Both | — |
| supermemory | 28.5k | MIT | Both (one-binary local) | — |
| letta | 23.9k | Apache-2.0 | Both (dev→letta-code) | — |
| screenpipe | 20.4k | Source-available | Local-first, $25/mo tier | opt-out PostHog+Sentry |
| getzep/zep | 4.8k | Apache-2.0 | Examples only (CE deprecated) | — |
| Windrecorder | 3.9k | GPL-2.0 | Fully local | none |
| OpenRecall | 2.9k | AGPL-3.0 | Fully local | none |
| Rewind/Limitless | n/a | Proprietary | Cloud; Meta-acquired, capture sunset 2025-12-19 | n/a |
