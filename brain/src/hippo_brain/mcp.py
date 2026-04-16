"""Hippo MCP Server — expose the knowledge base as tools for Claude Code."""

import dataclasses
import sqlite3
import time
import tomllib
from contextlib import nullcontext
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
from hippo_brain.mcp_logging import setup_logging
from hippo_brain.telemetry import add as _add, get_meter, hist as _hist
from hippo_brain.mcp_queries import (
    MAX_LIMIT,
    get_ci_status_impl,
    get_entities_impl,
    get_lessons_impl,
    search_events_impl,
    search_knowledge_lexical,
    shape_semantic_results,
)
from hippo_brain.rag import ask as rag_ask, format_rag_response
from hippo_brain.telemetry import get_tracer as _get_tracer

logger = setup_logging("hippo-mcp")

_meter = get_meter()

_tool_calls = (
    _meter.create_counter("hippo.brain.mcp.tool_calls", description="MCP tool invocations")
    if _meter
    else None
)
_tool_errors = (
    _meter.create_counter("hippo.brain.mcp.tool_errors", description="MCP tool failures")
    if _meter
    else None
)
_tool_duration = (
    _meter.create_histogram(
        "hippo.brain.mcp.tool_duration", description="MCP tool latency", unit="ms"
    )
    if _meter
    else None
)


def _load_config() -> dict:
    """Load Hippo config from ~/.config/hippo/config.toml.

    Returns a dict with db_path, data_dir, lmstudio_base_url, embedding_model, query_model.
    """
    config_path = Path.home() / ".config" / "hippo" / "config.toml"
    defaults = {
        "db_path": str(Path.home() / ".local" / "share" / "hippo" / "hippo.db"),
        "data_dir": str(Path.home() / ".local" / "share" / "hippo"),
        "lmstudio_base_url": "http://localhost:1234/v1",
        "embedding_model": "",
        "query_model": "",
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
        "query_model": models.get("query", "") or models.get("enrichment", ""),
    }


@dataclass
class _ServerState:
    """Holds initialized resources for the MCP server."""

    db_path: str = ""
    lm_client: LMStudioClient | None = None
    embedding_model: str = ""
    query_model: str = ""
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
    _state.query_model = config["query_model"]
    _state.lm_client = LMStudioClient(base_url=config["lmstudio_base_url"])

    try:
        db = open_vector_db(config["data_dir"])
        _state.vector_table = get_or_create_table(db)
        logger.info("Vector table initialized at %s/vectors", config["data_dir"])
    except Exception:
        logger.exception("Failed to initialize vector table — semantic search unavailable")
        _state.vector_table = None

    logger.info(
        "Hippo MCP server initialized (db=%s, embedding_model=%s, query_model=%s)",
        _state.db_path,
        _state.embedding_model or "<none>",
        _state.query_model or "<none>",
    )


