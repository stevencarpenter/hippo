"""Dashboard and alert names must resolve to metrics collected at runtime.

Python instruments use an isolated SDK reader. Rust emission and gauge-unit
contracts run in crates/hippo-daemon/tests/dashboard_metrics.rs. The standalone
knowledge exporter is scraped against a synthetic database below.
"""

import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

# ---------------------------------------------------------------------------
# Repo root, resolved relative to this file so the tests work from any cwd.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DASHBOARDS_DIR = _REPO_ROOT / "otel" / "grafana" / "dashboards"
_ALERTING_DIR = _REPO_ROOT / "otel" / "grafana" / "alerting"

# ---------------------------------------------------------------------------
# Canonical EMITTED metric names for production dashboards.
#
# Python entries are checked against SDK-collected data below. Rust entries are
# exercised by the dashboard_metrics integration test in hippo-daemon.
# Naming rules (OTel -> Prometheus exporter):
#   - dots -> underscores
#   - unit="ms"  -> _milliseconds suffix
#   - unit="By"  -> _bytes suffix
#   - unit="1"   -> _ratio suffix (a trap for scores/counts; we drop the unit instead)
#   - no unit    -> no suffix, bare name (the health / source_health gauges)
#   - counters   -> _total suffix (included in the shared contract)
#   - histograms -> only _bucket / _count / _sum samples appear in Prometheus
# ---------------------------------------------------------------------------
METRIC_CONTRACT: dict[str, str] = json.loads(
    (_REPO_ROOT / "tests" / "fixtures" / "otel-metric-names.json").read_text()
)
EMITTED_METRICS = frozenset(METRIC_CONTRACT)


def _sample_names(name: str, kind: str) -> set[str]:
    if kind == "histogram":
        return {name + suffix for suffix in ("_bucket", "_count", "_sum")}
    return {name}


# Production dashboard file names (bench dashboards will be deleted per the
# isolation decision; only these five are expected).
_PROD_DASHBOARD_NAMES = frozenset(
    [
        "hippo-overview.json",
        "hippo-daemon.json",
        "hippo-enrichment.json",
        "hippo-processes.json",
        "hippo-knowledge-health.json",
    ]
)

# Provisioned capture-reliability alert rules (SNUG-96) + knowledge-health rules.
_PROD_ALERT_FILES = frozenset(["hippo-capture-alerts.yml", "hippo-knowledge-alerts.yml"])

# ---------------------------------------------------------------------------
# Knowledge-health exporter metrics (scripts/hippo-metrics-exporter.py).
#
# These are NOT OTel instruments: they are computed by a stdlib Python exporter
# that reads hippo.db read-only and probes brain /ask. Dashboards and alert
# rules may reference EMITTED_METRICS (OTel) ∪ _EXPORTER_METRICS (exporter).
# Runtime scrape tests below hold this registry accountable to emitted samples.
# ---------------------------------------------------------------------------
_EXPORTER_SCRIPT = _REPO_ROOT / "scripts" / "hippo-metrics-exporter.py"


def _load_exporter():
    """Import scripts/hippo-metrics-exporter.py as a module (hyphenated filename)."""
    spec = importlib.util.spec_from_file_location("hippo_metrics_exporter", _EXPORTER_SCRIPT)
    assert spec and spec.loader, f"cannot load exporter from {_EXPORTER_SCRIPT}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_EXPORTER = _load_exporter()

# The allow-list is DERIVED from the exporter's own registry, not restated here.
# Test 12 then renders the exporter against a synthetic database and asserts the
# registry is honest: every declared name is actually emitted.
_EXPORTER_METRICS: frozenset[str] = frozenset(
    _EXPORTER.METRIC_NAMES
    + _EXPORTER.DEFERRED_METRIC_NAMES
    + _EXPORTER.COUNTER_METRIC_NAMES
    + _EXPORTER.FUTURE_METRIC_NAMES
)

DASHBOARD_ALLOWED_METRICS = _EXPORTER_METRICS | {
    sample for name, kind in METRIC_CONTRACT.items() for sample in _sample_names(name, kind)
}

_REQUIRED_ALERT_UIDS = frozenset(
    [
        "hippo_daemon_events_dropped",
        "hippo_watcher_events_dropped",
        "hippo_watchdog_stall",
        "hippo_probe_failure_rate",
        "hippo_invariant_violation",
    ]
)

