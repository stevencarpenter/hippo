"""OpenTelemetry initialization for Hippo Brain services.

Gated behind HIPPO_OTEL_ENABLED=1 environment variable.
When disabled or when OTel packages are not installed, all functions are no-ops.
"""

import logging
import os

logger = logging.getLogger("hippo_brain.telemetry")

DEFAULT_ENDPOINT = "http://localhost:4318"


def _is_otel_enabled() -> bool:
    return os.environ.get("HIPPO_OTEL_ENABLED", "").strip() == "1"


def init_telemetry(
    service_name: str,
    endpoint: str = "",
) -> "callable | None":
    """Initialize OpenTelemetry providers for traces, metrics, and logs.

    Returns a shutdown callable, or None if OTel is disabled/unavailable.
    """
    if not _is_otel_enabled():
        return None

    if not endpoint:
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_ENDPOINT)

    try:
        from opentelemetry import metrics as otel_metrics
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError:
        logger.warning("OpenTelemetry packages not installed — telemetry disabled")
        return None

    resource = Resource.create({"service.name": service_name})

    # Traces
    tracer_provider = TracerProvider(resource=resource)
    span_exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # Logs — bridge stdlib logging to OTel
    # timeout=30: default 10s is too short during long LLM calls
    # schedule_delay_millis=15000: export every 15s instead of 5s to reduce
    # pressure on the collector when the brain is busy with synthesis
    logger_provider = LoggerProvider(resource=resource)
    log_exporter = OTLPLogExporter(endpoint=f"{endpoint}/v1/logs", timeout=30)
    logger_provider.add_log_record_processor(
        BatchLogRecordProcessor(log_exporter, schedule_delay_millis=15000)
    )
    handler = LoggingHandler(logger_provider=logger_provider)
    logging.getLogger().addHandler(handler)

    # Metrics
    metric_exporter = OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics")
    metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=15000)
    meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
    otel_metrics.set_meter_provider(meter_provider)

    logger.info(
        "OpenTelemetry initialized: endpoint=%s, service=%s",
        endpoint,
        service_name,
    )

    def shutdown() -> None:
        tracer_provider.shutdown()
        logger_provider.shutdown()
        meter_provider.shutdown()

    return shutdown


def get_tracer(name: str = "hippo-brain"):
    """Get OTel tracer if available, else return None."""
    if not _is_otel_enabled():
        return None
    try:
        from opentelemetry import trace

        return trace.get_tracer(name)
    except ImportError:
        return None


def get_meter(name: str = "hippo-brain"):
    """Get OTel meter if available, else return None.

    The returned meter is a global proxy — instruments created against it
    will pick up the real MeterProvider once ``init_telemetry()`` calls
    ``set_meter_provider()``.
    """
    if not _is_otel_enabled():
        return None
    try:
        from opentelemetry import metrics as otel_metrics

        return otel_metrics.get_meter(name)
    except ImportError:
        return None


def add(counter, value=1, **attrs):
    """Increment an OTel counter if it exists (no-op when ``None``)."""
    if counter:
        counter.add(value, attrs)


def hist(histogram, value, **attrs):
    """Record an OTel histogram value if it exists (no-op when ``None``)."""
    if histogram:
        histogram.record(value, attrs)
