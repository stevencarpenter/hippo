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
