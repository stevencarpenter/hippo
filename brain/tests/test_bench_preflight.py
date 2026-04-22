from unittest.mock import MagicMock, patch

from hippo_brain.bench.preflight import (
    CheckResult,
    check_disk_space,
    check_hippo_services,
    check_lms_cli,
    check_lmstudio_reachable,
    check_power_plugged,
    check_spotlight_idle,
    run_all_preflight,
)


def test_check_result_is_dict_serializable():
    r = CheckResult(name="x", status="pass", detail="ok")
    assert r.to_dict() == {"check": "x", "status": "pass", "detail": "ok"}


def test_check_lms_cli_pass(tmp_path):
    with patch("shutil.which", return_value="/usr/local/bin/lms"):
        r = check_lms_cli()
    assert r.status == "pass"


def test_check_lms_cli_fail_aborts():
    with patch("shutil.which", return_value=None):
        r = check_lms_cli()
    assert r.status == "fail"


def test_check_lmstudio_reachable_pass():
    fake_resp = MagicMock(status_code=200)
    with patch("httpx.get", return_value=fake_resp):
        r = check_lmstudio_reachable("http://localhost:1234/v1/models")
    assert r.status == "pass"


def test_check_lmstudio_reachable_fail_on_connection_refused():
    import httpx

    with patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        r = check_lmstudio_reachable("http://localhost:1234/v1/models")
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


def test_check_power_plugged_warns_on_battery():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="Battery Power\n 'Battery' 45%; discharging"
        )
        r = check_power_plugged()
    assert r.status == "warn"


def test_check_power_plugged_pass_when_plugged():
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="AC Power\n 'InternalBattery' 100%; charged"
        )
        r = check_power_plugged()
    assert r.status == "pass"


def test_check_power_plugged_warns_when_pmset_missing():
    with patch("subprocess.run", side_effect=FileNotFoundError("pmset")):
        r = check_power_plugged()
    assert r.status == "warn"
    assert "pmset unavailable" in r.detail


def test_check_hippo_services_warns_when_launchctl_missing():
    with patch("subprocess.run", side_effect=FileNotFoundError("launchctl")):
        r = check_hippo_services()
    assert r.status == "warn"
    assert "launchctl unavailable" in r.detail


def test_check_spotlight_idle_warns_when_mdutil_missing():
    with patch("subprocess.run", side_effect=FileNotFoundError("mdutil")):
        r = check_spotlight_idle()
    assert r.status == "warn"
    assert "mdutil unavailable" in r.detail


def test_run_all_preflight_aborts_on_hard_fail(tmp_path):
    with (
        patch("shutil.which", return_value=None),  # lms missing
        patch("shutil.disk_usage", return_value=MagicMock(free=10 * 1024**3)),
    ):
        checks = run_all_preflight(tmp_path, lmstudio_url="http://localhost:1234/v1/models")
    assert any(c.status == "fail" for c in checks)
