# Hippo marketing/positioning draft

## 1. Positioning statement

Hippo is for engineers who spend their day moving between a terminal, an AI coding agent, and a browser tab — and who are tired of re-explaining context every time one of those tools starts a new session. AI coding agents (Claude Code, Codex, Cursor) reset to zero at the start of every conversation: yesterday's debugging trail, the StackOverflow tab that fixed it, and the shell commands that proved it are gone unless you paste them back in by hand. Shell history tools (Atuin, `~/.zsh_history`) only capture the command, not why you ran it or what happened after. Cloud "memory" products solve the recall problem by uploading your shell history and conversation transcripts to someone else's server. Hippo captures shell activity, Claude/Codex/Cursor agent sessions, and allowlisted browser visits into one local knowledge graph, enriches it with a local LLM, and hands it back to your coding agent over MCP — so the agent can ask "what was I just doing" and get a real, cited answer, without your data leaving your machine. Now is the moment because agentic coding sessions are the fastest-growing source of "context I need but don't have" — and they're also the easiest to capture, since the transcripts already exist on disk.

## 2. Tagline candidates

1. Your shell, your agent sessions, your browser history — one searchable graph, entirely on your machine.
2. Local memory for the tools that already forgot what you told them yesterday.
3. Cross-source recall for people who live in a terminal.
4. What you ran, what the agent did, and why — linked, local, and queryable.
5. The second brain that never leaves your laptop.

## 3. Elevator pitch (100 words)

Hippo is a local-first knowledge capture daemon for macOS. It watches your shell commands, your Claude Code / Codex / Cursor agent sessions, and your allowlisted Firefox browsing, redacts known secret formats, and links them into one SQLite knowledge graph — the shell command, the agent conversation that produced it, and the docs tab you had open, all connected. A local LLM (via oMLX or LM Studio) enriches events into summarized, embedded knowledge nodes. Query it with `hippo ask "how did I fix that build error"` from a terminal, or let Claude Code query it directly through the bundled MCP server, mid-conversation, with no cloud round-trip.

## 4. README hero section

### Hippo

Local-first knowledge capture daemon for macOS. Hippo watches your shell activity, your Claude Code / Codex / Cursor agent sessions, and your Firefox browsing, redacts known secret formats, enriches events with a local LLM, and builds a searchable second brain — all without sending data to third-party services.

AI coding agents don't remember what happened in your last session. Your shell history doesn't know what the agent was doing when you ran that command. Your browser tab with the fix doesn't know either. Hippo links all three into one knowledge graph on your own machine, so you — or your agent, through MCP — can ask "how did I fix that build error last week" and get a synthesized answer with cited sources instead of scrolling back through terminal scrollback.

