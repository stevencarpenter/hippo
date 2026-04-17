"""Retrieval engine for Hippo knowledge queries.

Implements hybrid (RRF + MMR) search over the sqlite-vec ``knowledge_vectors``
table and the FTS5 ``knowledge_fts`` table, with filter pushdown and
normalized cosine scores in ``[0, 1]``.

The underlying vec0 / FTS5 operations are delegated to a backend module (by
default :mod:`hippo_brain.vector_store`) so this file is testable in isolation
with a fake backend.
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import dataclass, field
from typing import Protocol, Sequence


RRF_K = 60
CANDIDATE_POOL = 3000
MMR_LAMBDA = 0.7
MAX_COSINE_DISTANCE = 2.0


@dataclass
class Filters:
    """Optional filters pushed down to the vec0/FTS5 query layer."""

    project: str | None = None
    since_ms: int | None = None
    source: str | None = None  # "shell" | "claude" | "browser" | "workflow"
    branch: str | None = None
    entity: str | None = None


@dataclass
class SearchResult:
    uuid: str
    score: float
    summary: str
    embed_text: str
    outcome: str | None
    tags: list[str]
    cwd: str
    git_branch: str
    captured_at: int
    linked_event_ids: list[int] = field(default_factory=list)


class _Backend(Protocol):
    """Shape of the vec0/FTS5 backend this module calls into.

    Matches :mod:`hippo_brain.vector_store` at commit d93a9bb — both primitives
    return dicts with ``knowledge_node_id`` + a pre-normalized ``score`` in
    ``[0, 1]``. The retrieval layer only uses ``knowledge_node_id`` + rank for
    RRF; ``distance`` is used for MMR diversification when available.
    """

    def knn_search(
        self,
        conn: sqlite3.Connection,
        query_vec: Sequence[float],
        column: str = ...,
        limit: int = ...,
    ) -> list[dict]: ...

    def fts_search(
        self,
        conn: sqlite3.Connection,
        query: str,
        limit: int = ...,
    ) -> list[dict]: ...


def _default_backend() -> _Backend:
    from hippo_brain import vector_store  # lazy — storage agent owns this module

    return vector_store  # type: ignore[return-value]


def _call_knn(
    backend: _Backend, conn: sqlite3.Connection, query_vec: Sequence[float], limit: int
) -> list[tuple[int, float]]:
    """Adapt the backend's dict return to a ``(id, distance)`` list."""
    raw = backend.knn_search(conn, query_vec, limit=limit)
    return [(r["knowledge_node_id"], float(r.get("distance", 0.0))) for r in raw]


def _call_fts(
    backend: _Backend, conn: sqlite3.Connection, query: str, limit: int
) -> list[tuple[int, float]]:
    raw = backend.fts_search(conn, query, limit=limit)
    return [(r["knowledge_node_id"], float(r.get("bm25", 0.0))) for r in raw]


def _get_vectors(conn: sqlite3.Connection, node_ids: Sequence[int]) -> dict[int, list[float]]:
    """Fetch knowledge vectors directly from the vec0 table.

    Uses sqlite-vec's ``vec_to_json`` helper so we get back plain JSON arrays
    we can deserialize. Returns ``{}`` on schema absence (e.g. unit tests
    running against a fixture that omits ``knowledge_vectors``) — MMR treats
    missing vectors as zero-similarity, which is a safe degradation.
    """
    if not node_ids:
        return {}
    placeholders = ",".join("?" for _ in node_ids)
    try:
        rows = conn.execute(  # nosemgrep
            f"""
            SELECT knowledge_node_id, vec_to_json(vec_knowledge)
            FROM knowledge_vectors
            WHERE knowledge_node_id IN ({placeholders})
            """,
            list(node_ids),
        ).fetchall()
    except sqlite3.OperationalError:
        return {}
    out: dict[int, list[float]] = {}
    for nid, json_str in rows:
        if not json_str:
            continue
        try:
            out[nid] = json.loads(json_str)
        except json.JSONDecodeError, TypeError:
            continue
    return out


