import os
import sys
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _reset_telemetry_active_state():
    """Defend against module-level `_telemetry_active` leaking between tests.

    Tests that call init_telemetry() flip the flag True, then call shutdown()
    which flips it back. If a test fails between those two events, the flag
    leaks into the next test. Resetting before each test makes failures local.
    """
    import hippo_brain.telemetry as telemetry_module

    telemetry_module._telemetry_active = False
    yield
    telemetry_module._telemetry_active = False


def test_telemetry_disabled_by_default():
    """When HIPPO_OTEL_ENABLED is not set, init_telemetry is a no-op."""
    env = {k: v for k, v in os.environ.items() if k != "HIPPO_OTEL_ENABLED"}
    with patch.dict(os.environ, env, clear=True):
        from hippo_brain.telemetry import init_telemetry, is_telemetry_active

        result = init_telemetry("test-service")
        assert result is None
        assert is_telemetry_active() is False


def test_telemetry_enabled_returns_providers():
    """When HIPPO_OTEL_ENABLED=1 and otel packages are available, returns providers."""
    with patch.dict(os.environ, {"HIPPO_OTEL_ENABLED": "1"}):
        try:
            from hippo_brain.telemetry import init_telemetry

            result = init_telemetry("test-service", endpoint="http://localhost:4318")
            assert result is None or callable(result)
            if callable(result):
                result()
        except ImportError:
            pass


def _hide_otel_modules():
    """Snapshot and remove every `opentelemetry*` and `hippo_brain.telemetry`
    entry from sys.modules, returning the snapshot so tests can restore them.

    Restoring `hippo_brain.telemetry` is critical: without it, a re-import
    creates a NEW module object that other consumers (e.g. hippo_brain.server)
    don't see, leaving the original module's `_telemetry_active` and the new
    module's `_telemetry_active` desynchronized across the test process.
    """
    snapshot = {}
    for mod_name in list(sys.modules.keys()):
        if "opentelemetry" in mod_name or mod_name == "hippo_brain.telemetry":
            snapshot[mod_name] = sys.modules.pop(mod_name)
    return snapshot


def test_telemetry_missing_packages_raises_when_enabled():
    """When HIPPO_OTEL_ENABLED=1 but otel packages are missing, init_telemetry
    must fail loud rather than silently disabling itself.

    The silent-warning behavior previously masked a corrupted venv (dist-info
    present but namespace empty), causing every Grafana panel sourced from the
    brain to go dark while the brain reported `enrichment_running: true`.
    """
    with patch.dict(os.environ, {"HIPPO_OTEL_ENABLED": "1"}):
        snapshot = _hide_otel_modules()
        try:
            with patch.dict(sys.modules, {"opentelemetry": None}):
                from hippo_brain.telemetry import (
                    TelemetryInitError,
                    init_telemetry,
                    is_telemetry_active,
                )

                with pytest.raises(TelemetryInitError) as excinfo:
                    init_telemetry("test-service")

                assert "HIPPO_OTEL_ENABLED=1" in str(excinfo.value)
                assert "uv sync" in str(excinfo.value)
                assert is_telemetry_active() is False
        finally:
            sys.modules.update(snapshot)


def test_telemetry_missing_packages_silent_when_disabled():
    """When HIPPO_OTEL_ENABLED is unset, missing OTel packages are not an error.

    Locks in that the hard-fail only kicks in for explicit opt-in. A user who
    never enabled telemetry should never see import errors from this module.
    """
    env = {k: v for k, v in os.environ.items() if k != "HIPPO_OTEL_ENABLED"}
    with patch.dict(os.environ, env, clear=True):
        snapshot = _hide_otel_modules()
        try:
            with patch.dict(sys.modules, {"opentelemetry": None}):
                from hippo_brain.telemetry import init_telemetry, is_telemetry_active

                assert init_telemetry("test-service") is None
                assert is_telemetry_active() is False
        finally:
            sys.modules.update(snapshot)


def test_telemetry_enabled_creates_meter_provider():
    """When OTel is enabled, init_telemetry should set up a meter provider."""
    with patch.dict(os.environ, {"HIPPO_OTEL_ENABLED": "1"}):
        from hippo_brain.telemetry import init_telemetry, is_telemetry_active

        shutdown = init_telemetry("test-service", endpoint="http://localhost:4318")
        assert shutdown is not None, "OTel packages are installed; init should succeed"
        try:
            assert is_telemetry_active() is True
            from opentelemetry import metrics as otel_metrics

            meter = otel_metrics.get_meter("test")
            counter = meter.create_counter("test.counter")
            counter.add(1)  # Should not raise
        finally:
            shutdown()
            assert is_telemetry_active() is False


def test_get_meter_returns_none_when_disabled():
    """get_meter should return None when OTel is disabled."""
    from hippo_brain.telemetry import get_meter

    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("HIPPO_OTEL_ENABLED", None)
        result = get_meter()
        assert result is None


def test_process_metrics_registered_when_enabled():
    """init_telemetry should register process.* observable instruments without error."""
    with patch.dict(os.environ, {"HIPPO_OTEL_ENABLED": "1"}):
        from hippo_brain.telemetry import init_telemetry

        shutdown = init_telemetry("test-service", endpoint="http://localhost:4318")
        assert shutdown is not None
        try:
            # Registration happens inside init_telemetry; if it raised we wouldn't
            # get here. Exercise the callbacks via a forced collect to make sure
            # none of them blow up on this interpreter.
            from opentelemetry import metrics as otel_metrics

            provider = otel_metrics.get_meter_provider()
            force_flush = getattr(provider, "force_flush", None)
            if callable(force_flush):
                force_flush(timeout_millis=500)
        finally:
            shutdown()


def test_process_metrics_missing_psutil_is_soft_failure(monkeypatch):
    """If psutil import fails, _register_process_metrics logs and returns; does not raise."""
    import sys

    monkeypatch.setitem(sys.modules, "psutil", None)
    # Save and restore the original `hippo_brain.telemetry` module reference.
    # A bare `del` would leak: the next test runs a fresh import, but
    # hippo_brain.server still holds function references from the original
    # module — and `server.is_telemetry_active()` would then read its
    # `_telemetry_active` from a module that the test can no longer reach.
    original_telemetry = sys.modules.get("hippo_brain.telemetry")
    if "hippo_brain.telemetry" in sys.modules:
        del sys.modules["hippo_brain.telemetry"]
    try:
        from hippo_brain.telemetry import _register_process_metrics

        # Should not raise even with psutil masked out.
        _register_process_metrics()
    finally:
        if original_telemetry is not None:
            sys.modules["hippo_brain.telemetry"] = original_telemetry
