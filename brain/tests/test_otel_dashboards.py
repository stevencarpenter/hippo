"""Drift-prevention tests for Grafana dashboard JSON files.

These tests guard against the class of bugs where dashboard PromQL expressions
reference metric names that do not exist in the OTel instrumentation — either
because an instrument was renamed, removed, or never created. A failure here
tells the developer exactly which dashboard and metric drifted, and which file
to look at for the authoritative name.

Run as part of the normal pytest suite; no external services required.
"""

import json
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo root, resolved relative to this file so the tests work from any cwd.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DASHBOARDS_DIR = _REPO_ROOT / "otel" / "grafana" / "dashboards"

# ---------------------------------------------------------------------------
# Canonical EMITTED metric names for production dashboards.
#
# Update this set when adding or removing OTel instruments.  The set is the
# single source of truth for what the dashboard layer is allowed to reference.
# Naming rules (OTel -> Prometheus exporter):
#   - dots -> underscores
#   - unit="ms"  -> _milliseconds suffix
#   - unit="By"  -> _bytes suffix
#   - unit="1"   -> NO suffix appended (bare name as specified)
#   - counters   -> _total suffix (already included below)
#   - histograms -> _bucket / _count / _sum appended by Prometheus (stripped in check)
# ---------------------------------------------------------------------------
EMITTED_METRICS: frozenset[str] = frozenset(
    [
        # --- daemon: health ---
        "hippo_daemon_health_grade",
        "hippo_daemon_health_active_alarms",
        # --- daemon: source health ---
        "hippo_daemon_source_health_consecutive_failures",
        "hippo_daemon_source_health_probe_ok",
        "hippo_daemon_source_health_lag_milliseconds",
        # --- daemon: events / sessions ---
        "hippo_daemon_buffer_size",
        "hippo_daemon_db_busy_count_total",
        "hippo_daemon_db_size_bytes",
        "hippo_daemon_events_ingested_total",
        "hippo_daemon_events_dropped_total",
        "hippo_daemon_fallback_pending",
        "hippo_daemon_fallback_recovered_total",
        "hippo_daemon_flush_batch_size",
        "hippo_daemon_flush_duration_milliseconds",
        "hippo_daemon_flush_events_total",
        "hippo_daemon_redactions_total",
        "hippo_daemon_request_duration_milliseconds",
        "hippo_daemon_requests_total",
        "hippo_daemon_sessions_created_total",
        # --- probe ---
        "hippo_probe_lag_milliseconds",
        "hippo_probe_run_total",
        # --- watchdog ---
        "hippo_watchdog_alarms_auto_resolved_total",
        "hippo_watchdog_alarms_fired_total",
        "hippo_watchdog_alarms_reset_total",
        "hippo_watchdog_invariant_violation_total",
        "hippo_watchdog_run_total",
        # --- watcher ---
        "hippo_watcher_process_duration_milliseconds",
        "hippo_watcher_segments_ingested_total",
        "hippo_watcher_events_dropped_total",
        # --- brain: embeddings ---
        "hippo_brain_embedding_duration_milliseconds",
        "hippo_brain_embedding_failures_total",
        # --- brain: enrichment ---
        "hippo_brain_enrichment_events_claimed_total",
        "hippo_brain_enrichment_failures_total",
        "hippo_brain_enrichment_loop_duration_milliseconds",
        "hippo_brain_enrichment_nodes_created_total",
        "hippo_brain_enrichment_preflight_skipped_total",
        "hippo_brain_enrichment_queue_depth",
        "hippo_brain_enrichment_reaped_total",
        # --- brain: inference ---
        "hippo_brain_inference_errors_total",
        "hippo_brain_inference_prompt_tokens",
        "hippo_brain_inference_request_duration_milliseconds",
        # --- brain: RAG ---
        "hippo_brain_rag_degraded_total",
        "hippo_brain_rag_duration_milliseconds",
        "hippo_brain_rag_retrieval_hits",
        # --- brain: MCP ---
        "hippo_brain_mcp_tool_calls_total",
        "hippo_brain_mcp_tool_errors_total",
        "hippo_brain_mcp_tool_duration_milliseconds",
    ]
)

# ---------------------------------------------------------------------------
# Histogram component suffixes that Prometheus appends automatically.
# These are NOT part of the instrument name and must be stripped before the
# membership check.
# ---------------------------------------------------------------------------
_HISTOGRAM_SUFFIXES = ("_bucket", "_count", "_sum")

