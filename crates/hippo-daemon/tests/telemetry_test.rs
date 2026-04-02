//! Integration test: verify OTel telemetry module initializes and shuts down cleanly.
//! Only compiled with --features otel.

#[cfg(feature = "otel")]
mod otel_tests {
    use hippo_daemon::telemetry;

    /// Test that init succeeds when pointing at a non-existent collector.
    /// The batch exporter buffers spans — it won't fail until export time.
    /// We just verify the init/shutdown cycle doesn't panic.
    ///
    /// A Tokio runtime is required because the OTLP gRPC exporter (tonic) spawns
    /// background tasks that need a reactor.
    #[tokio::test]
    async fn test_telemetry_init_shutdown() {
        let guard = telemetry::init("test-service", "http://localhost:19999")
            .expect("telemetry init should succeed even without a collector");
        guard.shutdown();
    }
}
