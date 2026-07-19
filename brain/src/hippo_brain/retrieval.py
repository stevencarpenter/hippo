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
import re
import sqlite3
import time
from dataclasses import dataclass, field, replace
from typing import Protocol, Sequence

from hippo_brain.enrichment import IDENTIFIER_ENTITY_TYPES
from hippo_brain.evidence_packets import (
    attach_retrieval_scores,
    make_agentic_packet,
    make_browser_packet,
    make_memory_packet,
    make_shell_packet,
    make_workflow_packet,
)
from hippo_brain.auto_memory_categories import validate_memory_category_filter
from hippo_brain.retrieval_eligibility import (
    agentic_session_eligible_sql,
    browser_event_eligible_sql,
    include_excluded_from_env,
    knowledge_node_eligible_exists_sql,
    shell_event_eligible_sql,
    workflow_run_eligible_sql,
)
from hippo_brain.confidence_scoring import attach_confidence_to_results
from hippo_brain.source_freshness import attach_freshness_to_results
from hippo_brain.source_filters import (
    knowledge_memory_category_clause,
    knowledge_memory_project_clause,
    knowledge_source_exists_clause,
)


RRF_K = 60
CANDIDATE_POOL = 3000
MMR_LAMBDA = 0.7
MAX_COSINE_DISTANCE = 2.0


@dataclass(frozen=True)
class Tuning:
    """Retrieval-quality knobs, overridable via the ``[retrieval]`` config section.

    Defaults reproduce the historical hardcoded behavior except for the recency
    prior, which is on by default (set ``recency_half_life_days = 0`` to disable
    and recover the pre-recency ordering exactly).
    """

    rrf_k: int = RRF_K
    candidate_pool: int = CANDIDATE_POOL
    mmr_lambda: float = MMR_LAMBDA
    vector_weight: float = 1.0
    lexical_weight: float = 1.0
    min_score: float = 0.0
    recency_half_life_days: float = 90.0
    recency_floor: float = 0.5


DEFAULT_TUNING = Tuning()
_active_tuning = DEFAULT_TUNING


def configure(section: dict | None) -> Tuning:
    """Install module-wide tuning from a ``[retrieval]`` config.toml section.

    Unknown keys are ignored; values are coerced and clamped to sane ranges so a
    typo'd config degrades to defaults instead of crashing the query path.
    Returns the installed :class:`Tuning`.
    """
    global _active_tuning
    section = section or {}

    def _num(key: str, default: float, *, lo: float, hi: float) -> float:
        try:
            value = float(section.get(key, default))
        except TypeError, ValueError:
            return default
        return max(lo, min(hi, value))

    _active_tuning = Tuning(
        rrf_k=int(_num("rrf_k", DEFAULT_TUNING.rrf_k, lo=1, hi=10_000)),
        candidate_pool=int(_num("candidate_pool", DEFAULT_TUNING.candidate_pool, lo=10, hi=50_000)),
        mmr_lambda=_num("mmr_lambda", DEFAULT_TUNING.mmr_lambda, lo=0.0, hi=1.0),
        vector_weight=_num("vector_weight", DEFAULT_TUNING.vector_weight, lo=0.0, hi=100.0),
        lexical_weight=_num("lexical_weight", DEFAULT_TUNING.lexical_weight, lo=0.0, hi=100.0),
        min_score=_num("min_score", DEFAULT_TUNING.min_score, lo=0.0, hi=1.0),
        recency_half_life_days=_num(
            "recency_half_life_days", DEFAULT_TUNING.recency_half_life_days, lo=0.0, hi=36_500.0
        ),
        recency_floor=_num("recency_floor", DEFAULT_TUNING.recency_floor, lo=0.0, hi=1.0),
    )
    return _active_tuning


def get_tuning() -> Tuning:
    return _active_tuning


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(row[1] == column for row in rows)


@dataclass
class Filters:
    """Optional filters pushed down to the vec0/FTS5 query layer."""

    project: str | None = None
    since_ms: int | None = None
    source: str | None = None  # "shell" | "claude" | "browser" | "workflow"
    memory_category: str | None = None
    branch: str | None = None
    entity: str | None = None
    include_excluded: bool = False


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
    intent: str = ""
    commands_raw: str = ""
    key_decisions: list[str] = field(default_factory=list)
    problems_encountered: list[str] = field(default_factory=list)
    design_decisions: list[dict] = field(default_factory=list)
    linked_event_ids: list[int] = field(default_factory=list)
    linked_source_ids: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)
    entities: dict[str, list[str]] = field(default_factory=dict)
    confidence: dict = field(default_factory=dict)


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


