# hippo-brain

Python enrichment and query server for Hippo. Polls the shared SQLite database for new shell events, enriches them via
a local OpenAI-compatible inference server (default oMLX, LM Studio also supported), and serves knowledge queries over HTTP.

## Development

Run from the repository root:

```bash
mise run build:brain       # Sync Python dependencies
mise run run:brain         # HTTP query server on port 9175
mise run test:python       # Tests with coverage
mise run lint:python
mise run fmt:check
```

## MCP Server

The brain also includes an MCP server that exposes Hippo's knowledge base as tools for Claude Code and other MCP clients.

```bash
uv run --project brain hippo-mcp
```

See [MCP setup](../README.md#mcp-server) for client configuration and
[the tool reference](../docs/mcp-reference.md) for arguments, return shapes,
and examples. Transport is stdio with one JSON-RPC message per line.

## Modules

| Module          | Purpose                                                                 |
|-----------------|-------------------------------------------------------------------------|
| `enrichment.py` | Polls SQLite queue, calls the inference server for summarization/entity extraction |
| `server.py`     | Starlette HTTP server for query/control API                             |
| `api_cli.py`    | Structured CLI wrapper for every brain HTTP API operation               |
| `openapi.py`    | Explicit OpenAPI 3.1 contract for the brain HTTP API                    |
| `embeddings.py` | Vector embedding generation via the inference server                    |
| `client.py`     | OpenAI-compatible InferenceClient (HTTP)                                |
| `models.py`     | Pydantic data models                                                    |
| `training.py`   | Training data export utilities                                          |

## API

The brain server listens on `127.0.0.1:9175` and exposes:

- `GET /health` — Server, queue, pause, schema, and inference health
- `GET /sessions` — Recent shell sessions
- `GET /events` — Recent shell events
- `GET /knowledge` — Enriched knowledge nodes
- `GET /knowledge/{id}` — One enriched knowledge node by id
- `POST /query` — Lexical or semantic search over events and knowledge nodes
- `POST /ask` — RAG answer synthesis over retrieved knowledge
- `POST /control/pause` — Stop claiming new enrichment work while keeping ingestion live
- `POST /control/resume` — Resume enrichment work
- `GET /openapi.json` — OpenAPI 3.1 contract for the routes above

Use the structured CLI instead of hand-written `curl` for normal operations:

```bash
mise run brain:api:health
mise run brain:api:sessions -- --limit 20
mise run brain:api:events -- --limit 20
mise run brain:api:knowledge -- --limit 20
mise run brain:api:knowledge-get -- 123
mise run brain:api:query -- "cargo build" --mode lexical
mise run brain:api:ask -- "how did I fix that build error?"
```

The wrapper defaults to the configured production brain URL. Override it with
`HIPPO_BRAIN_URL`, `BRAIN_URL`, or `--url`.

### OpenAPI

The OpenAPI contract can be read from a running server or generated offline:

```bash
mise run brain:api:openapi       # offline, no server required
mise run brain:api:openapi:live  # GET /openapi.json from the running brain
mise run brain:api:openapi:write # write OUT, default brain/openapi.json
```

The offline path is the stable integration point for code generators, API
diffing, and editor tooling because it does not require launchd or an inference
model to be running.

### Pausing enrichment

`POST /control/pause` pauses only the brain worker's queue claims. The daemon,
watchers, and pollers keep writing to SQLite, so queue depth can grow while the
local chat model is unloaded or reserved for another benchmark:

```bash
mise run brain:api:pause
mise run brain:api:health
mise run brain:api:resume
```

For complete model quiescence, unload the brain LaunchAgent instead of using the
soft pause endpoint:

```bash
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.hippo.brain.plist
```

Leave `com.hippo.daemon` and the ingest LaunchAgents running when you want
capture to continue and enrichment queues to fill. If the daemon is not already
loaded, bootstrap `~/Library/LaunchAgents/com.hippo.daemon.plist` separately.
