from unittest.mock import patch

from hippo_brain.bench.corpus import CorpusEntry
from hippo_brain.bench.enrich_call import CallResult
from hippo_brain.bench.runner import run_self_consistency_pass


@patch("hippo_brain.bench.runner.call_embedding")
@patch("hippo_brain.bench.runner.call_enrichment")
def test_self_consistency_pass_embeds_each_output(mock_call, mock_embed):
    mock_call.return_value = CallResult(
        raw_output='{"summary": "ok", "intent": "x", "outcome": "success", "entities": {}}',
        ttft_ms=None,
        total_ms=50,
        timeout=False,
    )
    mock_embed.return_value = [1.0, 0.0, 0.0]
    entries = [
        CorpusEntry(event_id="e1", source="shell", redacted_content="ls"),
        CorpusEntry(event_id="e2", source="shell", redacted_content="pwd"),
    ]
    attempts, per_event_vectors = run_self_consistency_pass(
        base_url="http://x",
        model="m1",
        entries=entries,
        runs_per_event=3,
        embedding_model="nomic",
        timeout_sec=10,
        metrics_snapshot=lambda: {"lmstudio_rss_mb": 0.0},
        temperature=0.7,
    )
    assert len(attempts) == 2 * 3
    assert len(per_event_vectors) == 2
    assert all(len(v) == 3 for v in per_event_vectors)