# Knowledge-health alert rules (recall, graveyard, hygiene, security canaries).
_REQUIRED_KB_ALERT_UIDS = frozenset(
    [
        "hippo_kb_recall_down",
        "hippo_kb_recall_slow",
        "hippo_kb_recall_failures",
        "hippo_kb_alarms_lingering",
        "hippo_kb_capture_stale",
        "hippo_kb_graveyard_contamination",
        "hippo_kb_stranded_hours_jump",
        "hippo_kb_env_secret_keys",
        "hippo_kb_canary_leak",
        "hippo_kb_collector_errors",
    ]
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_prod_dashboards() -> list[tuple[str, dict]]:
    """Return (filename, parsed_json) for every production dashboard."""
    results = []
    for name in sorted(_PROD_DASHBOARD_NAMES):
        path = _DASHBOARDS_DIR / name
        assert path.exists(), (
            f"Expected production dashboard not found: {path}. "
            "If it was intentionally removed, update _PROD_DASHBOARD_NAMES in this test."
        )
        results.append((name, json.loads(path.read_text())))
    return results


def _iter_panels(dashboard: dict):
    """Yield every panel, including those nested inside row panels."""
    for panel in dashboard.get("panels", []):
        yield panel
        # Row panels may contain nested panels
        for nested in panel.get("panels", []):
            yield nested


def _extract_metric_names(expr: str) -> list[str]:
    """Extract all metric name tokens from a PromQL expression.

    Matches both the hippo_* instruments and the process_* OTel
    semantic-convention instruments emitted by the daemon and brain (the
    hippo-processes.json dashboard queries the latter). Returns raw names as
    they appear (may include _bucket/_count/_sum).

    Label-matcher blocks ({...}) and grouping/matching clauses
    (by/without/on/ignoring/group_left/group_right (...)) are stripped first so
    a label KEY that shares the prefix — e.g. {process_command=~"..."} — is not
    mistaken for a metric name; metric names never appear inside those
    constructs. The negative lookbehind keeps a match from starting inside a
    longer identifier or a recording-rule name (ns:hippo_x). We deliberately do
    NOT add a trailing "(?!=)" guard: that would drop a real metric used in a
    comparison such as `hippo_x == 5`, which is a worse failure (an unguarded
    metric) than the label-key false positive it would prevent.
    """
    cleaned = re.sub(r"\{[^{}]*\}", " ", expr)
    cleaned = re.sub(
        r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\([^()]*\)",
        " ",
        cleaned,
    )
    return re.findall(r"(?<![A-Za-z0-9_:])(?:hippo|process)_[a-z0-9_]+", cleaned)


def _collect_all_exprs(dashboard: dict) -> list[tuple[int, str, str]]:
    """Return list of (panel_id, refId, expr) for every Prometheus target."""
    results = []
    for panel in _iter_panels(dashboard):
        panel_id = panel.get("id", -1)
        for target in panel.get("targets", []):
            ds = target.get("datasource", {})
            # Only check Prometheus targets; skip Tempo / Loki
            if isinstance(ds, dict) and ds.get("type") == "prometheus":
                expr = target.get("expr", "")
                if expr:
                    results.append((panel_id, target.get("refId", "?"), expr))
    return results


# ---------------------------------------------------------------------------
# Test 1: Every hippo_* metric referenced in a production dashboard must be
# in the EMITTED_METRICS allow-list.
# ---------------------------------------------------------------------------


def test_all_referenced_metrics_are_allowed():
    """Every hippo_* metric name in every production dashboard PromQL must be
    in the canonical EMITTED_METRICS set.

    A failure here means either:
      (a) a metric was renamed in its emitter (OTel instrument or the
          knowledge-health exporter) and the dashboard was not updated, or
      (b) a metric was removed from its emitter but the dashboard still
          references the old name.

    Fix: update the dashboard expr to use the new name, OR add the new
    instrument to brain/src/hippo_brain/ (or the Rust daemon) and update
    EMITTED_METRICS, OR add it to scripts/hippo-metrics-exporter.py and update
    _EXPORTER_METRICS in this file.
    """
    violations: list[str] = []

    for filename, dashboard in _load_prod_dashboards():
        for panel_id, ref_id, expr in _collect_all_exprs(dashboard):
            for raw_name in _extract_metric_names(expr):
                if raw_name not in DASHBOARD_ALLOWED_METRICS:
                    violations.append(
                        f"  dashboard={filename!r}  panel_id={panel_id}  "
                        f"refId={ref_id!r}  metric={raw_name!r} "
                        f"expr={expr!r}"
                    )

    assert not violations, (
        "The following production dashboard panels reference hippo_* metrics "
        "that are NOT in the EMITTED_METRICS ∪ _EXPORTER_METRICS allow-list in "
        "this test file.\n"
        "Update the dashboard to use the correct metric name, or add the "
        "instrument and update EMITTED_METRICS / _EXPORTER_METRICS.\n\n" + "\n".join(violations)
    )


def _selector_label_names(expr: str) -> set[str]:
    tokens = re.findall(
        r'''"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`[^`]*`|\#[^\n]*|[a-zA-Z_][a-zA-Z0-9_]*|=~|!~|!=|[{}=,]''',
        expr,
    )
    tokens = [token for token in tokens if not token.startswith("#")]
    labels = set()
    in_selector = False
    for index, token in enumerate(tokens[:-1]):
        if token == "{":
            in_selector = True
        elif token == "}":
            in_selector = False
        elif in_selector and tokens[index + 1] in {"=", "!=", "=~", "!~"}:
            labels.add(json.loads(token) if token.startswith('"') else token)
    return labels


def test_no_service_namespace_filter_in_prod_dashboards():
    violations = [
        f"dashboard={filename!r} panel_id={panel_id} refId={ref_id!r} expr={expr!r}"
        for filename, dashboard in _load_prod_dashboards()
        for panel_id, ref_id, expr in _collect_all_exprs(dashboard)
        if "service_namespace" in _selector_label_names(expr)
    ]
    assert not violations, "Retired service_namespace selectors:\n" + "\n".join(violations)


@pytest.mark.parametrize(
    ("expr", "rejected"),
    [
        ('hippo_x{status="service_namespace"}', False),
        (r'hippo_x{status="{service_namespace=\"x\"}"}', False),
        ("hippo_x{status='{service_namespace=\"x\"}'}", False),
        ('hippo_x{status=`{service_namespace="x"}`}', False),
        ('hippo_x # {service_namespace="x"}', False),
        *[
            (f'hippo_x{{status="ok", service_namespace {op} "x"}}', True)
            for op in ("=", "!=", "=~", "!~")
        ],
        ('{ "service_namespace" = "x" }', True),
        (r'hippo_x{status="brace } and escaped \"", service_namespace="x"}', True),
    ],
)
def test_service_namespace_selector_policy(monkeypatch, expr, rejected):
    dashboard = {
        "description": "Retired service_namespace filter",
        "panels": [
            {
                "panels": [
                    {
                        "id": 7,
                        "targets": [
                            {"refId": "A", "datasource": {"type": "prometheus"}, "expr": expr}
                        ],
                    }
                ]
            }
        ],
    }
    monkeypatch.setattr(
        sys.modules[__name__], "_load_prod_dashboards", lambda: [("fixture.json", dashboard)]
    )
    if rejected:
        with pytest.raises(AssertionError, match="Retired service_namespace selectors"):
            test_no_service_namespace_filter_in_prod_dashboards()
    else:
        test_no_service_namespace_filter_in_prod_dashboards()


# ---------------------------------------------------------------------------
# Test 3: No dashboard (production or bench) may reference any
# hippo_brain_lmstudio_* metric.
#
# The [lmstudio] section was renamed to [inference] and all lmstudio-prefixed
# instruments were removed.
# ---------------------------------------------------------------------------


def test_no_lmstudio_metrics_in_any_dashboard():
    """No dashboard may reference hippo_brain_lmstudio_* metric names.

    These instruments were removed when the LM Studio vendor coupling was
    replaced by the vendor-neutral InferenceClient.  Any reference is a
    dangling pointer.

    Fix: replace with the corresponding hippo_brain_inference_* name, or
    remove the panel if the signal no longer exists.
    """
    violations: list[str] = []

    for path in sorted(_DASHBOARDS_DIR.glob("*.json")):
        try:
            dashboard = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue  # malformed JSON is caught by other tests
        for panel in _iter_panels(dashboard):
            for target in panel.get("targets", []):
                expr = target.get("expr", "")
                lms_hits = re.findall(r"hippo_brain_lmstudio_[a-z0-9_]*", expr)
                if lms_hits:
                    violations.append(
                        f"  dashboard={path.name!r}  panel_id={panel.get('id', '?')}  "
                        f"refId={target.get('refId', '?')}  "
                        f"forbidden_names={lms_hits}  expr={expr!r}"
                    )

    assert not violations, (
        "The following dashboard panels reference hippo_brain_lmstudio_* "
        "metrics, which no longer exist.\n\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Test 7: Exactly the five expected production dashboards exist (no extras,
# no bench dashboards remaining after the isolation decision).
# ---------------------------------------------------------------------------


def test_only_prod_dashboards_exist():
    """The dashboards directory must contain exactly the five production
    dashboards plus dashboards.yml.  The three bench dashboards
    (bench-model-comparison, bench-model-drilldown, bench-run-overview) were
    deleted per the isolation decision and must not reappear.

    Fix: if you deleted a bench dashboard, this test should already pass.
    If a new production dashboard was added, add its filename to
    _PROD_DASHBOARD_NAMES in this test file.
    """
    actual_json = {p.name for p in _DASHBOARDS_DIR.glob("*.json")}
    expected = _PROD_DASHBOARD_NAMES

    unexpected = actual_json - expected
    missing = expected - actual_json

    messages = []
    if unexpected:
        messages.append(
            "Unexpected dashboard JSON files (bench dashboards must be deleted, "
            "new prod dashboards must be added to _PROD_DASHBOARD_NAMES):\n"
            + "\n".join(f"  {name}" for name in sorted(unexpected))
        )
    if missing:
        messages.append(
            "Expected production dashboard JSON files are missing:\n"
            + "\n".join(f"  {name}" for name in sorted(missing))
        )

    assert not messages, "\n\n".join(messages)


# OTel metadata is collected by the SDK, never parsed from implementation text.
_UNIT_TO_SUFFIX = {
    "": "",
    "{token}": "",
    "ms": "_milliseconds",
    "s": "_seconds",
    "By": "_bytes",
    "1": "_ratio",
}


def _prometheus_name(metric: dict) -> str:
    name = metric["name"].replace(".", "_") + _UNIT_TO_SUFFIX[metric["unit"]]
    return name + ("_total" if metric["data"].get("is_monotonic") else "")


def _metric_kind(metric: dict) -> str:
    if "bucket_counts" in metric["data"]["data_points"][0]:
        return "histogram"
    if "is_monotonic" in metric["data"]:
        return "counter" if metric["data"]["is_monotonic"] else "up_down_counter"
    return "gauge"


@pytest.fixture(scope="module")
def collected_brain_metrics(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    # Instruments are module globals and OTel permits only one global provider.
    # A child process avoids replacing instruments/providers used by other tests.
    result = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path_factory.mktemp("metrics"))],
        env={**os.environ, "HIPPO_OTEL_ENABLED": "1"},
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    data = json.loads(result.stdout)
    return {
        _prometheus_name(metric): metric
        for resource in data["resource_metrics"]
        for scope in resource["scope_metrics"]
        for metric in scope["metrics"]
        if metric["data"]["data_points"]
    }


def test_python_metric_inventory_is_emitted(collected_brain_metrics):
    expected = {name for name in EMITTED_METRICS if name.startswith(("hippo_brain_", "process_"))}
    assert not (expected - collected_brain_metrics.keys())
    for name in expected:
        assert _metric_kind(collected_brain_metrics[name]) == METRIC_CONTRACT[name], name


def test_python_dashboard_and_alert_metrics_are_emitted(collected_brain_metrics):
    samples = {
        sample
        for name, metric in collected_brain_metrics.items()
        for sample in _sample_names(name, _metric_kind(metric))
    }
    expressions = [
        (filename, expr)
        for filename, dashboard in _load_prod_dashboards()
        for _, _, expr in _collect_all_exprs(dashboard)
    ] + [
        (filename, expr)
        for filename, rules in _load_prod_alert_rules()
        for _, expr in _collect_alert_exprs(rules)
    ]
    missing = [
        (filename, raw)
        for filename, expr in expressions
        for raw in _extract_metric_names(expr)
        if raw.startswith(("hippo_brain_", "process_")) and raw not in samples
    ]
    assert not missing, f"Dashboard/alert metrics were not emitted: {missing}"


def test_decision_samples_and_queue_callbacks(collected_brain_metrics):
    def metric(suffix: str) -> dict:
        return collected_brain_metrics["hippo_brain_" + suffix]

    def values(suffix: str) -> list:
        return [point["value"] for point in metric(suffix)["data"]["data_points"]]

    counts = metric("decision_count_total")["data"]
    assert counts["is_monotonic"] is True
    assert {point["attributes"]["outcome"]: point["value"] for point in counts["data_points"]} == {
        "success": 1,
        "fallback": 1,
    }
    assert values("decision_errors_total") == [1]
    assert values("decision_requests_total") == [2]
    assert {
        point["attributes"]["direction"]: point["value"]
        for point in metric("decision_tokens_total")["data"]["data_points"]
    } == {"input": 11, "output": 3}
    assert values("decision_requests_unknown_total") == [1]
    assert values("decision_usage_unknown_total") == [1]
    duration = metric("decision_duration_milliseconds")
    assert duration["unit"] == "ms"
    assert {
        (point["attributes"]["task"], point["attributes"]["stage"]): point["sum"]
        for point in duration["data"]["data_points"]
    } == {
        ("rerank", "decision"): 30,
        ("rerank", "rules"): 1,
        ("rerank", "attempt"): 25,
        ("rerank", "queue"): 5,
        ("rerank", "http"): 20,
        ("classification", "decision"): 10,
        ("classification", "attempt"): 8,
        ("classification", "sql_apply"): 2,
    }
    assert all(point["count"] == 1 for point in duration["data"]["data_points"])
    for suffix, expected in (
        ("classification_queue_depth", {"pending": 2, "processing": 1, "failed": 0}),
        ("enrichment_queue_depth", {"pending": 1, "processing": 0, "failed": 0}),
    ):
        collected = metric(suffix)
        assert collected["unit"] == ""
        assert {
            point["attributes"]["status"]: point["value"]
            for point in collected["data"]["data_points"]
        } == expected


def _collect_brain_metrics(directory: Path) -> str:
    """Record samples through real instruments, decision recorder and DB callbacks."""
    from opentelemetry import metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    metrics.set_meter_provider(provider)
    try:
        from hippo_brain import client, embeddings, mcp, rag, server, telemetry, watchdog

        # These samples test instrument names, types and units. Application
        # workflow behavior has its own suites; no live inference is needed.
        for instrument in (
            embeddings._embed_failures,
            client._inference_errors,
            server._events_claimed,
            server._nodes_created,
            server._enrichment_failures,
            watchdog._reaped_counter,
            watchdog._preflight_skipped,
            rag._rag_degraded,
        ):
            telemetry.add(instrument)
        for instrument in (
            embeddings._embed_duration,
            client._request_duration,
            client._prompt_tokens,
            server._loop_duration,
            rag._rag_duration,
            rag._rag_hits,
        ):
            telemetry.hist(instrument, 7)
        mcp._init_telemetry_instruments()
        telemetry.add(mcp._tool_calls, tool="ask")
        telemetry.add(mcp._tool_errors, tool="ask")
        telemetry.hist(mcp._tool_duration, 7, tool="ask")
        telemetry.record_decision_metrics(
            {
                "backend": "jev",
                "stop_reason": "single_pass",
                "elapsed_ms": 30,
                "rule_ms": 1,
                "passes": [
                    {
                        "elapsed_ms": 25,
                        "transport": {"network_calls": 2, "queue_ms": 5, "http_ms": 20},
                        "usage": {"input_tokens": 11, "output_tokens": 3},
                    }
                ],
            }
        )
        telemetry.record_decision_metrics(
            {
                "backend": "local",
                "status": "assessment_failed",
                "total_ms": 10,
                "network_ms": 8,
                "apply_ms": 2,
            },
            task="classification",
        )
        telemetry._register_process_metrics()
        db_path = directory / "hippo.db"
        with closing(sqlite3.connect(db_path)) as conn:
            conn.executescript("""
                CREATE TABLE enrichment_queue (status TEXT NOT NULL);
                INSERT INTO enrichment_queue VALUES ('pending');
                CREATE TABLE knowledge_node_classifications (status TEXT NOT NULL);
                INSERT INTO knowledge_node_classifications VALUES
                    ('pending'), ('pending'), ('processing'), ('ready');
            """)
            conn.commit()
        # Construction registers callbacks without starting background workers.
        server.create_app(db_path=str(db_path), data_dir=str(directory))
        data = reader.get_metrics_data()
        assert data is not None
        return data.to_json()
    finally:
        provider.shutdown()


# ---------------------------------------------------------------------------
# Test 9: _extract_metric_names must pull out metric names only, never label
# keys. PromQL label keys can share the hippo_/process_ prefix (e.g. a
# {process_command=~"..."} selector); treating them as metrics would make
# dashboard reference checks fail on otherwise-valid dashboards.
# ---------------------------------------------------------------------------


def test_label_keys_are_not_extracted_as_metrics():
    """Label KEYS that share a metric prefix must not be picked up as metrics,
    and a real metric in a comparison must still be picked up.
    """
    # Label key inside a {...} selector -> only the metric is extracted.
    assert _extract_metric_names('hippo_daemon_requests_total{process_command=~"x"}') == [
        "hippo_daemon_requests_total"
    ]
    # Label key inside a by(...) grouping clause -> only the metric is extracted.
    assert _extract_metric_names(
        "sum by (process_command) (rate(hippo_daemon_requests_total[5m]))"
    ) == ["hippo_daemon_requests_total"]
    # A real metric used in a comparison must NOT be dropped (guard against an
    # over-eager negative-lookahead on '=' that would skip `metric == n`).
    assert "hippo_daemon_health_grade" in _extract_metric_names("hippo_daemon_health_grade == 5")


# ---------------------------------------------------------------------------
# Test 11: Provisioned Grafana alert rules must reference only allowed metrics
# and include the SNUG-96 capture-reliability alert set.
# ---------------------------------------------------------------------------


def _load_prod_alert_rules() -> list[tuple[str, dict]]:
    results = []
    for name in sorted(_PROD_ALERT_FILES):
        path = _ALERTING_DIR / name
        assert path.exists(), (
            f"Expected alert provisioning file not found: {path}. "
            "If it was intentionally removed, update _PROD_ALERT_FILES."
        )
        results.append((name, yaml.safe_load(path.read_text())))
    return results


def _collect_alert_exprs(alert_doc: dict) -> list[tuple[str, str]]:
    """Return (rule_uid, promql_expr) for every Prometheus query in alert data."""
    results: list[tuple[str, str]] = []
    for group in alert_doc.get("groups", []):
        for rule in group.get("rules", []):
            uid = rule.get("uid", "?")
            for query in rule.get("data", []):
                model = query.get("model", {})
                if model.get("datasource", {}).get("type") == "prometheus":
                    expr = model.get("expr", "")
                    if expr:
                        results.append((uid, expr))
    return results


def test_alert_rules_reference_allowed_metrics():
    """Every hippo_* metric in provisioned alert PromQL must be in
    EMITTED_METRICS ∪ _EXPORTER_METRICS."""
    violations: list[str] = []
    for filename, doc in _load_prod_alert_rules():
        for uid, expr in _collect_alert_exprs(doc):
            for raw in _extract_metric_names(expr):
                if raw not in DASHBOARD_ALLOWED_METRICS:
                    violations.append(
                        f"{filename} rule {uid}: {raw!r} not in EMITTED_METRICS ∪ _EXPORTER_METRICS"
                    )
    assert not violations, "Alert rule metric drift:\n" + "\n".join(violations)


def test_required_capture_alert_rules_exist():
    """SNUG-96 acceptance: daemon drops, watcher drops, watchdog stall, probe fail, invariant."""
    found: set[str] = set()
    for _filename, doc in _load_prod_alert_rules():
        for group in doc.get("groups", []):
            for rule in group.get("rules", []):
                uid = rule.get("uid")
                if uid:
                    found.add(uid)
    missing = _REQUIRED_ALERT_UIDS - found
    assert not missing, f"Missing required alert rule uids: {sorted(missing)}"
    missing_kb = _REQUIRED_KB_ALERT_UIDS - found
    assert not missing_kb, f"Missing required knowledge-health alert uids: {sorted(missing_kb)}"


# ---------------------------------------------------------------------------
# Test 12: Knowledge-health exporter metrics must actually be EMITTED.
#
# The earlier version of this test only checked that each name appeared as a
# string literal in the exporter source — which the exporter's own name
# registry satisfied trivially, so a metric that was declared but never passed
# to gauge()/counter() (hippo_kb_db_size_bytes was exactly that) still passed
# while its dashboard panel rendered permanently blank.
#
# This version imports the exporter, points it at a synthetic hippo.db built to
# exercise every family, renders a real scrape, and asserts on the sample names
# that come out, matching the runtime contract for OTel instruments.
# ---------------------------------------------------------------------------

_CORE_SCHEMA = """
CREATE TABLE events (
    id INTEGER PRIMARY KEY, timestamp INTEGER, cwd TEXT, git_repo TEXT,
    duration_ms INTEGER, stdout TEXT, stderr TEXT
);
CREATE TABLE knowledge_nodes (id INTEGER PRIMARY KEY, design_decisions TEXT, tags TEXT);
CREATE TABLE agentic_sessions (
    id INTEGER PRIMARY KEY, project_dir TEXT, message_count INTEGER DEFAULT 0
);
CREATE TABLE knowledge_node_agentic_sessions (
    knowledge_node_id INTEGER, agentic_session_id INTEGER
);
CREATE TABLE capture_alarms (id INTEGER PRIMARY KEY, resolved_at INTEGER);
CREATE TABLE source_health (source TEXT PRIMARY KEY, probe_ok INTEGER, last_event_ts INTEGER);
CREATE TABLE env_snapshots (id INTEGER PRIMARY KEY, content_hash TEXT, env_json TEXT);
"""

_SNOWBALL_SCHEMA = """
CREATE TABLE retrieval_events (id INTEGER PRIMARY KEY);
CREATE TABLE epitaphs (id INTEGER PRIMARY KEY, confirmed_by TEXT);
CREATE TABLE push_trials (id INTEGER PRIMARY KEY, tapped_useful INTEGER, tapped_noise INTEGER);
CREATE TABLE nodes (id INTEGER PRIMARY KEY, status TEXT);
CREATE TABLE contradictions (id INTEGER PRIMARY KEY, resolved_at INTEGER);
CREATE TABLE bets (id INTEGER PRIMARY KEY, resolved_at INTEGER);
"""


def _build_fixture_db(path, snowball: bool):
    """A synthetic hippo.db shaped to exercise every always-on metric family."""
    now_ms = int(time.time() * 1000)
    dead_ms = now_ms - 200 * 86400000  # older than every graveyard window
    conn = sqlite3.connect(path)
    conn.executescript(_CORE_SCHEMA)
    if snowball:
        conn.executescript(_SNOWBALL_SCHEMA)
    # A live project and a dead one, each over MIN_EVENTS_FOR_PROJECT.
    for i in range(12):
        conn.execute(
            "INSERT INTO events (timestamp, cwd, git_repo, duration_ms, stdout, stderr) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (now_ms - i * 1000, "/w/live", "/w/live", 1000, "out", "err"),
        )
    for i in range(12):
        conn.execute(
            "INSERT INTO events (timestamp, cwd, git_repo, duration_ms, stdout, stderr) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (dead_ms - i * 1000, "/w/dead-proj", "/w/dead-proj", 3600000, "", ""),
        )
    conn.execute("INSERT INTO knowledge_nodes (design_decisions, tags) VALUES ('[{}]', 'x')")
    conn.execute("INSERT INTO knowledge_nodes (design_decisions, tags) VALUES (NULL, 'y')")
    conn.execute(
        "INSERT INTO agentic_sessions (id, project_dir, message_count) VALUES (1, '/w/dead-proj', 7)"
    )
    conn.execute("INSERT INTO knowledge_node_agentic_sessions VALUES (1, 1)")
    conn.execute("INSERT INTO capture_alarms (resolved_at) VALUES (NULL)")
    conn.execute("INSERT INTO source_health VALUES ('shell', 1, ?)", (now_ms - 5000,))
    conn.execute(
        "INSERT INTO env_snapshots (content_hash, env_json) VALUES ('h', ?)",
        (json.dumps({"PATH": "/usr/bin", "HOME": "/home/x"}),),
    )
    if snowball:
        conn.execute("INSERT INTO retrieval_events DEFAULT VALUES")
        conn.execute("INSERT INTO epitaphs (confirmed_by) VALUES ('me')")
        conn.execute("INSERT INTO push_trials (tapped_useful, tapped_noise) VALUES (1, 0)")
        conn.execute("INSERT INTO nodes (status) VALUES ('provisional')")
        conn.execute("INSERT INTO contradictions (resolved_at) VALUES (NULL)")
        conn.execute("INSERT INTO bets (resolved_at) VALUES (NULL)")
    conn.commit()
    conn.close()


def _render_against(db_path, monkeypatch, canary_path=None):
    """Render one full scrape with the exporter pointed at a fixture DB."""
    monkeypatch.setattr(_EXPORTER, "DB_PATH", db_path)
    # Never let the test touch the real brain server: the probe must be a no-op.
    monkeypatch.setattr(_EXPORTER, "PROBE_TTL_S", 0)
    monkeypatch.setattr(
        _EXPORTER, "CANARY_FILE", canary_path or (db_path.parent / "no-canary.json")
    )
    _EXPORTER.reset_db_cache_for_test()
    _EXPORTER.reset_counters_for_test()
    return _EXPORTER.build_registry()


def test_recall_probe_disabled_without_inference(monkeypatch):
    monkeypatch.delenv("HIPPO_PROBE_TTL", raising=False)
    exporter = _load_exporter()
    request = MagicMock(side_effect=AssertionError("disabled probe made an HTTP request"))
    monkeypatch.setattr(exporter.urllib.request, "urlopen", request)
    exporter.run_probe()  # Startup path.
    reg = exporter.Registry()
    exporter.collect_probe(reg)  # Scrape path.
    request.assert_not_called()
    assert [(s["name"], s["value"]) for s in reg.samples] == [
        ("hippo_kb_recall_probe_enabled", 0.0)
    ]


def test_recall_probe_cooldown_starts_after_slow_batch(monkeypatch):
    monkeypatch.setattr(_EXPORTER, "PROBE_TTL_S", 120)
    monkeypatch.setattr(_EXPORTER, "probe_state", {"ts": 0.0, "samples": []})
    now = [1000.0]
    monkeypatch.setattr(_EXPORTER.time, "time", lambda: now[0])
    calls = []

    def timeout(question):
        calls.append(question)
        now[0] += 45
        return False, "llm_timeout"

    monkeypatch.setattr(_EXPORTER, "_ask_once", timeout)
    _EXPORTER.run_probe()
    assert _EXPORTER.probe_state["ts"] == 1135.0
    _EXPORTER.run_probe()
    assert len(calls) == 3
    now[0] += 120
    _EXPORTER.run_probe()
    assert len(calls) == 6


def test_recall_probe_rejects_null_and_degraded_answers(monkeypatch):
    response = MagicMock()
    response.__enter__.return_value = response
    monkeypatch.setattr(_EXPORTER.urllib.request, "urlopen", lambda *a, **kw: response)
    for body, expected in [
        ({"answer": None, "degraded": True}, (False, "degraded_answer")),
        ({"answer": "fallback", "error": "synthesis failed"}, (False, "degraded_answer")),
        ({"answer": None}, (False, "empty_answer")),
        ({"answer": " "}, (False, "empty_answer")),
        ({"answer": "A real answer", "degraded": False}, (True, "")),
    ]:
        response.read.return_value = json.dumps(body).encode()
        assert _EXPORTER._ask_once("question") == expected


def test_exporter_emits_every_always_on_metric(tmp_path, monkeypatch):
    """Every METRIC_NAMES entry must appear in a real render. No paper registry."""
    db = tmp_path / "hippo.db"
    _build_fixture_db(db, snowball=False)
    reg = _render_against(db, monkeypatch)
    emitted = {s["name"] for s in reg.samples}
    missing = sorted(set(_EXPORTER.METRIC_NAMES) - emitted)
    assert not missing, (
        "These METRIC_NAMES entries are declared but NOT emitted by a real scrape "
        f"of {_EXPORTER_SCRIPT.name}:\n"
        + "\n".join(f"  {name}" for name in missing)
        + "\n\nEither the registry name is stale, or the gauge()/counter() call "
        "was lost. A declared-but-unemitted name renders a blank dashboard panel."
    )


def test_exporter_emits_snowball_metrics_when_tables_exist(tmp_path, monkeypatch):
    """FUTURE_METRIC_NAMES must light up once their backing tables land."""
    db = tmp_path / "hippo.db"
    _build_fixture_db(db, snowball=True)
    reg = _render_against(db, monkeypatch)
    emitted = {s["name"] for s in reg.samples}
    missing = sorted(set(_EXPORTER.FUTURE_METRIC_NAMES) - emitted)
    assert not missing, f"Snowball metrics not emitted with backing tables present: {missing}"


def test_exporter_emits_no_undeclared_metrics(tmp_path, monkeypatch):
    """The reverse direction: nothing may be emitted that the registry omits."""
    db = tmp_path / "hippo.db"
    _build_fixture_db(db, snowball=True)
    reg = _render_against(db, monkeypatch)
    undeclared = sorted({s["name"] for s in reg.samples} - _EXPORTER_METRICS)
    assert not undeclared, (
        f"Exporter emits metrics absent from its registry: {undeclared}. "
        "Add them to METRIC_NAMES/FUTURE_METRIC_NAMES so dashboards may reference them."
    )


def test_exporter_counters_are_cumulative(tmp_path, monkeypatch):
    """`*_total` must accumulate across scrapes, or increase()/rate() is always 0.

    Two alerts (hippo_kb_recall_failures, hippo_kb_collector_errors) are built on
    increase() over these series. A per-scrape value that resets makes those
    alerts structurally unfireable, which is how they originally shipped.
    """
    db = tmp_path / "hippo.db"
    _build_fixture_db(db, snowball=False)
    monkeypatch.setattr(_EXPORTER, "DB_PATH", db)
    # 0 disables the probe outright. A huge TTL does NOT: probe_state["ts"]
    # starts at 0.0, so now - 0 exceeds any sane TTL and collect_probe spawns
    # a real probe thread whose failure bumps race this test's assertions.
    monkeypatch.setattr(_EXPORTER, "PROBE_TTL_S", 0)
    monkeypatch.setattr(_EXPORTER, "CANARY_FILE", tmp_path / "no-canary.json")
    _EXPORTER.reset_counters_for_test()

    def value_of(reg, name):
        return sum(s["value"] for s in reg.samples if s["name"] == name)

    for _ in range(3):
        _EXPORTER.bump("hippo_kb_recall_failures_total", {"reason": "brain_down"})
        _EXPORTER.reset_db_cache_for_test()
        reg = _EXPORTER.build_registry()
    assert value_of(reg, "hippo_kb_recall_failures_total") == 3.0

    _EXPORTER.bump("hippo_kb_recall_failures_total", {"reason": "brain_down"})
    _EXPORTER.reset_db_cache_for_test()
    reg = _EXPORTER.build_registry()
    assert value_of(reg, "hippo_kb_recall_failures_total") == 4.0


def test_exporter_counters_are_typed_counter(tmp_path, monkeypatch):
    """`# TYPE ... counter` must be emitted for the cumulative series."""
    db = tmp_path / "hippo.db"
    _build_fixture_db(db, snowball=False)
    monkeypatch.setattr(_EXPORTER, "DB_PATH", db)
    # 0 disables the probe outright. A huge TTL does NOT: probe_state["ts"]
    # starts at 0.0, so now - 0 exceeds any sane TTL and collect_probe spawns
    # a real probe thread whose failure bumps race this test's assertions.
    monkeypatch.setattr(_EXPORTER, "PROBE_TTL_S", 0)
    monkeypatch.setattr(_EXPORTER, "CANARY_FILE", tmp_path / "no-canary.json")
    _EXPORTER.reset_counters_for_test()
    _EXPORTER.reset_db_cache_for_test()
    _EXPORTER.bump("hippo_kb_collector_errors_total", {"name": "x"}, help="h")
    text = _EXPORTER.render_prometheus(_EXPORTER.build_registry()).decode()
    assert "# TYPE hippo_kb_collector_errors_total counter" in text
    # And no gauge may carry the counter-only `_total` suffix.
    gauge_totals = [
        line.split()[2]
        for line in text.splitlines()
        if line.startswith("# TYPE ")
        and line.endswith(" gauge")
        and line.split()[2].endswith("_total")
    ]
    assert not gauge_totals, f"gauges named _total (Prometheus convention): {gauge_totals}"


def test_exporter_secretish_env_keys_detected(tmp_path, monkeypatch):
    """The redaction canary must read real snapshot keys out of env_json.

    env_snapshots has no per-key column — env vars live in a JSON blob. An
    implementation that looks for a `key`/`name` column emits nothing at all,
    leaving the security panel and its alert permanently, falsely green.
    """
    db = tmp_path / "hippo.db"
    _build_fixture_db(db, snowball=False)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO env_snapshots (content_hash, env_json) VALUES ('leak', ?)",
        (json.dumps({"AWS_SECRET_ACCESS_KEY": "x", "PATH": "/usr/bin"}),),
    )
    conn.commit()
    conn.close()
    reg = _render_against(db, monkeypatch)
    vals = [s["value"] for s in reg.samples if s["name"] == "hippo_kb_env_secretish_keys"]
    assert vals == [1.0], f"expected exactly one secretish key detected, got {vals}"


