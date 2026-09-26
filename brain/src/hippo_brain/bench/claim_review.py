"""Advisory Jev assessment, blinded review, and descriptive fidelity reports."""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from hippo_brain.bench.claim_packets import (
    create_directory,
    load_packets,
    prepare,
    write_artifact,
)
from hippo_brain.jev import (
    MODEL,
    JevClient,
    JevUnavailable,
    canonical,
    digest,
    number,
    validate_response,
)
from hippo_brain.decision_capture import external_path

VERSION = "claim-review-v1"
DECISIONS = {"yes", "no", "unsure"}


def rubric() -> dict[str, Any]:
    return json.loads(
        (Path(__file__).parents[1] / "_fixtures/claim_review_rubric.json").read_text()
    )


def request_for(packet: dict[str, Any]) -> dict[str, Any]:
    return {"model": f"jev-{MODEL}", "state": packet["state"], "questions": rubric()["questions"]}


def disposition(record: dict[str, Any], threshold: float) -> str:
    """A hypothetical routing decision, never permission to publish knowledge."""
    number(threshold, 0, 1)
    if record["status"] != "ok":
        return "review"
    answer = record["response"]["answers"]["verdict"]
    if answer["confidence"] < threshold:
        return "review"
    return "approve" if answer["choice"] == "supports" else "reject"


async def assess(
    corpus: Path,
    out: Path,
    *,
    max_requests: int,
    timeout: float = 10,
    client: JevClient | None = None,
) -> dict[str, Any]:
    if type(max_requests) is not int or not 1 <= max_requests <= 1000:
        raise ValueError("max_requests must be between 1 and 1000")
    number(timeout, 0.001, 60)
    packets = load_packets(corpus)
    definition = rubric()
    manifest = {
        "version": VERSION,
        "created_at_ms": int(time.time() * 1000),
        "packets_hash": digest(packets),
        "rubric": definition,
        "model": f"jev-{MODEL}",
        "max_requests": max_requests,
        "timeout_seconds": timeout,
    }
    manifest["run_hash"] = digest(manifest)
    owned = client is None
    # Missing credentials are setup failure, not a successful empty run.
    client = client or JevClient.from_env()
    attempted = 0
    statuses: Counter[str] = Counter()
    try:
        root = create_directory(out)
        write_artifact(root / "run.json", manifest)
        for packet in packets:
            payload = request_for(packet)
            record = {
                "packet_hash": packet["packet_hash"],
                "run_hash": manifest["run_hash"],
                "request_hash": digest(payload),
                "status": "blocked",
                "elapsed_ms": 0,
            }
            if packet["problems"] or not packet["state"]["sources"] or not packet["state"]["claim"]:
                record["reason"] = "incomplete_evidence"
            elif attempted >= max_requests:
                record["status"] = "budget_exhausted"
            else:
                attempted += 1
                started = time.monotonic()
                try:
                    response = await client.assess(
                        payload["state"], payload["questions"], timeout_seconds=timeout
                    )
                    validate_response(response, payload["questions"], model=MODEL)
                    record.update(status="ok", response=response)
                except (httpx.HTTPError, JevUnavailable, TimeoutError, ValueError) as exc:
                    record.update(status="error", error=type(exc).__name__)
                    record["transport"] = getattr(exc, "jev_transport", None)
                record["elapsed_ms"] = (time.monotonic() - started) * 1000
            statuses[record["status"]] += 1
            write_artifact(root / f"{packet['packet_hash']}.json", record)
    finally:
        if owned:
            await client.aclose()
    completion = {"planned": len(packets), "attempted": attempted, "statuses": dict(statuses)}
    write_artifact(root / "completion.json", completion)
    return completion


