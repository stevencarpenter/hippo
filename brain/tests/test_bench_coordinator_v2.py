"""Tests for the v2 coordinator's per-model lifecycle.

Focus: failure-recovery contract — when any step in run_one_model_v2 raises,
teardown_shadow_stack must still be called (BT-03). Without this, model N's
failure leaks the shadow process group; model N+1's spawn races on the
fixed brain port.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from hippo_brain.bench import coordinator_v2


@pytest.fixture
def fake_corpus(tmp_path: Path) -> Path:
    """Tiny corpus fixture: empty SQLite file. Real schema not required —
    coordinator only reads it via _wait_for_queue_drain (which we patch out)
    and _collect_event_ids_from_db (which swallows OperationalError)."""
    p = tmp_path / "corpus.sqlite"
    p.write_bytes(b"")  # empty file; sqlite will treat as malformed but our patches bypass real reads
    return p


def _patch_lifecycle(
    monkeypatch: pytest.MonkeyPatch,
    *,
    spawn_raises: Exception | None = None,
    wait_raises: Exception | None = None,
    drain_raises: Exception | None = None,
    sc_raises: Exception | None = None,
    proxy_raises: Exception | None = None,
) -> dict[str, MagicMock]:
    """Stub every I/O dependency. Returns a dict of MagicMocks keyed by name
    so the test can assert call counts."""
    teardown_mock = MagicMock()
    spawn_mock = MagicMock()
    if spawn_raises is None:
        # Return a fake stack object — only the .daemon_proc/.brain_proc/.brain_base_url
        # attributes might be accessed downstream, but with our monkey-patches they aren't.
        spawn_mock.return_value = MagicMock(name="ShadowStack")
    else:
        spawn_mock.side_effect = spawn_raises

    wait_mock = MagicMock(return_value=0.05)
    if wait_raises is not None:
        wait_mock.side_effect = wait_raises

    drain_mock = MagicMock(return_value=False)
    if drain_raises is not None:
        drain_mock.side_effect = drain_raises

    monkeypatch.setattr(coordinator_v2, "spawn_shadow_stack", spawn_mock)
    monkeypatch.setattr(coordinator_v2, "wait_for_brain_ready", wait_mock)
    monkeypatch.setattr(coordinator_v2, "teardown_shadow_stack", teardown_mock)
    monkeypatch.setattr(coordinator_v2, "_wait_for_queue_drain", drain_mock)

    # Patch the lms module to no-ops.
    monkeypatch.setattr(coordinator_v2.lms, "unload_all", MagicMock())
    monkeypatch.setattr(coordinator_v2.lms, "load", MagicMock())

    # Patch shutil.copy2 — the empty fake corpus would fail real copy.
    monkeypatch.setattr(shutil, "copy2", MagicMock())

    # Patch corpus loading — returns empty list so warmup + SC are skipped naturally.
    monkeypatch.setattr(coordinator_v2, "_load_corpus_entries", MagicMock(return_value=[]))

    # Patch metrics sampler — we don't want a thread spinning.
    sampler_mock = MagicMock()
    sampler_mock.peak.return_value = {}
    monkeypatch.setattr(coordinator_v2, "MetricsSampler", MagicMock(return_value=sampler_mock))

    # Patch PauseRpcClient — health probe returns paused=False.
    pause_client_mock = MagicMock()
    pause_client_mock.probe_health.return_value = {"paused": False}
    monkeypatch.setattr(coordinator_v2, "PauseRpcClient", MagicMock(return_value=pause_client_mock))

    # Patch downstream proxy + SC pass for the optional-error variants.
    if proxy_raises is not None:
        monkeypatch.setattr(
            coordinator_v2,
            "run_downstream_proxy_pass",
            MagicMock(side_effect=proxy_raises),
        )
    if sc_raises is not None:
        monkeypatch.setattr(
            coordinator_v2,
            "run_self_consistency_pass",
            MagicMock(side_effect=sc_raises),
        )

    return {
        "spawn": spawn_mock,
        "wait": wait_mock,
        "teardown": teardown_mock,
        "drain": drain_mock,
        "sampler": sampler_mock,
    }


def test_teardown_runs_when_wait_for_brain_ready_raises(
    monkeypatch: pytest.MonkeyPatch, fake_corpus: Path
) -> None:
    """The whole point of BT-03: a raise inside the body still tears down."""
    mocks = _patch_lifecycle(
        monkeypatch,
        wait_raises=RuntimeError("synthetic: brain never came up"),
    )

    with pytest.raises(RuntimeError, match="synthetic"):
        coordinator_v2.run_one_model_v2(
            model="test-model",
            run_id="test-run",
            corpus_sqlite=fake_corpus,
            cooldown_max_sec=0,
        )

    assert mocks["spawn"].call_count == 1, "spawn should have been attempted"
    assert mocks["teardown"].call_count == 1, "teardown MUST run on raise (BT-03 contract)"
    assert mocks["sampler"].stop.call_count == 0, "sampler not yet started when wait raises"


def test_teardown_runs_on_clean_path(
    monkeypatch: pytest.MonkeyPatch, fake_corpus: Path
) -> None:
    """Sanity: clean path also tears down."""
    mocks = _patch_lifecycle(monkeypatch)

    result = coordinator_v2.run_one_model_v2(
        model="test-model",
        run_id="test-run",
        corpus_sqlite=fake_corpus,
        warmup_calls=0,
        sc_events=0,
        cooldown_max_sec=0,
    )

    assert mocks["teardown"].call_count == 1
    assert mocks["sampler"].stop.call_count == 1
    assert result.model == "test-model"
    assert result.process_ready_ms == 50  # 0.05 * 1000


def test_teardown_runs_when_drain_raises(
    monkeypatch: pytest.MonkeyPatch, fake_corpus: Path
) -> None:
    """A raise from the drain step (post-spawn, post-sampler-start) also tears down."""
    mocks = _patch_lifecycle(
        monkeypatch,
        drain_raises=RuntimeError("synthetic: queue drain blew up"),
    )

    with pytest.raises(RuntimeError, match="synthetic"):
        coordinator_v2.run_one_model_v2(
            model="test-model",
            run_id="test-run",
            corpus_sqlite=fake_corpus,
            warmup_calls=0,
            cooldown_max_sec=0,
        )

    assert mocks["teardown"].call_count == 1
    assert mocks["sampler"].stop.call_count == 1, "sampler was started before drain — must be stopped"
