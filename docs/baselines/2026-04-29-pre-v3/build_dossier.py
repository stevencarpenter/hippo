#!/usr/bin/env python3
"""Build a stratified 100-node dossier of v3 re-enriched knowledge nodes.

Output: /tmp/hippo-eval-panel/dossier.jsonl, one JSON per node:
  {
    "node_id": int, "uuid": str, "stratum": str,
    "source": "claude" | "shell" | "dual",
    "enrichment_model": str,
    "content": <parsed JSON>,    # summary, intent, outcome, entities, etc.
    "embed_text": str,
    "content_len": int, "embed_text_len": int,
    "shell_events": [...],       # only present if source has shell
    "claude_segments_text": str  # only present if source has claude (joined)
  }
"""

import json, random, sqlite3, sys
from pathlib import Path

random.seed(42)

DB = Path.home() / ".local" / "share" / "hippo" / "hippo.db"
OUT = Path("/tmp/hippo-eval-panel/dossier.jsonl")

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row

def rows(sql, params=()):
    return [dict(r) for r in conn.execute(sql, params).fetchall()]

# --- Pool of v3 nodes with their link-table membership flags ---
pool = rows("""
    SELECT
        n.id,
        n.uuid,
        n.content,
        n.embed_text,
        n.enrichment_model,
        length(n.content) AS clen,
        length(n.embed_text) AS elen,
        EXISTS(SELECT 1 FROM knowledge_node_events kne
               WHERE kne.knowledge_node_id = n.id) AS has_shell,
        EXISTS(SELECT 1 FROM knowledge_node_claude_sessions kncs
               WHERE kncs.knowledge_node_id = n.id) AS has_claude
    FROM knowledge_nodes n
    WHERE n.enrichment_version = 3
""")
print(f"v3 pool size: {len(pool)}", file=sys.stderr)

# Partition
shell_only  = [n for n in pool if n["has_shell"]  and not n["has_claude"]]
claude_only = [n for n in pool if n["has_claude"] and not n["has_shell"]]
dual        = [n for n in pool if n["has_shell"]  and     n["has_claude"]]
short_embed = [n for n in pool if n["elen"] < 200]
long_content= [n for n in pool if n["clen"] > 4000]

print(f"shell_only={len(shell_only)} claude_only={len(claude_only)} "
      f"dual={len(dual)} short_embed={len(short_embed)} "
      f"long_content={len(long_content)}", file=sys.stderr)

def take(lst, n, label):
    sample = random.sample(lst, min(n, len(lst)))
    for s in sample:
        s["stratum"] = label
    return sample

picked = []
seen_ids = set()

def add(items):
    for it in items:
        if it["id"] in seen_ids:
            continue
        seen_ids.add(it["id"])
        picked.append(it)

add(take(claude_only,   50, "claude_random"))
add(take(shell_only,    25, "shell_random"))
add(take(short_embed,   10, "short_embed_text"))
add(take(long_content,  10, "long_content"))
add(take(dual,           5, "dual_source"))

# Top up if dedup left us short
need = 100 - len(picked)
if need > 0:
    leftover = [n for n in claude_only + shell_only if n["id"] not in seen_ids]
    random.shuffle(leftover)
    for n in leftover[:need]:
        n["stratum"] = "topup_random"
        picked.append(n)
        seen_ids.add(n["id"])

print(f"picked: {len(picked)}", file=sys.stderr)

# --- Pull source rows + write dossier ---
def fetch_shell(node_id):
    return rows("""
        SELECT e.id, e.session_id, e.timestamp, e.command, e.exit_code,
               e.duration_ms, e.cwd, e.git_branch, e.git_repo,
               substr(e.stdout, 1, 4000) AS stdout_truncated,
               substr(e.stderr, 1, 4000) AS stderr_truncated,
               e.shell
        FROM events e
        JOIN knowledge_node_events kne ON kne.event_id = e.id
        WHERE kne.knowledge_node_id = ?
        ORDER BY e.timestamp ASC
    """, (node_id,))

def fetch_claude(node_id):
    rs = rows("""
        SELECT cs.id, cs.session_id, cs.cwd, cs.git_branch, cs.summary_text,
               cs.message_count, cs.start_time, cs.end_time
        FROM claude_sessions cs
        JOIN knowledge_node_claude_sessions kncs ON kncs.claude_session_id = cs.id
        WHERE kncs.knowledge_node_id = ?
        ORDER BY cs.start_time ASC
    """, (node_id,))
    text = "\n---\n\n".join((r.get("summary_text") or "") for r in rs)
    # cap to keep dossier reasonable
    if len(text) > 12000:
        text = text[:12000] + "\n…[truncated]…"
    return rs, text

OUT.parent.mkdir(parents=True, exist_ok=True)
with OUT.open("w") as f:
    for n in picked:
        try:
            content_json = json.loads(n["content"])
        except Exception:
            content_json = {"_invalid_json": True, "raw": n["content"][:2000]}

        rec = {
            "node_id": n["id"],
            "uuid": n["uuid"],
            "stratum": n["stratum"],
            "source": ("dual" if n["has_shell"] and n["has_claude"]
                       else "shell" if n["has_shell"] else "claude"),
            "enrichment_model": n["enrichment_model"],
            "content": content_json,
            "embed_text": n["embed_text"],
            "content_len": n["clen"],
            "embed_text_len": n["elen"],
        }
        if n["has_shell"]:
            shell_rows = fetch_shell(n["id"])
            # cap each rec's shell payload
            rec["shell_events"] = shell_rows[:30]
            rec["shell_event_count"] = len(shell_rows)
        if n["has_claude"]:
            cs_rows, joined = fetch_claude(n["id"])
            rec["claude_segments_text"] = joined
            rec["claude_segment_count"] = len(cs_rows)
            rec["claude_session_meta"] = [
                {k: v for k, v in r.items() if k != "summary_text"}
                for r in cs_rows
            ]
        f.write(json.dumps(rec) + "\n")

print(f"wrote {OUT}", file=sys.stderr)

# Quick stratum summary
from collections import Counter
print("strata:", dict(Counter(n["stratum"] for n in picked)), file=sys.stderr)
