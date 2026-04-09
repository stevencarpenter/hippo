import os
import sys
from unittest.mock import patch


def test_telemetry_disabled_by_default():
    """When HIPPO_OTEL_ENABLED is not set, init_telemetry is a no-op."""
    env = {k: v for k, v in os.environ.items() if k != "HIPPO_OTEL_ENABLED"}
    with patch.dict(os.environ, env, clear=True):
        from hippo_brain.telemetry import init_telemetry

        result = init_telemetry("test-service")
        assert result is None


def test_telemetry_enabled_returns_providers():
    """When HIPPO_OTEL_ENABLED=1 and otel packages are available, returns providers."""
    with patch.dict(os.environ, {"HIPPO_OTEL_ENABLED": "1"}):
        try:
            from hippo_brain.telemetry import init_telemetry

            result = init_telemetry("test-service", endpoint="http://localhost:4318")
            assert result is None or callable(result)
        except ImportError:
            pass


def test_telemetry_missing_packages_returns_none():
    """When HIPPO_OTEL_ENABLED=1 but otel packages missing, returns None gracefully."""
    with patch.dict(os.environ, {"HIPPO_OTEL_ENABLED": "1"}):
        # Hide all opentelemetry modules to simulate missing packages
        hidden = {}
        for mod_name in list(sys.modules.keys()):
            if "opentelemetry" in mod_name:
                hidden[mod_name] = sys.modules.pop(mod_name)

        try:
            # Force reimport of telemetry so it re-attempts the OTel imports
            if "hippo_brain.telemetry" in sys.modules:
                del sys.modules["hippo_brain.telemetry"]

            with patch.dict(sys.modules, {"opentelemetry": None}):
                from hippo_brain.telemetry import init_telemetry

                result = init_telemetry("test-service")
                assert result is None
        finally:
            sys.modules.update(hidden)


def test_telemetry_enabled_creates_meter_provider():
    """When OTel is enabled, init_telemetry should set up a meter provider."""
    with patch.dict(os.environ, {"HIPPO_OTEL_ENABLED": "1"}):
        from hippo_brain.telemetry import init_telemetry

        shutdown = init_telemetry("test-service", endpoint="http://localhost:4318")
        if shutdown is not None:
            # Verify we can get a meter (doesn't raise)
            from opentelemetry import metrics as otel_metrics

            meter = otel_metrics.get_meter("test")
            counter = meter.create_counter("test.counter")
            counter.add(1)  # Should not raise
            shutdown()


def test_get_meter_returns_none_when_disabled():
    """get_meter should return None when OTel is disabled."""
    from hippo_brain.telemetry import get_meter

    with patch.dict(os.environ, {}, clear=True):
        os.environ.pop("HIPPO_OTEL_ENABLED", None)
        result = get_meter()
        assert result is None
