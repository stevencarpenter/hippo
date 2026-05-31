from unittest.mock import MagicMock, patch

from hippo_brain.bench.preflight import (
    CheckResult,
    check_disk_space,
    check_inference_reachable,
    run_all_preflight,
)


def test_check_result_is_dict_serializable():
    r = CheckResult(name="x", status="pass", detail="ok")
    assert r.to_dict() == {"check": "x", "status": "pass", "detail": "ok"}


def test_check_inference_reachable_pass():
    fake_resp = MagicMock(status_code=200)
    with patch("httpx.get", return_value=fake_resp):
        r = check_inference_reachable("http://localhost:1234/v1/models")
    assert r.status == "pass"


def test_check_inference_reachable_fail_on_connection_refused():
    import httpx

    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        r = check_inference_reachable("http://localhost:1234/v1/models")
    assert r.status == "fail"


def test_check_inference_reachable_normalizes_bare_base_url():
    # Regression: run_all_preflight passes the bare base URL (`.../v1`), which is
    # only a namespace prefix. Spec-correct servers (oMLX) 404 on it; the check
    # must probe `/v1/models` regardless of which form the caller passes.
    fake_resp = MagicMock(status_code=200)
    with patch("httpx.get", return_value=fake_resp) as mock_get:
        r = check_inference_reachable("http://localhost:8000/v1")
    assert r.status == "pass"
    assert mock_get.call_args.args[0] == "http://localhost:8000/v1/models"


def test_check_inference_reachable_does_not_double_append_models():
    fake_resp = MagicMock(status_code=200)
    with patch("httpx.get", return_value=fake_resp) as mock_get:
        check_inference_reachable("http://localhost:8000/v1/models")
    assert mock_get.call_args.args[0] == "http://localhost:8000/v1/models"


def test_check_disk_space_pass(tmp_path):
    fake = MagicMock(free=10 * 1024**3)
    with patch("shutil.disk_usage", return_value=fake):
        r = check_disk_space(tmp_path, min_gb=2.0)
    assert r.status == "pass"


def test_check_disk_space_fail(tmp_path):
    fake = MagicMock(free=100 * 1024**2)
    with patch("shutil.disk_usage", return_value=fake):
        r = check_disk_space(tmp_path, min_gb=2.0)
    assert r.status == "fail"


def test_check_disk_space_uses_existing_parent_for_missing_path(tmp_path):
    fake = MagicMock(free=10 * 1024**3)
    missing = tmp_path / "nested" / "runs"
    with patch("shutil.disk_usage", return_value=fake) as mock_usage:
        r = check_disk_space(missing, min_gb=2.0)
    assert r.status == "pass"
    assert mock_usage.call_args.args[0] == tmp_path


def test_run_all_preflight_aborts_on_corpus_missing(tmp_path):
    """run_all_preflight reports aborted=True when corpus artifacts are missing."""
    missing_corpus = tmp_path / "absent.sqlite"
    missing_manifest = tmp_path / "absent.manifest.json"
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {"pid": 1234, "paused": False}

    with (
        patch("hippo_brain.bench.preflight.httpx.get", return_value=fake_resp),
        patch("shutil.disk_usage", return_value=MagicMock(free=10 * 1024**3)),
    ):
        checks, aborted = run_all_preflight(
            brain_url="http://localhost:9175",
            corpus_sqlite=missing_corpus,
            manifest=missing_manifest,
            inference_url="http://localhost:1234/v1/models",
            skip_prod_pause=True,
            brain_port=18923,
        )

    assert aborted is True
    corpus_check = next(c for c in checks if c.name == "corpus_present")
    assert corpus_check.status == "fail"


def _patch_all_other_checks(monkeypatch, preflight_mod):
    """Patch all preflight checks except qa_scoreable to pass/warn so tests can isolate qa behavior."""
    monkeypatch.setattr(
        preflight_mod,
        "check_prod_brain_reachable",
        lambda _u: preflight_mod.CheckResult("prod_brain_reachable", "warn", "off"),
    )
    monkeypatch.setattr(
        preflight_mod,
        "check_prod_brain_pauseable",
        lambda _u, skip: preflight_mod.CheckResult("prod_brain_pauseable", "warn", "off"),
    )
    monkeypatch.setattr(
        preflight_mod,
        "check_corpus_present",
        lambda _c, _m: preflight_mod.CheckResult("corpus_present", "pass", "schema_version=18"),
    )
    monkeypatch.setattr(
        preflight_mod,
        "check_inference_reachable",
        lambda _u: preflight_mod.CheckResult("inference_reachable", "pass", "HTTP 200"),
    )
    monkeypatch.setattr(
        preflight_mod,
        "check_disk_free_bench",
        lambda _p: preflight_mod.CheckResult("disk_free_bench", "pass", "ok"),
    )
    monkeypatch.setattr(
        preflight_mod,
        "check_brain_port_free",
        lambda _p: preflight_mod.CheckResult("brain_port_free", "pass", "ok"),
    )


def test_run_all_preflight_qa_fixture_missing_is_warn_not_aborted(tmp_path, monkeypatch):
    """A missing QA fixture yields status='warn' and does NOT set aborted.

    The run proceeds enrichment-only. A missing fixture is symmetric with a missing
    corpus (which also warns). Only a *present-but-failing* fixture is a hard abort.
    """
    from hippo_brain.bench import preflight

    corpus = tmp_path / "corpus.sqlite"
    manifest = tmp_path / "corpus.manifest.json"
    corpus.write_bytes(b"")
    manifest.write_text("{}")
    # qa file is intentionally NOT created

    _patch_all_other_checks(monkeypatch, preflight)
    monkeypatch.setattr(preflight, "bench_qa_path", lambda: tmp_path / "eval-qa-v1.jsonl")

    checks, aborted = preflight.run_all_preflight(
        brain_url="http://127.0.0.1:9175",
        corpus_sqlite=corpus,
        manifest=manifest,
        inference_url="http://localhost:1234/v1",
        skip_prod_pause=True,
        min_scoreable_qa=1,
    )

    assert aborted is False
    qa_check = next(c for c in checks if c.name == "qa_scoreable")
    assert qa_check.status == "warn"
    assert "skipped" in qa_check.detail.lower() or "missing" in qa_check.detail.lower()


def test_run_all_preflight_fails_when_qa_has_no_scoreable_items(tmp_path, monkeypatch):
    """A present QA fixture that fails validation is a hard fail and sets aborted=True."""
    from hippo_brain.bench import preflight

    corpus = tmp_path / "corpus.sqlite"
    manifest = tmp_path / "corpus.manifest.json"
    qa = tmp_path / "eval-qa-v1.jsonl"
    corpus.write_bytes(b"")
    manifest.write_text("{}")
    qa.write_text('{"qa_id":"q1","question":"x","golden_event_id":null}\n')

    _patch_all_other_checks(monkeypatch, preflight)
    monkeypatch.setattr(preflight, "bench_qa_path", lambda: qa)
    monkeypatch.setattr(
        preflight,
        "validate_qa_fixture",
        lambda *_a, **_k: type(
            "R", (), {"passes": False, "detail": "need at least 1 scoreable Q/A items"}
        )(),
    )

    checks, aborted = preflight.run_all_preflight(
        brain_url="http://127.0.0.1:9175",
        corpus_sqlite=corpus,
        manifest=manifest,
        inference_url="http://localhost:1234/v1",
        skip_prod_pause=True,
        min_scoreable_qa=1,
    )

    assert aborted is True
    assert any(c.name == "qa_scoreable" and c.status == "fail" for c in checks)
