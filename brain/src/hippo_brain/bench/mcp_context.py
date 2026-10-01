"""Bounded context replay over real MCP stdio, with private source-bound labels."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import time
import tomllib
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from hippo_brain.bench.agent_benefit_packets import file_hash
from hippo_brain.bench.agent_benefit_study import require
from hippo_brain.bench.claim_packets import create_directory, write_artifact
from hippo_brain.decision_capture import external_path
from hippo_brain.evaluation import graded_retrieval_metrics, mrr, recall_at_k

TOOLS = {"agent_query", "search_knowledge", "search_hybrid"}


def response_hits(wire: dict) -> tuple[list[dict], dict]:
    """Accept structured objects and the SDK's legacy JSON text blocks."""
    structured = wire.get("structuredContent")
    values = [structured] if structured is not None else []
    if not values:
        for block in wire.get("content", []):
            if block.get("type") == "text":
                try:
                    values.append(json.loads(block["text"]))
                except ValueError:
                    continue
    hits, metadata = [], {}
    for value in values:
        if isinstance(value, dict):
            metadata.update({key: value[key] for key in ("error", "retrieval") if key in value})
            value = value.get("hits", value.get("result", value))
        if isinstance(value, dict):
            value = [value]
        if isinstance(value, list):
            hits.extend(hit for hit in value if isinstance(hit, dict) and hit.get("uuid"))
    return hits, metadata


def score_response(case: dict, wire: dict, elapsed_ms: float) -> dict:
    hits, metadata = response_hits(wire)
    ids = [hit["uuid"] for hit in hits]
    judgments = case["judgments"]
    known = {uid for uid, grade in judgments.items() if grade >= 2}
    facts = {
        fact["id"]: any(
            hit["uuid"] in fact["node_ids"]
            and any(text.casefold() in json.dumps(hit).casefold() for text in fact["any_text"])
            for hit in hits
        )
        for fact in case.get("facts", [])
    }
    refs = [
        evidence["ref"]
        for hit in hits
        for evidence in hit.get("evidence", [])
        if isinstance(evidence, dict) and evidence.get("ref")
    ]
    return {
        "id": case["id"],
        "family": case["family"],
        "tool": case["tool"],
        "success": not wire.get("isError", False) and "error" not in metadata,
        "elapsed_ms": round(elapsed_ms, 3),
        "returned_ids": ids,
        "known_support_recall": recall_at_k(ids, known, len(ids)) if known else None,
        "known_support_mrr": mrr(ids, known) if known else None,
        "graded": graded_retrieval_metrics(ids, judgments),
        "literal_fact_availability": facts,
        "result_bytes": len(json.dumps(wire, ensure_ascii=False).encode()),
        "text_chars": sum(len(block.get("text", "")) for block in wire.get("content", [])),
        "structured": wire.get("structuredContent") is not None,
        "duplicate_source_fraction": 1 - len(set(refs)) / len(refs) if refs else None,
        **metadata,
    }


def load_inputs(config_path: Path, cases_path: Path, max_calls: int, repeats: int) -> tuple:
    config_path, cases_path = external_path(config_path), external_path(cases_path)
    manifest = json.loads(cases_path.read_text())
    source_audit = external_path(Path(manifest["source_audit"]))
    require(file_hash(source_audit) == manifest["source_audit_sha256"], "changed source audit")
    server = tomllib.loads(config_path.read_text())["mcp_servers"]["hippo"]
    home = Path(server["env"]["HOME"])
    brain_config = tomllib.loads((home / ".config/hippo/config.toml").read_text())
    database = external_path(Path(brain_config["storage"]["data_dir"]) / "hippo.db")
    require(file_hash(database) == manifest["snapshot_sha256"], "changed server snapshot")
    cases = manifest["cases"]
    require(1 <= repeats <= 10 and 1 <= len(cases) * repeats <= max_calls <= 100, "call budget")
    require(len({case["id"] for case in cases}) == len(cases), "duplicate case IDs")
    for case in cases:
        require(bool(case["id"]) and bool(case["family"]), "missing case identity")
        require(case["tool"] in TOOLS and isinstance(case["arguments"], dict), "context tool")
        graded_retrieval_metrics([], case["judgments"])
        require(
            len({fact["id"] for fact in case.get("facts", [])}) == len(case.get("facts", [])),
            "duplicate fact IDs",
        )
        for fact in case.get("facts", []):
            require(
                bool(fact["node_ids"])
                and all(case["judgments"].get(uid, 0) >= 2 for uid in fact["node_ids"])
                and bool(fact["any_text"])
                and all(isinstance(text, str) and text.strip() for text in fact["any_text"]),
                "fact requires audited support nodes and literal phrases",
            )
    return manifest, server, database


