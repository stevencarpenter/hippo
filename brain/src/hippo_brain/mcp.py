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
    format_context_block,
    get_ci_status_impl,
    get_entities_impl,
    get_lessons_impl,
    list_projects_impl,
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
    project: str = "",
    since: str = "",
    source: str = "",
    branch: str = "",
) -> list[dict]:
    """Search the Hippo knowledge base for enriched knowledge nodes.

    Args:
        query: Search query text.
        mode: "semantic" (vector similarity via LM Studio) or "lexical" (LIKE match).
              Defaults to "semantic"; falls back to lexical on embedding failure.
        limit: Maximum number of results to return (default 10).
        project: Substring match on cwd or git_repo of linked events/sessions.
        since: Window like "24h", "7d", "30m". Empty means no time filter.
        source: Restrict to nodes linked to a specific source: "shell",
                "claude", "browser", or "workflow". Empty means all sources.
        branch: Exact match on git_branch of linked events/sessions.
    """
    limit = _clamp_limit(limit)
    _add(_tool_calls, tool="search_knowledge")
    t0 = time.monotonic()
    logger.info(
        "search_knowledge called: query=%r mode=%s limit=%d project=%r since=%r "
        "source=%r branch=%r",
        query,
        mode,
        limit,
        project,
        since,
        source,
        branch,
    )

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
            if (
                mode == "semantic"
                and _state.lm_client
                and _state.vector_table
                and not (project or since or source or branch)
            ):
                try:
                    vecs = await _state.lm_client.embed([query], model=_state.embedding_model)
                    query_vec = _pad_or_truncate(vecs[0], EMBED_DIM)
                    hits = search_similar(_state.vector_table, query_vec, limit=limit)
                    conn = _get_conn()
                    try:
                        results = shape_semantic_results(hits, conn=conn)
                    finally:
                        conn.close()
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

            # Lexical search (explicit mode or fallback, or when filters are applied)
            conn = _get_conn()
            try:
                results = search_knowledge_lexical(
                    conn,
                    query,
                    limit=limit,
                    project=project,
                    since=since,
                    source=source,
                    branch=branch,
                )
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
async def ask(
    question: str,
    limit: int = 10,
    project: str = "",
    since: str = "",
    source: str = "",
    branch: str = "",
) -> str:
    """Ask a question and get a synthesized answer from your knowledge base.

    Uses semantic search to find relevant knowledge nodes, then synthesizes
    a conversational answer using a local LLM. Returns the answer along
    with source references.

    Use this tool when you need to understand past activity, recall specific
    commands or decisions, or answer questions about work history.

    Args:
        question: The natural language question to answer.
        limit: Number of knowledge nodes to retrieve for context (default 10).
        project: Substring match on cwd/git_repo to narrow scope. Empty = all.
        since: Window like "24h", "7d". Empty means no time filter.
        source: Restrict to nodes linked to "shell", "claude", "browser",
                or "workflow". Empty means all sources.
        branch: Exact match on git_branch of linked events/sessions.
    """
    limit = _clamp_limit(limit)
    _add(_tool_calls, tool="ask")
    t0 = time.monotonic()
    logger.info(
        "ask called: question=%r limit=%d project=%r since=%r source=%r branch=%r",
        question,
        limit,
        project,
        since,
        source,
        branch,
    )
    since_ms = _parse_since_ms(since) if since else None

    if not _state.lm_client or not _state.vector_table:
        return "Error: Semantic search not available (LM Studio or vector store not initialized)"

    if not _state.query_model:
        return "Error: No query model configured (set models.query in config.toml)"

    conn = _open_retrieval_conn()
    try:
        result = await rag_ask(
            question=question,
            lm_client=_state.lm_client,
            vector_table=_state.vector_table,
            query_model=_state.query_model,
            embedding_model=_state.embedding_model,
            limit=limit,
            project=project or None,
            since=since_ms,
            source=source or None,
            branch=branch or None,
            conn=conn,
        )
    except Exception:
        _add(_tool_errors, tool="ask")
        logger.exception("ask failed")
        raise
    finally:
        conn.close()

    elapsed = time.monotonic() - t0
    _hist(_tool_duration, elapsed * 1000, tool="ask")
    logger.info("ask completed in %.3fs", elapsed)

    return format_rag_response(result)


