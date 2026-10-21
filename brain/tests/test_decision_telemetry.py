"""Decision metrics use bounded labels, measured usage, and advisory exporters."""

from collections import defaultdict

import pytest

from hippo_brain import telemetry


PREFIX = "hippo.brain.decision."


class FakeInstrument:
    def __init__(self, records):
        self.records = records

    def add(self, value, attributes):
        self.records.append((value, attributes))

    record = add


class FakeMeter:
    def __init__(self):
        self.records = defaultdict(list)

    def create_counter(self, name, **_):
        return FakeInstrument(self.records[name])

    create_histogram = create_counter


@pytest.fixture
def meter(monkeypatch):
    result = FakeMeter()
    monkeypatch.setattr(telemetry, "get_meter", lambda _: result)
    return result


def values(meter, name):
    return [value for value, _ in meter.records[PREFIX + name]]


def test_decision_metrics_record_known_usage_and_distinct_overlapping_stages(meter):
    telemetry.record_decision_metrics(
        {
            "backend": "jev",
            "stop_reason": "single_pass",
            "elapsed_ms": 200,
            "rule_ms": 1,
            "passes": [
                {
                    "elapsed_ms": 100,
                    "composition_ms": 2,
                    "source_fetch_ms": 5,
                    "transport": {"network_calls": 1, "queue_ms": 10, "http_ms": 90},
                    "usage": {"input_tokens": 101, "output_tokens": 5},
                },
                {
                    "elapsed_ms": 80,
                    "transport": {"network_calls": 1, "queue_ms": 0, "http_ms": 80},
                    "usage": None,
                },
            ],
        }
    )
    assert values(meter, "count") == [1]
    assert values(meter, "requests") == [1, 1]
    assert values(meter, "requests_unknown") == []
    assert values(meter, "usage_unknown") == [1]
    assert values(meter, "tokens") == [101, 5]
    assert values(meter, "errors") == []
    durations = defaultdict(list)
    for value, attrs in meter.records[PREFIX + "duration"]:
        durations[attrs["stage"]].append(value)
        assert attrs["outcome"] == "success"
    assert dict(durations) == {
        "decision": [200],
        "rules": [1],
        "attempt": [100, 80],
        "composition": [2],
        "source_fetch": [5],
        "queue": [10, 0],
        "http": [90, 80],
    }


def test_decision_classification_missing_transport_and_usage_stay_unknown(meter):
    telemetry.record_decision_metrics(
        {
            "backend": "jev",
            "status": "ready",
            "total_ms": 70,
            "network_ms": 60,
            "apply_ms": 5,
            "usage": None,
            "node_uuid": "private-node",
            "recipe_hash": "private-recipe",
        },
        task="classification",
    )
    assert values(meter, "count") == [1]
    assert values(meter, "requests") == []
    assert values(meter, "requests_unknown") == [1]
    assert values(meter, "usage_unknown") == [1]
    assert values(meter, "tokens") == []
    assert {attrs["stage"] for _, attrs in meter.records[PREFIX + "duration"]} == {
        "decision",
        "attempt",
        "sql_apply",
    }


def test_decision_queue_cancellation_is_not_a_dispatched_request(meter):
    telemetry.record_decision_metrics(
        {
            "backend": "jev",
            "stop_reason": "cancelled",
            "elapsed_ms": 5,
            "passes": [
                {
                    "elapsed_ms": 5,
                    "usage": None,
                    "transport": {"network_calls": 0, "queue_ms": 5, "http_ms": None},
                }
            ],
        }
    )
    assert values(meter, "requests") == [0]
    assert values(meter, "usage_unknown") == values(meter, "tokens") == []
    assert values(meter, "errors") == []
    assert all(
        attrs["outcome"] == "cancelled"
        for records in meter.records.values()
        for _, attrs in records
    )


def test_decision_labels_never_include_identifiers_or_unbounded_input(meter):
    telemetry.record_decision_metrics(
        {
            "backend": "secret-provider-name",
            "stop_reason": "private exception text",
            "elapsed_ms": 1,
            "node_uuid": "private-node",
            "input_hash": "private-hash",
            "passes": [{"usage": {"input_tokens": 2, "output_tokens": 1}}],
        },
        task="private-query-text",
    )
    for records in meter.records.values():
        for _, attrs in records:
            assert set(attrs) <= {"backend", "task", "outcome", "stage", "direction"}
            assert attrs["backend"] == attrs["task"] == attrs["outcome"] == "unknown"
            assert not any(
                "private" in str(value) or "secret" in str(value) for value in attrs.values()
            )