LLM inference runs against any OpenAI-compatible local server — defaults target [oMLX](https://omlx.ai) for Apple Silicon, and [LM Studio](https://lmstudio.ai/) works as a drop-in alternative. No data reaches Anthropic, OpenAI, or any cloud service unless you point the inference URL at one yourself.

- **Cross-source recall.** A shell command, the Claude Code/Codex/Cursor session that produced it, and the browser tab you had open are linked into one searchable knowledge graph — not three disconnected logs.
- **Semantic + lexical retrieval.** `hippo ask "how did I fix that build error?"` returns a synthesized answer with cited sources, not a `grep` over command strings.
- **Local-only by default.** Shell stdout, agent transcripts, and browsing history never leave your machine. The LLM that summarizes them runs locally too.
- **MCP-native.** Claude Code (or any MCP client) queries your knowledge base mid-conversation through the bundled `hippo-mcp` server — the model can look up "what was I just doing" without you re-explaining it.
- **Built for reliability, not a demo.** A watchdog asserts capture invariants every 60 seconds, synthetic probes round-trip real events through every capture path every 5 minutes, and `hippo doctor` gives you a CAUSE/FIX/DOC breakdown the moment something drops.

## 5. Three differentiators (grounded in the code)

**vs. cloud memory products.** The entire pipeline — daemon, enrichment, embeddings, MCP server — reads and writes a single local SQLite file (`~/.local/share/hippo/hippo.db`) and calls out only to a local OpenAI-compatible inference server (oMLX or LM Studio) on `127.0.0.1`. There is no hosted service in the loop by default: no account, no upload step, no vendor with a copy of your shell history. The trade-off is real and stated in the README itself — the DB is unencrypted at rest and readable by any process running as your user, so it's a single-user, single-machine model, not a synced multi-device one.

**vs. plain RAG over your notes.** Most local RAG setups start from documents you deliberately wrote. Hippo's corpus is built from *ambient* capture — the shell hook (`preexec`/`precmd`), an FSEvents watcher on Claude session JSONL, Rust pollers for Codex and Cursor Agent transcripts, and Native Messaging from a Firefox extension — so the knowledge graph accumulates from things you did, not things you remembered to write down. The retrieval layer also fuses vector similarity (sqlite-vec) with FTS5 lexical search and returns per-hit `freshness` and `confidence` scores, not just a similarity number, so an agent citing hippo's output has a basis for trusting or discounting it.

**vs. harness-native memory (each agent's own session history).** Claude Code, Codex, and Cursor each keep their own session logs, in their own formats, with no link between a Codex session and the shell commands or browser research that happened alongside it. Hippo's `agentic_sessions` table is explicitly harness-spanning (a `harness` column tracks `claude-code`, `codex`, `opencode`, with Cursor sessions sharing the same enrichment path), and every one of those sessions is joined against `events` and `knowledge_nodes` from the *other* capture sources in the same window. The value isn't storing the Claude transcript again — Claude Code already does that — it's connecting it to the shell command it produced and the doc you were reading when you wrote the prompt.

## 6. Launch post outline (HN / blog, ~8 bullets)

1. **Hook** — the problem in one sentence: your coding agent forgets everything the moment the session ends, and so does your shell history, and so does your browser tab, and none of the three talk to each other.
2. **What hippo actually is** — one Rust daemon + one Python brain + a SQLite file, watching shell/Claude/Codex/Cursor/Firefox, all local.
3. **The graph, not just the log** — worked example: one `hippo ask` query that stitches together a shell error, the Claude Code session that diagnosed it, and the doc tab that had the fix, with cited sources.
4. **Local LLM, not cloud** — oMLX/LM Studio, why local inference specifically (no data-leaves-machine trade-off, no per-token bill for something that runs continuously in the background).
5. **MCP as the interesting part** — Claude Code querying its own history mid-conversation via the bundled MCP server; this is the part that's different from "just index my files."
6. **What we don't pretend to solve** — single-user/single-machine, unencrypted DB (FileVault is on you), redaction is regex-based and best-effort, not a DLP guarantee, MCP access is a broad trust grant.
7. **Reliability isn't an afterthought** — link the capture-reliability stack (watchdog invariants, 5-minute synthetic probes, `hippo doctor --explain`), and the honest reason it exists: two multi-week silent capture outages that a naive health check missed.
8. **Try it / contribute** — Apple Silicon quick-install one-liner, link to CONTRIBUTING.md and the capture anti-patterns doc for anyone extending it, license (MIT), and where the roadmap items live.

## Claims to verify

- **"Codex/Cursor" as first-class, not aspirational.** Confirmed real in `docs/lifecycle.md` and `docs/capture/sources.md` — both have working Rust pollers (`codex_session::poll_tick`, `cursor_session::poll_tick`). But `docs/capture/sources.md` also states the `agentic_sessions.harness` column is "harness-agnostic by design" and that **opencode rows are aspirational in v14** — don't imply opencode support is live; it isn't.
- **Cross-platform framing.** The README and Cargo badges are explicit: **macOS Apple Silicon (arm64) only** for release binaries; Intel requires building from source. No Linux/Windows support exists. Any launch copy implying broader platform support needs a correction.
- **"Second brain" framing vs. actual retrieval quality.** I read the MCP reference and lifecycle docs but did not run `hippo ask` against a live corpus myself — the "synthesized answer with cited sources" example in the pitch is copied verbatim from `docs/mcp-reference.md`'s own documented example output, not independently reproduced in this session.
- **hippobrain.org.** The domain's existence was asserted in the task but I did not check what (if anything) currently resolves there or whether it matches this positioning.
- **"Built for reliability, not a demo" bullet.** Grounded in real docs (`docs/capture/architecture.md`, `docs/brain-watchdog.md`, the watchdog/probe LaunchAgents in the README's architecture table) but I did not execute `hippo doctor` or trigger a probe myself in this session — the capability is documented, not freshly verified end-to-end.
- **Version currency.** Positioning cites `v0.31.0` (from `Cargo.toml` / `brain/pyproject.toml`) and schema `v14` (from README) as of this read — reconfirm before publishing if the repo has moved since.
