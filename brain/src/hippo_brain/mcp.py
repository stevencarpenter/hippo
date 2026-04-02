"""Hippo MCP Server — expose the knowledge base as tools for Claude Code."""

import sqlite3
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from hippo_brain.client import LMStudioClient
from hippo_brain.embeddings import (
    EMBED_DIM,
    _pad_or_truncate,
    get_or_create_table,
    open_vector_db,
    search_similar,
)
from hippo_brain.mcp_logging import MetricsCollector, setup_logging
from hippo_brain.mcp_queries import (
    MAX_LIMIT,
    get_entities_impl,
    search_events_impl,
    search_knowledge_lexical,
    shape_semantic_results,
)

logger = setup_logging("hippo-mcp")
metrics = MetricsCollector()


def _load_config() -> dict:
    """Load Hippo config from ~/.config/hippo/config.toml.

    Returns a dict with db_path, data_dir, lmstudio_base_url, embedding_model.
    """
    config_path = Path.home() / ".config" / "hippo" / "config.toml"
    defaults = {
        "db_path": str(Path.home() / ".local" / "share" / "hippo" / "hippo.db"),
        "data_dir": str(Path.home() / ".local" / "share" / "hippo"),
        "lmstudio_base_url": "http://localhost:1234/v1",
        "embedding_model": "",
    }

    if not config_path.exists():
        logger.warning("Config not found at %s, using defaults", config_path)
        return defaults

    with config_path.open("rb") as f:
        config = tomllib.load(f)

    storage = config.get("storage", {})
    data_dir = Path(
        storage.get("data_dir", Path.home() / ".local" / "share" / "hippo")
    ).expanduser()

    lmstudio = config.get("lmstudio", {})
    models = config.get("models", {})

    return {
        "db_path": str(data_dir / "hippo.db"),
        "data_dir": str(data_dir),
        "lmstudio_base_url": lmstudio.get("base_url", "http://localhost:1234/v1"),
        "embedding_model": models.get("embedding", ""),
    }


@dataclass
class _ServerState:
    """Holds initialized resources for the MCP server."""

    db_path: str = ""
    lm_client: LMStudioClient | None = None
    embedding_model: str = ""
    vector_table: object | None = None  # lancedb.table.Table


_state = _ServerState()


def _clamp_limit(limit: int) -> int:
    """Keep tool limits within the supported inclusive range."""
    return max(0, min(limit, MAX_LIMIT))


