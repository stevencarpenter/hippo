"""Tests for the v2 coordinator's per-model lifecycle.

Focus: failure-recovery contract — when any step in run_one_model_v2 raises,
teardown_shadow_stack must still be called (BT-03). Without this, model N's
failure leaks the shadow process group; model N+1's spawn races on the
fixed brain port.
"""

from __future__ import annotations

import shutil
import time
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
    p.write_bytes(
        b""
    )  # empty file; sqlite will treat as malformed but our patches bypass real reads
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


def test_teardown_runs_on_clean_path(monkeypatch: pytest.MonkeyPatch, fake_corpus: Path) -> None:
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
    assert mocks["sampler"].stop.call_count == 1, (
        "sampler was started before drain — must be stopped"
    )


def test_wait_for_queue_drain_raises_on_missing_tables(tmp_path: Path) -> None:
    """BT-05: schema mismatch must fail fast, not be reported as 'drained instantly'."""
    import sqlite3

    bench_db = tmp_path / "bench.sqlite"
    # Build a sqlite DB with NONE of the expected queue tables.
    conn = sqlite3.connect(str(bench_db))
    conn.execute("CREATE TABLE unrelated (id INTEGER)")
    conn.commit()
    conn.close()

    t0 = time.monotonic()
    with pytest.raises(RuntimeError, match="no queue tables present"):
        coordinator_v2._wait_for_queue_drain(
            bench_db, drain_timeout_sec=10.0, poll_interval_sec=0.1
        )
    elapsed = time.monotonic() - t0
    assert elapsed < 1.0, (
        f"should raise on first poll (~instantaneous), not wait for timeout — took {elapsed:.2f}s"
    )


def test_wait_for_queue_drain_returns_drained_when_tables_empty(tmp_path: Path) -> None:
    """Sanity: when at least one queue table exists and is empty, returns False (drained)."""
    import sqlite3

    bench_db = tmp_path / "bench.sqlite"
    conn = sqlite3.connect(str(bench_db))
    # Only one of the four exists — that's enough to satisfy schema_checked.
    conn.execute("CREATE TABLE enrichment_queue (id INTEGER, status TEXT)")
    conn.commit()
    conn.close()

    timeout_hit = coordinator_v2._wait_for_queue_drain(
        bench_db, drain_timeout_sec=5.0, poll_interval_sec=0.05
    )
    assert timeout_hit is False, "empty queue should return drained, not timeout"


def test_smoke_run_one_model_v2_full_lifecycle(
    monkeypatch: pytest.MonkeyPatch, fake_corpus: Path
) -> None:
    """BT-12: end-to-end smoke through every step.

    Patches every I/O boundary, runs through unload→load→spawn→drain→
    downstream-proxy→SC→teardown→cooldown, and asserts:
    - downstream_proxy is populated (not silently {})
    - SC attempts list is populated
    - errors list is empty (clean path)
    - teardown was called exactly once
    """
    mocks = _patch_lifecycle(monkeypatch)

    embedding_fn = MagicMock(return_value=[0.0] * 8)
    monkeypatch.setattr(
        coordinator_v2,
        "bench_qa_path",
        lambda: fake_corpus.parent / "qa.jsonl",
    )
    qa_path = fake_corpus.parent / "qa.jsonl"
    qa_path.write_text('{"id":"q1","question":"x","golden_event_ids":["shell-1"]}\n')
    monkeypatch.setattr(
        coordinator_v2,
        "load_qa_items",
        MagicMock(return_value=([{"id": "q1"}], [])),
    )
    monkeypatch.setattr(
        coordinator_v2,
        "_collect_event_ids_from_db",
        MagicMock(return_value={"shell-1"}),
    )
    # Populated downstream proxy result.
    monkeypatch.setattr(
        coordinator_v2,
        "run_downstream_proxy_pass",
        MagicMock(return_value={"hit_at_1": 0.4, "mrr": 0.35, "ndcg_at_10": 0.42}),
    )
    # Provide a non-empty corpus so SC pass actually runs.
    fake_entry = MagicMock(redacted_content="hello", source="shell")
    monkeypatch.setattr(
        coordinator_v2,
        "_load_corpus_entries",
        MagicMock(return_value=[fake_entry, fake_entry, fake_entry]),
    )
    sc_attempt = MagicMock()
    sc_attempt.to_dict.return_value = {"k": "v"}
    monkeypatch.setattr(
        coordinator_v2,
        "run_self_consistency_pass",
        MagicMock(return_value=([sc_attempt, sc_attempt], [[[0.0]]])),
    )

    result = coordinator_v2.run_one_model_v2(
        model="test-model",
        run_id="test-run",
        corpus_sqlite=fake_corpus,
        embedding_fn=embedding_fn,
        warmup_calls=0,
        sc_events=2,
        sc_runs=1,
        cooldown_max_sec=0,
    )

    assert mocks["teardown"].call_count == 1, "teardown called exactly once on clean path"
    assert result.downstream_proxy == {
        "hit_at_1": 0.4,
        "mrr": 0.35,
        "ndcg_at_10": 0.42,
    }, "downstream_proxy must be populated, not silently empty"
    assert len(result.attempts) == 2, "SC attempts plumbed into result"
    assert result.errors == [], "no errors on clean path"


