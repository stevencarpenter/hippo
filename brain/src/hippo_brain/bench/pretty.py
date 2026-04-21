"""Pretty text rendering of a run JSONL file."""

from __future__ import annotations

import json
from pathlib import Path


def render_summary_text(run_file: Path) -> str:
    manifest: dict | None = None
    summaries: list[dict] = []
    for line in run_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        rt = obj.get("record_type")
        if rt == "run_manifest":
            manifest = obj
        elif rt == "model_summary":
            summaries.append(obj)

    lines: list[str] = []
    if manifest is None:
        return "no run_manifest found in file"

    lines.append(f"run_id = {manifest.get('run_id')}")
    lines.append(f"corpus_version = {manifest.get('corpus_version')}")
    lines.append(f"candidate_models = {manifest.get('candidate_models')}")
    lines.append("")

    if not summaries:
        lines.append("no model summaries in run (empty or dry-run)")
        return "\n".join(lines)

    header = (
        f"{'model':30} "
        f"{'verdict':7} "
        f"{'sch':>6} "
        f"{'ref':>6} "
        f"{'p95ms':>7} "
        f"{'sc_mean':>8} "
        f"{'ent':>6} "
        f"{'walls':>6}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for s in summaries:
        g = s["gates"]
        v = s["tier0_verdict"]
        peak = s.get("system_peak", {})
        lines.append(
            f"{s['model']['id'][:30]:30} "
            f"{'pass' if v['passed'] else 'fail':7} "
            f"{g.get('schema_validity_rate', 0):6.2f} "
            f"{g.get('refusal_rate', 0):6.2f} "
            f"{g.get('latency_p95_ms', 0):7d} "
            f"{g.get('self_consistency_mean', 0):8.3f} "
            f"{g.get('entity_sanity_mean', 0):6.2f} "
            f"{peak.get('wall_clock_sec', 0):5d}s"
        )
        if not v["passed"]:
            lines.append(f"  failed: {', '.join(v['failed_gates'])}")
    return "\n".join(lines)