def load_run(corpus: Path, run: Path) -> tuple[list[dict], dict, dict[str, dict]]:
    packets = load_packets(corpus)
    manifest = json.loads((run / "run.json").read_text())
    if (
        manifest.get("version") != VERSION
        or manifest.get("packets_hash") != digest(packets)
        or manifest.get("rubric") != rubric()
        or manifest.get("model") != f"jev-{MODEL}"
        or manifest.get("run_hash")
        != digest({k: v for k, v in manifest.items() if k != "run_hash"})
    ):
        raise ValueError("assessment identity mismatch")
    records = {}
    for packet in packets:
        key = packet["packet_hash"]
        path = run / f"{key}.json"
        if not path.exists():
            records[key] = {"status": "missing"}
            continue
        record = json.loads(path.read_text())
        if (
            record.get("packet_hash") != key
            or record.get("run_hash") != manifest["run_hash"]
            or record.get("request_hash") != digest(request_for(packet))
            or record.get("status") not in {"ok", "blocked", "budget_exhausted", "error"}
        ):
            raise ValueError("assessment record mismatch")
        if record["status"] == "ok":
            if packet["problems"] or not packet["state"]["sources"] or not packet["state"]["claim"]:
                raise ValueError("incomplete evidence cannot have a successful assessment")
            validate_response(record["response"], rubric()["questions"], model=MODEL)
        records[key] = record
    return packets, manifest, records


def make_queue(
    corpus: Path,
    run: Path,
    out: Path,
    *,
    threshold: float,
    limit: int = 20,
    audit: int = 5,
    seed: str,
) -> dict[str, Any]:
    number(threshold, 0, 1)
    if not seed or not 1 <= limit <= 1000 or not 1 <= audit <= limit:
        raise ValueError("queue needs a seed and 1 <= audit <= limit <= 1000")
    packets, manifest, records = load_run(corpus, run)
    keys = [p["packet_hash"] for p in packets]
    audit_keys = sorted(keys, key=lambda key: digest([seed, key]))[:audit]
    audit_set = set(audit_keys)
    exceptions = [
        key
        for key in keys
        if key not in audit_set and disposition(records[key], threshold) == "review"
    ]
    exceptions.sort(
        key=lambda key: (
            records[key]
            .get("response", {})
            .get("answers", {})
            .get("verdict", {})
            .get("confidence", 0),
            key,
        )
    )
    selected = [{"packet_hash": key, "stratum": "audit"} for key in audit_keys]
    selected += [
        {"packet_hash": key, "stratum": "exception"} for key in exceptions[: limit - len(selected)]
    ]
    selected.sort(key=lambda row: digest(["presentation", seed, row["packet_hash"]]))
    body = {
        "version": VERSION,
        "run_hash": manifest["run_hash"],
        "packets_hash": digest(packets),
        "threshold": threshold,
        "limit": limit,
        "audit": audit,
        "seed": seed,
        "population": len(keys),
        "rows": selected,
    }
    queue = {"queue_hash": digest(body), **body}
    write_artifact(out, queue)
    return queue


def load_queue(
    corpus: Path, run: Path, path: Path
) -> tuple[dict, dict[str, dict], dict[str, dict]]:
    packets, manifest, records = load_run(corpus, run)
    queue = json.loads(path.read_text())
    if (
        queue.get("version") != VERSION
        or queue.get("queue_hash") != digest({k: v for k, v in queue.items() if k != "queue_hash"})
        or queue.get("run_hash") != manifest["run_hash"]
        or queue.get("packets_hash") != digest(packets)
    ):
        raise ValueError("review queue identity mismatch")
    number(queue["threshold"], 0, 1)
    indexed = {p["packet_hash"]: p for p in packets}
    seen = set()
    for row in queue["rows"]:
        key = row["packet_hash"]
        if key not in indexed or key in seen or row["stratum"] not in {"audit", "exception"}:
            raise ValueError("invalid review queue member")
        seen.add(key)
    return queue, indexed, records


def complete_claim(packet: dict) -> bool:
    return bool(packet["state"]["claim"].strip()) and "truncated_claim" not in packet["problems"]


