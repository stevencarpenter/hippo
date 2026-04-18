"""Retrieval + synthesis evaluation harness.

Exposes pure metric functions and a ``hippo-eval`` CLI. See the design spec
at ``docs/superpowers/specs/2026-04-17-eval-harness-design.md``.

The CLI runs a labeled Q/A set against the live hippo corpus via
:func:`hippo_brain.retrieval.search` and :func:`hippo_brain.rag.ask`, and
emits a Markdown scorecard.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import re
import sqlite3
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from hippo_brain.retrieval import Filters, SearchResult
from hippo_brain.retrieval import search as retrieval_search


# ---------------------------------------------------------------------------
# Quantitative metrics (pure functions)
# ---------------------------------------------------------------------------


def recall_at_k(retrieved: Sequence[str], relevant: Iterable[str], k: int) -> float:
    """Fraction of ``relevant`` uuids found in ``retrieved[:k]``.

    Returns ``nan`` if ``relevant`` is empty (metric undefined).
    """
    rel = {r for r in relevant if r}
    if not rel:
        return float("nan")
    if k <= 0:
        return 0.0
    hits = sum(1 for uid in retrieved[:k] if uid in rel)
    return hits / len(rel)


def mrr(retrieved: Sequence[str], relevant: Iterable[str]) -> float:
    """Reciprocal rank of the first relevant hit.

    Returns ``nan`` if ``relevant`` is empty; ``0.0`` if no relevant uuid
    appears in ``retrieved``.
    """
    rel = {r for r in relevant if r}
    if not rel:
        return float("nan")
    for rank, uid in enumerate(retrieved, 1):
        if uid in rel:
            return 1.0 / rank
    return 0.0


def ndcg_at_k(
    retrieved: Sequence[str],
    relevance: dict[str, float],
    k: int,
) -> float:
    """Normalized DCG at ``k``.

    ``relevance`` maps uuid → graded relevance (0 for anything not listed).
    Returns ``nan`` if no graded entries exist.
    """
    if not relevance or k <= 0:
        return float("nan")
    gains = [relevance.get(uid, 0.0) for uid in retrieved[:k]]
    dcg = sum(g / math.log2(i + 2) for i, g in enumerate(gains))
    ideal = sorted(relevance.values(), reverse=True)[:k]
    idcg = sum(g / math.log2(i + 2) for i, g in enumerate(ideal))
    if idcg == 0.0:
        return float("nan")
    return dcg / idcg


def source_diversity(sources_per_hit: Sequence[Sequence[str]]) -> float:
    """Shannon entropy of source-type occurrences, normalized to [0, 1]."""
    counts: dict[str, int] = {}
    total = 0
    for sources in sources_per_hit:
        for s in sources:
            if not s:
                continue
            counts[s] = counts.get(s, 0) + 1
            total += 1
    if total == 0 or len(counts) <= 1:
        return 0.0
    entropy = -sum((c / total) * math.log2(c / total) for c in counts.values())
    max_entropy = math.log2(len(counts))
    return entropy / max_entropy if max_entropy > 0 else 0.0


def near_duplicate_density(vectors: Sequence[Sequence[float]]) -> float:
    """Mean pairwise cosine similarity across returned hits.

    Higher = more near-duplicates in the result set = worse.
    """
    vecs = [list(v) for v in vectors if v]
    if len(vecs) < 2:
        return float("nan")
    sims: list[float] = []
    for i in range(len(vecs)):
        for j in range(i + 1, len(vecs)):
            sims.append(_cosine(vecs[i], vecs[j]))
    if not sims:
        return float("nan")
    return sum(sims) / len(sims)


def coverage_gap_score(scores: Sequence[float], threshold: float = 0.5) -> float:
    """Fraction of top-K scores that fall below ``threshold``."""
    if not scores:
        return 1.0
    weak = sum(1 for s in scores if s < threshold)
    return weak / len(scores)


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
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
# Qualitative metrics
# ---------------------------------------------------------------------------


_GROUNDEDNESS_PROMPT = (
    "You are scoring whether an ANSWER is grounded in the SOURCES. "
    "Score from 0.0 (the answer invents facts not in the sources) to 1.0 "
    "(every factual claim in the answer is supported by at least one "
    "source). Output JUST a single decimal number between 0 and 1 on the "
    "first line — nothing else."
)


async def groundedness(
    answer: str,
    sources: Sequence[dict],
    lm_client: Any,
    model: str,
) -> float:
    """LLM-judge: does every factual claim in ``answer`` appear in ``sources``?

    Returns ``nan`` on any error (no LLM, parse failure, etc.).
    """
    if not answer or not sources or lm_client is None or not model:
        return float("nan")
    source_blob = "\n\n".join(
        f"[{i + 1}] {s.get('summary', '')} — {s.get('embed_text', '')}"
        for i, s in enumerate(sources)
    )
    messages = [
        {"role": "system", "content": _GROUNDEDNESS_PROMPT},
        {
            "role": "user",
            "content": f"ANSWER:\n{answer}\n\nSOURCES:\n{source_blob}",
        },
    ]
    try:
        raw = await lm_client.chat(messages, model=model, temperature=0.0, max_tokens=32)
    except Exception:
        return float("nan")
    if not raw:
        return float("nan")
    first = raw.strip().splitlines()[0]
    match = re.search(r"([01](?:\.\d+)?|0?\.\d+)", first)
    if not match:
        return float("nan")
    try:
        val = float(match.group(1))
    except ValueError:
        return float("nan")
    return max(0.0, min(1.0, val))


def summary_coherence(summary: str, entities: Sequence[str]) -> bool:
    """True if ``summary`` mentions at least one entity (case-insensitive)."""
    if not summary or not entities:
        return False
    lowered = summary.lower()
    return any(e and e.lower() in lowered for e in entities)


def keyword_match(answer: str, keywords: Sequence[str]) -> bool:
    """True if any acceptable keyword appears in ``answer`` (case-insensitive)."""
    if not answer or not keywords:
        return False
    lowered = answer.lower()
    return any(k and k.lower() in lowered for k in keywords)


def embedding_cohesion(
    conn: sqlite3.Connection,
    project: str,
    sample: int = 200,
) -> float:
    """Ratio of in-project mean cosine to random-pair mean cosine.

    Ratios > 1 mean nodes sharing a project cluster tighter than background.
    Returns ``nan`` if either pool is too small or ``knowledge_vectors``
    isn't loaded.
    """
    try:
        rows = conn.execute(
            """
            SELECT DISTINCT kv.knowledge_node_id, vec_to_json(kv.vec_knowledge)
            FROM knowledge_vectors kv
            JOIN knowledge_node_events kne ON kne.knowledge_node_id = kv.knowledge_node_id
            JOIN events e ON e.id = kne.event_id
            WHERE e.git_repo LIKE ? OR e.cwd LIKE ?
            LIMIT ?
            """,
            (f"%{project}%", f"%{project}%", sample),
        ).fetchall()
    except sqlite3.OperationalError:
        return float("nan")

    in_project = [_parse_vec(r[1]) for r in rows if r[1]]
    in_project = [v for v in in_project if v]
    if len(in_project) < 4:
        return float("nan")

    try:
        bg_rows = conn.execute(
            "SELECT vec_to_json(vec_knowledge) FROM knowledge_vectors LIMIT ?",
            (sample,),
        ).fetchall()
    except sqlite3.OperationalError:
        return float("nan")
    background = [_parse_vec(r[0]) for r in bg_rows if r[0]]
    background = [v for v in background if v]
    if len(background) < 4:
        return float("nan")

    in_mean = _pairwise_mean_cosine(in_project)
    bg_mean = _pairwise_mean_cosine(background)
    if bg_mean <= 0:
        return float("nan")
    return in_mean / bg_mean


def _parse_vec(blob: str | None) -> list[float]:
    if not blob:
        return []
    try:
        data = json.loads(blob)
    except json.JSONDecodeError, TypeError:
        return []
    if not isinstance(data, list):
        return []
    try:
        return [float(x) for x in data]
    except TypeError, ValueError:
        return []


def _pairwise_mean_cosine(vecs: Sequence[Sequence[float]], max_pairs: int = 2000) -> float:
    n = len(vecs)
    if n < 2:
        return 0.0
    pairs = [(i, j) for i in range(n) for j in range(i + 1, n)]
    if len(pairs) > max_pairs:
        rng = random.Random(1234)
        pairs = rng.sample(pairs, max_pairs)
    sims = [_cosine(vecs[i], vecs[j]) for i, j in pairs]
    return sum(sims) / len(sims) if sims else 0.0


# ---------------------------------------------------------------------------
# Source-type derivation
# ---------------------------------------------------------------------------


def _lookup_enrichment_models(conn: sqlite3.Connection | None, uuids: Sequence[str]) -> list[str]:
    """Return the distinct enrichment_model values seen across ``uuids``.

    Empty list when the column or table is missing, or when ``uuids`` is empty.
    Used for per-vintage stratification (corpus-analyst #12).
    """
    if conn is None or not uuids:
        return []
    placeholders = ",".join("?" for _ in uuids)
    sql = (
        "SELECT DISTINCT enrichment_model FROM knowledge_nodes "
        "WHERE uuid IN (" + placeholders + ") AND enrichment_model IS NOT NULL"
    )
    try:
        rows = conn.execute(sql, list(uuids)).fetchall()  # nosemgrep
    except sqlite3.OperationalError:
        return []
    return sorted({str(r[0]) for r in rows if r[0]})


def derive_sources(conn: sqlite3.Connection | None, uuids: Sequence[str]) -> dict[str, list[str]]:
    """Return ``uuid -> [source_types]`` (shell/claude/browser/workflow).

    Returns an empty mapping when ``conn`` is ``None`` or uuids is empty.
    """
    if conn is None or not uuids:
        return {}
    placeholders = ",".join("?" for _ in uuids)
    # SQL is composed of a fixed literal + a run of `?` placeholders whose
    # count matches `uuids`; all user values are bound parameters.
    sql_lookup = "SELECT id, uuid FROM knowledge_nodes WHERE uuid IN (" + placeholders + ")"
    try:
        rows = conn.execute(sql_lookup, list(uuids)).fetchall()  # nosemgrep
    except sqlite3.OperationalError:
        return {}
    id_to_uuid = {row[0]: row[1] for row in rows}
    if not id_to_uuid:
        return {}
    ids = list(id_to_uuid.keys())
    id_ph = ",".join("?" for _ in ids)
    out: dict[str, list[str]] = {uid: [] for uid in id_to_uuid.values()}

    # Fixed allow-list of (pre-composed SQL, label) pairs — no user input
    # reaches the SQL except via bound parameters below.
    link_queries: list[tuple[str, str]] = [
        (
            "SELECT DISTINCT knowledge_node_id FROM knowledge_node_events "
            "WHERE knowledge_node_id IN (" + id_ph + ")",
            "shell",
        ),
        (
            "SELECT DISTINCT knowledge_node_id FROM knowledge_node_claude_sessions "
            "WHERE knowledge_node_id IN (" + id_ph + ")",
            "claude",
        ),
        (
            "SELECT DISTINCT knowledge_node_id FROM knowledge_node_browser_events "
            "WHERE knowledge_node_id IN (" + id_ph + ")",
            "browser",
        ),
        (
            "SELECT DISTINCT knowledge_node_id FROM knowledge_node_workflow_runs "
            "WHERE knowledge_node_id IN (" + id_ph + ")",
            "workflow",
        ),
    ]
    for sql_link, label in link_queries:
        try:
            found = conn.execute(sql_link, ids).fetchall()  # nosemgrep
        except sqlite3.OperationalError:
            continue
        for (nid,) in found:
            uid = id_to_uuid.get(nid)
            if uid:
                out[uid].append(label)
    return out


# ---------------------------------------------------------------------------
# Harness data classes
# ---------------------------------------------------------------------------


@dataclass
class Question:
    id: str
    question: str
    intent: str = ""
    relevant_knowledge_node_uuids: list[str] = field(default_factory=list)
    acceptable_answer_keywords: list[str] = field(default_factory=list)
    source_bias: str = "mixed"


@dataclass
class QuestionResult:
    q: Question
    retrieval: list[SearchResult]
    answer: str | None
    degraded: bool
    error: str | None
    recall_at_k: float
    mrr: float
    ndcg_at_k: float
    source_diversity: float
    near_duplicate_density: float
    coverage_gap_score: float
    groundedness: float
    keyword_hit: bool
    elapsed_ms: float
    enrichment_models: list[str] = field(default_factory=list)


@dataclass
class ScoreReport:
    results: list[QuestionResult]
    config: dict
    corpus: dict
    started_at: float
    finished_at: float


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def load_questions(path: str | Path) -> list[Question]:
    data = json.loads(Path(path).read_text())
    raw = data.get("questions", data) if isinstance(data, dict) else data
    questions: list[Question] = []
    for q in raw:
        questions.append(
            Question(
                id=str(q.get("id", "")),
                question=str(q.get("question", "")),
                intent=str(q.get("intent", "")),
                relevant_knowledge_node_uuids=list(q.get("relevant_knowledge_node_uuids", [])),
                acceptable_answer_keywords=list(q.get("acceptable_answer_keywords", [])),
                source_bias=str(q.get("source_bias", "mixed")),
            )
        )
    return questions


# ---------------------------------------------------------------------------
# Harness core
# ---------------------------------------------------------------------------


async def score_question(
    q: Question,
    *,
    conn: sqlite3.Connection,
    lm_client: Any | None,
    embedding_model: str,
    query_model: str,
    mode: str,
    limit: int,
    run_synthesis: bool,
    run_judge: bool,
) -> QuestionResult:
    """Score a single question end-to-end."""
    t0 = time.monotonic()
    relevant = set(q.relevant_knowledge_node_uuids)
    relevance_graded = {uid: 1.0 for uid in relevant}

    query_vec: list[float] | None = None
    if lm_client is not None and embedding_model:
        try:
            vecs = await lm_client.embed([q.question], model=embedding_model)
            if vecs:
                query_vec = list(vecs[0])
        except Exception:
            query_vec = None

    try:
        hits = retrieval_search(
            conn, q.question, query_vec, filters=Filters(), mode=mode, limit=limit
        )
    except Exception as e:
        elapsed = (time.monotonic() - t0) * 1000
        return QuestionResult(
            q=q,
            retrieval=[],
            answer=None,
            degraded=True,
            error=f"retrieval: {type(e).__name__}: {e}",
            recall_at_k=float("nan"),
            mrr=float("nan"),
            ndcg_at_k=float("nan"),
            source_diversity=0.0,
            near_duplicate_density=float("nan"),
            coverage_gap_score=1.0,
            groundedness=float("nan"),
            keyword_hit=False,
            elapsed_ms=elapsed,
        )

    retrieved_uuids = [h.uuid for h in hits]
    scores = [h.score for h in hits]
    source_map = derive_sources(conn, retrieved_uuids)
    sources_per_hit = [source_map.get(uid, []) for uid in retrieved_uuids]
    enrichment_models = _lookup_enrichment_models(conn, retrieved_uuids)

    answer: str | None = None
    degraded = False
    error: str | None = None
    ground = float("nan")

    if run_synthesis and lm_client is not None and query_model:
        try:
            from hippo_brain.rag import ask as rag_ask

            res = await rag_ask(
                q.question,
                lm_client,
                conn,
                query_model,
                embedding_model,
                limit=limit,
                skip_preflight=True,
                filters=Filters(),
                mode=mode,
                conn=conn,
            )
            answer = res.get("answer")
            degraded = bool(res.get("degraded"))
            error = res.get("error")
            if run_judge and answer and not degraded:
                ground = await groundedness(
                    answer,
                    res.get("sources", []),
                    lm_client,
                    query_model,
                )
        except Exception as e:
            degraded = True
            error = f"ask: {type(e).__name__}: {e}"

    elapsed = (time.monotonic() - t0) * 1000
    return QuestionResult(
        q=q,
        retrieval=hits,
        answer=answer,
        degraded=degraded,
        error=error,
        recall_at_k=recall_at_k(retrieved_uuids, relevant, limit),
        mrr=mrr(retrieved_uuids, relevant),
        ndcg_at_k=ndcg_at_k(retrieved_uuids, relevance_graded, limit),
        source_diversity=source_diversity(sources_per_hit),
        near_duplicate_density=float("nan"),
        coverage_gap_score=coverage_gap_score(scores),
        groundedness=ground,
        keyword_hit=keyword_match(answer or "", q.acceptable_answer_keywords),
        elapsed_ms=elapsed,
        enrichment_models=enrichment_models,
    )


async def run_benchmark(
    *,
    questions: Sequence[Question],
    conn: sqlite3.Connection,
    lm_client: Any | None,
    embedding_model: str,
    query_model: str,
    mode: str = "hybrid",
    limit: int = 10,
    run_synthesis: bool = True,
    run_judge: bool = True,
    corpus_stats: dict | None = None,
) -> ScoreReport:
    """Score every question and return a ``ScoreReport``."""
    started = time.time()
    results: list[QuestionResult] = []
    for q in questions:
        results.append(
            await score_question(
                q,
                conn=conn,
                lm_client=lm_client,
                embedding_model=embedding_model,
                query_model=query_model,
                mode=mode,
                limit=limit,
                run_synthesis=run_synthesis,
                run_judge=run_judge,
            )
        )
    return ScoreReport(
        results=results,
        config={
            "mode": mode,
            "limit": limit,
            "run_synthesis": run_synthesis,
            "run_judge": run_judge,
            "embedding_model": embedding_model,
            "query_model": query_model,
        },
        corpus=corpus_stats or {},
        started_at=started,
        finished_at=time.time(),
    )


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _fmt(x: float) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x:.3f}"


def _mean(values: Iterable[float]) -> float:
    clean = [v for v in values if isinstance(v, float) and not math.isnan(v)]
    return statistics.mean(clean) if clean else float("nan")


def _median(values: Iterable[float]) -> float:
    clean = [v for v in values if isinstance(v, float) and not math.isnan(v)]
    return statistics.median(clean) if clean else float("nan")


def _percentile(values: Sequence[float], p: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, int(round(p * (len(ordered) - 1)))))
    return ordered[idx]


def render_markdown(report: ScoreReport) -> str:
    lines: list[str] = []
    lines.append("# Hippo Evaluation Scorecard")
    lines.append("")
    lines.append(f"- Run at: {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(report.started_at))}")
    lines.append(f"- Duration: {report.finished_at - report.started_at:.1f}s")
    lines.append(f"- Mode: `{report.config['mode']}`  |  Limit: {report.config['limit']}")
    lines.append(
        f"- Synthesis: {'on' if report.config['run_synthesis'] else 'off'}  |  "
        f"Judge: {'on' if report.config['run_judge'] else 'off'}"
    )
    lines.append(f"- Embedding model: `{report.config['embedding_model'] or '(none)'}`")
    lines.append(f"- Query model: `{report.config['query_model'] or '(none)'}`")
    if report.corpus:
        lines.append("")
        lines.append("## Corpus")
        for k, v in report.corpus.items():
            lines.append(f"- **{k}**: {v}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| Metric | Mean | Median |")
    lines.append("|---|---:|---:|")
    summary_metrics: list[tuple[str, list[float]]] = [
        ("recall@k", [r.recall_at_k for r in report.results]),
        ("mrr", [r.mrr for r in report.results]),
        ("ndcg@k", [r.ndcg_at_k for r in report.results]),
        ("source_diversity", [r.source_diversity for r in report.results]),
        ("coverage_gap", [r.coverage_gap_score for r in report.results]),
        ("groundedness", [r.groundedness for r in report.results]),
        ("keyword_hit_rate", [1.0 if r.keyword_hit else 0.0 for r in report.results]),
    ]
    for name, vals in summary_metrics:
        lines.append(f"| {name} | {_fmt(_mean(vals))} | {_fmt(_median(vals))} |")
    latencies = [r.elapsed_ms for r in report.results if r.elapsed_ms]
    if latencies:
        p50 = statistics.median(latencies)
        p95 = _percentile(latencies, 0.95)
        lines.append(f"| latency_ms_p50 | {p50:.1f} | — |")
        lines.append(f"| latency_ms_p95 | {p95:.1f} | — |")
    lines.append("")

    model_buckets: dict[str, list[QuestionResult]] = {}
    for r in report.results:
        if not r.enrichment_models:
            model_buckets.setdefault("(unknown)", []).append(r)
            continue
        for m in r.enrichment_models:
            model_buckets.setdefault(m, []).append(r)
    if model_buckets and any(m != "(unknown)" for m in model_buckets):
        lines.append("## Stratified by enrichment_model")
        lines.append("")
        lines.append("| model | n | mean recall@k | mean gap | mean ground |")
        lines.append("|---|---:|---:|---:|---:|")
        for model, bucket in sorted(model_buckets.items()):
            lines.append(
                f"| `{model}` | {len(bucket)} | "
                f"{_fmt(_mean(r.recall_at_k for r in bucket))} | "
                f"{_fmt(_mean(r.coverage_gap_score for r in bucket))} | "
                f"{_fmt(_mean(r.groundedness for r in bucket))} |"
            )
        lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append(
        "- **FTS5 phrase-wrap (R-03)**: lexical mode wraps multi-word queries in "
        "a single phrase, so recall on long natural-language questions is "
        "pathologically low — not a ranking bug."
    )
    lines.append(
        "- **RRF normalization (R-07)**: hybrid scores are normalized to top=1.0 "
        "per query. Absolute score thresholds are not comparable across queries."
    )
    lines.append(
        "- **vec0 brute-force (R-02)**: there is no ANN index on "
        "`knowledge_vectors`. Latency is O(N); hybrid≥LanceDB will stop holding "
        "once the corpus grows well past ~2K nodes."
    )
    lines.append(
        "- **events.git_repo is NULL** across the live v5 corpus, so project "
        "filtering silently falls back to cwd-prefix. Low recall on "
        "project-filtered queries is a data bug, not a retrieval bug."
    )
    lines.append(
        "- **Branch corpus coverage**: on the `postgres` branch only ~1.7% of "
        "events have knowledge-node coverage (vs ~13.4% on main). Labels are "
        "drawn from the main-hippo corpus until backfill runs."
    )
    lines.append("")

    lines.append("## Per-question")
    lines.append("")
    lines.append("| id | intent | top | gap | div | ground | kw | degraded |")
    lines.append("|---|---|---:|---:|---:|---:|:---:|:---:|")
    for r in report.results:
        top = r.retrieval[0].score if r.retrieval else float("nan")
        lines.append(
            f"| {r.q.id} | {r.q.intent or '—'} | {_fmt(top)} | "
            f"{_fmt(r.coverage_gap_score)} | {_fmt(r.source_diversity)} | "
            f"{_fmt(r.groundedness)} | {'✓' if r.keyword_hit else '✗'} | "
            f"{'⚠' if r.degraded else ''} |"
        )
    lines.append("")

    lines.append("## Coverage gaps (weakest 10 questions)")
    lines.append("")
    weakest = sorted(report.results, key=lambda r: -r.coverage_gap_score)[:10]
    for r in weakest:
        lines.append(f"- `{r.q.id}` (gap={_fmt(r.coverage_gap_score)}): {r.q.question}")
    lines.append("")

    errors = [r for r in report.results if r.error]
    if errors:
        lines.append("## Errors")
        lines.append("")
        for r in errors:
            lines.append(f"- `{r.q.id}`: {r.error}")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


_DEFAULT_QUESTIONS = Path(__file__).parent.parent.parent / "tests" / "eval_questions.json"


def _corpus_stats(conn: sqlite3.Connection) -> dict:
    stats: dict[str, Any] = {}
    for name, sql in (
        ("knowledge_nodes", "SELECT COUNT(*) FROM knowledge_nodes"),
        ("events", "SELECT COUNT(*) FROM events"),
        ("claude_sessions", "SELECT COUNT(*) FROM claude_sessions"),
    ):
        try:
            stats[name] = conn.execute(sql).fetchone()[0]
        except sqlite3.OperationalError:
            stats[name] = "n/a"
    return stats


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="hippo-eval", description="Hippo retrieval eval harness")
    p.add_argument("--questions", default=str(_DEFAULT_QUESTIONS))
    p.add_argument("--out", default="")
    p.add_argument("--mode", default="hybrid", choices=["hybrid", "semantic", "lexical", "recent"])
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--no-synthesis", action="store_true")
    p.add_argument("--no-judge", action="store_true")
    p.add_argument("--subset", default="", help="Comma-separated question ids")
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    from hippo_brain.client import LMStudioClient
    from hippo_brain.mcp import _load_config
    from hippo_brain.vector_store import open_conn

    cfg = _load_config()
    questions = load_questions(args.questions)
    if args.subset:
        wanted = {s.strip() for s in args.subset.split(",") if s.strip()}
        questions = [q for q in questions if q.id in wanted]
        if not questions:
            print(f"No questions matched --subset {args.subset!r}", file=sys.stderr)
            return 2

    conn = open_conn(cfg["db_path"])
    lm_client: Any | None = None
    try:
        lm_client = LMStudioClient(base_url=cfg["lmstudio_base_url"])
    except Exception as e:
        print(f"LM Studio client unavailable: {e}", file=sys.stderr)

    report = asyncio.run(
        run_benchmark(
            questions=questions,
            conn=conn,
            lm_client=lm_client,
            embedding_model=cfg["embedding_model"],
            query_model=cfg["query_model"],
            mode=args.mode,
            limit=args.limit,
            run_synthesis=not args.no_synthesis,
            run_judge=not args.no_judge,
            corpus_stats=_corpus_stats(conn),
        )
    )
    md = render_markdown(report)
    if args.out:
        Path(args.out).write_text(md)
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