def search(
    conn: sqlite3.Connection,
    query: str,
    query_vec: Sequence[float] | None,
    filters: Filters | None = None,
    mode: str = "hybrid",
    limit: int = 10,
    *,
    backend: _Backend | None = None,
) -> list[SearchResult]:
    """Search knowledge nodes.

    Parameters
    ----------
    conn:
        An already-open sqlite3 connection with sqlite-vec loaded.
    query:
        Free-text query used by FTS5 and (in ``recent`` mode) as a loose
        filter. May be empty for ``semantic``/``recent``.
    query_vec:
        Embedding of ``query``. Required for ``semantic`` and ``hybrid`` modes.
    filters:
        Optional :class:`Filters` applied as WHERE clauses on the joined
        ``knowledge_nodes`` graph.
    mode:
        One of ``"semantic"``, ``"lexical"``, ``"hybrid"``, ``"recent"``.
    limit:
        Maximum results returned.
    backend:
        Override the vec0/FTS5 backend (for unit tests). Defaults to
        :mod:`hippo_brain.vector_store`.
    """
    if limit <= 0:
        return []
    filters = filters or Filters()
    backend = backend or _default_backend()

    if mode == "semantic":
        return _semantic(conn, query_vec, filters, limit, backend)
    if mode == "lexical":
        return _lexical(conn, query, filters, limit, backend)
    if mode == "recent":
        return _recent(conn, query, filters, limit, backend)
    if mode == "hybrid":
        return _hybrid(conn, query, query_vec, filters, limit, backend)
    raise ValueError(f"unknown retrieval mode: {mode!r}")


# ---------------------------------------------------------------------------
# Mode implementations
# ---------------------------------------------------------------------------


def _semantic(
    conn: sqlite3.Connection,
    query_vec: Sequence[float] | None,
    filters: Filters,
    limit: int,
    backend: _Backend,
) -> list[SearchResult]:
    if query_vec is None:
        raise ValueError("semantic mode requires a query_vec")
    raw = _call_knn(backend, conn, query_vec, CANDIDATE_POOL)
    if not raw:
        return []
    allowed = _apply_filters(conn, [nid for nid, _ in raw], filters)
    ordered = [(nid, dist) for nid, dist in raw if nid in allowed]
    details = _fetch_details(conn, [nid for nid, _ in ordered])
    vecs = _get_vectors(conn, [nid for nid, _ in ordered])
    scored = [(nid, _cosine_to_score(dist)) for nid, dist in ordered]
    picked = _mmr(scored, vecs, limit)
    return [_to_result(score, details.get(nid)) for nid, score in picked if nid in details]


def _lexical(
    conn: sqlite3.Connection,
    query: str,
    filters: Filters,
    limit: int,
    backend: _Backend,
) -> list[SearchResult]:
    if not query:
        return []
    raw = _call_fts(backend, conn, query, CANDIDATE_POOL)
    if not raw:
        return []
    allowed = _apply_filters(conn, [nid for nid, _ in raw], filters)
    ordered = [nid for nid, _ in raw if nid in allowed][:limit]
    details = _fetch_details(conn, ordered)
    # Score = positional (1.0 for top, linearly down to ~0).
    n = max(len(ordered), 1)
    results: list[SearchResult] = []
    for rank, nid in enumerate(ordered):
        if nid not in details:
            continue
        score = 1.0 - rank / n
        results.append(_to_result(score, details[nid]))
    return results


def _recent(
    conn: sqlite3.Connection,
    query: str,
    filters: Filters,
    limit: int,
    backend: _Backend,
) -> list[SearchResult]:
    # "date-ordered with loose query match" — use FTS if query provided, else
    # pull most recent knowledge_nodes filtered by the same WHERE stack.
    if query:
        raw = _call_fts(backend, conn, query, CANDIDATE_POOL)
        candidate_ids = [nid for nid, _ in raw]
        if not candidate_ids:
            return []
    else:
        candidate_ids = _all_recent_ids(conn, CANDIDATE_POOL)
    allowed = _apply_filters(conn, candidate_ids, filters)
    details = _fetch_details(conn, list(allowed))
    ordered = sorted(
        (d for d in details.values()),
        key=lambda d: d["captured_at"],
        reverse=True,
    )[:limit]
    return [_to_result(1.0 - i / max(len(ordered), 1), d) for i, d in enumerate(ordered)]


