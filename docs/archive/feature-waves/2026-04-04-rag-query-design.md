# Hippo RAG Query (`ask`)

**Date:** 2026-04-04
**Status:** Approved

## Problem

Hippo captures and enriches shell, Claude, and browser activity into knowledge nodes with vector embeddings in LanceDB.
Current search (`search_knowledge`, `hippo query`) returns ranked raw results — the user must mentally synthesize across
multiple nodes to get an answer. Two gaps:

1. **Claude context limits** — hundreds of past sessions have been enriched into Hippo. Claude can't fit that history in
   context, but needs a way to retrieve and reason over it.
2. **Quick terminal recall** — the user wants one-off answers ("what was that rsync flag I used?") without sifting
   through a results list.

## Solution

A retrieval-augmented generation (RAG) layer: embed the question, retrieve relevant knowledge nodes via vector
similarity, feed them to a local LLM for synthesis, return a conversational answer with cited sources.

## Data Flow

```
User question
  → LM Studio /v1/embeddings (embed the question)
  → LanceDB ANN search on vec_knowledge (top N nodes)
  → Build prompt: system instructions + retrieved context + question
  → LM Studio /v1/chat/completions (synthesis)
  → Return { answer, sources[], model }
```

## Components

### 1. Config: `models.chat`

New key in `[models]` in `config.default.toml`:

```toml
[models]
enrichment = "qwen3.5-35b-a3b"
embedding = "text-embedding-nomic-embed-text-v2-moe"
chat = ""  # defaults to enrichment model when empty
```

Resolution logic: if `models.chat` is empty or unset, fall back to the resolved enrichment model (which itself has
dynamic fallback). This makes it zero-config by default but allows pointing at a beefier model for Q&A.

### 2. Core RAG function: `brain/src/hippo_brain/rag.py`

New module containing the RAG pipeline:

```python
async def ask(
    question: str,
    lm_client: LMStudioClient,
    vector_table,
    chat_model: str,
    embedding_model: str,
    limit: int = 10,
) -> dict:
```

Steps:
1. Embed `question` via `lm_client.embed([question], model=embedding_model)`.
2. Call `search_similar(vector_table, query_vec, limit=limit)` to retrieve top N knowledge nodes.
3. Build the synthesis prompt (see System Prompt below).
4. Call `lm_client.chat(prompt, model=chat_model)` for synthesis.
5. Return response dict (see Response Shape below).

Error handling:
- LM Studio unreachable for embedding → return error dict, no crash.
- LM Studio unreachable for chat → return the retrieved sources without an answer, so the caller still gets value.
- Zero results from vector search → return an answer stating no relevant knowledge was found.

### 3. MCP tool: `ask`

New tool in `mcp.py`:

```python
@mcp.tool()
async def ask(question: str, limit: int = 10) -> str:
    """Ask a question and get a synthesized answer from your knowledge base.
    
    Uses semantic search to find relevant knowledge, then synthesizes
    a conversational answer using a local LLM. Returns the answer
    along with source references.
    """
```

Parameters:
- `question: str` — the natural language question
- `limit: int = 10` — number of knowledge nodes to retrieve for context (clamped to MAX_LIMIT)

Returns a formatted string with the answer and sources list. Falls back gracefully on any LM Studio failure.

### 4. Brain HTTP endpoint: `POST /ask`

New route in `server.py`:

- Accepts: `{"question": "...", "limit": 10}`
- Returns: JSON response (see Response Shape)
- Uses the same `rag.ask()` core function as the MCP tool

### 5. CLI command: `hippo ask <question>`

New subcommand in the Rust CLI (`main.rs` / `commands.rs`):

```
hippo ask "how did I set up Firefox native messaging?"
```

Implementation:
1. POST to `http://localhost:{brain_port}/ask` with `{"question": text}`.
2. Render the answer text.
3. List sources below with scores, timestamps, summaries, and working directories.
4. On brain unreachable: print error suggesting `hippo doctor` — no raw query fallback, since RAG requires the brain.

CLI output format:
```
You set up Firefox native messaging by running `hippo daemon install --force`,
which writes the NM manifest to ~/Library/Application Support/Mozilla/...

Sources:
  [0.92] Configured Firefox native messaging host for hippo
         ~/projects/hippo (main) — 2026-03-31
  [0.87] Built and installed hippo with browser support
         ~/projects/hippo (main) — 2026-03-31
```

## System Prompt for Synthesis

```
You are a personal knowledge assistant. The user is asking about their own past
activity — commands they ran, decisions they made, problems they solved.

Answer the user's question using ONLY the context provided below. Be specific:
reference actual commands, file paths, error messages, and details from the
context. If the context doesn't contain enough information to answer fully,
say what you can and note what's missing.

Do not make up information. Do not hallucinate commands or paths.
```

The retrieved knowledge nodes are formatted as numbered context blocks:

```
[1] (score: 0.92, 2026-03-31)
Summary: Configured Firefox native messaging host for hippo
Commands: cargo build --release && hippo daemon install --force
CWD: ~/projects/hippo
Branch: main
Outcome: success
Tags: firefox, native-messaging, install
```

## Response Shape

```json
{
  "answer": "You set up Firefox native messaging by running...",
  "sources": [
    {
      "score": 0.92,
      "summary": "Configured Firefox native messaging host for hippo",
      "cwd": "~/projects/hippo",
      "git_branch": "main",
      "timestamp": 1717012345000,
      "commands_raw": "cargo build --release && hippo daemon install --force"
    }
  ],
  "model": "qwen3.5-35b-a3b"
}
```

## Scope Exclusions

These are explicitly out of scope for this spec:

- **`vec_command` search** — fast-follow to also search by command similarity for "what was that command?" queries.
- **Conversation history** — each `ask` call is independent, single-shot. Follow-up context is Claude's job.
- **Streaming** — waits for full LLM response. Streaming can be added later if latency is a concern.
- **Source filtering** — no `source` or `project` parameter for now. Retrieval searches all knowledge nodes.
