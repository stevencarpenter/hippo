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
