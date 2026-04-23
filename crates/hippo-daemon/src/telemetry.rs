//! OpenTelemetry initialization — only compiled with `--features otel`.

use anyhow::Result;
use opentelemetry::global;
use opentelemetry::trace::TracerProvider as _;
use opentelemetry_appender_tracing::layer::OpenTelemetryTracingBridge;
use opentelemetry_otlp::{LogExporter, MetricExporter, SpanExporter, WithExportConfig};
use opentelemetry_sdk::Resource;
use opentelemetry_sdk::logs::SdkLoggerProvider;
use opentelemetry_sdk::metrics::SdkMeterProvider;
use opentelemetry_sdk::trace::SdkTracerProvider;
use tracing::info;
use tracing_opentelemetry::OpenTelemetryLayer;
use tracing_subscriber::EnvFilter;
use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;

fn resource(service_name: &str) -> Resource {
    Resource::builder()
        .with_service_name(service_name.to_string())
        .build()
}

pub struct TelemetryGuard {
    tracer_provider: SdkTracerProvider,
    meter_provider: SdkMeterProvider,
    logger_provider: SdkLoggerProvider,
}

impl TelemetryGuard {
    pub fn shutdown(self) {
        if let Err(e) = self.tracer_provider.shutdown() {
            eprintln!("tracer shutdown error: {e}");
        }
        if let Err(e) = self.meter_provider.shutdown() {
            eprintln!("meter shutdown error: {e}");
        }
        if let Err(e) = self.logger_provider.shutdown() {
            eprintln!("logger shutdown error: {e}");
        }
    }
}

/// Initialize OTel tracing subscriber with OTLP exporters.
/// Replaces the default `tracing_subscriber::fmt` when OTel is enabled.
/// Returns a guard that must be held for the lifetime of the program.
///
/// `writer` is the log destination for the fmt layer (typically a
/// `tracing_appender::non_blocking::NonBlocking` file writer).
pub fn init<W>(service_name: &str, endpoint: &str, writer: W) -> Result<TelemetryGuard>
where
    W: for<'a> tracing_subscriber::fmt::MakeWriter<'a> + Send + Sync + 'static,
{
    let res = resource(service_name);

    // Traces
    let span_exporter = SpanExporter::builder()
        .with_tonic()
        .with_endpoint(endpoint)
        .build()?;
    let tracer_provider = SdkTracerProvider::builder()
        .with_resource(res.clone())
        .with_batch_exporter(span_exporter)
        .build();

    // Metrics
    let metric_exporter = MetricExporter::builder()
        .with_tonic()
        .with_endpoint(endpoint)
        .build()?;
    let meter_provider = SdkMeterProvider::builder()
        .with_resource(res.clone())
        .with_periodic_exporter(metric_exporter)
        .build();

    // Logs
    let log_exporter = LogExporter::builder()
        .with_tonic()
        .with_endpoint(endpoint)
        .build()?;
    let logger_provider = SdkLoggerProvider::builder()
        .with_resource(res)
        .with_batch_exporter(log_exporter)
        .build();

    // Set global providers
    global::set_tracer_provider(tracer_provider.clone());
    global::set_meter_provider(meter_provider.clone());

    // Get a tracer from the provider (OpenTelemetryLayer needs a Tracer, not a TracerProvider)
    let tracer = tracer_provider.tracer(service_name.to_string());

    // Build the tracing subscriber with OTel layers.
    // EnvFilter must come last so that OpenTelemetryLayer sees a Subscriber that
    // still implements LookupSpan (which EnvFilter wrapping would hide).
    // Suppress OTel SDK export errors from stderr — these fire every batch interval
    // when the collector is unreachable and are not actionable application errors.
    // Note: RUST_LOG, if set, replaces this default entirely (including suppressions).
    let env_filter = EnvFilter::try_from_default_env().unwrap_or_else(|_| {
        EnvFilter::new("info,opentelemetry_sdk=off,opentelemetry_otlp=off,opentelemetry_http=off")
    });

    let fmt_layer = tracing_subscriber::fmt::layer().with_writer(writer);

    let otel_trace_layer = OpenTelemetryLayer::new(tracer);

    let otel_log_layer = OpenTelemetryTracingBridge::new(&logger_provider);

    tracing_subscriber::registry()
        .with(fmt_layer)
        .with(otel_trace_layer)
        .with(otel_log_layer)
        .with(env_filter)
        .init();

    info!("OpenTelemetry initialized: endpoint={endpoint}, service={service_name}");

    Ok(TelemetryGuard {
        tracer_provider,
        meter_provider,
        logger_provider,
    })
}
