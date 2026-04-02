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
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
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
    logger_provider = LoggerProvider(resource=resource)
    log_exporter = OTLPLogExporter(endpoint=f"{endpoint}/v1/logs")
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    handler = LoggingHandler(logger_provider=logger_provider)
    logging.getLogger().addHandler(handler)

    logger.info(
        "OpenTelemetry initialized: endpoint=%s, service=%s",
        endpoint,
        service_name,
    )

    def shutdown() -> None:
        tracer_provider.shutdown()
        logger_provider.shutdown()

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
