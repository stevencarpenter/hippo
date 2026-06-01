from unittest.mock import patch

from hippo_brain.bench.corpus import CorpusEntry
from hippo_brain.bench.enrich_call import CallResult
from hippo_brain.bench.runner import run_main_enrichment_pass, run_self_consistency_pass


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
        metrics_snapshot=lambda: {"inference_rss_mb": 0.0},
        temperature=0.7,
    )
    assert len(attempts) == 2 * 3
    assert len(per_event_vectors) == 2
    assert all(len(v) == 3 for v in per_event_vectors)


@patch("hippo_brain.bench.runner.call_enrichment")
def test_main_enrichment_pass_one_attempt_per_event(mock_call):
    """Cycle 1 RED: exactly one main-purpose attempt per corpus event."""
    mock_call.return_value = CallResult(
        raw_output='{"summary": "ok", "intent": "x", "outcome": "success", "entities": {}}',
        ttft_ms=None,
        total_ms=50,
        timeout=False,
    )
    entries = [
        CorpusEntry(event_id="e1", source="shell", redacted_content="ls"),
        CorpusEntry(event_id="e2", source="shell", redacted_content="pwd"),
        CorpusEntry(event_id="e3", source="shell", redacted_content="cargo test"),
    ]
    attempts = run_main_enrichment_pass(
        base_url="http://x",
        model="m1",
        entries=entries,
        timeout_sec=10,
        metrics_snapshot=lambda: {},
        temperature=0.0,
        run_id="run-test",
    )
    assert len(attempts) == len(entries), "one attempt per corpus event, no more"
    assert all(a.purpose == "main" for a in attempts), "every attempt must be purpose='main'"
    assert [a.event["event_id"] for a in attempts] == ["e1", "e2", "e3"]
    assert all(a.attempt_idx == 0 for a in attempts)
    assert mock_call.call_count == len(entries)


@patch("hippo_brain.bench.runner.call_enrichment")
def test_main_enrichment_pass_populates_gates(mock_call):
    """Cycle 1 RED: gates are computed and stored on each main-pass attempt."""
    mock_call.return_value = CallResult(
        raw_output='{"summary": "ok", "intent": "x", "outcome": "success", "entities": {}}',
        ttft_ms=None,
        total_ms=80,
        timeout=False,
    )
    entries = [CorpusEntry(event_id="e1", source="shell", redacted_content="echo hi")]
    attempts = run_main_enrichment_pass(
        base_url="http://x",
        model="m1",
        entries=entries,
        timeout_sec=10,
        metrics_snapshot=lambda: {},
        temperature=0.0,
    )
    assert len(attempts) == 1
    gates = attempts[0].gates
    assert "schema_valid" in gates
    assert "refusal_detected" in gates
    assert "echo_similarity" in gates
    assert attempts[0].parsed_output is not None


@patch("hippo_brain.bench.runner.call_enrichment")
def test_main_enrichment_pass_does_not_embed(mock_call):
    """Cycle 1 RED: main pass never calls call_embedding (no SC vectors needed)."""
    mock_call.return_value = CallResult(
        raw_output='{"summary": "ok", "intent": "x", "outcome": "success", "entities": {}}',
        ttft_ms=None,
        total_ms=50,
        timeout=False,
    )
    entries = [CorpusEntry(event_id="e1", source="shell", redacted_content="ls")]
    with patch("hippo_brain.bench.runner.call_embedding") as mock_embed:
        run_main_enrichment_pass(
            base_url="http://x",
            model="m1",
            entries=entries,
            timeout_sec=10,
            metrics_snapshot=lambda: {},
            temperature=0.0,
        )
        mock_embed.assert_not_called()
