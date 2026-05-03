"""OpenTelemetry initialization for Hippo Brain services.

Gated behind HIPPO_OTEL_ENABLED=1 environment variable.
When the gate is unset, all functions are no-ops. When the gate is set
explicitly but the OTel Python packages cannot be imported (e.g., a
half-installed venv after an upgrade), ``init_telemetry`` raises so the
service fails loud rather than silently shipping zero metrics.
"""

import logging
import os

logger = logging.getLogger("hippo_brain.telemetry")

DEFAULT_ENDPOINT = "http://localhost:4318"

# Set to True only after init_telemetry() successfully wires up providers.
# Surfaced via is_telemetry_active() so the /health endpoint and `hippo doctor`
# can distinguish "configured-on AND running" from "configured-on but dead".
_telemetry_active: bool = False


def is_telemetry_enabled() -> bool:
    """Return True iff the HIPPO_OTEL_ENABLED env gate is set.

    Reflects user intent only — does not guarantee providers are initialized.
    Use is_telemetry_active() to check actual runtime state.
    """
    return os.environ.get("HIPPO_OTEL_ENABLED", "").strip() == "1"


def is_telemetry_active() -> bool:
    """Return True only after init_telemetry() has fully succeeded."""
    return _telemetry_active


class TelemetryInitError(RuntimeError):
    """HIPPO_OTEL_ENABLED=1 was set but OTel providers could not be wired up.

    Raised on import failure of any OTel SDK module — almost always a stale
    or half-installed brain venv. The service should crash visibly and let
    launchd report the failure rather than continue without telemetry.
    """


def init_telemetry(
    service_name: str,
    endpoint: str = "",
) -> "callable | None":
    """Initialize OpenTelemetry providers for traces, metrics, and logs.

    Returns a shutdown callable on success, or ``None`` when telemetry is not
    enabled. Raises ``TelemetryInitError`` when telemetry is explicitly
    enabled (``HIPPO_OTEL_ENABLED=1``) but the OTel packages cannot be
    imported — silently degrading would let dashboards go dark unnoticed.
    """
    global _telemetry_active

    if not is_telemetry_enabled():
        return None

    if not endpoint:
        endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", DEFAULT_ENDPOINT)

    try:
        from opentelemetry import metrics as otel_metrics
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        try:
            from opentelemetry.instrumentation.logging.handler import LoggingHandler
        except ImportError:
            from opentelemetry.sdk._logs import LoggingHandler
    except (ImportError, AttributeError) as e:
        # ImportError covers "package missing" and the half-installed-namespace
        # case (dist-info present, package contents empty) seen 2026-04-26.
        # AttributeError covers a partially-extracted `__init__.py` that
        # imports cleanly but is missing the symbols we then access. Both
        # are "deployed venv out of sync with pyproject.toml" — same recovery.
        msg = (
            "HIPPO_OTEL_ENABLED=1 but OpenTelemetry packages cannot be imported "
            "(error: %s). The deployed brain venv is out of sync with "
            "pyproject.toml. Recover with: "
            "`uv sync --project ~/.local/share/hippo-brain --reinstall` then "
            "restart the brain. If you intended to disable telemetry, unset "
            "HIPPO_OTEL_ENABLED."
        ) % e
        logger.error(msg)
        raise TelemetryInitError(msg) from e

    # Resource.create() merges OTEL_RESOURCE_ATTRIBUTES from the environment
    # via the Python SDK default detector chain. bench/shadow_stack.py injects
    # service.namespace=hippo-bench here at process spawn time.
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

    # Mark active BEFORE optional process-metrics registration: the providers
    # are already wired up at this point, and a downstream failure in
    # _register_process_metrics (e.g., psutil.AccessDenied in a sandbox) must
    # not leave the flag desynchronized from "providers initialized" state.
    _telemetry_active = True
    _register_process_metrics()

    logger.info(
        "OpenTelemetry initialized: endpoint=%s, service=%s",
        endpoint,
        service_name,
    )

    def shutdown() -> None:
        global _telemetry_active
        _telemetry_active = False
        tracer_provider.shutdown()
        logger_provider.shutdown()
        meter_provider.shutdown()

    return shutdown


def get_tracer(name: str = "hippo-brain"):
    """Get OTel tracer if available, else return None."""
    if not is_telemetry_enabled():
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
    if not is_telemetry_enabled():
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


def _register_process_metrics() -> None:
    """Register OTel process.* semantic-convention metrics via psutil.

    Uses observable gauges/counters so sampling happens lazily, on the OTel
    export tick, without a separate background task. Safe to call exactly once
    after ``set_meter_provider``; subsequent calls would register duplicate
    instruments and emit SDK warnings.
    """
    try:
        from opentelemetry import metrics as otel_metrics
        from opentelemetry.metrics import CallbackOptions, Observation

        import psutil
    except ImportError as e:
        logger.warning("process metrics unavailable: %s", e)
        return

    proc = psutil.Process()
    # First cpu_percent() call returns 0.0 and seeds the delta baseline.
    # Subsequent calls report utilization over the interval since the last call.
    proc.cpu_percent(interval=None)

    def _safe_observations(get_value) -> list[Observation]:
        try:
            return [Observation(get_value(), {})]
        except psutil.Error:
            # Covers NoSuchProcess / AccessDenied / ZombieProcess / TimeoutExpired —
            # all surface only transient failures we want to soft-ignore.
            return []

    def cpu_cb(_options: CallbackOptions) -> list[Observation]:
        # psutil returns process CPU as percent of a single CPU (100 = one full
        # core). OTel process.cpu.utilization is "fraction of one CPU" per
        # semconv — dividing by 100 matches.
        return _safe_observations(lambda: proc.cpu_percent(interval=None) / 100.0)

    def rss_cb(_options: CallbackOptions) -> list[Observation]:
        return _safe_observations(lambda: proc.memory_info().rss)

    def vms_cb(_options: CallbackOptions) -> list[Observation]:
        return _safe_observations(lambda: proc.memory_info().vms)

    def threads_cb(_options: CallbackOptions) -> list[Observation]:
        return _safe_observations(proc.num_threads)

    def cpu_time_cb(_options: CallbackOptions) -> list[Observation]:
        def _total_ms() -> int:
            times = proc.cpu_times()
            return int((times.user + times.system) * 1000)

        return _safe_observations(_total_ms)

    meter = otel_metrics.get_meter("hippo-brain.process")
    meter.create_observable_gauge(
        "process.cpu.utilization",
        callbacks=[cpu_cb],
        unit="1",
        description=(
            "Difference in process.cpu.time since last observation, "
            "divided by interval time (1.0 = one full CPU)."
        ),
    )
    meter.create_observable_gauge(
        "process.memory.usage",
        callbacks=[rss_cb],
        unit="By",
        description="Resident set size of the process.",
    )
    meter.create_observable_gauge(
        "process.memory.virtual",
        callbacks=[vms_cb],
        unit="By",
        description="Virtual memory size of the process.",
    )
    meter.create_observable_gauge(
        "process.threads",
        callbacks=[threads_cb],
        description="Number of OS threads in the process.",
    )
    meter.create_observable_counter(
        "process.cpu.time",
        callbacks=[cpu_time_cb],
        unit="ms",
        description="Total CPU time consumed by the process.",
    )
