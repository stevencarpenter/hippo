"""Tests for telemetry isolation in hippo-bench v2 — ensures spans/metrics
emitted from the shadow stack are tagged with `service.namespace=hippo-bench`
so prod dashboards (which filter on empty namespace) never see bench data.

Mocks subprocess.Popen — does NOT spawn real hippo processes.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

from hippo_brain.bench import shadow_stack
from hippo_brain.bench.shadow_stack import spawn_shadow_stack

BENCH_NAMESPACE = "hippo-bench"
TEST_RUN_ID = "run-2026-04-27-tel-iso"
TEST_MODEL_ID = "qwen3.5-35b-a3b-test"


def _spawn_kwargs(tmp_path, **overrides):
    return {
        "run_tree": tmp_path / "run-tree",
        "run_id": TEST_RUN_ID,
        "model_id": TEST_MODEL_ID,
        "corpus_version": "corpus-v2",
        "embedding_model": "embedding-test",
        **overrides,
    }


def _capture_popen_calls(monkeypatch, tmp_path):
    """Patch subprocess.Popen, tempfile.mkdtemp, and pgrp syscalls so
    spawn_shadow_stack runs to completion without touching real binaries.

    Each fake Popen returns poll()=None (alive) so the daemon liveness
    check inside _spawn_pgrp_pair doesn't raise."""
    calls: list[tuple[tuple, dict]] = []

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        proc = MagicMock()
        proc.pid = 99999
        proc.poll.return_value = None  # alive
        proc.returncode = None
        return proc

    def fake_mkdtemp(prefix: str = "tmp", **_kwargs) -> str:
        d = tmp_path / f"{prefix}fake-mkdtemp"
        d.mkdir(parents=True, exist_ok=True)
        return str(d)

    monkeypatch.setattr(shadow_stack.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(shadow_stack.tempfile, "mkdtemp", fake_mkdtemp)
    monkeypatch.setattr(shadow_stack.os, "getpgid", lambda _pid: 88888)
    monkeypatch.setattr(shadow_stack.os, "setpgid", lambda _pid, _pgid: None)
    return calls


def test_otel_resource_attributes_contains_namespace(tmp_path, monkeypatch):
    """Every Popen call must carry service.namespace=hippo-bench in OTEL_RESOURCE_ATTRIBUTES."""
    calls = _capture_popen_calls(monkeypatch, tmp_path)
    with patch.dict(os.environ, {}, clear=True):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path))

    assert len(calls) == 2
    for _args, kwargs in calls:
        env = kwargs["env"]
        attrs = env["OTEL_RESOURCE_ATTRIBUTES"]
        assert f"service.namespace={BENCH_NAMESPACE}" in attrs


def test_otel_resource_attributes_contains_run_id(tmp_path, monkeypatch):
    """Every Popen call must carry bench.run_id=<run_id> in OTEL_RESOURCE_ATTRIBUTES."""
    calls = _capture_popen_calls(monkeypatch, tmp_path)
    with patch.dict(os.environ, {}, clear=True):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path))

    assert len(calls) == 2
    for _args, kwargs in calls:
        env = kwargs["env"]
        attrs = env["OTEL_RESOURCE_ATTRIBUTES"]
        assert f"bench.run_id={TEST_RUN_ID}" in attrs


def test_otel_resource_attributes_contains_model_id(tmp_path, monkeypatch):
    """Every Popen call must carry bench.model_id=<model_id> in OTEL_RESOURCE_ATTRIBUTES."""
    calls = _capture_popen_calls(monkeypatch, tmp_path)
    with patch.dict(os.environ, {}, clear=True):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path))

    assert len(calls) == 2
    for _args, kwargs in calls:
        env = kwargs["env"]
        attrs = env["OTEL_RESOURCE_ATTRIBUTES"]
        assert f"bench.model_id={TEST_MODEL_ID}" in attrs


def test_python_sdk_picks_up_env_namespace():
    """The OTel Python SDK's OTELResourceDetector must merge OTEL_RESOURCE_ATTRIBUTES
    into Resource.create() output. This is the upstream contract that makes the
    shadow-stack env injection effective end-to-end."""
    from opentelemetry.sdk.resources import Resource

    saved = os.environ.get("OTEL_RESOURCE_ATTRIBUTES")
    try:
        os.environ["OTEL_RESOURCE_ATTRIBUTES"] = (
            f"service.namespace={BENCH_NAMESPACE},bench.run_id={TEST_RUN_ID}"
        )
        resource = Resource.create({"service.name": "test"})
        attrs = dict(resource.attributes)
        assert attrs.get("service.namespace") == BENCH_NAMESPACE
        assert attrs.get("bench.run_id") == TEST_RUN_ID
        assert attrs.get("service.name") == "test"
    finally:
        if saved is None:
            os.environ.pop("OTEL_RESOURCE_ATTRIBUTES", None)
        else:
            os.environ["OTEL_RESOURCE_ATTRIBUTES"] = saved


def test_bench_namespace_is_not_empty(tmp_path, monkeypatch):
    """Prod dashboards filter on empty namespace; bench MUST NOT use empty.
    Otherwise bench spans would leak into prod views."""
    calls = _capture_popen_calls(monkeypatch, tmp_path)
    with patch.dict(os.environ, {}, clear=True):
        spawn_shadow_stack(**_spawn_kwargs(tmp_path))

    assert len(calls) == 2
    for _args, kwargs in calls:
        env = kwargs["env"]
        attrs = env["OTEL_RESOURCE_ATTRIBUTES"]
        # Reject empty namespace (the prod filter token).
        assert "service.namespace=," not in attrs
        assert not attrs.endswith("service.namespace=")
        assert f"service.namespace={BENCH_NAMESPACE}" in attrs
        assert BENCH_NAMESPACE != ""