# Stopwords stripped from the tokenized FTS query. Deliberately small: only
# words that carry no retrieval signal for a technical knowledge base —
# aggressive stopword lists would eat meaningful tokens like "make" or "run".
_FTS_STOPWORDS = frozenset(
    """
    a an and are as at be but by did do does for from had has have how i in is
    it its me my of on or our so that the their there these they this to was
    we were what when where which who why will with you your
    """.split()
)

_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./@:-]*")

_FTS_MAX_TOKENS = 16


def _sanitize_fts_query(query: str) -> str:
    """Build an FTS5 MATCH query from free text: exact phrase OR keywords.

    Natural-language questions contain characters FTS5 treats as operators
    (``?``, ``:``, ``-``, ``*``, ``(``, ``"``), so every emitted term is
    quoted (embedded double-quotes doubled per FTS5's rules). The historical
    behavior — the whole query as one quoted phrase — required the question
    to appear verbatim in a document for BM25 to hit at all, which almost
    never happens for real questions and silently degraded hybrid search to
    vector-only. We now OR the phrase with individually quoted keyword
    tokens (stopwords dropped, capped at ``_FTS_MAX_TOKENS``): documents
    matching more keywords rank higher, and the intact phrase still boosts
    exact matches.
    """
    escaped = query.replace('"', '""')
    phrase = f'"{escaped}"'
    seen: set[str] = set()
    tokens: list[str] = []
    for tok in _FTS_TOKEN_RE.findall(query):
        lowered = tok.lower()
        if len(tok) < 2 or lowered in _FTS_STOPWORDS or lowered in seen:
            continue
        seen.add(lowered)
        tokens.append(tok.replace('"', '""'))
        if len(tokens) >= _FTS_MAX_TOKENS:
            break
    if not tokens:
        return phrase
    # A quoted multi-word query is already a phrase of its single token when
    # only one token survives — no need to OR it with itself.
    if len(tokens) == 1 and tokens[0].lower() == escaped.strip().lower():
        return phrase
    return " OR ".join([phrase, *(f'"{tok}"' for tok in tokens)])