def test_decision_fallback_counts_errors_without_exception_labels(meter):
    telemetry.record_decision_metrics(
        {
            "backend": "jev",
            "stop_reason": "deadline",
            "fallback_reason": "TimeoutError",
            "elapsed_ms": 2000,
            "passes": [],
        }
    )
    assert values(meter, "errors") == [1]
    assert values(meter, "requests") == values(meter, "usage_unknown") == []
    assert meter.records[PREFIX + "count"][0][1]["outcome"] == "fallback"


def test_decision_only_finite_nonnegative_timings_and_provider_token_fields_are_used(meter):
    telemetry.record_decision_metrics(
        {
            "backend": "jev",
            "stop_reason": "single_pass",
            "elapsed_ms": float("nan"),
            "passes": [
                {
                    "elapsed_ms": -1,
                    "composition_ms": True,
                    "source_fetch_ms": float("inf"),
                    "transport": {"network_calls": True, "queue_ms": None, "http_ms": -4},
                    "usage": {"input_tokens": 0, "output_tokens": float("nan")},
                    "estimated_tokens": 9999,
                }
            ],
        }
    )
    assert values(meter, "duration") == []
    assert values(meter, "tokens") == [0]  # Zero is only valid when explicitly reported.
    assert values(meter, "usage_unknown") == [1]
    assert values(meter, "requests_unknown") == [1]
    assert values(meter, "requests") == []


def test_decision_metrics_disabled_do_not_import_or_start_exporters(monkeypatch):
    import sys

    monkeypatch.delenv("HIPPO_OTEL_ENABLED", raising=False)
    monkeypatch.setitem(sys.modules, "opentelemetry", None)
    telemetry.record_decision_metrics({"backend": "jev", "elapsed_ms": 1, "passes": []})


@pytest.mark.parametrize("failure", ["meter", "instrument"])
def test_decision_metrics_exporter_failure_cannot_fail_a_decision(monkeypatch, failure):
    def fail(*_, **__):
        raise RuntimeError("exporter failed")

    if failure == "meter":
        monkeypatch.setattr(telemetry, "get_meter", fail)
    else:
        meter = FakeMeter()
        monkeypatch.setattr(telemetry, "get_meter", lambda _: meter)
        monkeypatch.setattr(FakeInstrument, "add", fail)
    telemetry.record_decision_metrics({"backend": "jev", "stop_reason": "single_pass"})


def test_prime_exports_zero_counters_on_the_same_label_sets_the_recorder_uses(meter):
    telemetry.prime_decision_metrics("jev", classification=True)
    telemetry.record_decision_metrics(
        {
            "backend": "jev",
            "stop_reason": "single_pass",
            "passes": [
                {
                    "transport": {"network_calls": 1},
                    "usage": {"input_tokens": 11, "output_tokens": 3},
                }
            ],
        }
    )
    success_input = {"backend": "jev", "task": "rerank", "outcome": "success", "direction": "input"}
    tokens = meter.records[PREFIX + "tokens"]
    assert [value for value, attrs in tokens if attrs == success_input] == [0, 11]
    assert (0, {**success_input, "outcome": "fallback"}) in tokens
    assert (0, {**success_input, "task": "classification", "direction": "output"}) in tokens
    for name in ("count", "requests", "usage_unknown", "requests_unknown"):
        assert (0, {"backend": "jev", "task": "rerank", "outcome": "success"}) in meter.records[
            PREFIX + name
        ]
    # A histogram cannot hold a zero placeholder without a fake observation.
    assert meter.records[PREFIX + "duration"] == []


def label_sets(meter):
    return {
        name.removeprefix(PREFIX): {frozenset(attrs.items()) for _, attrs in records}
        for name, records in meter.records.items()
        if name != PREFIX + "duration"
    }


def test_prime_covers_only_producible_outcomes_and_error_outcomes(meter):
    telemetry.prime_decision_metrics("jev", classification=True)
    primed = label_sets(meter)

    def outcomes(name, task):
        return {dict(attrs)["outcome"] for attrs in primed[name] if dict(attrs)["task"] == task}

    assert outcomes("count", "rerank") == {"success", "fallback", "cancelled"}
    assert outcomes("count", "classification") == {
        "success",
        "stale",
        "cancelled",
        "failed",
        "retry",
    }
    assert outcomes("errors", "rerank") == {"fallback"}
    assert outcomes("errors", "classification") == {"failed", "retry"}


