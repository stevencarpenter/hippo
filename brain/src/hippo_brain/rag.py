"""RAG (retrieval-augmented generation) pipeline for Hippo knowledge queries."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from datetime import datetime, timezone

from hippo_brain.embeddings import EMBED_DIM, _pad_or_truncate, search_similar
from hippo_brain.enrichment import IDENTIFIER_ENTITY_TYPES
from hippo_brain.retrieval import Filters, SearchResult
from hippo_brain.retrieval import search as retrieval_search
from hippo_brain.telemetry import get_meter

_meter = get_meter()
_rag_duration = (
    _meter.create_histogram("hippo.brain.rag.duration", description="RAG stage latency", unit="ms")
    if _meter
    else None
)
_rag_hits = (
    _meter.create_histogram(
        "hippo.brain.rag.retrieval_hits", description="Vector search result count"
    )
    if _meter
    else None
)
_rag_degraded = (
    _meter.create_counter(
        "hippo.brain.rag.degraded", description="Count of degraded ask() responses"
    )
    if _meter
    else None
)

logger = logging.getLogger("hippo_brain.rag")

DEFAULT_MAX_CONTEXT_CHARS = 12000
DEFAULT_SOURCES_LIMIT = 10
_MIN_PER_HIT_FIELD_CHARS = 80
_ENTITIES_LINE_CAP = 500

_SYSTEM_PROMPT = (
    "You are a personal knowledge assistant. The user is asking about their own past "
    "activity — commands they ran, decisions they made, problems they solved.\n\n"
    "Answer the user's question using ONLY the context provided below. Be specific: "
    "reference actual commands, file paths, error messages, and details from the "
    "context. If the context doesn't contain enough information to answer fully, "
    "say what you can and note what's missing.\n\n"
    "VERBATIM PRESERVATION: When the context contains specific identifiers — function "
    "or symbol names, environment variable names (UPPERCASE_WITH_UNDERSCORES), version "
    "strings (\\d+\\.\\d+\\.\\d+), package@version pairs, file paths, CLI flag names, "
    "exact integer counts, or error codes — reproduce them EXACTLY in your answer. Do "
    "not paraphrase, normalize, or substitute plausible-sounding variants. If the user "
    "asks 'what version is X?' or 'what's the name of Y?', answer with the literal "
    "token from the context. If the specific token is not in the context, say so "
    "explicitly — never guess. A hallucinated identifier (e.g. HIPPO_FORCE_INSTALL "
    "when the real value is HIPPO_FORCE) is worse than admitting the value is "
    "unknown.\n\n"
    "Keep your answer concise and direct — a few short paragraphs at most. "
    "Use markdown formatting: headers for sections, backticks for commands and paths, "
    "code blocks for multi-line commands.\n\n"
    "Do not make up information. Do not hallucinate commands or paths."
)


def _format_timestamp(ts_ms: int) -> str:
    """Format epoch-ms timestamp as YYYY-MM-DD."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len, adding ellipsis if needed."""
    if max_len <= 0:
        return ""
    if len(text) <= max_len:
        return text
    return text[: max(max_len - 1, 1)] + "…"


def _render_entities_line(entities: dict | None) -> str | None:
    """Render structured entities as a flat comma-separated line.

    Returns None when there is nothing to surface — caller omits the line
    rather than emitting a bare "Entities:" prefix. Capped at
    _ENTITIES_LINE_CAP so identifier-rich hits don't crowd embed_text out
    of the structural budget.
    """
    if not isinstance(entities, dict):
        return None
    seen: set[str] = set()
    tokens: list[str] = []
    for etype in IDENTIFIER_ENTITY_TYPES:
        for name in entities.get(etype) or []:
            if not isinstance(name, str) or not name:
                continue
            if name in seen:
                continue
            seen.add(name)
            tokens.append(name)
    if not tokens:
        return None
    return f"Entities: {_truncate(', '.join(tokens), _ENTITIES_LINE_CAP)}"


def _shape_rag_sources(
    hits: list[dict], min_score: float = 0.0, limit: int = DEFAULT_SOURCES_LIMIT
) -> list[dict]:
    """Transform raw retrieval hits into the response source shape.

    Filters out sources below ``min_score`` and caps at ``limit`` results.
    """
    sources = []
    for hit in hits:
        score = round(1.0 - hit.get("_distance", 1.0), 4)
        if score < min_score:
            continue
        sources.append(
            {
                "score": score,
                "summary": _truncate(hit.get("summary", ""), 120),
                "cwd": hit.get("cwd", ""),
                "git_branch": hit.get("git_branch", ""),
                "timestamp": hit.get("captured_at", 0),
                "commands_raw": hit.get("commands_raw", ""),
                "uuid": hit.get("uuid", ""),
                "linked_event_ids": list(hit.get("linked_event_ids", []) or []),
            }
        )
    return sources[:limit]