mcp = FastMCP(
    "hippo",
    instructions=(
        "Hippo is a local knowledge base capturing shell activity, Claude sessions, "
        "and browser history. Use ask to get synthesized answers about past activity. "
        "Use search_knowledge for raw semantic or lexical search over knowledge nodes. "
        "Use search_events for raw event history. "
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
    _add(_tool_calls, tool="search_knowledge")
    t0 = time.monotonic()
    logger.info("search_knowledge called: query=%r mode=%s limit=%d", query, mode, limit)

    tracer = _get_tracer()
    span_ctx = (
        tracer.start_as_current_span(
            "mcp.search_knowledge",
            attributes={"hippo.query": query, "hippo.mode": mode},
        )
        if tracer
        else nullcontext()
    )
    with span_ctx:
        try:
            if mode == "semantic" and _state.lm_client and _state.vector_table:
                try:
                    vecs = await _state.lm_client.embed([query], model=_state.embedding_model)
                    query_vec = _pad_or_truncate(vecs[0], EMBED_DIM)
                    hits = search_similar(_state.vector_table, query_vec, limit=limit)
                    results = shape_semantic_results(hits)
                    elapsed = time.monotonic() - t0
                    _hist(_tool_duration, elapsed * 1000, tool="search_knowledge")
                    logger.info(
                        "search_knowledge completed: %d results in %.3fs (semantic)",
                        len(results),
                        elapsed,
                    )
                    return results
                except Exception:
                    logger.exception("Semantic search failed, falling back to lexical")

            # Lexical search (explicit mode or fallback)
            conn = _get_conn()
            try:
                results = search_knowledge_lexical(conn, query, limit=limit)
            finally:
                conn.close()

            elapsed = time.monotonic() - t0
            _hist(_tool_duration, elapsed * 1000, tool="search_knowledge")
            logger.info(
                "search_knowledge completed: %d results in %.3fs (lexical)",
                len(results),
                elapsed,
            )
            return results

        except Exception:
            _add(_tool_errors, tool="search_knowledge")
            logger.exception("search_knowledge failed")
            raise


@mcp.tool()
async def ask(question: str, limit: int = 10) -> str:
    """Ask a question and get a synthesized answer from your knowledge base.

    Uses semantic search to find relevant knowledge nodes, then synthesizes
    a conversational answer using a local LLM. Returns the answer along
    with source references.

    Use this tool when you need to understand past activity, recall specific
    commands or decisions, or answer questions about work history.

    Args:
        question: The natural language question to answer.
        limit: Number of knowledge nodes to retrieve for context (default 10).
    """
    limit = _clamp_limit(limit)
    _add(_tool_calls, tool="ask")
    t0 = time.monotonic()
    logger.info("ask called: question=%r limit=%d", question, limit)

    if not _state.lm_client or not _state.vector_table:
        return "Error: Semantic search not available (LM Studio or vector store not initialized)"

    if not _state.query_model:
        return "Error: No query model configured (set models.query in config.toml)"

    try:
        result = await rag_ask(
            question=question,
            lm_client=_state.lm_client,
            vector_table=_state.vector_table,
            query_model=_state.query_model,
            embedding_model=_state.embedding_model,
            limit=limit,
        )

        elapsed = time.monotonic() - t0
        _hist(_tool_duration, elapsed * 1000, tool="ask")
        logger.info("ask completed in %.3fs", elapsed)

        return format_rag_response(result)

    except Exception:
        _add(_tool_errors, tool="ask")
        logger.exception("ask failed")
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
    _add(_tool_calls, tool="search_events")
    t0 = time.monotonic()
    logger.info(
        "search_events called: query=%r source=%s since=%r project=%r limit=%d",
        query,
        source,
        since,
        project,
        limit,
    )

    tracer = _get_tracer()
    span_ctx = (
        tracer.start_as_current_span(
            "mcp.search_events",
            attributes={"hippo.query": query, "hippo.mode": source},
        )
        if tracer
        else nullcontext()
    )
    with span_ctx:
        try:
            conn = _get_conn()
            try:
                results = search_events_impl(
                    conn, query=query, source=source, since=since, project=project, limit=limit
                )
            finally:
                conn.close()

            elapsed = time.monotonic() - t0
            _hist(_tool_duration, elapsed * 1000, tool="search_events")
            logger.info("search_events completed: %d results in %.3fs", len(results), elapsed)
            return results

        except Exception:
            _add(_tool_errors, tool="search_events")
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
    _add(_tool_calls, tool="get_entities")
    t0 = time.monotonic()
    logger.info("get_entities called: type=%r query=%r limit=%d", type, query, limit)

    tracer = _get_tracer()
    span_ctx = (
        tracer.start_as_current_span(
            "mcp.get_entities",
            attributes={"hippo.query": query, "hippo.mode": type},
        )
        if tracer
        else nullcontext()
    )
    with span_ctx:
        try:
            conn = _get_conn()
            try:
                results = get_entities_impl(conn, entity_type=type, query=query, limit=limit)
            finally:
                conn.close()

            elapsed = time.monotonic() - t0
            _hist(_tool_duration, elapsed * 1000, tool="get_entities")
            logger.info("get_entities completed: %d results in %.3fs", len(results), elapsed)
            return results

        except Exception:
            _add(_tool_errors, tool="get_entities")
            logger.exception("get_entities failed")
            raise


@mcp.tool()
async def get_ci_status(
    repo: str,
    sha: str | None = None,
    branch: str | None = None,
) -> dict:
    """Return the most recent CI workflow run for a repo, filtered by SHA or branch.

    Use this after a 'git push' to check whether CI passed. Returns structured
    job and annotation data — prefer over `ask` for known-shape queries.

    Args:
        repo: Repository in 'owner/repo' format.
        sha: Git commit SHA to look up.
        branch: Branch name (used when sha is not provided).
    """
    _add(_tool_calls, tool="get_ci_status")
    t0 = time.monotonic()
    logger.info("get_ci_status called: repo=%r sha=%r branch=%r", repo, sha, branch)

    tracer = _get_tracer()
    span_ctx = (
        tracer.start_as_current_span(
            "mcp.get_ci_status",
            attributes={"hippo.repo": repo},
        )
        if tracer
        else nullcontext()
    )
    with span_ctx:
        try:
            status = get_ci_status_impl(_state.db_path, repo=repo, sha=sha, branch=branch)
            result = dataclasses.asdict(status) if status else {}
            elapsed = time.monotonic() - t0
            _hist(_tool_duration, elapsed * 1000, tool="get_ci_status")
            logger.info("get_ci_status completed: found=%s in %.3fs", status is not None, elapsed)
            return result
        except Exception:
            _add(_tool_errors, tool="get_ci_status")
            logger.exception("get_ci_status failed")
            raise


@mcp.tool()
async def get_lessons(
    repo: str | None = None,
    path: str | None = None,
    tool: str | None = None,
    limit: int = 10,
) -> list[dict]:
    """Return distilled past-mistake lessons for the given filters.

    Use pre-flight before editing code in a known failure-prone area. Lessons
    only appear for patterns seen 2+ times (single failures do not graduate).

    Args:
        repo: Filter by repository in 'owner/repo' format.
        path: Filter by file path — returns lessons whose stored path_prefix matches as a prefix of this path.
        tool: Filter by tool name (e.g. 'ruff', 'clippy').
        limit: Maximum number of results to return (default 10).
    """
    limit = _clamp_limit(limit)
    _add(_tool_calls, tool="get_lessons")
    t0 = time.monotonic()
    logger.info("get_lessons called: repo=%r path=%r tool=%r limit=%d", repo, path, tool, limit)

    tracer = _get_tracer()
    span_ctx = (
        tracer.start_as_current_span(
            "mcp.get_lessons",
            attributes={"hippo.repo": repo or ""},
        )
        if tracer
        else nullcontext()
    )
    with span_ctx:
        try:
            lessons = get_lessons_impl(_state.db_path, repo=repo, path=path, tool=tool, limit=limit)
            result = [dataclasses.asdict(lesson) for lesson in lessons]
            elapsed = time.monotonic() - t0
            _hist(_tool_duration, elapsed * 1000, tool="get_lessons")
            logger.info("get_lessons completed: %d results in %.3fs", len(result), elapsed)
            return result
        except Exception:
            _add(_tool_errors, tool="get_lessons")
            logger.exception("get_lessons failed")
            raise


def main() -> None:
    """Entry point for the hippo-mcp script."""
    _init_state()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