async def replay(
    config_path: Path,
    cases_path: Path,
    output: Path,
    *,
    max_calls: int,
    repeats: int,
    timeout_seconds: float,
) -> dict:
    require(1 <= timeout_seconds <= 120, "timeout must be 1..120 seconds")
    manifest, server, database = load_inputs(config_path, cases_path, max_calls, repeats)
    output = create_directory(output)
    write_artifact(
        output / "inputs.json",
        {
            "cases_sha256": file_hash(cases_path),
            "server_config_sha256": file_hash(config_path),
            "snapshot_sha256": file_hash(database),
            "source_audit_sha256": manifest["source_audit_sha256"],
            "max_calls": max_calls,
            "repeats": repeats,
            "timeout_seconds": timeout_seconds,
            "role": "diagnostic_context_replay",
        },
    )
    params = StdioServerParameters(
        command=server["command"],
        args=server.get("args", []),
        env=server.get("env"),
        cwd=server.get("cwd"),
    )
    rows = []
    started = time.monotonic()
    with (output / "server.stderr.log").open("x") as errlog:
        os.chmod(errlog.name, 0o600)
        async with stdio_client(params, errlog=errlog) as (reader, writer):
            async with ClientSession(
                reader, writer, read_timeout_seconds=timedelta(seconds=timeout_seconds)
            ) as session:
                await session.initialize()
                inventory = await session.list_tools()
                require(inventory.nextCursor is None, "incomplete tool inventory")
                write_artifact(output / "inventory.json", inventory.model_dump(mode="json"))
                require(TOOLS <= {tool.name for tool in inventory.tools}, "missing context tools")
                startup_ms = (time.monotonic() - started) * 1000
                for repeat in range(repeats):
                    for position, case in enumerate(manifest["cases"]):
                        call_started = time.monotonic()
                        try:
                            result = await session.call_tool(case["tool"], case["arguments"])
                            wire = result.model_dump(mode="json")
                        except Exception as error:
                            wire = {
                                "isError": True,
                                "content": [],
                                "error_type": type(error).__name__,
                            }
                        elapsed_ms = (time.monotonic() - call_started) * 1000
                        write_artifact(output / f"call-{repeat}-{position}.json", wire)
                        row = score_response(case, wire, elapsed_ms)
                        row["repeat"] = repeat
                        rows.append(row)
                        write_artifact(output / f"score-{repeat}-{position}.json", row)
    elapsed = sorted(row["elapsed_ms"] for row in rows)
    report = {
        "role": "diagnostic_context_replay",
        "rows": rows,
        "case_count": len(manifest["cases"]),
        "family_count": len({row["family"] for row in rows}),
        "startup_ms": round(startup_ms, 3),
        "call_count": len(rows),
        "call_successes": sum(row["success"] for row in rows),
        "latency_p50_ms": statistics.median(elapsed),
        "latency_p95_ms": elapsed[math.ceil(len(elapsed) * 0.95) - 1],
    }
    write_artifact(output / "report.json", report)
    return {key: value for key, value in report.items() if key != "rows"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-config", type=Path, required=True)
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--max-calls", type=int, default=20)
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--timeout-seconds", type=float, default=90)
    args = parser.parse_args()
    os.umask(0o077)
    print(
        json.dumps(
            asyncio.run(
                replay(
                    args.server_config,
                    args.cases,
                    args.out,
                    max_calls=args.max_calls,
                    repeats=args.repeats,
                    timeout_seconds=args.timeout_seconds,
                )
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