def _hit_lines(
    index: int,
    hit: dict,
    embed_cap: int,
    cmd_cap: int,
    design_cap: int,
) -> list[str]:
    """Render a single retrieval hit as a list of context lines."""
    score = round(1.0 - hit.get("_distance", 1.0), 4)
    ts = hit.get("captured_at", 0)
    date_str = _format_timestamp(ts) if ts else "unknown"

    lines = [f"[{index}] (score: {score}, {date_str})"]
    if hit.get("summary"):
        lines.append(f"Summary: {hit['summary']}")
    entities_line = _render_entities_line(hit.get("entities"))
    if entities_line:
        lines.append(entities_line)
    if hit.get("embed_text"):
        lines.append(f"Detail: {_truncate(hit['embed_text'], embed_cap)}")
    # Render design_decisions verbatim — the "considered X, chose Y, reason Z"
    # structure is exactly what the synthesis LLM needs to answer questions
    # about why a particular approach was picked. (Issue #98 F3.)
    design_lines = _render_design_decision_lines(hit.get("design_decisions") or [], design_cap)
    if design_lines:
        lines.append("Design decisions:")
        lines.extend(design_lines)
    if hit.get("commands_raw"):
        lines.append(f"Commands: {_truncate(hit['commands_raw'], cmd_cap)}")
    if hit.get("cwd"):
        lines.append(f"CWD: {hit['cwd']}")
    if hit.get("git_branch"):
        lines.append(f"Branch: {hit['git_branch']}")
    if hit.get("outcome"):
        lines.append(f"Outcome: {hit['outcome']}")

    tags = hit.get("tags", "")
    if isinstance(tags, str) and tags:
        try:
            tags = json.loads(tags)
        except json.JSONDecodeError, TypeError:
            tags = []
    if tags and isinstance(tags, list):
        lines.append(f"Tags: {', '.join(tags)}")
    return lines


def _design_decision_payload(design_decisions: list[dict] | object) -> str:
    """Render valid design_decisions entries as plain text for budgeting."""
    if not isinstance(design_decisions, list):
        return ""
    rendered: list[str] = []
    for decision in design_decisions:
        if not isinstance(decision, dict):
            continue
        considered = decision.get("considered", "")
        chosen = decision.get("chosen", "")
        reason = decision.get("reason", "")
        if considered and chosen and reason:
            rendered.append(f"  - considered {considered!r}; chose {chosen!r}; reason: {reason}")
    return "\n".join(rendered)


def _render_design_decision_lines(
    design_decisions: list[dict] | object, max_chars: int
) -> list[str]:
    """Render design_decisions under a total character cap."""
    payload = _design_decision_payload(design_decisions)
    if not payload or max_chars <= 0:
        return []
    return _truncate(payload, max_chars).splitlines()


