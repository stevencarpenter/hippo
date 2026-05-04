"""BT-06: tests for the pause-lockfile crash-recovery contract.

A SIGKILL'd bench leaves prod brain paused indefinitely unless a
lockfile-based recovery path exists. These tests verify:
1. pause() writes the lockfile before the HTTP call.
2. resume() removes the lockfile.
3. recover_stale_pause() finds the lockfile and POSTs resume.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hippo_brain.bench import pause_rpc


@pytest.fixture
def isolated_lockfile(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Redirect PAUSE_LOCKFILE to a tmp path so tests don't trample the real one."""
    lock = tmp_path / "pause.lock"
    monkeypatch.setattr(pause_rpc, "PAUSE_LOCKFILE", lock)
    return lock


def test_lockfile_written_on_pause(isolated_lockfile: Path) -> None:
    rpc = pause_rpc.PauseRpcClient(base_url="http://localhost:8000")

    with patch.object(pause_rpc.httpx, "post") as mock_post:
        mock_post.return_value = MagicMock(
            json=lambda: {"paused_at": "x"}, raise_for_status=lambda: None
        )
        rpc.pause()

    assert isolated_lockfile.exists()
    data = json.loads(isolated_lockfile.read_text())
    assert data["brain_url"] == "http://localhost:8000"
    assert data["pid"] > 0
    assert "started_iso" in data


def test_lockfile_removed_on_resume(isolated_lockfile: Path) -> None:
    isolated_lockfile.parent.mkdir(parents=True, exist_ok=True)
    isolated_lockfile.write_text(json.dumps({"brain_url": "http://localhost:8000"}))

    rpc = pause_rpc.PauseRpcClient(base_url="http://localhost:8000")
    with patch.object(pause_rpc.httpx, "post") as mock_post:
        mock_post.return_value = MagicMock(json=lambda: {"resumed_at": "x"})
        rpc.resume()

    assert not isolated_lockfile.exists()


def test_lockfile_removed_even_if_resume_post_fails(isolated_lockfile: Path) -> None:
    """Defensive: if the brain is gone, we still remove our lockfile so the
    next bench start doesn't think there's something to recover."""
    isolated_lockfile.parent.mkdir(parents=True, exist_ok=True)
    isolated_lockfile.write_text(json.dumps({"brain_url": "http://localhost:8000"}))

    rpc = pause_rpc.PauseRpcClient(base_url="http://localhost:8000")
    with patch.object(pause_rpc.httpx, "post", side_effect=ConnectionError("brain dead")):
        rpc.resume()

    assert not isolated_lockfile.exists()


def test_recover_resumes_when_stale_lockfile_present(isolated_lockfile: Path) -> None:
    """Simulates the post-SIGKILL state: prior bench wrote a lockfile and
    didn't clear it. Recovery must POST resume and unlink the file."""
    isolated_lockfile.parent.mkdir(parents=True, exist_ok=True)
    isolated_lockfile.write_text(
        json.dumps(
            {
                "started_iso": "2026-05-03T00:00:00+00:00",
                "brain_url": "http://localhost:8000",
                "pid": 12345,
            }
        )
    )

    with patch.object(pause_rpc.httpx, "post") as mock_post:
        mock_post.return_value = MagicMock()
        recovered = pause_rpc.recover_stale_pause("http://fallback:9999")

    assert recovered is True
    assert not isolated_lockfile.exists()
    # Used the lockfile's brain_url, not the fallback.
    assert mock_post.call_args[0][0] == "http://localhost:8000/control/resume"


def test_recover_no_op_when_no_lockfile(isolated_lockfile: Path) -> None:
    assert not isolated_lockfile.exists()
    with patch.object(pause_rpc.httpx, "post") as mock_post:
        recovered = pause_rpc.recover_stale_pause("http://localhost:8000")
    assert recovered is False
    mock_post.assert_not_called()


