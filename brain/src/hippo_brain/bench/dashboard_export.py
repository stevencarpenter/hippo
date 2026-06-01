"""Render the bench results datastore into one self-contained HTML file.

No server, no network: the data is embedded as a JSON blob and a small
vanilla-JS view renders the leaderboard, per-node lookup, and run history.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from hippo_brain.bench.paths import bench_results_db_path
from hippo_brain.bench.results_store import (
    all_node_details,
    connect,
    leaderboard_latest,
    run_history,
)


def _gather(conn: sqlite3.Connection) -> dict:
    # Per-node ("best model per corpus member") view: retrieval + enrichment for
    # every scored node, fetched in two queries total (not an N+1 over nodes).
    return {
        "leaderboard": leaderboard_latest(conn, mode="hybrid"),
        "history": run_history(conn),
        "nodes": all_node_details(conn, mode="hybrid"),
    }


_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>hippo-bench dashboard</title>
<style>
 body {{ font-family: ui-monospace, monospace; margin: 2rem; background:#0f1115; color:#e6e6e6; }}
 h1, h2 {{ color:#7aa2ff; }}
 table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem; }}
 th, td {{ border: 1px solid #2a2f3a; padding: 6px 10px; text-align: left; }}
 th {{ background:#1a1e27; }}
 tr:nth-child(even) {{ background:#161a22; }}
</style>
</head>
<body>
<h1>hippo-bench dashboard</h1>
<h2>Leaderboard — latest scored run (hybrid retrieval)</h2>
<div id="leaderboard"></div>
<h2>Per-node — best model per corpus member</h2>
<select id="node-select"></select>
<h3>Retrieval (all runs, best score first)</h3>
<div id="node-retrieval"></div>
<h3>Enrichment (all runs)</h3>
<div id="node-enrichment"></div>
<h2>Run history</h2>
<div id="history"></div>
<script type="application/json" id="hippo-bench-data">{data_json}</script>
<script>
 const data = JSON.parse(document.getElementById("hippo-bench-data").textContent);
 // Build the DOM with textContent so cell values (incl. LLM-generated
 // parsed_output) can never inject markup — no innerHTML with data.
 function renderTable(target, rows, cols) {{
   const el = document.getElementById(target);
   if (!rows || !rows.length) {{ el.textContent = "(no data)"; return; }}
   const tbl = document.createElement("table");
   const thead = tbl.insertRow();
   for (const c of cols) {{
     const th = document.createElement("th");
     th.textContent = c;
     thead.appendChild(th);
   }}
   for (const r of rows) {{
     const tr = tbl.insertRow();
     for (const c of cols) {{
       const td = tr.insertCell();
       td.textContent = (r[c] === null || r[c] === undefined) ? "" : String(r[c]);
     }}
   }}
   el.replaceChildren(tbl);
 }}
 renderTable("leaderboard", data.leaderboard, ["model_id", "avg_mrr", "hit_at_1", "scored_nodes", "run_id"]);
 renderTable("history", data.history, ["started_at_ms", "run_id", "corpus_version", "finished_at_ms"]);

 // Per-node view: a <select> of every scored corpus node; on change, render
 // that node's retrieval ranking and enrichment rows across all runs.
 const sel = document.getElementById("node-select");
 const nodeIds = Object.keys(data.nodes).sort();
 for (const id of nodeIds) {{
   const opt = document.createElement("option");
   opt.value = id; opt.textContent = id;
   sel.appendChild(opt);
 }}
 function renderNode(id) {{
   const detail = data.nodes[id] || {{retrieval: [], enrichment: []}};
   renderTable("node-retrieval", detail.retrieval,
     ["model_id", "mrr", "rank", "hit_at_1", "run_id", "started_at_ms"]);
   renderTable("node-enrichment", detail.enrichment,
     ["model_id", "schema_valid", "refusal_detected", "echo_similarity",
      "entity_sanity", "parsed_output_json", "run_id"]);
 }}
 sel.addEventListener("change", () => renderNode(sel.value));
 if (nodeIds.length) renderNode(nodeIds[0]);
</script>
</body>
</html>
"""


def build_dashboard_html(conn: sqlite3.Connection) -> str:
    payload = _gather(conn)
    # Escape "</script>" defensively so embedded data can't break out of the tag.
    blob = json.dumps(payload, sort_keys=True).replace("</", "<\\/")
    return _TEMPLATE.format(data_json=blob)


def export_dashboard(out_path: Path | None = None, *, db_path: Path | None = None) -> Path:
    out = out_path or (bench_results_db_path().parent / "dashboard.html")
    conn = connect(db_path)
    try:
        html_text = build_dashboard_html(conn)
    finally:
        conn.close()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html_text, encoding="utf-8")
    return out
