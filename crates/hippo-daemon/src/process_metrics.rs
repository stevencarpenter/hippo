//! OpenTelemetry process.* semantic-convention metrics for hippo-daemon.
//!
//! Samples the current process's CPU utilization, RSS/VSZ memory, thread count,
//! and cumulative CPU time at a fixed interval via `sysinfo`, then exposes them
//! as OTel observable gauges and a counter. Only compiled with `--features otel`.
//!
//! Implementation note: a background tokio task refreshes `sysinfo` state on a
//! timer and stores the latest values in atomics. Observable callbacks read the
//! atomics without touching `sysinfo` themselves — this decouples sampling
//! cadence from exporter cadence and keeps the callbacks lock-free.

use opentelemetry::global;
use std::process;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};
use sysinfo::{Pid, ProcessRefreshKind, ProcessesToUpdate, System};
use tokio::time::{self, Duration};

/// How often the background task refreshes sysinfo and updates atomics.
/// 10s lines up with a typical 10–15s OTel export interval without
/// oversampling. CPU% is computed by sysinfo across the refresh delta.
const REFRESH_INTERVAL_SECS: u64 = 10;

/// Shared sampling state. Atomics so observable callbacks are lock-free and
/// can run concurrently with the refresh task.
struct Samples {
    /// CPU utilization × 10_000. Divided back to f64 in the 0.0–N.0 range
    /// (where N is the CPU count) when exported. Integer storage keeps the
    /// struct Send + Sync without a Mutex.
    cpu_ratio_x10k: AtomicU64,
    rss_bytes: AtomicU64,
    vms_bytes: AtomicU64,
    accumulated_cpu_ms: AtomicU64,
}

impl Samples {
    const fn new() -> Self {
        Self {
            cpu_ratio_x10k: AtomicU64::new(0),
            rss_bytes: AtomicU64::new(0),
            vms_bytes: AtomicU64::new(0),
            accumulated_cpu_ms: AtomicU64::new(0),
        }
    }
}

/// Register process.* observable gauges + spawn the refresh task. Call once,
/// from `daemon::run` after telemetry init, under `cfg(feature = "otel")`.
///
/// Gauges stay alive because the SDK retains them via the registered callback;
/// we drop the builder return values to avoid holding handles we don't need.
pub fn register() {
    let samples = Arc::new(Samples::new());
    spawn_refresh_task(Arc::clone(&samples));

    let meter = global::meter("hippo-daemon");

    let s_cpu = Arc::clone(&samples);
    let _ = meter
        .f64_observable_gauge("process.cpu.utilization")
        .with_description("Difference in process.cpu.time since last observation, divided by interval time (1.0 = one full CPU).")
        .with_unit("1")
        .with_callback(move |g| {
            let raw = s_cpu.cpu_ratio_x10k.load(Ordering::Relaxed);
            g.observe(raw as f64 / 10_000.0, &[]);
        })
        .build();

    let s_rss = Arc::clone(&samples);
    let _ = meter
        .u64_observable_gauge("process.memory.usage")
        .with_description("Resident set size of the process.")
        .with_unit("By")
        .with_callback(move |g| {
            g.observe(s_rss.rss_bytes.load(Ordering::Relaxed), &[]);
        })
        .build();

    let s_vms = Arc::clone(&samples);
    let _ = meter
        .u64_observable_gauge("process.memory.virtual")
        .with_description("Virtual memory size of the process.")
        .with_unit("By")
        .with_callback(move |g| {
            g.observe(s_vms.vms_bytes.load(Ordering::Relaxed), &[]);
        })
        .build();

    let s_cpu_time = Arc::clone(&samples);
    let _ = meter
        .u64_observable_counter("process.cpu.time")
        .with_description("Total CPU time consumed by the process.")
        .with_unit("ms")
        .with_callback(move |c| {
            c.observe(s_cpu_time.accumulated_cpu_ms.load(Ordering::Relaxed), &[]);
        })
        .build();
}

fn spawn_refresh_task(samples: Arc<Samples>) {
    tokio::spawn(async move {
        let self_pid = Pid::from_u32(process::id());
        let mut sys = System::new();
        let mut interval = time::interval(Duration::from_secs(REFRESH_INTERVAL_SECS));
        // First tick returns immediately; second tick is REFRESH_INTERVAL_SECS
        // away. The first refresh seeds the CPU delta baseline (cpu_usage()
        // returns 0.0 on first call), so the first meaningful CPU sample lands
        // at t = 2 * REFRESH_INTERVAL_SECS after startup.
        loop {
            interval.tick().await;
            sys.refresh_processes_specifics(
                ProcessesToUpdate::Some(&[self_pid]),
                true,
                ProcessRefreshKind::nothing().with_cpu().with_memory(),
            );
            if let Some(proc) = sys.process(self_pid) {
                // cpu_usage() returns percent-of-one-core (0..100*N on N cores).
                // OTel process.cpu.utilization is "fraction of one CPU" per
                // semconv, matching this semantics. Store ×10_000 for 4-decimal
                // precision without floats in atomics.
                let cpu_x10k = (proc.cpu_usage() * 100.0) as u64;
                samples.cpu_ratio_x10k.store(cpu_x10k, Ordering::Relaxed);
                samples.rss_bytes.store(proc.memory(), Ordering::Relaxed);
                samples
                    .vms_bytes
                    .store(proc.virtual_memory(), Ordering::Relaxed);
                samples
                    .accumulated_cpu_ms
                    .store(proc.accumulated_cpu_time(), Ordering::Relaxed);
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn samples_default_zero() {
        let s = Samples::new();
        assert_eq!(s.cpu_ratio_x10k.load(Ordering::Relaxed), 0);
        assert_eq!(s.rss_bytes.load(Ordering::Relaxed), 0);
        assert_eq!(s.vms_bytes.load(Ordering::Relaxed), 0);
        assert_eq!(s.accumulated_cpu_ms.load(Ordering::Relaxed), 0);
    }

    #[tokio::test]
    async fn refresh_task_populates_memory_sample() {
        let samples = Arc::new(Samples::new());
        spawn_refresh_task(Arc::clone(&samples));
        // First tick is immediate; wait briefly for it to complete.
        tokio::time::sleep(Duration::from_millis(250)).await;
        // Memory is available on the first refresh (no delta required).
        assert!(samples.rss_bytes.load(Ordering::Relaxed) > 0);
        assert!(samples.vms_bytes.load(Ordering::Relaxed) > 0);
    }
}