def _call_fts(
    backend: _Backend, conn: sqlite3.Connection, query: str, limit: int
) -> list[tuple[int, float]]:
    raw = backend.fts_search(conn, _sanitize_fts_query(query), limit=limit)
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
    tuning: Tuning | None = None,
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
    tuning:
        Override retrieval knobs for this call. Defaults to the module-wide
        tuning installed by :func:`configure` (or :data:`DEFAULT_TUNING`).
    """
    if limit <= 0:
        return []
    filters = filters or Filters()
    if include_excluded_from_env():
        filters = replace(filters, include_excluded=True)
    backend = backend or _default_backend()
    t = tuning or _active_tuning

    if mode == "semantic":
        results = _semantic(conn, query_vec, filters, limit, backend, t)
    elif mode == "lexical":
        results = _lexical(conn, query, filters, limit, backend, t)
    elif mode == "recent":
        results = _recent(conn, query, filters, limit, backend, t)
    elif mode == "hybrid":
        results = _hybrid(conn, query, query_vec, filters, limit, backend, t)
    else:
        raise ValueError(f"unknown retrieval mode: {mode!r}")

    if t.min_score > 0.0:
        results = [r for r in results if r.score >= t.min_score]

    attach_freshness_to_results(conn, results)
    attach_confidence_to_results(results)
    return results


# ---------------------------------------------------------------------------
# Mode implementations
# ---------------------------------------------------------------------------


def _semantic(
    conn: sqlite3.Connection,
    query_vec: Sequence[float] | None,
    filters: Filters,
    limit: int,
    backend: _Backend,
    t: Tuning,
) -> list[SearchResult]:
    if query_vec is None:
        raise ValueError("semantic mode requires a query_vec")
    raw = _call_knn(backend, conn, query_vec, t.candidate_pool)
    if not raw:
        return []
    allowed = _apply_filters(conn, [nid for nid, _ in raw], filters)
    ordered = [(nid, dist) for nid, dist in raw if nid in allowed]
    details = _fetch_details(
        conn, [nid for nid, _ in ordered], include_excluded=filters.include_excluded
    )
    vecs = _get_vectors(conn, [nid for nid, _ in ordered])
    scored = [(nid, _cosine_to_score(dist)) for nid, dist in ordered]
    scored = _apply_recency(conn, scored, t)
    picked = _mmr(scored, vecs, limit, t.mmr_lambda)
    return [_to_result(score, details.get(nid)) for nid, score in picked if nid in details]


def _lexical(
    conn: sqlite3.Connection,
    query: str,
    filters: Filters,
    limit: int,
    backend: _Backend,
    t: Tuning,
) -> list[SearchResult]:
    if not query:
        return []
    raw = _call_fts(backend, conn, query, t.candidate_pool)
    if not raw:
        return []
    allowed = _apply_filters(conn, [nid for nid, _ in raw], filters)
    ordered = [nid for nid, _ in raw if nid in allowed][:limit]
    details = _fetch_details(conn, ordered, include_excluded=filters.include_excluded)
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
    t: Tuning,
) -> list[SearchResult]:
    # "date-ordered with loose query match" — use FTS if query provided, else
    # pull most recent knowledge_nodes filtered by the same WHERE stack.
    if query:
        raw = _call_fts(backend, conn, query, t.candidate_pool)
        candidate_ids = [nid for nid, _ in raw]
    else:
        candidate_ids = []
    if not candidate_ids:
        candidate_ids = _all_recent_ids(conn, t.candidate_pool)
    allowed = _apply_filters(conn, candidate_ids, filters)
    details = _fetch_details(conn, list(allowed), include_excluded=filters.include_excluded)
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
    t: Tuning,
) -> list[SearchResult]:
    if query_vec is None:
        # Degrade to lexical if we don't have a vector.
        return _lexical(conn, query, filters, limit, backend, t)

    vec_hits = _call_knn(backend, conn, query_vec, t.candidate_pool)
    fts_hits = _call_fts(backend, conn, query, t.candidate_pool) if query else []

    # Weighted RRF merge (both weights default to 1.0 — classic RRF).
    rrf: dict[int, float] = {}
    for rank, hit in enumerate(vec_hits):
        rrf[hit[0]] = rrf.get(hit[0], 0.0) + t.vector_weight / (t.rrf_k + rank + 1)
    for rank, hit in enumerate(fts_hits):
        rrf[hit[0]] = rrf.get(hit[0], 0.0) + t.lexical_weight / (t.rrf_k + rank + 1)

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

    scored = _apply_recency(conn, scored, t)
    vecs = _get_vectors(conn, [nid for nid, _ in scored])
    picked = _mmr(scored, vecs, limit, t.mmr_lambda)
    details = _fetch_details(
        conn, [nid for nid, _ in picked], include_excluded=filters.include_excluded
    )
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
    shell / agentic-session / browser event link tables so filters can pushdown
    across any node type.
    """
    if not candidate_ids:
        return set()
    if _is_empty_filter(filters) and filters.include_excluded:
        return set(candidate_ids)

    placeholders = ",".join("?" for _ in candidate_ids)
    clauses: list[str] = [f"kn.id IN ({placeholders})"]
    params: list[object] = list(candidate_ids)

    if not filters.include_excluded:
        elig_sql, elig_params = knowledge_node_eligible_exists_sql(
            conn, "kn.id", include_excluded=False
        )
        clauses.append(elig_sql)
        params.extend(elig_params)

    if filters.since_ms is not None:
        clauses.append(
            "(kn.created_at >= ? OR e.timestamp >= ? OR asx.start_time >= ? OR be.timestamp >= ?)"
        )
        params.extend([filters.since_ms] * 4)

    if filters.project:
        pattern = f"%{filters.project}%"
        project_terms = [
            "e.cwd LIKE ?",
            "e.git_repo LIKE ?",
            "asx.cwd LIKE ?",
            "asx.project_dir LIKE ?",
        ]
        params.extend([pattern, pattern, pattern, pattern])
        # Auto-memory nodes link only via knowledge_node_memory_chunks (no event /
        # session row), so reach memory_documents.repository via the shared clause —
        # otherwise project-scoped RAG silently drops them (parity with MCP search).
        memory_project = knowledge_memory_project_clause(conn)
        if memory_project is not None:
            project_terms.append(memory_project)
            params.extend([pattern, pattern])
        clauses.append("(" + " OR ".join(project_terms) + ")")

    if filters.branch:
        clauses.append("(e.git_branch = ? OR asx.git_branch = ?)")
        params.extend([filters.branch, filters.branch])

    if filters.source:
        source_clause = knowledge_source_exists_clause(
            filters.source, conn, include_excluded=filters.include_excluded
        )
        if source_clause is None:
            raise ValueError(f"unknown source filter: {filters.source!r}")
        clauses.append(source_clause)

    if filters.memory_category:
        validate_memory_category_filter(filters.memory_category)
        category_clause = knowledge_memory_category_clause(conn)
        if category_clause is None:
            raise ValueError("memory category filter requires schema v20+")
        clauses.append(category_clause)
        params.append(filters.memory_category)

    sql = f"""
        SELECT DISTINCT kn.id
        FROM knowledge_nodes kn
        LEFT JOIN knowledge_node_events kne ON kne.knowledge_node_id = kn.id
        LEFT JOIN events e ON e.id = kne.event_id
        LEFT JOIN knowledge_node_agentic_sessions kncs ON kncs.knowledge_node_id = kn.id
        LEFT JOIN agentic_sessions asx ON asx.id = kncs.agentic_session_id
            AND {agentic_session_eligible_sql("asx", include_excluded=filters.include_excluded)}
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
            LEFT JOIN knowledge_node_agentic_sessions kncs ON kncs.knowledge_node_id = kn.id
            LEFT JOIN agentic_sessions asx ON asx.id = kncs.agentic_session_id
            AND {agentic_session_eligible_sql("asx", include_excluded=filters.include_excluded)}
            LEFT JOIN knowledge_node_browser_events knbe ON knbe.knowledge_node_id = kn.id
            LEFT JOIN browser_events be ON be.id = knbe.browser_event_id
            WHERE {" AND ".join(clauses)}
        """

    # SQL is assembled from static table/column identifiers + a fixed list of
    # clause fragments defined above; all user-controlled values flow through
    # `params` as bound parameters.
    rows = conn.execute(sql, params).fetchall()  # nosemgrep
    return {row[0] for row in rows}


