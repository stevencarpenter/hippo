# Hippo MCP Server Design

## Context

Hippo captures shell commands, Claude Code sessions, and Firefox browsing activity, enriches them via local LLMs, and stores them as knowledge nodes with vector embeddings. The knowledge base is queryable via `hippo query` (CLI) and the brain's HTTP `/query` endpoint, but both return raw results — the user must synthesize answers themselves.

An MCP server exposes Hippo's knowledge base as tools that Claude Code (or any MCP client) can call mid-conversation. Instead of switching to a separate terminal, the user asks a natural question and Claude retrieves relevant context from their personal knowledge base.

MCP tool calls are automatically captured by the existing Claude session ingestion pipeline (they appear as `tool_use` entries in session JSONL logs), so usage of the MCP server itself becomes part of the knowledge graph — no additional capture needed.

## Design Decisions

- **Language**: Python — the brain is already Python, all query infrastructure (LanceDB, SQLite, LM Studio client) is reusable
- **Packaging**: Inside the `brain/` project as a new module + entry point — shares code, one `pyproject.toml`
- **Transport**: stdio — matches all existing MCP servers in the user's config
- **Data access**: Direct SQLite + LanceDB + LM Studio — independent of the brain HTTP server
- **Framework**: FastMCP (official Python MCP SDK high-level API)

## Architecture

```
Claude Code / Claude Desktop / any MCP client
       ↕ stdio (JSON-RPC)
  hippo-mcp  (Python, FastMCP)
       │
       ├── SQLite (read-only, WAL mode)
       │     events, claude_sessions, browser_events,
       │     knowledge_nodes, entities
       │
       ├── LanceDB (read-only)
       │     knowledge.lance (vector search)
       │
       └── LM Studio (HTTP)
             embed query text for semantic search
```

The MCP server is **read-only** — it queries the knowledge base but never writes to it. The brain remains the sole writer (enrichment, embedding). SQLite WAL mode safely supports concurrent readers alongside the brain's writer.

**Transport protocol note:** The stdio transport uses newline-delimited JSON (JSONL) — one JSON-RPC message per line, not HTTP Content-Length framing. This is the wire format used by MCP SDK v1.x for stdio-based servers.

## Tools

### 1. `search_knowledge`

Search enriched knowledge nodes — the synthesized, LLM-enriched summaries of developer activity.

**Parameters:**
| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `query` | string | yes | — | Natural language search query |
| `mode` | string | no | `"semantic"` | `"semantic"` (vector similarity) or `"lexical"` (substring match) |
| `limit` | int | no | 10 | Max results to return |

**Returns:** JSON array of knowledge nodes, each with:
- `score` — relevance score (semantic: 0.0–1.0, lexical: null)
- `summary` — what was accomplished
- `intent` — developer goal (debugging, testing, refactoring, research, etc.)
- `outcome` — success / partial / failure / unknown
- `tags` — descriptive tags
- `embed_text` — detailed work log text (optimized for search)
- `cwd` — working directory
- `git_branch` — branch at time of activity

**Semantic mode:** Embeds the query via LM Studio, performs vector search on LanceDB knowledge table. Falls back to lexical if LM Studio or LanceDB unavailable.

**Lexical mode:** SQL LIKE search on `knowledge_nodes.content` and `knowledge_nodes.embed_text`.

### 2. `search_events`

Search raw, unenriched events across all source types — shell commands, Claude sessions, and browser visits.

**Parameters:**
| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `query` | string | no | — | Keyword search (substring match on command/URL/summary) |
| `source` | string | no | `"all"` | Filter by source: `"shell"`, `"claude"`, `"browser"`, `"all"` |
| `since` | string | no | — | Time filter, e.g. `"24h"`, `"7d"`, `"30d"` |
| `project` | string | no | — | Substring match on working directory |
| `limit` | int | no | 20 | Max results per source type |

**Returns:** JSON array of events, each with:
- `source` — "shell", "claude", or "browser"
- `timestamp` — epoch milliseconds
- `summary` — command text (shell), summary_text (claude), or URL + title (browser)
- `cwd` — working directory (shell/claude) or empty (browser)
- `detail` — exit_code (shell), tool count (claude), or dwell_ms + scroll_depth (browser)
- `git_branch` — if available