@mcp.tool()
async def search_events(
    query: str = "",
    source: str = "all",
    since: str = "",
    project: str = "",
    branch: str = "",
    limit: int = 20,
) -> list[dict]:
    """Search raw events across shell commands, Claude sessions, and browser history.

    Args:
        query: Text to search for in event content.
        source: Filter by source: "shell", "claude", "browser", or "all" (default).
        since: Time window like "24h", "7d", "30m". Empty means no time filter.
        project: Filter by project directory (substring match on cwd).
        branch: Exact-match git_branch filter (ignored for browser events).
        limit: Maximum number of results (default 20).
    """
    limit = _clamp_limit(limit)
    _add(_tool_calls, tool="search_events")
    t0 = time.monotonic()
    logger.info(
        "search_events called: query=%r source=%s since=%r project=%r branch=%r limit=%d",
        query,
        source,
        since,
        project,
        branch,
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
                    conn,
                    query=query,
                    source=source,
                    since=since,
                    project=project,
                    branch=branch,
                    limit=limit,
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
    project: str = "",
    since: str = "",
) -> list[dict]:
    """List entities from the Hippo knowledge graph.

    Args:
        type: Filter by entity type: "project", "tool", "file", "domain", "concept", "service".
              Empty means all types.
        query: Filter entities whose name matches this substring.
        limit: Maximum number of results (default 50).
        project: Substring match on cwd/git_repo of co-occurring knowledge nodes.
        since: Window like "24h", "7d". Filters by entities.last_seen.
    """
    limit = _clamp_limit(limit)
    _add(_tool_calls, tool="get_entities")
    t0 = time.monotonic()
    logger.info(
        "get_entities called: type=%r query=%r limit=%d project=%r since=%r",
        type,
        query,
        limit,
        project,
        since,
    )

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
                results = get_entities_impl(
                    conn,
                    entity_type=type,
                    query=query,
                    limit=limit,
                    project=project,
                    since=since,
                )
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


def _result_to_dict(result) -> dict:
    """Convert a retrieval.SearchResult to the public MCP dict shape."""
    return {
        "uuid": result.uuid,
        "score": result.score,
        "summary": result.summary,
        "embed_text": result.embed_text,
        "outcome": result.outcome,
        "tags": list(result.tags),
        "cwd": result.cwd,
        "git_branch": result.git_branch,
        "captured_at": result.captured_at,
        "linked_event_ids": list(result.linked_event_ids),
    }


async def _retrieve_filtered(
    *,
    query: str,
    mode: str,
    limit: int,
    project: str,
    since: str,
    source: str,
    branch: str,
    entity: str = "",
) -> list[dict]:
    """Run a filtered retrieval, returning SearchResult-shaped dicts.

    Delegates to :func:`hippo_brain.retrieval.search` when sqlite-vec is
    available. Falls back to the SQL-only ``search_knowledge_lexical`` path
    if vec0/FTS5 are not loadable (e.g. older DB or test fixtures).
    """
    from hippo_brain import retrieval as _retrieval

    since_ms = _parse_since_ms(since)
    filters = _retrieval.Filters(
        project=project or None,
        since_ms=since_ms or None,
        source=source or None,
        branch=branch or None,
        entity=entity or None,
    )

    query_vec = None
    if mode in ("hybrid", "semantic") and _state.lm_client and query:
        try:
            vecs = await _state.lm_client.embed([query], model=_state.embedding_model)
            query_vec = _pad_or_truncate(vecs[0], EMBED_DIM)
        except Exception:
            logger.exception("query embedding failed in _retrieve_filtered")

    conn = _open_retrieval_conn()
    try:
        try:
            results = _retrieval.search(conn, query, query_vec, filters, mode=mode, limit=limit)
            return [_result_to_dict(r) for r in results]
        except Exception:
            logger.exception("retrieval.search failed; falling back to lexical SQL")
            return search_knowledge_lexical(
                conn,
                query,
                limit=limit,
                project=project,
                since=since,
                source=source,
                branch=branch,
            )
    finally:
        conn.close()


def _parse_since_ms(since: str) -> int:
    """Reuse the parse_since helper without importing it for one call."""
    from hippo_brain.mcp_queries import parse_since

    return parse_since(since)


def _open_retrieval_conn() -> sqlite3.Connection:
    """Open a sqlite3 connection with sqlite-vec loaded when available."""
    try:
        from hippo_brain.vector_store import open_conn

        return open_conn(_state.db_path)
    except Exception:
        # vector_store may be missing in older deploys; fall back to plain conn
        return _get_conn()


