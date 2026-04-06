"""RAG (retrieval-augmented generation) pipeline for Hippo knowledge queries."""

import json
import logging
from datetime import datetime, timezone

from hippo_brain.embeddings import EMBED_DIM, _pad_or_truncate, search_similar

logger = logging.getLogger("hippo_brain.rag")

_SYSTEM_PROMPT = (
    "You are a personal knowledge assistant. The user is asking about their own past "
    "activity — commands they ran, decisions they made, problems they solved.\n\n"
    "Answer the user's question using ONLY the context provided below. Be specific: "
    "reference actual commands, file paths, error messages, and details from the "
    "context. If the context doesn't contain enough information to answer fully, "
    "say what you can and note what's missing.\n\n"
    "Keep your answer concise and direct — a few short paragraphs at most. "
    "Use markdown formatting: headers for sections, backticks for commands and paths, "
    "code blocks for multi-line commands.\n\n"
    "Do not make up information. Do not hallucinate commands or paths."
)


def _format_timestamp(ts_ms: int) -> str:
    """Format epoch-ms timestamp as YYYY-MM-DD."""
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def _shape_rag_sources(hits: list[dict], min_score: float = 0.0) -> list[dict]:
    """Transform raw LanceDB hits into the response source shape.

    Filters out sources below min_score and caps at 5 results.
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
            }
        )
    return sources[:5]


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len, adding ellipsis if needed."""
    if len(text) <= max_len:
        return text
    return text[: max_len - 1] + "…"


def _build_rag_prompt(question: str, hits: list[dict]) -> list[dict]:
    """Build chat messages for the synthesis LLM from question + retrieved hits."""
    context_blocks = []
    for i, hit in enumerate(hits, 1):
        score = round(1.0 - hit.get("_distance", 1.0), 4)
        ts = hit.get("captured_at", 0)
        date_str = _format_timestamp(ts) if ts else "unknown"

        lines = [f"[{i}] (score: {score}, {date_str})"]
        if hit.get("summary"):
            lines.append(f"Summary: {hit['summary']}")
        if hit.get("embed_text"):
            lines.append(f"Detail: {hit['embed_text']}")
        if hit.get("commands_raw"):
            lines.append(f"Commands: {hit['commands_raw']}")
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

        context_blocks.append("\n".join(lines))

    context = "\n\n".join(context_blocks)

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"},
    ]


def format_rag_response(result: dict) -> str:
    """Format a RAG result dict as a human-readable string (for MCP tool output)."""
    parts = []

    if "error" in result:
        parts.append(f"Error: {result['error']}")
    if "answer" in result:
        parts.append(result["answer"])

    sources = result.get("sources", [])
    if sources:
        parts.append("")
        parts.append("Sources:")
        for i, src in enumerate(sources, 1):
            score = src.get("score", 0)
            summary = src.get("summary", "")
            cwd = src.get("cwd", "")
            branch = src.get("git_branch", "")
            ts = src.get("timestamp", 0)
            date_str = _format_timestamp(ts) if ts else ""

            location_parts = []
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

    return "\n".join(parts)


async def ask(
    question: str,
    lm_client,
    vector_table,
    query_model: str,
    embedding_model: str,
    limit: int = 10,
) -> dict:
    """Run the full RAG pipeline: embed, retrieve, synthesize.

    Returns a dict with 'answer', 'sources', and 'model' keys.
    On failure, returns 'error' instead of 'answer' (sources may still be present).
    """
    # 1. Embed the question
    try:
        vecs = await lm_client.embed([question], model=embedding_model)
    except Exception as e:
        logger.error("RAG embed failed: %s", e)
        return {"error": f"Embedding failed: {e}", "sources": [], "model": query_model}

    query_vec = _pad_or_truncate(vecs[0], EMBED_DIM)

    # 2. Retrieve relevant knowledge nodes
    hits = search_similar(vector_table, query_vec, limit=limit)

    if not hits:
        return {
            "answer": "No relevant knowledge found in the database.",
            "sources": [],
            "model": query_model,
        }

    # 3. Shape sources for response
    sources = _shape_rag_sources(hits)

    # 4. Build prompt and synthesize
    messages = _build_rag_prompt(question, hits)

    try:
        answer = await lm_client.chat(messages, model=query_model)
    except Exception as e:
        logger.error("RAG synthesis failed: %s", e)
        return {"error": f"Synthesis failed: {e}", "sources": sources, "model": query_model}

    return {"answer": answer, "sources": sources, "model": query_model}