**Query behavior:** When `query` is provided, filters by LIKE match. When absent, returns most recent events (ordered by timestamp DESC).

**`since` parsing:** Regex match on `(\d+)(h|d|m)` — hours, days, minutes. Converted to epoch ms threshold: `now_ms - (value * unit_ms)`. Invalid format is silently ignored (no time filter applied).

### 3. `get_entities`

List known entities extracted from enrichment — projects, tools, files, domains, concepts.

**Parameters:**
| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `type` | string | no | — | Filter by entity type: `"project"`, `"tool"`, `"file"`, `"domain"`, `"concept"`, `"service"` |
| `query` | string | no | — | Substring match on entity name |
| `limit` | int | no | 50 | Max results |

**Returns:** JSON array of entities, each with:
- `type` — entity type
- `name` — entity name
- `canonical` — normalized form
- `first_seen` — epoch ms
- `last_seen` — epoch ms

## Module Structure

One new file: `brain/src/hippo_brain/mcp.py`

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "hippo",
    instructions="Search a developer's personal knowledge base..."
)

@mcp.tool()
async def search_knowledge(query: str, mode: str = "semantic", limit: int = 10) -> list[dict]:
    ...

@mcp.tool()
async def search_events(query: str = "", source: str = "all", since: str = "", project: str = "", limit: int = 20) -> list[dict]:
    ...

@mcp.tool()
async def get_entities(type: str = "", query: str = "", limit: int = 50) -> list[dict]:
    ...

def main():
    mcp.run(transport="stdio")
```

The module initializes database connections at startup:
- SQLite: opened per-query (same `_get_conn()` pattern as the brain server — WAL + busy_timeout)
- LanceDB: opened once at module load, reused for all vector searches
- LM Studio client: initialized once, used for embedding queries

Config is loaded from `~/.config/hippo/config.toml` using the same `load_config()` path as the brain's `__init__.py`.

## Entry Point and Packaging

**pyproject.toml** addition:
```toml
[project.scripts]
hippo-brain = "hippo_brain:main"
hippo-mcp = "hippo_brain.mcp:main"
```

**New dependency:**
```toml
dependencies = [
    ...existing...,
    "mcp>=1.0",
]
```

**MCP master config** (`~/.config/mcp/mcp-master.json`):
```json
{
  "hippo": {
    "type": "stdio",
    "command": "uv",
    "args": ["run", "--project", "~/projects/hippo/brain", "hippo-mcp"],
    "disabled": false,
    "autoApprove": ["search_knowledge", "search_events", "get_entities"]
  }
}
```

## Error Handling

- **LM Studio down:** `search_knowledge` with `mode="semantic"` falls back to lexical search and includes a warning in the response
- **LanceDB missing/empty:** Same fallback to lexical with warning
- **SQLite locked:** `busy_timeout=5000` retries for 5 seconds before erroring
- **Invalid parameters:** FastMCP validates types via Pydantic; invalid inputs return structured errors automatically

## Testing

- Unit tests for each tool function with in-memory SQLite (same pattern as `test_browser_enrichment.py`)
- Test semantic fallback to lexical when LM Studio is unreachable
- Test `since` time parsing ("24h", "7d", "30d")
- Test source filtering in `search_events`
- Integration test: start MCP server, send a `tools/list` JSON-RPC message via stdin, verify 3 tools returned

## File Changes

| File | Change |
|------|--------|
| `brain/src/hippo_brain/mcp.py` | **New** — MCP server module with 3 tools |
| `brain/pyproject.toml` | Add `hippo-mcp` entry point, add `mcp` dependency |
| `brain/tests/test_mcp.py` | **New** — tests for MCP tool functions |

## Verification

1. `uv run --project brain hippo-mcp` starts without error and listens on stdio
2. Send `{"jsonrpc":"2.0","id":1,"method":"tools/list"}` to stdin → get 3 tools back
3. Send a `tools/call` for `search_knowledge` → get knowledge node results
4. Add to `mcp-master.json` → Claude Code sees the hippo tools
5. In Claude Code: "What was I working on yesterday?" → Claude calls `search_knowledge` → returns relevant activity
