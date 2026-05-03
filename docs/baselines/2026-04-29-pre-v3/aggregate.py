"""Aggregate panel scores across the 5 experts."""
import json, statistics
from collections import defaultdict, Counter
from pathlib import Path

EXPERTS = ["enrichment", "vector", "schema", "rag", "mcp"]
DIMS = ["accuracy", "succinctness", "usefulness", "ask_suitability", "mcp_suitability"]
PANEL_DIR = Path("/tmp/hippo-eval-panel")

# Load
expert_scores = {}
for exp in EXPERTS:
    rows = [json.loads(l) for l in open(PANEL_DIR / f"scores_{exp}.jsonl")]
    expert_scores[exp] = {r["uuid"]: r for r in rows}

uuids = list(expert_scores["enrichment"].keys())
assert all(set(expert_scores[e].keys()) == set(uuids) for e in EXPERTS), \
    "expert scorecards disagree on uuid set"
print(f"# uuids covered: {len(uuids)}")

# Load dossier metadata for stratum lookup
dossier = {r["uuid"]: r for r in (json.loads(l) for l in open(PANEL_DIR / "dossier.jsonl"))}

# Per-expert means
print("\n## Per-expert means")
print("| expert | accuracy | succinctness | usefulness | ask | mcp |")
print("|---|---|---|---|---|---|")
expert_means = {}
for exp in EXPERTS:
    means = {}
    for d in DIMS:
        vs = [expert_scores[exp][u][d] for u in uuids]
        means[d] = statistics.mean(vs)
    expert_means[exp] = means
    print(f"| {exp:11s} | {means['accuracy']:.2f} | {means['succinctness']:.2f} | "
          f"{means['usefulness']:.2f} | {means['ask_suitability']:.2f} | "
          f"{means['mcp_suitability']:.2f} |")

# Cross-expert mean per dimension (panel mean)
print("\n## Panel mean per dimension (mean across 5 experts of each expert's mean)")
panel_means = {d: statistics.mean(expert_means[e][d] for e in EXPERTS) for d in DIMS}
panel_stdev = {d: statistics.stdev([expert_means[e][d] for e in EXPERTS]) for d in DIMS}
for d in DIMS:
    print(f"  {d:18s} mean={panel_means[d]:.2f}  stdev_across_experts={panel_stdev[d]:.2f}")

# Per-node cross-expert agreement
# For each node, compute max-min across experts per dim. Agreement = % of nodes
# where max-min ≤ 1 (tight) vs ≥ 3 (wide).
print("\n## Inter-rater agreement (per-dimension distribution of max-min across experts)")
print("| dim | tight (Δ≤1) | medium (Δ=2) | wide (Δ≥3) |")
print("|---|---|---|---|")
for d in DIMS:
    deltas = []
    for u in uuids:
        vs = [expert_scores[e][u][d] for e in EXPERTS]
        deltas.append(max(vs) - min(vs))
    tight = sum(1 for x in deltas if x <= 1)
    med = sum(1 for x in deltas if x == 2)
    wide = sum(1 for x in deltas if x >= 3)
    print(f"| {d:18s} | {tight} | {med} | {wide} |")

# Per-node cross-expert mean — find consensus winners and losers
node_means = {}
for u in uuids:
    means = {}
    for d in DIMS:
        means[d] = statistics.mean(expert_scores[e][u][d] for e in EXPERTS)
    means["overall"] = statistics.mean(means[d] for d in DIMS)
    node_means[u] = means

# Bottom 10 by overall
sorted_by_overall = sorted(node_means.items(), key=lambda kv: kv[1]["overall"])

print("\n## Worst 10 nodes by overall panel mean")
print("| rank | uuid | source | stratum | overall | acc | suc | use | ask | mcp |")
print("|---|---|---|---|---|---|---|---|---|---|")
for i, (u, m) in enumerate(sorted_by_overall[:10], 1):
    d = dossier[u]
    print(f"| {i} | `{u[:8]}…` | {d['source']:5s} | {d['stratum']:18s} | "
          f"{m['overall']:.2f} | {m['accuracy']:.1f} | {m['succinctness']:.1f} | "
          f"{m['usefulness']:.1f} | {m['ask_suitability']:.1f} | {m['mcp_suitability']:.1f} |")