def annotate(queue: dict, packet: dict, *, reviewer: str, decision: str, notes: str = "") -> dict:
    key = packet["packet_hash"]
    if not reviewer.strip() or decision not in DECISIONS:
        raise ValueError("a named reviewer and yes/no/unsure decision are required")
    if key not in {r["packet_hash"] for r in queue["rows"]}:
        raise ValueError("annotation is outside the queue")
    if decision == "yes" and not complete_claim(packet):
        raise ValueError("yes requires a complete summary")
    return {
        "version": VERSION,
        "queue_hash": queue["queue_hash"],
        "packet_hash": key,
        "reviewer": reviewer.strip(),
        "annotator_type": "human",
        "decision": decision,
        "notes": notes,
        "reviewed_at_ms": int(time.time() * 1000),
    }


def review(corpus: Path, run: Path, queue_path: Path, out: Path, *, reviewer: str) -> int:
    if not reviewer.strip():
        raise ValueError("reviewer is required")
    queue, packets, _ = load_queue(corpus, run, queue_path)
    root = external_path(out)
    if root.exists():
        if not root.is_dir() or root.stat().st_mode & 0o077:
            raise ValueError("existing review directory must be private (0700)")
    else:
        root = create_directory(root)
    completed = load_annotations(queue, root, packets)
    count = 0
    print("Does every part of the summary follow from the displayed captured evidence?")
    print("y = yes, n = no, u = unsure, s = skip, q = quit. Predictions are hidden.")
    for index, member in enumerate(queue["rows"], 1):
        key = member["packet_hash"]
        if key in completed:
            continue
        packet = packets[key]
        print(
            f"\n{index}/{len(queue['rows'])}: {json.dumps(packet['state']['claim'], ensure_ascii=False)}"
        )
        print(json.dumps(packet["state"]["sources"], indent=2, ensure_ascii=False))
        if packet["problems"]:
            print("Evidence gaps: " + ", ".join(packet["problems"]))
        allowed = {"y", "n", "u", "s", "q"} if complete_claim(packet) else {"n", "u", "s", "q"}
        if "y" not in allowed:
            print("yes requires a complete summary; use n, u, s, or q.")
        try:
            answer = input("Supported? [y/n/u/s/q] ").strip().lower()
            while answer not in allowed:
                answer = input("Use " + "/".join(sorted(allowed)) + ": ").strip().lower()
            if answer == "q":
                break
            if answer == "s":
                continue
            notes = input("Optional note (Enter to skip): ")
        except EOFError, KeyboardInterrupt:
            break
        row = annotate(
            queue,
            packet,
            reviewer=reviewer,
            decision={"y": "yes", "n": "no", "u": "unsure"}[answer],
            notes=notes,
        )
        write_artifact(root / f"{key}.json", row)
        count += 1
    return count


def load_annotations(queue: dict, labels: Path, packets: dict[str, dict]) -> dict[str, dict]:
    members = {r["packet_hash"]: r["stratum"] for r in queue["rows"]}
    annotations = {}
    if not labels.is_dir():
        raise ValueError("labels must be a review directory")
    for path in sorted(labels.glob("*.json")):
        row = json.loads(path.read_text())
        key = row.get("packet_hash")
        if (
            row.get("version") != VERSION
            or row.get("queue_hash") != queue["queue_hash"]
            or key not in members
            or key in annotations
            or row.get("decision") not in DECISIONS
            or row.get("annotator_type") != "human"
            or not isinstance(row.get("reviewer"), str)
            or not row["reviewer"].strip()
            or not isinstance(row.get("notes"), str)
            or type(row.get("reviewed_at_ms")) is not int
            or row["reviewed_at_ms"] <= 0
        ):
            raise ValueError("invalid, duplicate, or mismatched human annotation")
        if row["decision"] == "yes" and not complete_claim(packets[key]):
            raise ValueError("yes requires a complete summary")
        annotations[key] = row
    return annotations