# Production dashboard file names (bench dashboards will be deleted per the
# isolation decision; only these four are expected).
_PROD_DASHBOARD_NAMES = frozenset(
    [
        "hippo-overview.json",
        "hippo-daemon.json",
        "hippo-enrichment.json",
        "hippo-processes.json",
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


def _extract_hippo_metrics(expr: str) -> list[str]:
    """Extract all hippo_* metric name tokens from a PromQL expression.

    Returns raw names as they appear (may include _bucket/_count/_sum).
    """
    return re.findall(r"hippo_[a-z0-9_]+", expr)


def _normalize_metric_name(raw: str) -> str:
    """Strip histogram component suffixes so membership checks work correctly.

    hippo_brain_inference_request_duration_milliseconds_bucket
    -> hippo_brain_inference_request_duration_milliseconds
    """
    for suffix in _HISTOGRAM_SUFFIXES:
        if raw.endswith(suffix):
            return raw[: -len(suffix)]
    return raw


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
      (a) a metric was renamed in the OTel instrumentation and the dashboard
          was not updated, or
      (b) a metric was removed from the instrumentation but the dashboard still
          references the old name.

    Fix: update the dashboard expr to use the new name, OR add the new
    instrument to brain/src/hippo_brain/ (or the Rust daemon) and update
    EMITTED_METRICS in this file.
    """
    violations: list[str] = []

    for filename, dashboard in _load_prod_dashboards():
        for panel_id, ref_id, expr in _collect_all_exprs(dashboard):
            for raw_name in _extract_hippo_metrics(expr):
                normalized = _normalize_metric_name(raw_name)
                if normalized not in EMITTED_METRICS:
                    violations.append(
                        f"  dashboard={filename!r}  panel_id={panel_id}  "
                        f"refId={ref_id!r}  metric={raw_name!r} "
                        f"(normalized: {normalized!r})  expr={expr!r}"
                    )

    assert not violations, (
        "The following production dashboard panels reference hippo_* metrics "
        "that are NOT in the EMITTED_METRICS allow-list in this test file.\n"
        "Update the dashboard to use the correct metric name, or add the "
        "instrument and update EMITTED_METRICS.\n\n" + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Test 2: No production dashboard JSON may contain "service_namespace".
#
# The isolation decision locks this: the {service_namespace!~".+"} filter was
# a no-op and has been stripped from all production dashboards.  Any
# re-introduction is a regression.
# ---------------------------------------------------------------------------


def test_no_service_namespace_filter_in_prod_dashboards():
    """Production dashboards must not contain 'service_namespace' anywhere.

    The {service_namespace!~".+"} selector was a permanent no-op (bench
    dashboards are deleted; the OTel collector does not promote resource
    attributes to labels).  It has been stripped.  If it reappears, the
    dashboard was edited without reading the isolation decision.

    Fix: remove every occurrence of 'service_namespace' from the dashboard
    JSON, following the stripping rules in the shared contract:
      {service_namespace!~".+"}                 -> bare metric name
      {service_namespace!~".+", status="failed"} -> {status="failed"}
    """
    violations: list[str] = []

    for filename, dashboard in _load_prod_dashboards():
        raw_text = json.dumps(dashboard)
        if "service_namespace" in raw_text:
            # Find which panels/exprs contain it for a useful error message
            panel_hits = []
            for panel in _iter_panels(dashboard):
                for target in panel.get("targets", []):
                    expr = target.get("expr", "")
                    if "service_namespace" in expr:
                        panel_hits.append(
                            f"    panel_id={panel.get('id', '?')}  "
                            f"refId={target.get('refId', '?')}  expr={expr!r}"
                        )
            # Also flag if the literal appears outside exprs (e.g. in a label_selector field)
            hit_detail = (
                "\n".join(panel_hits)
                if panel_hits
                else "    (not in a target expr — search the raw JSON)"
            )
            violations.append(
                f"  dashboard={filename!r} still contains 'service_namespace':\n{hit_detail}"
            )

    assert not violations, (
        "The following production dashboards contain 'service_namespace', "
        "which is a no-op filter that must be stripped.\n\n" + "\n".join(violations)
    )


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
# Test 4: MCP metric names in hippo-enrichment.json match the instruments
# actually created in brain/src/hippo_brain/mcp.py.
#
# The test reads mcp.py, extracts every create_counter / create_histogram
# call's first string argument, converts dots -> underscores, appends _total
# for counters, and cross-checks against what the enrichment dashboard uses.
# ---------------------------------------------------------------------------


def _extract_mcp_instrument_names() -> dict[str, str]:
    """Parse mcp.py and return {prometheus_name: instrument_kind} for every
    create_counter and create_histogram call in that file.

    Instrument names in source use dot notation (e.g. "hippo.brain.mcp.tool_calls");
    we convert to Prometheus form (underscores, _total for counters).
    """
    mcp_path = _REPO_ROOT / "brain" / "src" / "hippo_brain" / "mcp.py"
    assert mcp_path.exists(), f"mcp.py not found at {mcp_path}"
    source = mcp_path.read_text()

    instruments: dict[str, str] = {}

    # Match any create_counter or create_histogram call whose first string
    # argument starts with "hippo.brain.mcp.".  The call may appear as:
    #   _meter.create_counter("hippo.brain.mcp.tool_calls", ...)
    #   meter.create_counter("hippo.brain.mcp.tool_calls", ...)
    #   meter.create_histogram("hippo.brain.mcp.tool_duration", unit="ms", ...)
    # We anchor on the method name only, not the receiver, to be robust against
    # variable-name changes.
    #
    # Unit-to-suffix mapping mirrors the OTel->Prometheus exporter convention:
    #   unit="ms"  -> _milliseconds
    #   unit="By"  -> _bytes
    #   unit="1"   -> (no suffix)
    #   (absent)   -> (no suffix)
    _UNIT_SUFFIX: dict[str, str] = {"ms": "_milliseconds", "By": "_bytes"}

    # Capture: .create_counter("name", ...) or .create_histogram("name", ..., unit="ms", ...)
    # We grab everything between the opening paren and the closing paren so we
    # can also extract the optional unit= keyword argument.
    pattern = re.compile(r"\.(create_counter|create_histogram)\(([^)]+)\)")
    for m in pattern.finditer(source):
        kind = m.group(1)  # "create_counter" or "create_histogram"
        args_text = m.group(2)  # raw argument text

        # Extract the first string literal (the instrument name)
        name_match = re.search(r'["\']([^"\']+)["\']', args_text)
        if not name_match:
            continue
        otel_name = name_match.group(1)
        if not otel_name.startswith("hippo.brain.mcp."):
            continue

        # Extract optional unit="..." keyword argument
        unit_match = re.search(r'unit\s*=\s*["\']([^"\']*)["\']', args_text)
        unit = unit_match.group(1) if unit_match else ""

        prom_name = otel_name.replace(".", "_")
        if kind == "create_counter":
            prom_name = prom_name + "_total"
        else:
            # Histograms: append unit suffix if unit maps to one
            prom_name = prom_name + _UNIT_SUFFIX.get(unit, "")

        instruments[prom_name] = kind

    return instruments


def test_enrichment_dashboard_mcp_names_match_instruments():
    """hippo-enrichment.json MCP panel exprs must reference exactly the metric
    names that mcp.py actually creates via create_counter / create_histogram.

    If mcp.py renames an instrument (e.g. "hippo.brain.mcp.tool_calls" ->
    "hippo.brain.mcp.calls"), the dashboard must be updated in the same PR —
    this test enforces that invariant.

    Fix: update the dashboard expr or the instrument name so they agree.
    """
    mcp_instruments = _extract_mcp_instrument_names()
    assert mcp_instruments, "No MCP instruments found in mcp.py — check the regex in this test."

    enrichment_path = _DASHBOARDS_DIR / "hippo-enrichment.json"
    assert enrichment_path.exists(), f"hippo-enrichment.json not found at {enrichment_path}"
    dashboard = json.loads(enrichment_path.read_text())

    violations: list[str] = []

    for panel_id, ref_id, expr in _collect_all_exprs(dashboard):
        for raw_name in _extract_hippo_metrics(expr):
            if "mcp" not in raw_name:
                continue
            normalized = _normalize_metric_name(raw_name)
            if normalized not in mcp_instruments:
                violations.append(
                    f"  panel_id={panel_id}  refId={ref_id!r}  "
                    f"metric={raw_name!r} (normalized: {normalized!r}) "
                    f"not found in mcp.py instruments={sorted(mcp_instruments)!r}"
                )

    assert not violations, (
        "hippo-enrichment.json references MCP metric names that do not match "
        "any create_counter / create_histogram call in brain/src/hippo_brain/mcp.py.\n\n"
        + "\n".join(violations)
    )


# ---------------------------------------------------------------------------
# Test 5: Drift-keeps-honest — health_score.rs must NOT have .with_unit("1")
# on the health.grade gauge, confirming the rename is in effect.
# ---------------------------------------------------------------------------


def test_health_grade_gauge_has_no_unit_1():
    """health_score.rs must not call .with_unit(\"1\") on the health.grade gauge.

    The rename from hippo_daemon_health_grade_ratio -> hippo_daemon_health_grade
    was achieved by dropping .with_unit(\"1\") from the gauge builder.  If that
    call is re-introduced the OTel->Prometheus exporter will append \"_ratio\"
    and every dashboard panel querying hippo_daemon_health_grade will go dark.

    Fix: remove .with_unit(\"1\") from the health.grade observable gauge in
    crates/hippo-daemon/src/health_score.rs.
    """
    health_score_path = _REPO_ROOT / "crates" / "hippo-daemon" / "src" / "health_score.rs"
    assert health_score_path.exists(), f"health_score.rs not found at {health_score_path}"
    source = health_score_path.read_text()

    # Strip Rust comment lines (// and /// doc comments) before checking so a
    # doc comment that *mentions* the forbidden pattern as a re-introduction
    # guard does not trip this assertion — we only care about real builder code.
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("//"))
    assert '.with_unit("1")' not in code, (
        'health_score.rs contains .with_unit("1") which would cause the '
        "OTel->Prometheus exporter to append '_ratio' to the metric name, "
        "breaking hippo_daemon_health_grade and hippo_daemon_health_active_alarms "
        'dashboard queries.  Remove .with_unit("1") from the gauge builder(s) '
        f"in {health_score_path}."
    )


# ---------------------------------------------------------------------------
# Test 6: Drift-keeps-honest — source_health_metric.rs must NOT have
# .with_unit("1") on consecutive_failures or probe_ok gauges.
# ---------------------------------------------------------------------------


def test_source_health_gauges_have_no_unit_1_for_renamed_instruments():
    """source_health_metric.rs must not call .with_unit(\"1\") on the
    consecutive_failures or probe_ok gauges.

    Dropping .with_unit(\"1\") from these two gauges is what removed the
    '_ratio' suffix from their Prometheus names, giving:
      hippo_daemon_source_health_consecutive_failures  (not *_ratio)
      hippo_daemon_source_health_probe_ok              (not *_ratio)

    Re-introducing .with_unit(\"1\") on either gauge would append '_ratio' and
    break any dashboard panel or alert rule querying the canonical names above.

    Note: the lag gauge correctly keeps .with_unit(\"ms\") — this test does NOT
    check for that, only for the unit=\"1\" regression.

    Fix: remove .with_unit(\"1\") from the consecutive_failures and probe_ok
    gauge builders in crates/hippo-daemon/src/source_health_metric.rs.
    """
    source_health_path = _REPO_ROOT / "crates" / "hippo-daemon" / "src" / "source_health_metric.rs"
    assert source_health_path.exists(), f"source_health_metric.rs not found at {source_health_path}"
    source = source_health_path.read_text()

    # Locate the consecutive_failures gauge block and the probe_ok gauge block.
    # We parse for the two gauge builder chains that follow the
    # "hippo.daemon.source_health.consecutive_failures" and
    # "hippo.daemon.source_health.probe_ok" string literals.
    for gauge_name in (
        "hippo.daemon.source_health.consecutive_failures",
        "hippo.daemon.source_health.probe_ok",
    ):
        # Find the position of the gauge name literal in source
        idx = source.find(f'"{gauge_name}"')
        assert idx != -1, (
            f"Could not find gauge name literal {gauge_name!r} in "
            f"{source_health_path} — was the instrument renamed or removed?"
        )

        # Scan ahead to the .build() call that terminates this gauge builder
        build_idx = source.find(".build()", idx)
        assert build_idx != -1, (
            f"Could not find .build() after gauge {gauge_name!r} in {source_health_path}"
        )

        builder_block = source[idx : build_idx + len(".build()")]
        assert '.with_unit("1")' not in builder_block, (
            f'Gauge {gauge_name!r} in {source_health_path} has .with_unit("1"), '
            "which would cause the OTel->Prometheus exporter to append '_ratio' "
            'to its Prometheus name.  Remove .with_unit("1") from this gauge '
            "builder to keep the canonical name without the '_ratio' suffix."
        )


# ---------------------------------------------------------------------------
# Test 7: Exactly the four expected production dashboards exist (no extras,
# no bench dashboards remaining after the isolation decision).
# ---------------------------------------------------------------------------


def test_only_prod_dashboards_exist():
    """The dashboards directory must contain exactly the four production
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


# ---------------------------------------------------------------------------
# Test 8: Regression guard — hippo_daemon_fallback_writes_total must not
# appear in any dashboard (it never existed as an instrument).
# ---------------------------------------------------------------------------


def test_no_fallback_writes_total_in_any_dashboard():
    """No dashboard may reference hippo_daemon_fallback_writes_total.

    This name was never a real OTel instrument.  The correct names are
    hippo_daemon_fallback_pending (gauge) and hippo_daemon_fallback_recovered_total
    (counter).

    Fix: replace hippo_daemon_fallback_writes_total with the appropriate
    instrument name in the dashboard expr.
    """
    violations: list[str] = []

    for path in sorted(_DASHBOARDS_DIR.glob("*.json")):
        try:
            raw_text = path.read_text()
        except OSError:
            continue
        if "hippo_daemon_fallback_writes_total" in raw_text:
            violations.append(f"  dashboard={path.name!r}")

    assert not violations, (
        "The following dashboards reference hippo_daemon_fallback_writes_total, "
        "which was never a real OTel instrument.  Use hippo_daemon_fallback_pending "
        "or hippo_daemon_fallback_recovered_total instead.\n\n" + "\n".join(violations)
    )
