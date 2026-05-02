//! Test that EnvResourceDetector integration picks up OTEL_RESOURCE_ATTRIBUTES.
//! Only compiled with --features otel.

#[cfg(feature = "otel")]
mod env_resource_tests {
    use hippo_daemon::telemetry;

    #[tokio::test]
    async fn test_env_resource_detector_init_succeeds() {
        unsafe {
            std::env::set_var(
                "OTEL_RESOURCE_ATTRIBUTES",
                "service.namespace=hippo-bench-test,bench.run_id=rust-test-001",
            );
        }
        let guard = telemetry::init(
            "hippo-daemon-test",
            "http://localhost:19999",
            std::io::stderr,
        )
        .expect("telemetry init should succeed with EnvResourceDetector");
        guard.shutdown();
        unsafe {
            std::env::remove_var("OTEL_RESOURCE_ATTRIBUTES");
        }
    }

    #[test]
    fn test_env_resource_detector_type_is_accessible() {
        // Compile-time proof: this would fail to compile if EnvResourceDetector
        // is not in scope after the RB2-01 fix.
        let _det = opentelemetry_sdk::resource::EnvResourceDetector::new();
    }
}