# Top 10
print("\n## Best 10 nodes by overall panel mean")
print("| rank | uuid | source | stratum | overall |")
print("|---|---|---|---|---|")
for i, (u, m) in enumerate(sorted_by_overall[-10:][::-1], 1):
    d = dossier[u]
    print(f"| {i} | `{u[:8]}…` | {d['source']:5s} | {d['stratum']:18s} | {m['overall']:.2f} |")

# Outliers: any node where ≥2 experts gave ≤2 on any dimension
print("\n## Outliers: nodes where ≥2 experts scored ≤2 on any single dimension")
outliers = []
for u in uuids:
    flags = []
    for d in DIMS:
        low = [e for e in EXPERTS if expert_scores[e][u][d] <= 2]
        if len(low) >= 2:
            flags.append(f"{d}({','.join(low)})")
    if flags:
        outliers.append((u, flags))
print(f"  {len(outliers)} outlier(s)")
for u, flags in outliers[:15]:
    d = dossier[u]
    print(f"  - `{u[:8]}…` ({d['source']}, {d['stratum']}): {'; '.join(flags)}")

# Stratum-level analysis
print("\n## Mean overall score by stratum")
strata_scores = defaultdict(list)
for u in uuids:
    strata_scores[dossier[u]["stratum"]].append(node_means[u]["overall"])
for s, vs in sorted(strata_scores.items(), key=lambda kv: -statistics.mean(kv[1])):
    print(f"  {s:20s}  n={len(vs):3d}  mean={statistics.mean(vs):.2f}  "
          f"min={min(vs):.2f}  max={max(vs):.2f}")

# Source-level analysis
print("\n## Mean overall score by source")
src_scores = defaultdict(list)
for u in uuids:
    src_scores[dossier[u]["source"]].append(node_means[u]["overall"])
for s, vs in sorted(src_scores.items(), key=lambda kv: -statistics.mean(kv[1])):
    print(f"  {s:6s}  n={len(vs):3d}  mean={statistics.mean(vs):.2f}")

# How tight is each expert vs the panel? (which expert is harshest / easiest)
print("\n## Expert deviation from panel mean (positive = harsher than panel)")
for exp in EXPERTS:
    devs = []
    for d in DIMS:
        devs.append(panel_means[d] - expert_means[exp][d])
    print(f"  {exp:11s}  delta={sum(devs)/len(devs):+.2f}  "
          f"(acc {panel_means['accuracy']-expert_means[exp]['accuracy']:+.2f} / "
          f"suc {panel_means['succinctness']-expert_means[exp]['succinctness']:+.2f} / "
          f"use {panel_means['usefulness']-expert_means[exp]['usefulness']:+.2f} / "
          f"ask {panel_means['ask_suitability']-expert_means[exp]['ask_suitability']:+.2f} / "
          f"mcp {panel_means['mcp_suitability']-expert_means[exp]['mcp_suitability']:+.2f})")

# Save the per-node panel scorecard
out = PANEL_DIR / "panel_scorecard.jsonl"
with out.open("w") as f:
    for u in uuids:
        rec = {
            "uuid": u,
            "node_id": dossier[u]["node_id"],
            "source": dossier[u]["source"],
            "stratum": dossier[u]["stratum"],
            "panel_mean": node_means[u],
            "expert_scores": {e: {d: expert_scores[e][u][d] for d in DIMS}
                              for e in EXPERTS},
            "expert_notes": {e: expert_scores[e][u].get("notes", "") for e in EXPERTS},
        }
        f.write(json.dumps(rec) + "\n")
print(f"\nWrote panel scorecard: {out}")
