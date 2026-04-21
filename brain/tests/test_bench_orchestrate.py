from unittest.mock import patch

from hippo_brain.bench.orchestrate import orchestrate_run


def test_orchestrate_dry_run_produces_manifest_only(tmp_path):
    fixture = tmp_path / "corpus-v1.jsonl"
    manifest = tmp_path / "corpus-v1.manifest.json"
    fixture.write_text("")
    manifest.write_text('{"corpus_content_hash": "sha256:empty", "corpus_version": "corpus-v1"}')
    out = tmp_path / "run.jsonl"

    with patch("hippo_brain.bench.orchestrate.run_all_preflight") as mock_pf:
        mock_pf.return_value = []
        result = orchestrate_run(
            candidate_models=[],
            corpus_version="corpus-v1",
            fixture_path=fixture,
            manifest_path=manifest,
            base_url="http://localhost:1234/v1",
            embedding_model="nomic",
            out_path=out,
            timeout_sec=60,
            self_consistency_events=0,
            self_consistency_runs=0,
            skip_checks=True,
            dry_run=True,
        )
    assert out.exists()
    lines = out.read_text().splitlines()
    assert len(lines) == 1
    import json

    assert json.loads(lines[0])["record_type"] == "run_manifest"
    assert result.models_completed == []
