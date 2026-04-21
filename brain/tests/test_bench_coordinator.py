from unittest.mock import MagicMock, patch

from hippo_brain.bench.coordinator import run_one_model


@patch("hippo_brain.bench.coordinator.lms")
@patch("hippo_brain.bench.coordinator.MetricsSampler")
@patch("hippo_brain.bench.coordinator.run_self_consistency_pass")
@patch("hippo_brain.bench.coordinator.run_model_main_pass")
@patch("hippo_brain.bench.coordinator.call_enrichment")
@patch("hippo_brain.bench.coordinator.time.sleep", lambda _: None)
def test_run_one_model_lifecycle(mock_warmup, mock_main, mock_sc, mock_sampler_cls, mock_lms):
    mock_lms.list_loaded.return_value = [{"identifier": "m1"}]
    mock_sampler = MagicMock()
    mock_sampler.peak.return_value = {
        "lmstudio_rss_mb": 100.0,
        "load_avg_1m": 1.0,
        "mem_free_mb": 1000.0,
        "lmstudio_cpu_pct": 50.0,
    }
    mock_sampler.latest.return_value = MagicMock(
        lmstudio_rss_mb=100.0,
        lmstudio_cpu_pct=50.0,
        load_avg_1m=1.0,
        mem_free_mb=1000.0,
    )
    mock_sampler._sample_once.return_value = MagicMock(load_avg_1m=0.5)
    mock_sampler_cls.return_value = mock_sampler
    mock_main.return_value = []
    mock_sc.return_value = ([], [])

    result = run_one_model(
        model="m1",
        base_url="http://x/v1",
        entries=[],
        sc_entries=[],
        runs_per_event=3,
        embedding_model="nomic",
        timeout_sec=10,
        warmup_calls=2,
        cooldown_max_sec=0,
        run_id="r",
    )
    assert result.model == "m1"
    mock_lms.unload_all.assert_called_once()
    assert mock_lms.load.call_args.args[0] == "m1"
    mock_lms.unload.assert_called_once()
    assert mock_warmup.call_count == 2


@patch("hippo_brain.bench.coordinator.lms")
@patch("hippo_brain.bench.coordinator.MetricsSampler")
@patch("hippo_brain.bench.coordinator.run_self_consistency_pass")
@patch("hippo_brain.bench.coordinator.run_model_main_pass")
@patch("hippo_brain.bench.coordinator.call_enrichment")
@patch("hippo_brain.bench.coordinator.time.sleep", lambda _: None)
def test_run_one_model_unloads_on_exception(
    mock_warmup, mock_main, mock_sc, mock_sampler_cls, mock_lms
):
    mock_sampler = MagicMock()
    mock_sampler.peak.return_value = {}
    mock_sampler.latest.return_value = None
    mock_sampler._sample_once.return_value = MagicMock(load_avg_1m=0.5)
    mock_sampler_cls.return_value = mock_sampler
    mock_lms.load.side_effect = None
    mock_main.side_effect = RuntimeError("boom")

    try:
        run_one_model(
            model="m1",
            base_url="http://x/v1",
            entries=[],
            sc_entries=[],
            runs_per_event=1,
            embedding_model="nomic",
            timeout_sec=10,
            warmup_calls=0,
            cooldown_max_sec=0,
            run_id="r",
        )
    except RuntimeError:
        pass

    mock_lms.unload.assert_called_with("m1")