def _get_conn(db_path: str = "") -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and busy timeout."""
    path = db_path or _state.db_path
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_state() -> None:
    """Load config and initialize LM client and vector table (called once at startup)."""
    config = _load_config()
    _state.db_path = config["db_path"]
    _state.embedding_model = config["embedding_model"]
    _state.lm_client = LMStudioClient(base_url=config["lmstudio_base_url"])

    try:
        db = open_vector_db(config["data_dir"])
        _state.vector_table = get_or_create_table(db)
        logger.info("Vector table initialized at %s/vectors", config["data_dir"])
    except Exception:
        logger.exception("Failed to initialize vector table — semantic search unavailable")
        _state.vector_table = None

    logger.info(
        "Hippo MCP server initialized (db=%s, embedding_model=%s)",
        _state.db_path,
        _state.embedding_model or "<none>",
    )


mcp = FastMCP(
    "hippo",
    instructions=(
        "Hippo is a local knowledge base capturing shell activity, Claude sessions, "
        "and browser history. Use search_knowledge for semantic or lexical search over "
        "enriched knowledge nodes. Use search_events for raw event history. "
        "Use get_entities to explore the knowledge graph."
    ),
)


@mcp.tool()
async def search_knowledge(
    query: str,
    mode: str = "semantic",
    limit: int = 10,
) -> list[dict]:
    """Search the Hippo knowledge base for enriched knowledge nodes.

    Args:
        query: Search query text.
        mode: "semantic" (vector similarity via LM Studio) or "lexical" (LIKE match).
              Defaults to "semantic"; falls back to lexical on embedding failure.
        limit: Maximum number of results to return (default 10).
    """
    limit = _clamp_limit(limit)
    metrics.tool_calls += 1
    t0 = time.monotonic()
    logger.info("search_knowledge called: query=%r mode=%s limit=%d", query, mode, limit)

    try:
        if mode == "semantic" and _state.lm_client and _state.vector_table:
            metrics.semantic_searches += 1
            try:
                vecs = await _state.lm_client.embed([query], model=_state.embedding_model)
                query_vec = _pad_or_truncate(vecs[0], EMBED_DIM)
                hits = search_similar(_state.vector_table, query_vec, limit=limit)
                results = shape_semantic_results(hits)
                elapsed = time.monotonic() - t0
                logger.info(
                    "search_knowledge completed: %d results in %.3fs (semantic)",
                    len(results),
                    elapsed,
                )
                return results
            except Exception:
                logger.exception("Semantic search failed, falling back to lexical")
                metrics.lexical_fallbacks += 1
                metrics.lmstudio_errors += 1

        # Lexical search (explicit mode or fallback)
        metrics.lexical_searches += 1
        conn = _get_conn()
        try:
            results = search_knowledge_lexical(conn, query, limit=limit)
        finally:
            conn.close()

        elapsed = time.monotonic() - t0
        logger.info(
            "search_knowledge completed: %d results in %.3fs (lexical)",
            len(results),
            elapsed,
        )
        return results

    except Exception:
        metrics.tool_errors += 1
        logger.exception("search_knowledge failed")
        raise


@mcp.tool()
async def search_events(
    query: str = "",
    source: str = "all",
    since: str = "",
    project: str = "",
    limit: int = 20,
) -> list[dict]:
    """Search raw events across shell commands, Claude sessions, and browser history.

    Args:
        query: Text to search for in event content.
        source: Filter by source: "shell", "claude", "browser", or "all" (default).
        since: Time window like "24h", "7d", "30m". Empty means no time filter.
        project: Filter by project directory (substring match on cwd).
        limit: Maximum number of results (default 20).
    """
    limit = _clamp_limit(limit)
    metrics.tool_calls += 1
    t0 = time.monotonic()
    logger.info(
        "search_events called: query=%r source=%s since=%r project=%r limit=%d",
        query,
        source,
        since,
        project,
        limit,
    )

    try:
        conn = _get_conn()
        try:
            results = search_events_impl(
                conn, query=query, source=source, since=since, project=project, limit=limit
            )
        finally:
            conn.close()

        elapsed = time.monotonic() - t0
        metrics.events_searched += len(results)
        logger.info("search_events completed: %d results in %.3fs", len(results), elapsed)
        return results

    except Exception:
        metrics.tool_errors += 1
        logger.exception("search_events failed")
        raise


@mcp.tool()
async def get_entities(
    type: str = "",
    query: str = "",
    limit: int = 50,
) -> list[dict]:
    """List entities from the Hippo knowledge graph.

    Args:
        type: Filter by entity type: "project", "tool", "file", "domain", "concept", "service".
              Empty means all types.
        query: Filter entities whose name matches this substring.
        limit: Maximum number of results (default 50).
    """
    limit = _clamp_limit(limit)
    metrics.tool_calls += 1
    t0 = time.monotonic()
    logger.info("get_entities called: type=%r query=%r limit=%d", type, query, limit)

    try:
        conn = _get_conn()
        try:
            results = get_entities_impl(conn, entity_type=type, query=query, limit=limit)
        finally:
            conn.close()

        metrics.entities_returned += len(results)
        elapsed = time.monotonic() - t0
        logger.info("get_entities completed: %d results in %.3fs", len(results), elapsed)
        return results

    except Exception:
        metrics.tool_errors += 1
        logger.exception("get_entities failed")
        raise


def main() -> None:
    """Entry point for the hippo-mcp script."""
    _init_state()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