@mcp.tool()
async def search_hybrid(
    query: str,
    mode: str = "hybrid",
    limit: int = 10,
    project: str = "",
    since: str = "",
    source: str = "",
    branch: str = "",
    entity: str = "",
) -> list[dict]:
    """Hybrid retrieval over the knowledge base — no synthesis.

    Returns SearchResult-shaped dicts (uuid, score, summary, embed_text,
    outcome, tags, cwd, git_branch, captured_at, linked_event_ids,
    linked_claude_session_ids, linked_browser_event_ids).

    Args:
        query: Natural language query.
        mode: "hybrid" (default), "semantic", "lexical", or "recent".
        limit: Maximum number of results (default 10).
        project: Substring match on cwd/git_repo of linked events.
        since: Window like "24h", "7d". Empty means no time filter.
        source: "shell" | "claude" | "browser" | "workflow" | "" (all).
        branch: Exact-match git_branch filter.
        entity: Canonical entity name to require among linked entities.
    """
    limit = _clamp_limit(limit)
    _add(_tool_calls, tool="search_hybrid")
    t0 = time.monotonic()
    logger.info(
        "search_hybrid called: query=%r mode=%s limit=%d filters=%r",
        query,
        mode,
        limit,
        {
            "project": project,
            "since": since,
            "source": source,
            "branch": branch,
            "entity": entity,
        },
    )

    try:
        results = await _retrieve_filtered(
            query=query,
            mode=mode,
            limit=limit,
            project=project,
            since=since,
            source=source,
            branch=branch,
            entity=entity,
        )
        elapsed = time.monotonic() - t0
        _hist(_tool_duration, elapsed * 1000, tool="search_hybrid")
        logger.info("search_hybrid completed: %d results in %.3fs", len(results), elapsed)
        return results
    except Exception:
        _add(_tool_errors, tool="search_hybrid")
        logger.exception("search_hybrid failed")
        raise


@mcp.tool()
async def get_context(
    query: str,
    limit: int = 5,
    project: str = "",
    since: str = "",
    source: str = "",
) -> str:
    """Return a Markdown context block ready to paste into an agent prompt.

    Performs a hybrid retrieval and renders the top hits as a numbered list
    with summary, outcome, cwd, branch, captured-at timestamp, and uuid.
    Embed text is truncated per-hit to keep the block prompt-friendly.

    Args:
        query: Natural language query.
        limit: Maximum number of sources to embed (default 5).
        project: Substring match on cwd/git_repo of linked events.
        since: Window like "24h", "7d". Empty means no time filter.
        source: "shell" | "claude" | "browser" | "workflow" | "" (all).
    """
    limit = _clamp_limit(limit)
    _add(_tool_calls, tool="get_context")
    t0 = time.monotonic()
    logger.info(
        "get_context called: query=%r limit=%d project=%r since=%r source=%r",
        query,
        limit,
        project,
        since,
        source,
    )

    try:
        results = await _retrieve_filtered(
            query=query,
            mode="hybrid",
            limit=limit,
            project=project,
            since=since,
            source=source,
            branch="",
        )
        block = format_context_block(query, results)
        elapsed = time.monotonic() - t0
        _hist(_tool_duration, elapsed * 1000, tool="get_context")
        logger.info("get_context completed: %d sources in %.3fs", len(results), elapsed)
        return block
    except Exception:
        _add(_tool_errors, tool="get_context")
        logger.exception("get_context failed")
        raise


@mcp.tool()
async def list_projects(limit: int = 50) -> list[dict]:
    """Return distinct projects (git_repo + cwd_root) seen in the knowledge base.

    Ordered by most recent activity first. Use this for discovery before
    filtering other tools by ``project``.

    Args:
        limit: Maximum number of projects to return (default 50).
    """
    limit = _clamp_limit(limit)
    _add(_tool_calls, tool="list_projects")
    t0 = time.monotonic()
    logger.info("list_projects called: limit=%d", limit)

    try:
        conn = _get_conn()
        try:
            results = list_projects_impl(conn, limit=limit)
        finally:
            conn.close()
        elapsed = time.monotonic() - t0
        _hist(_tool_duration, elapsed * 1000, tool="list_projects")
        logger.info("list_projects completed: %d results in %.3fs", len(results), elapsed)
        return results
    except Exception:
        _add(_tool_errors, tool="list_projects")
        logger.exception("list_projects failed")
        raise


def main() -> None:
    """Entry point for the hippo-mcp script."""
    _init_state()
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
