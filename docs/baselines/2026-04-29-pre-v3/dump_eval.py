"""Re-run hippo-eval retrieval-only and dump per-question metrics as JSON."""
import asyncio, json, sys
from pathlib import Path
sys.path.insert(0, "/Users/carpenter/projects/hippo/brain/src")
from hippo_brain.evaluation import (
    load_questions, run_benchmark, _corpus_stats,
)
from hippo_brain.client import LMStudioClient
from hippo_brain.mcp import _load_config
from hippo_brain.vector_store import open_conn

cfg = _load_config()
qs = load_questions("/Users/carpenter/projects/hippo/brain/tests/eval_questions.json")
conn = open_conn(cfg["db_path"])
lm = LMStudioClient(base_url=cfg["lmstudio_base_url"])

report = asyncio.run(run_benchmark(
    questions=qs, conn=conn, lm_client=lm,
    embedding_model=cfg["embedding_model"], query_model=cfg["query_model"],
    mode="hybrid", limit=10,
    run_synthesis=False, run_judge=False,
    corpus_stats=_corpus_stats(conn),
))
out = []
for r in report.results:
    out.append({
        "id": r.q.id,
        "intent": r.q.intent,
        "source_bias": r.q.source_bias,
        "n_relevant": len(r.q.relevant_knowledge_node_uuids),
        "recall_at_k": None if r.recall_at_k != r.recall_at_k else r.recall_at_k,  # NaN-safe
        "mrr": None if r.mrr != r.mrr else r.mrr,
        "ndcg_at_k": None if r.ndcg_at_k != r.ndcg_at_k else r.ndcg_at_k,
        "source_diversity": r.source_diversity,
        "coverage_gap_score": r.coverage_gap_score,
        "elapsed_ms": r.elapsed_ms,
        "n_hits": len(r.retrieval),
        "top_uuids": [h.uuid for h in r.retrieval[:5]],
    })
Path("/tmp/hippo-eval-panel/per_question.json").write_text(json.dumps(out, indent=2))
print(f"wrote {len(out)} rows")