def test_recover_falls_back_when_lockfile_corrupt(isolated_lockfile: Path) -> None:
    """Hardened against a partial-write or hand-edited lockfile."""
    isolated_lockfile.parent.mkdir(parents=True, exist_ok=True)
    isolated_lockfile.write_text("not json {{{")

    with patch.object(pause_rpc.httpx, "post") as mock_post:
        mock_post.return_value = MagicMock()
        recovered = pause_rpc.recover_stale_pause("http://fallback:9999")

    assert recovered is True
    assert not isolated_lockfile.exists()
    assert mock_post.call_args[0][0] == "http://fallback:9999/control/resume"


def test_skip_flag_does_not_write_lockfile(isolated_lockfile: Path) -> None:
    """skip=True must short-circuit pause() so no lockfile is created
    when prod is intentionally not contacted (e.g. CI without LM Studio)."""
    rpc = pause_rpc.PauseRpcClient(base_url="http://localhost:8000", skip=True)
    rpc.pause()
    assert not isolated_lockfile.exists()


# ----------------------------------------------------------------------------
# Post-review CC-1: pause RPC failure must NOT leave a stale lockfile behind
# (otherwise watchdog suppresses I-2/I-4/I-8 even though prod was never paused)
# ----------------------------------------------------------------------------


def test_lockfile_unlinked_when_pause_http_call_raises(isolated_lockfile: Path) -> None:
    """If httpx.post raises, pause() must roll back the lockfile and re-raise.

    Without this, a transient pause RPC error (network blip, brain restart
    between probe and pause) would leave the lockfile in place; the watchdog
    would then suppress I-2/I-4/I-8 alarms for up to the C-1 staleness window
    even though prod was never actually paused.
    """
    import httpx

    rpc = pause_rpc.PauseRpcClient(base_url="http://localhost:8000")

    with patch.object(pause_rpc.httpx, "post") as mock_post:
        mock_post.side_effect = httpx.ConnectError("synthetic: brain unreachable")
        with pytest.raises(httpx.ConnectError, match="synthetic"):
            rpc.pause()

    assert not isolated_lockfile.exists(), (
        "CC-1: pause() must unlink the lockfile if the HTTP POST raises — "
        "leaving it behind mutes the watchdog for the suppression window"
    )


def test_lockfile_unlinked_when_pause_returns_5xx(isolated_lockfile: Path) -> None:
    """raise_for_status() raises on 5xx; same rollback contract applies."""
    import httpx

    rpc = pause_rpc.PauseRpcClient(base_url="http://localhost:8000")

    def _raise_5xx() -> None:
        raise httpx.HTTPStatusError("synthetic 503", request=MagicMock(), response=MagicMock())

    with patch.object(pause_rpc.httpx, "post") as mock_post:
        mock_post.return_value = MagicMock(raise_for_status=_raise_5xx)
        with pytest.raises(httpx.HTTPStatusError, match="synthetic 503"):
            rpc.pause()

    assert not isolated_lockfile.exists()


def test_pause_cleans_up_orphan_tmp_when_write_lockfile_raises(
    isolated_lockfile: Path,
) -> None:
    """Post-review M2: if `_write_lockfile_atomic` raises mid-write (e.g. disk
    full, permission error, parent dir disappeared), `.lock.tmp` may exist
    even though `pause.lock` doesn't — and a future `recover_stale_pause`
    only looks at `pause.lock`, so the tmp would orphan forever. The
    rollback path must clean up BOTH paths.
    """
    isolated_lockfile.parent.mkdir(parents=True, exist_ok=True)
    # Simulate the orphan state: tmp exists from a partial prior write.
    tmp_path = isolated_lockfile.with_suffix(".lock.tmp")
    tmp_path.write_text("partial content from a previous attempt")

    rpc = pause_rpc.PauseRpcClient(base_url="http://localhost:8000")
    with patch.object(pause_rpc, "_write_lockfile_atomic") as mock_write:
        mock_write.side_effect = OSError("synthetic: disk full")
        with pytest.raises(OSError, match="synthetic: disk full"):
            rpc.pause()

    # Both paths must be gone — pause.lock never existed, but tmp was
    # orphaned and the rollback should sweep it.
    assert not isolated_lockfile.exists()
    assert not tmp_path.exists(), (
        "M2: rollback must unlink .lock.tmp too, otherwise a write-time "
        "failure leaves an orphan that recover_stale_pause never sees"
    )