def _hybrid(
    conn: sqlite3.Connection,
    query: str,
    query_vec: Sequence[float] | None,
    filters: Filters,
    limit: int,
    backend: _Backend,
) -> list[SearchResult]:
    if query_vec is None:
        # Degrade to lexical if we don't have a vector.
        return _lexical(conn, query, filters, limit, backend)

    vec_hits = _call_knn(backend, conn, query_vec, CANDIDATE_POOL)
    fts_hits = _call_fts(backend, conn, query, CANDIDATE_POOL) if query else []

    # RRF merge.
    rrf: dict[int, float] = {}
    for rank, hit in enumerate(vec_hits):
        rrf[hit[0]] = rrf.get(hit[0], 0.0) + 1.0 / (RRF_K + rank + 1)
    for rank, hit in enumerate(fts_hits):
        rrf[hit[0]] = rrf.get(hit[0], 0.0) + 1.0 / (RRF_K + rank + 1)

    if not rrf:
        return []

    allowed = _apply_filters(conn, list(rrf.keys()), filters)
    scored = [(nid, score) for nid, score in rrf.items() if nid in allowed]
    scored.sort(key=lambda x: x[1], reverse=True)
    if not scored:
        return []

    # Normalize so top RRF score = 1.0.
    top = scored[0][1] or 1.0
    scored = [(nid, s / top) for nid, s in scored]

    vecs = _get_vectors(conn, [nid for nid, _ in scored])
    picked = _mmr(scored, vecs, limit)
    details = _fetch_details(conn, [nid for nid, _ in picked])
    return [_to_result(score, details.get(nid)) for nid, score in picked if nid in details]


# ---------------------------------------------------------------------------
# Filter pushdown
# ---------------------------------------------------------------------------


def _apply_filters(
    conn: sqlite3.Connection,
    candidate_ids: Sequence[int],
    filters: Filters,
) -> set[int]:
    """Return the subset of ``candidate_ids`` that satisfy ``filters``.

    The WHERE clause is built over a join of ``knowledge_nodes`` with the
    shell / Claude / browser event link tables so filters can pushdown across
    any node type.
    """
    if not candidate_ids:
        return set()
    if _is_empty_filter(filters):
        return set(candidate_ids)

    placeholders = ",".join("?" for _ in candidate_ids)
    clauses: list[str] = [f"kn.id IN ({placeholders})"]
    params: list[object] = list(candidate_ids)

    if filters.since_ms is not None:
        clauses.append(
            "(kn.created_at >= ? OR e.timestamp >= ? OR cs.start_time >= ? OR be.timestamp >= ?)"
        )
        params.extend([filters.since_ms] * 4)

    if filters.project:
        clauses.append("(e.cwd LIKE ? OR cs.cwd LIKE ?)")
        pattern = f"{filters.project}%"
        params.extend([pattern, pattern])

    if filters.branch:
        clauses.append("(e.git_branch = ? OR cs.git_branch = ?)")
        params.extend([filters.branch, filters.branch])

    if filters.source:
        clauses.append(_source_clause(filters.source))

    sql = f"""
        SELECT DISTINCT kn.id
        FROM knowledge_nodes kn
        LEFT JOIN knowledge_node_events kne ON kne.knowledge_node_id = kn.id
        LEFT JOIN events e ON e.id = kne.event_id
        LEFT JOIN knowledge_node_claude_sessions kncs ON kncs.knowledge_node_id = kn.id
        LEFT JOIN claude_sessions cs ON cs.id = kncs.claude_session_id
        LEFT JOIN knowledge_node_browser_events knbe ON knbe.knowledge_node_id = kn.id
        LEFT JOIN browser_events be ON be.id = knbe.browser_event_id
        WHERE {" AND ".join(clauses)}
    """

    if filters.entity:
        sql = sql.replace(
            "FROM knowledge_nodes kn",
            "FROM knowledge_nodes kn\n"
            "        JOIN knowledge_node_entities kne2 ON kne2.knowledge_node_id = kn.id\n"
            "        JOIN entities ent ON ent.id = kne2.entity_id",
            1,
        )
        clauses.append("(ent.canonical = ? OR ent.name = ?)")
        params.extend([filters.entity, filters.entity])
        # Rebuild with the new clause list included.
        sql = f"""
            SELECT DISTINCT kn.id
            FROM knowledge_nodes kn
            JOIN knowledge_node_entities kne2 ON kne2.knowledge_node_id = kn.id
            JOIN entities ent ON ent.id = kne2.entity_id
            LEFT JOIN knowledge_node_events kne ON kne.knowledge_node_id = kn.id
            LEFT JOIN events e ON e.id = kne.event_id
            LEFT JOIN knowledge_node_claude_sessions kncs ON kncs.knowledge_node_id = kn.id
            LEFT JOIN claude_sessions cs ON cs.id = kncs.claude_session_id
            LEFT JOIN knowledge_node_browser_events knbe ON knbe.knowledge_node_id = kn.id
            LEFT JOIN browser_events be ON be.id = knbe.browser_event_id
            WHERE {" AND ".join(clauses)}
        """

    # SQL is assembled from static table/column identifiers + a fixed list of
    # clause fragments defined above; all user-controlled values flow through
    # `params` as bound parameters.
    rows = conn.execute(sql, params).fetchall()  # nosemgrep
    return {row[0] for row in rows}


