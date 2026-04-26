import os
import sys
from unittest.mock import patch

import pytest


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


def test_telemetry_missing_packages_raises_when_enabled():
    """When HIPPO_OTEL_ENABLED=1 but otel packages are missing, init_telemetry
    must fail loud rather than silently disabling itself.

    The silent-warning behavior previously masked a corrupted venv (dist-info
    present but namespace empty), causing every Grafana panel sourced from the
    brain to go dark while the brain reported `enrichment_running: true`.
    """
    with patch.dict(os.environ, {"HIPPO_OTEL_ENABLED": "1"}):
        # Hide all opentelemetry modules to simulate missing packages.
        hidden = {}
        for mod_name in list(sys.modules.keys()):
            if "opentelemetry" in mod_name:
                hidden[mod_name] = sys.modules.pop(mod_name)

        try:
            if "hippo_brain.telemetry" in sys.modules:
                del sys.modules["hippo_brain.telemetry"]

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
            sys.modules.update(hidden)


def test_telemetry_missing_packages_silent_when_disabled():
    """When HIPPO_OTEL_ENABLED is unset, missing OTel packages are not an error.

    Locks in that the hard-fail only kicks in for explicit opt-in. A user who
    never enabled telemetry should never see import errors from this module.
    """
    env = {k: v for k, v in os.environ.items() if k != "HIPPO_OTEL_ENABLED"}
    with patch.dict(os.environ, env, clear=True):
        hidden = {}
        for mod_name in list(sys.modules.keys()):
            if "opentelemetry" in mod_name:
                hidden[mod_name] = sys.modules.pop(mod_name)

        try:
            if "hippo_brain.telemetry" in sys.modules:
                del sys.modules["hippo_brain.telemetry"]

            with patch.dict(sys.modules, {"opentelemetry": None}):
                from hippo_brain.telemetry import init_telemetry, is_telemetry_active

                assert init_telemetry("test-service") is None
                assert is_telemetry_active() is False
        finally:
            sys.modules.update(hidden)


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
    if "hippo_brain.telemetry" in sys.modules:
        del sys.modules["hippo_brain.telemetry"]
    from hippo_brain.telemetry import _register_process_metrics

    # Should not raise even with psutil masked out.
    _register_process_metrics()
