import json
from pathlib import Path


def _dashboard(name: str) -> dict:
    root = Path(__file__).resolve().parents[2]
    return json.loads((root / "otel" / "grafana" / "dashboards" / name).read_text())


def _panel_by_id(dashboard: dict, panel_id: int) -> dict:
    return next(panel for panel in dashboard["panels"] if panel["id"] == panel_id)


def test_enrichment_dashboard_tracks_all_enrichment_sources():
    dashboard = _dashboard("hippo-enrichment.json")
    queue_exprs = [target["expr"] for target in _panel_by_id(dashboard, 1)["targets"]]
    claimed_exprs = [target["expr"] for target in _panel_by_id(dashboard, 3)["targets"]]

    for source in ("shell", "claude", "codex", "browser", "workflow", "opencode"):
        assert any(f'source="{source}"' in expr for expr in queue_exprs), source
        assert any(f'source="{source}"' in expr for expr in claimed_exprs), source