def _source_clause(source: str) -> str:
    mapping = {
        "shell": "e.id IS NOT NULL",
        "claude": "cs.id IS NOT NULL",
        "browser": "be.id IS NOT NULL",
        "workflow": "kn.node_type = 'workflow'",
    }
    clause = mapping.get(source)
    if clause is None:
        raise ValueError(f"unknown source filter: {source!r}")
    return clause


def _is_empty_filter(f: Filters) -> bool:
    return (
        f.project is None
        and f.since_ms is None
        and f.source is None
        and f.branch is None
        and f.entity is None
    )


# ---------------------------------------------------------------------------
# Detail fetch
# ---------------------------------------------------------------------------


def _fetch_details(conn: sqlite3.Connection, node_ids: Sequence[int]) -> dict[int, dict]:
    """Fetch the canonical SearchResult fields for each node id."""
    if not node_ids:
        return {}
    placeholders = ",".join("?" for _ in node_ids)

    # `placeholders` is a run of "?" separators whose length matches
    # `node_ids`; the id values themselves are bound parameters.
    rows = conn.execute(  # nosemgrep
        f"""
        SELECT id, uuid, content, embed_text, outcome, tags, created_at
        FROM knowledge_nodes
        WHERE id IN ({placeholders})
        """,
        list(node_ids),
    ).fetchall()

    details: dict[int, dict] = {}
    for node_id, uuid, content_str, embed_text, outcome, tags_str, created_at in rows:
        summary = _extract_summary(content_str)
        tags = _parse_tags(tags_str)
        details[node_id] = {
            "id": node_id,
            "uuid": uuid,
            "summary": summary,
            "embed_text": embed_text or "",
            "outcome": outcome,
            "tags": tags,
            "cwd": "",
            "git_branch": "",
            "captured_at": created_at,
            "linked_event_ids": [],
        }

    # Attach shell event metadata (cwd/branch/captured_at prefer event data).
    ev_rows = conn.execute(  # nosemgrep
        f"""
        SELECT kne.knowledge_node_id, e.id, e.timestamp, e.cwd, e.git_branch
        FROM knowledge_node_events kne
        JOIN events e ON e.id = kne.event_id
        WHERE kne.knowledge_node_id IN ({placeholders})
        ORDER BY e.timestamp DESC
        """,
        list(node_ids),
    ).fetchall()
    for kn_id, ev_id, ts, cwd, branch in ev_rows:
        d = details.get(kn_id)
        if d is None:
            continue
        d["linked_event_ids"].append(ev_id)
        if not d["cwd"] and cwd:
            d["cwd"] = cwd
        if not d["git_branch"] and branch:
            d["git_branch"] = branch
        if ts and ts > d["captured_at"]:
            d["captured_at"] = ts

    # Fill from claude sessions if still empty.
    cs_rows = conn.execute(  # nosemgrep
        f"""
        SELECT kncs.knowledge_node_id, cs.start_time, cs.cwd, cs.git_branch
        FROM knowledge_node_claude_sessions kncs
        JOIN claude_sessions cs ON cs.id = kncs.claude_session_id
        WHERE kncs.knowledge_node_id IN ({placeholders})
        ORDER BY cs.start_time DESC
        """,
        list(node_ids),
    ).fetchall()
    for kn_id, ts, cwd, branch in cs_rows:
        d = details.get(kn_id)
        if d is None:
            continue
        if not d["cwd"] and cwd:
            d["cwd"] = cwd
        if not d["git_branch"] and branch:
            d["git_branch"] = branch
        if ts and ts > d["captured_at"]:
            d["captured_at"] = ts

    return details