RERANK_PASS = {
    "transport": {"network_calls": 1},
    "usage": {"input_tokens": 11, "output_tokens": 3},
}
FAILED_PASS = {"status": "failed", "usage": None, "transport": None}


@pytest.mark.parametrize(
    "diagnostics",
    [
        {"stop_reason": "single_pass", "passes": [RERANK_PASS]},
        {"stop_reason": "invalid_decision", "fallback_reason": "ValueError", "passes": []},
        {"stop_reason": "deadline", "fallback_reason": "TimeoutError", "passes": [FAILED_PASS]},
        {"stop_reason": "cancelled", "passes": [{**FAILED_PASS, "status": "cancelled"}]},
    ],
    ids=["success", "fallback-invalid", "fallback-deadline", "cancelled"],
)
@pytest.mark.parametrize("backend", ["jev", "rules"])
def test_recorded_rerank_label_sets_were_primed(monkeypatch, backend, diagnostics):
    primed_meter, recorded_meter = FakeMeter(), FakeMeter()
    monkeypatch.setattr(telemetry, "get_meter", lambda _: primed_meter)
    telemetry.prime_decision_metrics(backend)
    monkeypatch.setattr(telemetry, "get_meter", lambda _: recorded_meter)
    telemetry.record_decision_metrics({**diagnostics, "backend": backend}, task="rerank")
    primed, recorded = label_sets(primed_meter), label_sets(recorded_meter)
    assert recorded["count"]
    for name, attrs in recorded.items():
        assert attrs <= primed[name], name


@pytest.mark.parametrize("status", ["ready", "stale", "cancelled", "failed", "retry"])
def test_recorded_classification_label_sets_were_primed(monkeypatch, status):
    primed_meter, recorded_meter = FakeMeter(), FakeMeter()
    monkeypatch.setattr(telemetry, "get_meter", lambda _: primed_meter)
    telemetry.prime_decision_metrics(None, classification=True)
    monkeypatch.setattr(telemetry, "get_meter", lambda _: recorded_meter)
    for record in (
        {"backend": "jev", "status": status, "usage": None, "transport": None},
        {"backend": "jev", "status": status, **RERANK_PASS},
    ):
        telemetry.record_decision_metrics(record, task="classification")
    primed, recorded = label_sets(primed_meter), label_sets(recorded_meter)
    assert recorded["count"]
    for name, attrs in recorded.items():
        assert attrs <= primed[name], name


def test_prime_flushes_the_global_provider_and_tolerates_one_without_flush(meter, monkeypatch):
    from opentelemetry import metrics as otel_metrics

    class Flushing:
        def __init__(self):
            self.calls = []

        def force_flush(self, timeout_millis):
            self.calls.append(timeout_millis)

    provider = Flushing()
    monkeypatch.setattr(otel_metrics, "get_meter_provider", lambda: provider)
    telemetry.prime_decision_metrics("jev")
    assert len(provider.calls) == 1
    assert meter.records[PREFIX + "count"]

    monkeypatch.setattr(otel_metrics, "get_meter_provider", object)
    telemetry.prime_decision_metrics("jev")

    def broken(timeout_millis):
        raise RuntimeError("collector down")

    provider.force_flush = broken
    monkeypatch.setattr(otel_metrics, "get_meter_provider", lambda: provider)
    telemetry.prime_decision_metrics("jev")


def test_prime_skips_local_backend_and_disabled_classification(meter):
    telemetry.prime_decision_metrics("local")
    telemetry.prime_decision_metrics(None)
    assert dict(meter.records) == {}


def test_prime_produces_zero_data_points_through_the_otel_sdk(monkeypatch):
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    monkeypatch.setattr(telemetry, "get_meter", provider.get_meter)
    try:
        telemetry.prime_decision_metrics("jev")
        data = reader.get_metrics_data()
        points = {
            (metric.name, tuple(sorted(point.attributes.items()))): point.value
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
            for point in metric.data.data_points
        }
    finally:
        provider.shutdown()
    labels = (
        ("backend", "jev"),
        ("direction", "input"),
        ("outcome", "success"),
        ("task", "rerank"),
    )
    assert points[(PREFIX + "tokens", labels)] == 0