def test_exporter_render_is_reentrant(tmp_path, monkeypatch):
    """Concurrent scrapes must not interleave samples into each other.

    The exporter is served by ThreadingHTTPServer; shared module-level sample
    state produced responses containing every in-flight render's samples, i.e.
    duplicate series, which Prometheus rejects outright.
    """
    import threading as _threading

    db = tmp_path / "hippo.db"
    _build_fixture_db(db, snowball=True)
    monkeypatch.setattr(_EXPORTER, "DB_PATH", db)
    # 0 disables the probe outright. A huge TTL does NOT: probe_state["ts"]
    # starts at 0.0, so now - 0 exceeds any sane TTL and collect_probe spawns
    # a real probe thread whose failure bumps race this test's assertions.
    monkeypatch.setattr(_EXPORTER, "PROBE_TTL_S", 0)
    monkeypatch.setattr(_EXPORTER, "CANARY_FILE", tmp_path / "no-canary.json")
    _EXPORTER.reset_db_cache_for_test()
    _EXPORTER.reset_counters_for_test()

    baseline = len(_EXPORTER.build_registry().samples)
    counts: list[int] = []
    lock = _threading.Lock()

    def scrape():
        n = len(_EXPORTER.build_registry().samples)
        with lock:
            counts.append(n)

    threads = [_threading.Thread(target=scrape) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert counts and all(c == baseline for c in counts), (
        f"concurrent scrapes returned varying sample counts {sorted(set(counts))}; "
        f"expected all == {baseline}. Per-request state has leaked back to module scope."
    )


def test_exporter_no_duplicate_series_in_exposition(tmp_path, monkeypatch):
    """Every (name, labels) pair may appear at most once per exposition."""
    db = tmp_path / "hippo.db"
    _build_fixture_db(db, snowball=True)
    reg = _render_against(db, monkeypatch)
    seen = [(s["name"], tuple(sorted(s["labels"].items()))) for s in reg.samples]
    dupes = sorted({k for k in seen if seen.count(k) > 1})
    assert not dupes, f"duplicate series in one exposition (Prometheus rejects these): {dupes}"


def test_exporter_script_exists():
    assert _EXPORTER_SCRIPT.exists(), (
        f"Knowledge-health exporter script missing: {_EXPORTER_SCRIPT}."
    )


if __name__ == "__main__":
    print(_collect_brain_metrics(Path(sys.argv[1])))