def _all_recent_ids(conn: sqlite3.Connection, limit: int) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM knowledge_nodes ORDER BY created_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [r[0] for r in rows]


# ---------------------------------------------------------------------------
# Score + MMR helpers
# ---------------------------------------------------------------------------


def _cosine_to_score(distance: float) -> float:
    """Map cosine distance ``[0, 2]`` onto a similarity score ``[0, 1]``."""
    d = max(0.0, min(MAX_COSINE_DISTANCE, distance))
    return 1.0 - d / MAX_COSINE_DISTANCE


def _mmr(
    scored: Sequence[tuple[int, float]],
    vecs: dict[int, list[float]],
    k: int,
) -> list[tuple[int, float]]:
    """Select ``k`` items with MMR diversification.

    Items missing a vector are still considered (with zero diversity penalty),
    so lexical-only hits don't get disadvantaged in hybrid mode.
    """
    if k <= 0 or not scored:
        return []
    pool = list(scored)
    pool.sort(key=lambda x: x[1], reverse=True)

    picked: list[tuple[int, float]] = [pool[0]]
    remaining = pool[1:]

    while remaining and len(picked) < k:
        best_idx = 0
        best_mmr = -math.inf
        for i, (nid, score) in enumerate(remaining):
            diversity = _max_similarity(vecs.get(nid), [vecs.get(p) for p, _ in picked])
            mmr = MMR_LAMBDA * score - (1.0 - MMR_LAMBDA) * diversity
            if mmr > best_mmr:
                best_mmr = mmr
                best_idx = i
        picked.append(remaining.pop(best_idx))

    return picked


def _max_similarity(vec: list[float] | None, others: Sequence[list[float] | None]) -> float:
    """Maximum cosine similarity between ``vec`` and any of ``others``.

    Returns ``0`` when either side is missing a vector — i.e. lexical-only
    hits pay no diversity penalty because we can't measure their distance.
    """
    if vec is None:
        return 0.0
    best = 0.0
    for o in others:
        if o is None:
            continue
        sim = _cosine_similarity(vec, o)
        if sim > best:
            best = sim
    return best


def _cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0 or nb == 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ---------------------------------------------------------------------------
# Result shaping
# ---------------------------------------------------------------------------


def _to_result(score: float, detail: dict | None) -> SearchResult:
    if detail is None:
        # Defensive — callers should have filtered unknown ids before calling.
        return SearchResult(
            uuid="",
            score=score,
            summary="",
            embed_text="",
            outcome=None,
            tags=[],
            cwd="",
            git_branch="",
            captured_at=0,
            linked_event_ids=[],
        )
    return SearchResult(
        uuid=detail["uuid"],
        score=round(max(0.0, min(1.0, score)), 4),
        summary=detail["summary"],
        embed_text=detail["embed_text"],
        outcome=detail["outcome"],
        tags=detail["tags"],
        cwd=detail["cwd"],
        git_branch=detail["git_branch"],
        captured_at=detail["captured_at"],
        linked_event_ids=list(detail["linked_event_ids"]),
    )


def _extract_summary(content_str: str | None) -> str:
    if not content_str:
        return ""
    try:
        payload = json.loads(content_str)
    except json.JSONDecodeError, TypeError:
        return ""
    if isinstance(payload, dict):
        return payload.get("summary") or ""
    return ""


def _parse_tags(tags_str: str | None) -> list[str]:
    if not tags_str:
        return []
    try:
        value = json.loads(tags_str)
    except json.JSONDecodeError, TypeError:
        return []
    if isinstance(value, list):
        return [str(t) for t in value]
    return []