def _allocate_payload_caps(
    per_hit: int, *, embed_len: int, cmd_len: int, design_len: int
) -> tuple[int, int, int]:
    """Split one hit's payload budget across embed/cmd/design fields.

    Uses proportional floor division, distributes any remainder to the largest
    fields, and preserves at least one character for each non-empty field when
    the per-hit budget is large enough to do so.
    """
    lengths = {
        "embed": max(embed_len, 0),
        "cmd": max(cmd_len, 0),
        "design": max(design_len, 0),
    }
    total = sum(lengths.values())
    if total == 0:
        return 0, 0, 0

    caps = {name: (per_hit * length) // total if length else 0 for name, length in lengths.items()}
    remainder = per_hit - sum(caps.values())

    ranked = sorted(lengths, key=lambda name: lengths[name], reverse=True)
    for name in ranked:
        if remainder <= 0:
            break
        if lengths[name]:
            caps[name] += 1
            remainder -= 1

    non_empty = [name for name, length in lengths.items() if length]
    if per_hit >= len(non_empty):
        for name in non_empty:
            if caps[name] > 0:
                continue
            donor = next((candidate for candidate in ranked if caps[candidate] > 1), None)
            if donor is None:
                break
            caps[donor] -= 1
            caps[name] = 1

    return caps["embed"], caps["cmd"], caps["design"]


def _build_rag_prompt(
    question: str,
    hits: list[dict],
    max_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
) -> list[dict]:
    """Build chat messages for the synthesis LLM from question + retrieved hits.

    If the rendered context exceeds ``max_chars``, the long per-hit fields
    (``embed_text``, ``commands_raw``, and ``design_decisions``) are truncated
    proportionally so the final prompt fits the budget.
    """
    # First pass: render with generous caps.
    embed_cap = 2_000
    cmd_cap = 2_000
    blocks = [
        "\n".join(_hit_lines(i, h, embed_cap, cmd_cap, cmd_cap)) for i, h in enumerate(hits, 1)
    ]
    total = sum(len(b) for b in blocks) + max(0, (len(blocks) - 1)) * 2

    if total > max_chars and hits:
        # How much budget is consumed by structural text (headers, cwd, tags, etc)
        # that we don't want to truncate? Approximate by re-rendering with the
        # large payload fields stripped.
        structural = 0
        for i, h in enumerate(hits, 1):
            stripped = dict(h)
            stripped["embed_text"] = ""
            stripped["commands_raw"] = ""
            stripped["design_decisions"] = []
            structural += len("\n".join(_hit_lines(i, stripped, 0, 0, 0)))
        structural += max(0, (len(hits) - 1)) * 2

        remaining = max(max_chars - structural, _MIN_PER_HIT_FIELD_CHARS * len(hits))
        # Split remaining budget across hits, then split per-hit budget across
        # embed_text, commands_raw, and design_decisions proportionally to their
        # current rendered sizes.
        per_hit = max(remaining // max(len(hits), 1), _MIN_PER_HIT_FIELD_CHARS)

        blocks = []
        for i, h in enumerate(hits, 1):
            e_len = len(h.get("embed_text", "") or "")
            c_len = len(h.get("commands_raw", "") or "")
            d_len = len(_design_decision_payload(h.get("design_decisions") or []))
            total_payload = e_len + c_len + d_len
            if total_payload == 0:
                # All payload-heavy fields are suppressed in this branch;
                # structural fields (summary/cwd/tags/etc.) still render.
                e_cap = c_cap = d_cap = 0
            else:
                e_cap, c_cap, d_cap = _allocate_payload_caps(
                    per_hit,
                    embed_len=e_len,
                    cmd_len=c_len,
                    design_len=d_len,
                )
            blocks.append("\n".join(_hit_lines(i, h, e_cap, c_cap, d_cap)))

    context = "\n\n".join(blocks)
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"},
    ]


def format_rag_response(result: dict) -> str:
    """Format a RAG result dict as a human-readable string (for MCP tool output).

    Degraded responses render sources under a "Raw notes" header so the caller
    still gets useful material even when synthesis failed.
    """
    parts: list[str] = []
    degraded = bool(result.get("degraded"))
    err = result.get("error")
    answer = result.get("answer")

    if err and degraded:
        parts.append(f"(degraded: {err})")
    elif err:
        parts.append(f"Error: {err}")

    if answer:
        parts.append(answer)

    sources = result.get("sources", [])
    if sources:
        header = "Raw notes (synthesis unavailable):" if degraded else "Sources:"
        parts.append("")
        parts.append(header)
        for i, src in enumerate(sources, 1):
            score = src.get("score", 0)
            summary = src.get("summary", "")
            cwd = src.get("cwd", "")
            branch = src.get("git_branch", "")
            ts = src.get("timestamp", 0)
            date_str = _format_timestamp(ts) if ts else ""

            location_parts: list[str] = []
            if cwd:
                location_parts.append(cwd)
            if branch:
                if location_parts:
                    location_parts[-1] = f"{location_parts[-1]} ({branch})"
                else:
                    location_parts.append(f"({branch})")
            if date_str:
                location_parts.append(date_str)

            loc = " — ".join(location_parts) if location_parts else ""
            parts.append(f"  {i}. [{score:.0%}] {summary}")
            if loc:
                parts.append(f"     {loc}")
            if degraded:
                cmds = src.get("commands_raw", "")
                if cmds:
                    parts.append(f"     $ {_truncate(cmds, 200)}")

    return "\n".join(parts)


def _describe_exception(exc: BaseException, *, stage: str, model: str, endpoint: str) -> str:
    """Render an actionable error string with exception type, stage, model, endpoint."""
    cause_type = type(exc).__name__
    msg = str(exc).strip() or repr(exc)
    return f"{stage} failed [{cause_type}] model={model!r} endpoint={endpoint}: {msg}"


def _degraded_response(
    *,
    model: str,
    sources: list[dict],
    error: str,
    stage: str,
) -> dict:
    if _rag_degraded:
        _rag_degraded.add(1, {"stage": stage})
    return {
        "answer": None,
        "sources": sources,
        "error": error,
        "model": model,
        "degraded": True,
        "stage": stage,
    }


def _result_to_hit(r: SearchResult) -> dict:
    """Adapt a retrieval ``SearchResult`` into the legacy hit-dict shape.

    The existing prompt builder + source shaper use ``_distance``-style dicts
    (inherited from the LanceDB path). We reverse the score back to a synthetic
    distance so those helpers keep working without a branch.
    """
    return {
        "_distance": round(1.0 - max(0.0, min(1.0, r.score)), 4),
        "summary": r.summary,
        "embed_text": r.embed_text,
        "commands_raw": "",
        "cwd": r.cwd,
        "git_branch": r.git_branch,
        "captured_at": r.captured_at,
        "outcome": r.outcome or "",
        "tags": json.dumps(r.tags) if r.tags else "",
        "design_decisions": list(r.design_decisions),
        "uuid": r.uuid,
        "linked_event_ids": list(r.linked_event_ids),
        "entities": dict(r.entities),
    }


def _resolve_filters(
    filters: Filters | None,
    *,
    project: str | None,
    since: int | None,
    source: str | None,
    branch: str | None,
    entity: str | None,
) -> Filters | None:
    """Merge explicit ``filters`` with flat kwargs. Returns ``None`` if no filter is set."""
    if filters is not None:
        return filters
    if any(v is not None for v in (project, since, source, branch, entity)):
        return Filters(
            project=project,
            since_ms=since,
            source=source,
            branch=branch,
            entity=entity,
        )
    return None


async def ask(
    question: str,
    lm_client,
    vector_table,
    query_model: str,
    embedding_model: str,
    limit: int = 10,
    *,
    max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
    skip_preflight: bool = False,
    filters: Filters | None = None,
    project: str | None = None,
    since: int | None = None,
    source: str | None = None,
    branch: str | None = None,
    entity: str | None = None,
    mode: str = "hybrid",
    conn=None,
) -> dict:
    """Run the full RAG pipeline: preflight → embed → retrieve → synthesize.

    Returns a shaped dict. On the happy path:

        {"answer": str, "sources": [...], "model": str, "degraded": False}

    On failure at any stage, returns a *degraded* response — ``answer`` is
    ``None`` but ``sources`` is still populated where possible, and ``error``
    contains exception type, stage, model, and endpoint. ``format_rag_response``
    renders degraded responses as a "Raw notes" Markdown block so callers still
    get useful material.

    Args:
        max_context_chars: Cap on rendered retrieval context; long fields are
            truncated proportionally if the cap is exceeded. Default 8000.
        skip_preflight: Skip the LM Studio ``health_check`` (useful in tests
            where the client doesn't expose one).
        filters: Fully-formed :class:`retrieval.Filters` object, if the caller
            already has one.
        project / since / source / branch / entity: flat shortcut kwargs —
            if any are set (and ``filters`` is not), a ``Filters`` is built
            internally. Setting any filter routes retrieval through
            :func:`hippo_brain.retrieval.search` instead of the legacy
            ``search_similar`` path.
        mode: Retrieval mode (``"hybrid"`` / ``"semantic"`` / ``"lexical"`` /
            ``"recent"``). Only applied on the filtered path.
        conn: sqlite3 connection for the filtered path. Falls back to
            ``vector_table`` if not provided (for callers that pass their
            connection positionally).
    """
    endpoint = getattr(lm_client, "base_url", "<unknown>")

    # 0. Preflight: is the query model actually loaded?
    if not skip_preflight:
        health = None
        try:
            probe = getattr(lm_client, "health_check", None)
            if probe is not None:
                health = await probe(query_model)
        except Exception as e:
            logger.warning("RAG preflight raised: %s", e)
            health = {
                "ok": False,
                "reason": _describe_exception(
                    e, stage="preflight", model=query_model, endpoint=endpoint
                ),
            }
        if isinstance(health, dict) and health.get("ok") is False:
            reason = health.get("reason") or f"query model {query_model!r} not available"
            logger.error("RAG preflight failed: %s", reason)
            return _degraded_response(
                model=query_model,
                sources=[],
                error=f"preflight: {reason}",
                stage="preflight",
            )

    # 1. Embed the question.
    try:
        _t0 = time.monotonic()
        vecs = await lm_client.embed([question], model=embedding_model)
        if _rag_duration:
            _rag_duration.record((time.monotonic() - _t0) * 1000, {"stage": "embed"})
    except Exception as e:
        logger.error("RAG embed failed: %s", e)
        return _degraded_response(
            model=query_model,
            sources=[],
            error=_describe_exception(e, stage="embed", model=embedding_model, endpoint=endpoint),
            stage="embed",
        )

    if not vecs:
        return _degraded_response(
            model=query_model,
            sources=[],
            error=f"embed failed [EmptyResponse] model={embedding_model!r} endpoint={endpoint}: "
            "embedding endpoint returned no vectors",
            stage="embed",
        )

    query_vec = _pad_or_truncate(vecs[0], EMBED_DIM)

    # 2. Retrieve relevant knowledge nodes.
    #
    # Prefer ``retrieval.search`` (hybrid RRF + FTS5 + MMR diversification)
    # whenever we have a real sqlite3.Connection. This applies regardless of
    # whether filters are set, so vanilla ``ask`` calls also benefit from
    # MMR's lexical+temporal diversity instead of pure-semantic KNN. The
    # legacy ``search_similar`` path is kept for non-sqlite handles (e.g.
    # LanceDB tables on deploys without sqlite-vec).
    effective_filters = _resolve_filters(
        filters,
        project=project,
        since=since,
        source=source,
        branch=branch,
        entity=entity,
    )
    retrieval_conn = conn if conn is not None else vector_table
    use_retrieval_search = isinstance(retrieval_conn, sqlite3.Connection)
    try:
        _t1 = time.monotonic()
        if use_retrieval_search:
            results = retrieval_search(
                retrieval_conn,
                question,
                list(query_vec),
                filters=effective_filters,
                mode=mode,
                limit=limit,
            )
            hits = [_result_to_hit(r) for r in results]
        elif effective_filters is not None:
            return _degraded_response(
                model=query_model,
                sources=[],
                error=(
                    "retrieve failed [ConfigError] model="
                    f"{query_model!r} endpoint={endpoint}: filters were requested "
                    "but no sqlite connection was supplied (pass conn=... or ensure "
                    "vector_table is a sqlite3.Connection)"
                ),
                stage="retrieve",
            )
        else:
            hits = search_similar(vector_table, query_vec, limit=limit)
        if _rag_duration:
            _rag_duration.record((time.monotonic() - _t1) * 1000, {"stage": "retrieve"})
    except Exception as e:
        logger.error("RAG retrieve failed: %s", e)
        return _degraded_response(
            model=query_model,
            sources=[],
            error=_describe_exception(e, stage="retrieve", model=query_model, endpoint=endpoint),
            stage="retrieve",
        )
    if _rag_hits:
        _rag_hits.record(len(hits))

    if not hits:
        return {
            "answer": "No relevant knowledge found in the database.",
            "sources": [],
            "model": query_model,
            "degraded": False,
        }

    # 3. Shape sources (preserved even if synthesis fails).
    sources = _shape_rag_sources(hits, limit=limit)
    messages = _build_rag_prompt(question, hits, max_chars=max_context_chars)

    # 4. Synthesize.
    try:
        _t2 = time.monotonic()
        answer = await lm_client.chat(messages, model=query_model)
        if _rag_duration:
            _rag_duration.record((time.monotonic() - _t2) * 1000, {"stage": "synthesize"})
    except Exception as e:
        logger.error("RAG synthesis failed: %s", e)
        return _degraded_response(
            model=query_model,
            sources=sources,
            error=_describe_exception(e, stage="synthesize", model=query_model, endpoint=endpoint),
            stage="synthesize",
        )

    if not answer or not str(answer).strip():
        return _degraded_response(
            model=query_model,
            sources=sources,
            error=f"synthesize failed [EmptyResponse] model={query_model!r} endpoint={endpoint}: "
            "chat endpoint returned an empty message",
            stage="synthesize",
        )

    return {
        "answer": answer,
        "sources": sources,
        "model": query_model,
        "degraded": False,
    }