def _is_empty_filter(f: Filters) -> bool:
    return (
        f.project is None
        and f.since_ms is None
        and f.source is None
        and f.memory_category is None
        and f.branch is None
        and f.entity is None
    )


# ---------------------------------------------------------------------------
# Detail fetch
# ---------------------------------------------------------------------------


def _fetch_details(
    conn: sqlite3.Connection,
    node_ids: Sequence[int],
    *,
    include_excluded: bool = False,
) -> dict[int, dict]:
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
        content = _parse_content(content_str)
        tags = _parse_tags(tags_str)
        details[node_id] = {
            "id": node_id,
            "uuid": uuid,
            "summary": content.get("summary") or "",
            "embed_text": embed_text or "",
            "outcome": outcome,
            "tags": tags,
            "intent": str(content.get("intent") or ""),
            "commands_raw": str(content.get("commands_raw") or ""),
            "key_decisions": _string_list(content.get("key_decisions")),
            "problems_encountered": _string_list(content.get("problems_encountered")),
            "design_decisions": _dict_list(content.get("design_decisions")),
            "cwd": "",
            "git_branch": "",
            "captured_at": created_at,
            "linked_event_ids": [],
            "linked_source_ids": [],
            "evidence_packets": [],
            "entities": {},
        }

    # Attach shell event metadata (cwd/branch/captured_at prefer event data).
    shell_extra = [col for col in ("command", "source_kind") if _column_exists(conn, "events", col)]
    shell_cols = ["e.id", "e.timestamp", "e.cwd", "e.git_branch", *[f"e.{c}" for c in shell_extra]]
    ev_rows = conn.execute(  # nosemgrep: unfiltered-event-table-select
        f"""
        SELECT kne.knowledge_node_id, {", ".join(shell_cols)}
        FROM knowledge_node_events kne
        JOIN events e ON e.id = kne.event_id
        WHERE kne.knowledge_node_id IN ({placeholders})
          AND {shell_event_eligible_sql("e", include_excluded=include_excluded, conn=conn)}
        ORDER BY e.timestamp DESC
        """,
        list(node_ids),
    ).fetchall()
    for row in ev_rows:
        kn_id = row[0]
        ev_id = row[1]
        ts = row[2]
        cwd = row[3]
        branch = row[4]
        extras = dict(zip(shell_extra, row[5:], strict=False))
        d = details.get(kn_id)
        if d is None:
            continue
        d["linked_event_ids"].append(ev_id)
        d["linked_source_ids"].append(f"shell-{ev_id}")
        d["evidence_packets"].append(
            make_shell_packet(
                event_id=ev_id,
                timestamp_ms=ts or 0,
                command=extras.get("command"),
                source_kind=extras.get("source_kind"),
            )
        )
        if not d["cwd"] and cwd:
            d["cwd"] = cwd
        if not d["git_branch"] and branch:
            d["git_branch"] = branch
        if ts and ts > d["captured_at"]:
            d["captured_at"] = ts

    # Attach browser source linkage. Probes never enqueue, so browser-linked
    # nodes are normally real events; `be.probe_tag IS NULL` is defense-in-depth
    # vs AP-6 (probes must never surface in user-facing queries).
    browser_extra = [
        col
        for col in ("timestamp", "title", "url", "domain")
        if _column_exists(conn, "browser_events", col)
    ]
    br_cols = ["be.id", *[f"be.{c}" for c in browser_extra]]
    br_rows = conn.execute(  # nosemgrep: unfiltered-event-table-select
        f"""
        SELECT knbe.knowledge_node_id, {", ".join(br_cols)}
        FROM knowledge_node_browser_events knbe
        JOIN browser_events be ON be.id = knbe.browser_event_id
        WHERE knbe.knowledge_node_id IN ({placeholders})
          AND {browser_event_eligible_sql("be", include_excluded=include_excluded)}
        ORDER BY be.id DESC
        """,
        list(node_ids),
    ).fetchall()
    for row in br_rows:
        kn_id = row[0]
        be_id = row[1]
        fields = dict(zip(browser_extra, row[2:], strict=False))
        d = details.get(kn_id)
        if d is None:
            continue
        d["linked_source_ids"].append(f"browser-{be_id}")
        d["evidence_packets"].append(
            make_browser_packet(
                event_id=be_id,
                timestamp_ms=fields.get("timestamp") or 0,
                title=fields.get("title"),
                url=fields.get("url"),
                domain=fields.get("domain"),
            )
        )

    # Attach workflow source linkage (CI runs have no probe variant).
    workflow_extra = [
        col
        for col in ("started_at", "name", "repo", "conclusion")
        if _column_exists(conn, "workflow_runs", col)
    ]
    wf_cols = ["wr.id", *[f"wr.{c}" for c in workflow_extra]]
    wf_rows = conn.execute(
        f"""
        SELECT knwr.knowledge_node_id, {", ".join(wf_cols)}
        FROM knowledge_node_workflow_runs knwr
        JOIN workflow_runs wr ON wr.id = knwr.run_id
        WHERE knwr.knowledge_node_id IN ({placeholders})
          AND {workflow_run_eligible_sql("wr", include_excluded=include_excluded)}
        ORDER BY wr.id DESC
        """,
        list(node_ids),
    ).fetchall()
    for row in wf_rows:
        kn_id = row[0]
        wr_id = row[1]
        fields = dict(zip(workflow_extra, row[2:], strict=False))
        d = details.get(kn_id)
        if d is None:
            continue
        d["linked_source_ids"].append(f"workflow-{wr_id}")
        d["evidence_packets"].append(
            make_workflow_packet(
                run_id=wr_id,
                timestamp_ms=fields.get("started_at") or 0,
                name=fields.get("name"),
                repo=fields.get("repo"),
                conclusion=fields.get("conclusion"),
            )
        )

    # Attach agentic-session linkage (cwd/branch backfill only if still empty;
    # linked_source_ids is always appended). Probes are excluded via
    # `asx.probe_tag IS NULL` (defense-in-depth vs AP-6; probes never enqueue so
    # are normally unlinked anyway).
    optional_agentic = [
        col
        for col in ("session_id", "segment_index", "summary_text", "source_file", "end_time")
        if _column_exists(conn, "agentic_sessions", col)
    ]
    cs_cols = [
        "asx.id",
        "asx.harness",
        "asx.start_time",
        "asx.cwd",
        "asx.git_branch",
        *[f"asx.{col}" for col in optional_agentic],
    ]
    cs_rows = conn.execute(  # nosemgrep: unfiltered-event-table-select
        f"""
        SELECT kncs.knowledge_node_id, {", ".join(cs_cols)}
        FROM knowledge_node_agentic_sessions kncs
        JOIN agentic_sessions asx ON asx.id = kncs.agentic_session_id
        WHERE kncs.knowledge_node_id IN ({placeholders})
          AND {agentic_session_eligible_sql("asx", include_excluded=include_excluded)}
        ORDER BY asx.start_time DESC, asx.id DESC
        """,
        list(node_ids),
    ).fetchall()
    for row in cs_rows:
        kn_id = row[0]
        asx_id = row[1]
        harness = row[2]
        start_time = row[3]
        cwd = row[4]
        branch = row[5]
        fields = dict(zip(optional_agentic, row[6:], strict=False))
        session_id = fields.get("session_id", "")
        segment_index = fields.get("segment_index", 0)
        summary_text = fields.get("summary_text")
        source_file = fields.get("source_file")
        end_time = fields.get("end_time", start_time)
        d = details.get(kn_id)
        if d is None:
            continue
        prefix = "claude" if harness == "claude-code" else harness
        ref = f"{prefix}-{asx_id}"
        d["linked_source_ids"].append(ref)
        d["evidence_packets"].append(
            make_agentic_packet(
                row_id=asx_id,
                harness=harness,
                session_id=str(session_id) if session_id is not None else "",
                segment_index=int(segment_index or 0),
                timestamp_ms=end_time or start_time or 0,
                summary_text=str(summary_text) if summary_text is not None else None,
                source_path=str(source_file) if source_file is not None else None,
                ref=ref,
            )
        )
        if not d["cwd"] and cwd:
            d["cwd"] = cwd
        if not d["git_branch"] and branch:
            d["git_branch"] = branch
        if start_time and start_time > d["captured_at"]:
            d["captured_at"] = start_time

    if _column_exists(conn, "memory_chunks", "id"):
        mem_rows = conn.execute(
            f"""
            SELECT knmc.knowledge_node_id, mc.id, mc.heading_path, mc.content,
                   mr.created_at, md.repository, md.source_path
            FROM knowledge_node_memory_chunks knmc
            JOIN memory_chunks mc ON mc.id = knmc.memory_chunk_id
            JOIN memory_revisions mr ON mr.id = mc.revision_id
            JOIN memory_documents md ON md.id = mr.document_id
            WHERE knmc.knowledge_node_id IN ({placeholders})
              AND md.state = 'active'
              AND md.active_revision_id = mr.id
            ORDER BY mr.created_at DESC
            """,
            list(node_ids),
        ).fetchall()
        for row in mem_rows:
            kn_id = row[0]
            chunk_id = row[1]
            heading = row[2]
            content = row[3]
            created_at = row[4]
            repository = row[5]
            source_path = row[6]
            d = details.get(kn_id)
            if d is None:
                continue
            ref = f"memory-{chunk_id}"
            d["linked_source_ids"].append(ref)
            d["evidence_packets"].append(
                make_memory_packet(
                    chunk_id=chunk_id,
                    timestamp_ms=created_at or 0,
                    heading=heading,
                    content=content,
                    repository=repository,
                    source_path=source_path,
                )
            )
            if created_at and created_at > d["captured_at"]:
                d["captured_at"] = created_at

    # Hydrate type-bucketed entity names so the RAG renderer can surface them
    # as a structural `Entities:` line above the truncatable `Detail:` block.
    # The window function caps each node at 20 names (defends against
    # pathological enrichments) and `substr(..., 1, 200)` caps each name
    # (the schema has no length limit on entities.name).
    #
    # The outer `ORDER BY` is load-bearing: the inner `ROW_NUMBER() OVER`
    # only orders rows *within each partition*. Without an explicit outer
    # ORDER BY, SQLite is free to return rows in any plan-dependent order,
    # which would make the rendered `Entities:` line non-deterministic
    # across runs (and flake any test that pins token order). Sorting by
    # (knowledge_node_id, type, name) gives a stable, alphabetical surface.
    type_placeholders = ",".join("?" for _ in IDENTIFIER_ENTITY_TYPES)
    ent_rows = conn.execute(  # nosemgrep
        f"""
        SELECT knowledge_node_id, type, name FROM (
          SELECT
            kne.knowledge_node_id AS knowledge_node_id,
            ent.type AS type,
            substr(ent.name, 1, 200) AS name,
            ROW_NUMBER() OVER (
              PARTITION BY kne.knowledge_node_id
              ORDER BY ent.type, ent.name
            ) AS rn
          FROM knowledge_node_entities kne
          JOIN entities ent ON ent.id = kne.entity_id
          WHERE kne.knowledge_node_id IN ({placeholders})
            AND ent.type IN ({type_placeholders})
        )
        WHERE rn <= 20
        ORDER BY knowledge_node_id, type, name
        """,
        [*node_ids, *IDENTIFIER_ENTITY_TYPES],
    ).fetchall()
    for kn_id, etype, ename in ent_rows:
        d = details.get(kn_id)
        if d is None:
            continue
        d["entities"].setdefault(etype, []).append(ename)

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


