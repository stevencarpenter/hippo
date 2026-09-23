"""Frozen real-corpus retrieval experiments. All data stays outside the checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sqlite3
import statistics
import time
import tomllib
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

import httpx
import sqlite_vec

from hippo_brain import retrieval
from hippo_brain.bench.decision_capture import decision_root
from hippo_brain.bench.decision_sidecar import digest, read_rows, write_json
from hippo_brain.redaction import redact

LINKS = {
    "shell": ("knowledge_node_events", "event_id"),
    "claude": ("knowledge_node_agentic_sessions", "agentic_session_id"),
    "browser": ("knowledge_node_browser_events", "browser_event_id"),
    "workflow": ("knowledge_node_workflow_runs", "run_id"),
}


def open_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA query_only=ON")
    return conn


def golden_nodes(conn: sqlite3.Connection, source_id: str) -> list[str]:
    source, identifier = source_id.split("-", 1)
    table, column = LINKS[source]
    return [
        row[0]
        for row in conn.execute(
            f"SELECT n.uuid FROM knowledge_nodes n JOIN {table} l "
            f"ON l.knowledge_node_id=n.id WHERE l.{column}=? ORDER BY n.uuid",
            (identifier,),
        )
    ]


def prepare(source: Path, qa_path: Path, config_path: Path, count: int) -> Path:
    if count <= 0:
        raise ValueError("count must be positive")
    root = decision_root() / f"knowledge-{time.time_ns() // 1_000_000}"
    root.mkdir(mode=0o700, parents=True)
    snapshot = root / "baseline.sqlite"
    # SQLite backup includes committed WAL pages without pausing or modifying Hippo.
    with closing(open_readonly(source)) as live, closing(sqlite3.connect(snapshot)) as destination:
        # Hold one read snapshot so ongoing capture cannot restart a paged backup.
        live.execute("BEGIN")
        live.execute("SELECT count(*) FROM knowledge_nodes").fetchone()
        live.backup(destination, pages=2048)
    snapshot.chmod(0o600)
    config = tomllib.loads(config_path.read_text())
    tuning = retrieval.configure(config.get("retrieval"))
    qa = [json.loads(line) for line in qa_path.read_text().splitlines() if line.strip()]
    conn = open_readonly(snapshot)
    try:
        stored_model = conn.execute("SELECT model FROM embed_model_meta WHERE id=1").fetchone()[0]
        if stored_model != config["models"]["embedding"]:
            raise ValueError("snapshot and configured embedding model differ")
        availability = [{**q, "golden_nodes": golden_nodes(conn, q["golden_event_id"])} for q in qa]
        eligible = [q for q in availability if q["golden_nodes"]]
        # Selection precedes retrieval and all candidate judgments, independent of scores.
        selected = sorted(eligible, key=lambda q: digest(["knowledge-v1", q["qa_id"]]))[:count]
        if len(selected) != count:
            raise ValueError(f"only {len(selected)} source-linked questions available")
        with snapshot.open("rb") as stream:
            snapshot_hash = hashlib.file_digest(stream, "sha256").hexdigest()
        protocol = {
            "created_at_ms": time.time_ns() // 1_000_000,
            "source_database": str(source.resolve()),
            "snapshot_sha256": snapshot_hash,
            "qa_sha256": hashlib.sha256(qa_path.read_bytes()).hexdigest(),
            "qa_source": str(qa_path.resolve()),
            "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "question_ids": [q["qa_id"] for q in selected],
            "all_questions": len(qa),
            "source_linked_questions": len(eligible),
            "selected_questions": count,
            "tuning": asdict(tuning),
            "embedding_model": stored_model,
            "llm_model": config["models"]["query"],
            "llm_url": config["inference"]["base_url"],
            "jev_model": "jev-1.13.0",
            "label_semantics": "known-source recovery; other candidates are unjudged, not negatives",
            "population": "previously authored source-grounded questions; not sampled live query traffic",
            "gates": {
                "hit_at_1_not_below_local": True,
                "hit_at_5_not_below_retrieval": True,
                "mrr_not_below_retrieval": True,
                "jev_p95_ms_max": 2000,
                "errors_max": 0,
            },
            "gates_scope": "necessary screen only; source-target labels are not full relevance judgments",
        }
        write_json(root / "protocol.json", protocol)
        write_json(root / "availability.json", availability)
        (root / "knowledge_probe.py").write_bytes(Path(__file__).read_bytes())
        vectors = []
        with httpx.Client(timeout=60, trust_env=False) as client:
            for offset in range(0, len(selected), 10):
                batch = [redact(q["question"]) for q in selected[offset : offset + 10]]
                start = time.perf_counter()
                response = client.post(
                    protocol["llm_url"].rstrip("/") + "/embeddings",
                    json={"model": stored_model, "input": batch},
                )
                response.raise_for_status()
                data = response.json()
                items = sorted(data["data"], key=lambda item: item["index"])
                if [item["index"] for item in items] != list(range(len(batch))):
                    raise ValueError("embedding batch indices differ from input")
                for item in items:
                    vector = item["embedding"]
                    if len(vector) != 768 or not all(math.isfinite(x) for x in vector):
                        raise ValueError("invalid embedding")
                    vectors.append(vector)
                write_json(
                    root / f"embedding-{offset}.json",
                    {
                        "input": batch,
                        "response": data,
                        "elapsed_ms": (time.perf_counter() - start) * 1000,
                    },
                )
        cases, targets = [], []
        for q, vector in zip(selected, vectors, strict=True):
            start = time.perf_counter()
            hits = retrieval.search(
                conn,
                q["question"],
                vector,
                retrieval.Filters(source=q["source_filter"]),
                limit=tuning.rerank_pool,
                tuning=tuning,
            )
            elapsed = (time.perf_counter() - start) * 1000
            if len(hits) < 2:
                raise ValueError(f"insufficient candidates for {q['qa_id']}")
            cases.append(
                {
                    "id": q["qa_id"],
                    "task": "ranking",
                    "query": redact(q["question"]),
                    "candidates": [
                        {
                            key: redact(getattr(hit, key))
                            for key in ("summary", "embed_text", "commands_raw")
                        }
                        for hit in hits
                    ],
                }
            )
            targets.append(
                {
                    **q,
                    "query_vector": vector,
                    "candidate_uuids": [h.uuid for h in hits],
                    "retrieval_ms": elapsed,
                    "candidate_hash": digest(cases[-1]),
                }
            )
            print(f"prepared {len(cases)}/{count}: {q['qa_id']}", flush=True)
        write_json(root / "cases.json", cases)
        write_json(root / "targets.json", targets)
        write_json(
            root / "freeze.json",
            {
                "cases_hash": digest(cases),
                "targets_hash": digest(targets),
                "protocol_hash": digest(protocol),
            },
        )
    finally:
        conn.close()
    return root


def recovery(targets: list[dict], orders: dict[str, list[int]]) -> dict:
    ranks = []
    for target in targets:
        candidates = target["candidate_uuids"]
        order = orders.get(target["qa_id"], [])
        ranks.append(
            next(
                (
                    rank
                    for rank, index in enumerate(order, 1)
                    if candidates[index] in target["golden_nodes"]
                ),
                None,
            )
        )
    n = len(ranks)
    return {
        "questions": n,
        "hit_at_1": sum(r == 1 for r in ranks) / n,
        "hit_at_5": sum(r is not None and r <= 5 for r in ranks) / n,
        "hit_at_10": sum(r is not None and r <= 10 for r in ranks) / n,
        "hit_at_pool": sum(r is not None for r in ranks) / n,
        "mrr": sum(1 / r for r in ranks if r) / n,
    }


def report(root: Path, run: Path) -> dict:
    targets = json.loads((root / "targets.json").read_text())
    cases = json.loads((root / "cases.json").read_text())
    protocol = json.loads((root / "protocol.json").read_text())
    freeze = json.loads((root / "freeze.json").read_text())
    for name, value in (("cases", cases), ("targets", targets), ("protocol", protocol)):
        if digest(value) != freeze[name + "_hash"]:
            raise ValueError(f"changed {name}")
    manifest = json.loads((run / "manifest.json").read_text())
    completion = json.loads((run / "completion.json").read_text())
    if any(completion["exit_codes"].get(arm) != 0 for arm in manifest["arms"]):
        raise ValueError("run has unfinished or failed workers")
    for key in ("llm_model", "llm_url", "jev_model"):
        if manifest[key] != protocol[key]:
            raise ValueError(f"run uses a different {key}")
    if manifest["repeats"] != 1:
        raise ValueError("this source-recovery report requires exactly one repeat")
    from hippo_brain.bench.decision_sidecar import load_cases, parse_jev, requests_for
    from hippo_brain.rerank import parse_ranking

    normalized, _ = load_cases(root / "cases.json")
    if manifest["corpus_hash"] != digest(normalized):
        raise ValueError("run uses a different corpus")
    all_orders = {
        "retrieval": {t["qa_id"]: list(range(len(t["candidate_uuids"]))) for t in targets}
    }
    groups = {"retrieval": recovery(targets, all_orders["retrieval"])}
    rows = read_rows(run)
    by_id = {case["id"]: case for case in normalized}
    for row in rows:
        if row["arm"] not in manifest["arms"] or row["case_id"] not in by_id:
            raise ValueError("unexpected row identity")
        if row["repeat"] != 0 or row["input_hash"] != by_id[row["case_id"]]["input_hash"]:
            raise ValueError("row uses a different input")
        if "request" in row and digest(row["request"]) != row["request_hash"]:
            raise ValueError("changed request")
        if "request" in row:
            llm_request, jev_request = requests_for(
                by_id[row["case_id"]], protocol["llm_model"], protocol["jev_model"]
            )
            expected_request = jev_request if row["backend"] == "jev" else llm_request
            if row["request"] != expected_request:
                raise ValueError("request differs from frozen input or configured question")
            if row["status"] == "ok":
                response = row["response"]
                raw_order = (
                    parse_jev(response, row["request"], "ranking")["order"]
                    if row["backend"] == "jev"
                    else parse_ranking(
                        response["choices"][0]["message"]["content"],
                        len(by_id[row["case_id"]]["state"]["candidates"]),
                    )
                )
                if raw_order != row["canonical_order"]:
                    raise ValueError("recorded ranking differs from raw response")
        if row["status"] == "ok" and sorted(row["canonical_order"]) != list(
            range(len(by_id[row["case_id"]]["state"]["candidates"]))
        ):
            raise ValueError("invalid ranking permutation")
    for arm in manifest["arms"]:
        arm_rows = [r for r in rows if r["arm"] == arm]
        ids = [r["case_id"] for r in arm_rows]
        if len(set(ids)) != len(ids) or set(ids) != {t["qa_id"] for t in targets}:
            raise ValueError("missing, duplicate or unexpected case")
        all_orders[arm] = {
            r["case_id"]: r["canonical_order"] for r in arm_rows if r["status"] == "ok"
        }
        group = recovery(targets, all_orders[arm])
        latency = sorted(r["elapsed_ms"] for r in arm_rows)
        group.update(
            {
                "p50_ms": statistics.median(latency),
                "p95_ms": latency[math.ceil(len(latency) * 0.95) - 1],
                "errors": sum(r["status"] == "error" for r in arm_rows),
                "abstentions": sum(r["status"] == "abstain" for r in arm_rows),
            }
        )
        groups[arm] = group
    jev, local, baseline = groups["jev"], groups["llm"], groups["retrieval"]
    gates = {
        "local_baseline_valid": local["errors"] == 0 and local["abstentions"] == 0,
        "hit_at_1_not_below_local": jev["hit_at_1"] >= local["hit_at_1"],
        "hit_at_5_not_below_retrieval": jev["hit_at_5"] >= baseline["hit_at_5"],
        "mrr_not_below_retrieval": jev["mrr"] >= baseline["mrr"],
        "latency": jev["p95_ms"] <= protocol["gates"]["jev_p95_ms_max"],
        "errors": jev["errors"] <= protocol["gates"]["errors_max"],
    }
    return {
        "run_id": manifest["run_id"],
        "metric": protocol["label_semantics"],
        "groups": groups,
        "gates": gates,
        "screen_passed": all(gates.values()),
        "source_ranks": [
            {
                "qa_id": t["qa_id"],
                "ranks": {
                    arm: next(
                        (
                            rank
                            for rank, index in enumerate(orders.get(t["qa_id"], []), 1)
                            if t["candidate_uuids"][index] in t["golden_nodes"]
                        ),
                        None,
                    )
                    for arm, orders in all_orders.items()
                },
            }
            for t in targets
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare")
    prep.add_argument("--source", type=Path, default=Path.home() / ".local/share/hippo/hippo.db")
    prep.add_argument("--qa", type=Path, default=Path(__file__).with_name("qa_template.jsonl"))
    prep.add_argument("--config", type=Path, default=Path.home() / ".config/hippo/config.toml")
    prep.add_argument("--count", type=int, default=50)
    replay = commands.add_parser("report")
    replay.add_argument("root", type=Path)
    replay.add_argument("run", type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    if args.command == "prepare":
        print(prepare(args.source, args.qa, args.config, args.count))
    else:
        print(json.dumps(report(args.root, args.run), indent=2))


if __name__ == "__main__":
    main()