def test_sc_failure_captured_with_attempts_empty(
    monkeypatch: pytest.MonkeyPatch, fake_corpus: Path
) -> None:
    """BT-12: SC pass raises → result.errors records it, attempts stays empty."""
    mocks = _patch_lifecycle(
        monkeypatch,
        sc_raises=RuntimeError("synthetic: SC pass exploded"),
    )
    fake_entry = MagicMock(redacted_content="hello", source="shell")
    monkeypatch.setattr(
        coordinator_v2,
        "_load_corpus_entries",
        MagicMock(return_value=[fake_entry, fake_entry, fake_entry]),
    )

    result = coordinator_v2.run_one_model_v2(
        model="test-model",
        run_id="test-run",
        corpus_sqlite=fake_corpus,
        warmup_calls=0,
        sc_events=2,
        sc_runs=1,
        cooldown_max_sec=0,
    )

    assert mocks["teardown"].call_count == 1
    assert result.attempts == [], "SC failure → no attempts recorded"
    sc_error = next((e for e in result.errors if e["step"] == "self_consistency"), None)
    assert sc_error is not None, f"expected self_consistency error, got: {result.errors}"
    assert "synthetic" in sc_error["error"]


def test_downstream_proxy_failure_captured_as_structured_error(
    monkeypatch: pytest.MonkeyPatch, fake_corpus: Path
) -> None:
    """BT-04: downstream_proxy raise is captured into result.errors, not silently swallowed."""
    mocks = _patch_lifecycle(
        monkeypatch,
        proxy_raises=RuntimeError("synthetic: downstream proxy exploded"),
    )

    # Need an embedding_fn for the downstream proxy branch to be reached.
    embedding_fn = MagicMock(return_value=[0.0] * 8)
    # And a qa_path that exists.
    monkeypatch.setattr(
        coordinator_v2,
        "bench_qa_path",
        lambda: fake_corpus.parent / "qa.jsonl",
    )
    qa_path = fake_corpus.parent / "qa.jsonl"
    qa_path.write_text('{"id":"q1","question":"x","golden_event_ids":["shell-1"]}\n')
    monkeypatch.setattr(
        coordinator_v2,
        "load_qa_items",
        MagicMock(return_value=([{"id": "q1"}], [])),
    )
    # Force _collect_event_ids_from_db to return non-empty so downstream_proxy actually runs.
    monkeypatch.setattr(
        coordinator_v2,
        "_collect_event_ids_from_db",
        MagicMock(return_value={"shell-1"}),
    )

    result = coordinator_v2.run_one_model_v2(
        model="test-model",
        run_id="test-run",
        corpus_sqlite=fake_corpus,
        embedding_fn=embedding_fn,
        warmup_calls=0,
        sc_events=0,
        cooldown_max_sec=0,
    )

    assert mocks["teardown"].call_count == 1
    assert result.errors, "errors list should contain the proxy failure"
    proxy_error = next((e for e in result.errors if e["step"] == "downstream_proxy"), None)
    assert proxy_error is not None, f"expected downstream_proxy error, got: {result.errors}"
    assert "synthetic" in proxy_error["error"]
    assert proxy_error["type"] == "RuntimeError"