def _recency_multiplier(age_ms: float, half_life_days: float, floor: float) -> float:
    """Exponential decay from 1.0 (fresh) toward ``floor`` (ancient).

    The floor keeps old-but-relevant nodes retrievable — recency is a prior,
    not a filter.
    """
    half_life_ms = half_life_days * 86_400_000.0
    if half_life_ms <= 0:
        return 1.0
    decay = 0.5 ** (max(0.0, age_ms) / half_life_ms)
    return floor + (1.0 - floor) * decay


def _apply_recency(
    conn: sqlite3.Connection,
    scored: list[tuple[int, float]],
    t: Tuning,
    *,
    now_ms: int | None = None,
) -> list[tuple[int, float]]:
    """Blend a recency prior into relevance scores (before MMR selection).

    Uses ``knowledge_nodes.created_at`` — a cheap, always-present proxy for
    when the underlying activity happened. Disabled (identity) when
    ``recency_half_life_days`` is 0. Nodes with no ``created_at`` row keep
    their score untouched.
    """
    if t.recency_half_life_days <= 0 or not scored:
        return scored
    ids = [nid for nid, _ in scored]
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = conn.execute(  # nosemgrep
            f"SELECT id, created_at FROM knowledge_nodes WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    except sqlite3.OperationalError:
        return scored
    created = {nid: ts for nid, ts in rows}
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    out: list[tuple[int, float]] = []
    for nid, score in scored:
        ts = created.get(nid)
        if ts:
            score *= _recency_multiplier(now - ts, t.recency_half_life_days, t.recency_floor)
        out.append((nid, score))
    return out


def _mmr(
    scored: Sequence[tuple[int, float]],
    vecs: dict[int, list[float]],
    k: int,
    mmr_lambda: float = MMR_LAMBDA,
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
            mmr = mmr_lambda * score - (1.0 - mmr_lambda) * diversity
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
            design_decisions=[],
            linked_event_ids=[],
            linked_source_ids=[],
            evidence=[],
            entities={},
            confidence={},
        )
    packets = attach_retrieval_scores(list(detail.get("evidence_packets") or []), score)
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
        intent=str(detail.get("intent") or ""),
        commands_raw=str(detail.get("commands_raw") or ""),
        key_decisions=list(detail.get("key_decisions") or []),
        problems_encountered=list(detail.get("problems_encountered") or []),
        design_decisions=list(detail.get("design_decisions") or []),
        linked_event_ids=list(detail["linked_event_ids"]),
        linked_source_ids=sorted(detail.get("linked_source_ids") or []),
        evidence=packets,
        entities=dict(detail.get("entities") or {}),
    )


def _parse_content(content_str: str | None) -> dict:
    """Parse a knowledge_node content JSON blob, returning ``{}`` on any junk."""
    if not content_str:
        return {}
    try:
        payload = json.loads(content_str)
    except json.JSONDecodeError, TypeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _string_list(raw: object) -> list[str]:
    """Coerce an enrichment list field (key_decisions, problems_encountered)."""
    if not isinstance(raw, list):
        return []
    return [str(entry) for entry in raw if isinstance(entry, (str, int, float)) and str(entry)]


def _dict_list(raw: object) -> list[dict]:
    """Coerce design_decisions entries — each expected to be a dict with
    `considered`, `chosen`, `reason` keys (enforced by validate_enrichment_data;
    older pre-issue-#98 nodes simply lack the key)."""
    if not isinstance(raw, list):
        return []
    return [entry for entry in raw if isinstance(entry, dict)]


def _extract_summary(content_str: str | None) -> str:
    return str(_parse_content(content_str).get("summary") or "")


def _extract_design_decisions(content_str: str | None) -> list[dict]:
    return _dict_list(_parse_content(content_str).get("design_decisions"))


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