def report(corpus: Path, run: Path, queue_path: Path, labels: Path) -> dict[str, Any]:
    queue, packets, records = load_queue(corpus, run, queue_path)
    members = {r["packet_hash"]: r["stratum"] for r in queue["rows"]}
    annotations = load_annotations(queue, labels, packets)
    routing = {key: disposition(record, queue["threshold"]) for key, record in records.items()}
    strata = {}
    for stratum in ("audit", "exception"):
        keys = [key for key in members if members[key] == stratum]
        counts = Counter(
            {
                "selected": len(keys),
                "unreviewed": 0,
                "unsure": 0,
                "yes": 0,
                "no": 0,
                "reviewed_approvals": 0,
                "false_approvals": 0,
                "reviewed_rejections": 0,
                "false_rejections": 0,
            }
        )
        for key in keys:
            if key not in annotations:
                counts["unreviewed"] += 1
                continue
            decision = annotations[key]["decision"]
            counts[decision] += 1
            if decision == "unsure":
                continue
            if routing[key] == "approve":
                counts["reviewed_approvals"] += 1
                counts["false_approvals"] += decision == "no"
            elif routing[key] == "reject":
                counts["reviewed_rejections"] += 1
                counts["false_rejections"] += decision == "yes"
        strata[stratum] = dict(counts)
    usage = [
        record.get("response", {}).get("usage")
        for record in records.values()
        if record["status"] == "ok"
    ]
    return {
        "scope": "descriptive frozen-cohort results; not production approval or population accuracy",
        "run_hash": queue["run_hash"],
        "queue_hash": queue["queue_hash"],
        "threshold": queue["threshold"],
        "planned": len(packets),
        "statuses": dict(Counter(r["status"] for r in records.values())),
        "hypothetical_routing": dict(Counter(routing.values())),
        "automation_coverage": sum(r != "review" for r in routing.values()) / len(packets)
        if packets
        else None,
        "review": strata,
        "reported_input_tokens": sum(u["input_tokens"] for u in usage if u is not None),
        "successful_requests_missing_usage": sum(u is None for u in usage),
        "failed_or_missing_decisions_without_usage": sum(
            r["status"] in {"error", "missing"} for r in records.values()
        ),
        "acceptance_qualified": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("prepare")
    export.add_argument("--database", type=Path, required=True)
    export.add_argument("--limit", type=int, default=50)
    export.add_argument("--out", type=Path, required=True)
    for command in ("assess", "queue", "review", "report"):
        cmd = sub.add_parser(command)
        cmd.add_argument("--corpus", type=Path, required=True)
        cmd.add_argument("--run", type=Path, required=True)
        if command == "assess":
            cmd.add_argument("--max-requests", type=int, required=True)
            cmd.add_argument("--timeout", type=float, default=10)
        elif command == "queue":
            cmd.add_argument("--confidence", type=float, required=True)
            cmd.add_argument("--limit", type=int, default=20)
            cmd.add_argument("--audit", type=int, default=5)
            cmd.add_argument("--seed", required=True)
            cmd.add_argument("--out", type=Path, required=True)
        else:
            cmd.add_argument("--queue", type=Path, required=True)
            if command == "review":
                cmd.add_argument("--reviewer", required=True)
                cmd.add_argument("--out", type=Path, required=True)
            else:
                cmd.add_argument("--labels", type=Path, required=True)
                cmd.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args.database, args.out, limit=args.limit)
        elif args.command == "assess":
            result = asyncio.run(
                assess(args.corpus, args.run, max_requests=args.max_requests, timeout=args.timeout)
            )
        elif args.command == "queue":
            queue = make_queue(
                args.corpus,
                args.run,
                args.out,
                threshold=args.confidence,
                limit=args.limit,
                audit=args.audit,
                seed=args.seed,
            )
            result = {"selected": len(queue["rows"]), "queue_hash": queue["queue_hash"]}
        elif args.command == "review":
            result = {
                "reviewed": review(
                    args.corpus, args.run, args.queue, args.out, reviewer=args.reviewer
                )
            }
        else:
            result = report(args.corpus, args.run, args.queue, args.labels)
            write_artifact(args.out, result)
    except (ValueError, OSError, JevUnavailable) as exc:
        parser.exit(2, f"claim-review: {exc}\n")
    print(canonical(result))
    if args.command == "assess" and result["statuses"].get("error"):
        raise SystemExit(3)


if __name__ == "__main__":
    main()
