# hippo-brain

Python enrichment and query server for Hippo. Polls the shared SQLite database for new shell events, enriches them via
local LLMs (LM Studio), and serves knowledge queries over HTTP.

## Setup

```bash
uv sync --project brain
```

## Running

```bash
# Start the HTTP query server (port 9175)
uv run --project brain hippo-brain serve

# Or via mise
mise run run:brain
```

## Testing

```bash
uv run --project brain pytest brain/tests -v
uv run --project brain pytest brain/tests -v --cov=hippo_brain --cov-report=term-missing
```

## Linting

```bash
uv run --project brain ruff check brain/
uv run --project brain ruff format --check brain/
```

## MCP Server

The brain also includes an MCP server that exposes Hippo's knowledge base as tools for Claude Code and other MCP clients.

```bash
uv run --project brain hippo-mcp
```

**Tools:**

| Tool | Description |
|------|-------------|
| `search_knowledge` | Semantic or lexical search over enriched knowledge nodes |
| `search_events` | Search raw events (shell commands, Claude sessions, browser visits) |
| `get_entities` | List extracted entities (projects, tools, files, domains, concepts) |

**Transport:** stdio using newline-delimited JSON (JSONL) — one JSON-RPC message per line. This is the default for MCP SDK v1.x stdio transport.

**Configuration:** See the MCP Server section in the project root `CLAUDE.md` for mcp-master.json setup and config propagation steps.

## Modules

| Module          | Purpose                                                                 |
|-----------------|-------------------------------------------------------------------------|
| `enrichment.py` | Polls SQLite queue, calls LM Studio for summarization/entity extraction |
| `server.py`     | Starlette HTTP server for query API                                     |
| `embeddings.py` | Vector embedding generation via LM Studio                               |
| `client.py`     | Daemon communication client                                             |
| `models.py`     | Pydantic data models                                                    |
| `training.py`   | Training data export utilities                                          |

## API

The brain server listens on `127.0.0.1:9175` and exposes:

- `POST /query` — Full-text search over events and enriched knowledge nodes
