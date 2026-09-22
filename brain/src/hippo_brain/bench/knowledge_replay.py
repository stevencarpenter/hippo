"""Checkpointed fixed-pool replay through the production ranking contract.

Invoked by ``hippo-decision-bench replay``. All generated records stay external;
candidate generation and annotation remain separate existing harness stages.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import math
import os
import resource
import sqlite3
import sys
import time
from collections.abc import Iterator
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

import httpx

from hippo_brain.decision_capture import external_path
from hippo_brain.jev import MODEL, JevClient, canonical, digest, redact_state
from hippo_brain.rerank import build_rerank_messages, rerank_results
from hippo_brain.retrieval import Filters, SearchResult, _apply_filters, _fetch_details, _to_result

ARMS = (
    "retrieval",
    "rules",
    "jev-single",
    "jev-multi",
    "jev-adaptive",
    "local-original",
    "local-optimized",
    "local-cached",
)


def _read(path: Path) -> list[dict]:
    text = path.read_text()
    rows = (
        [json.loads(line) for line in text.splitlines() if line.strip()]
        if path.suffix == ".jsonl"
        else json.loads(text)
    )
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("expected JSON or JSONL records")
    return rows


def _hash_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _write(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(canonical(value) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def _checkpoints(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = path.read_bytes()
    if data and not data.endswith(b"\n"):
        boundary = data.rfind(b"\n") + 1
        tail = data[boundary:]
        archived = path.with_name("interrupted-tail-" + hashlib.sha256(tail).hexdigest() + ".bin")
        fd = os.open(archived, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(tail)
        with path.open("r+b") as stream:
            stream.truncate(boundary)
    return _read(path)


def _account_usage(usage: dict | None, reservation: int) -> int:
    if isinstance(usage, dict):
        for pair in (("input_tokens", "output_tokens"), ("prompt_tokens", "completion_tokens")):
            if all(type(usage.get(key)) is int and usage[key] >= 0 for key in pair):
                return sum(usage[key] for key in pair)
    return reservation


def _job_batches(
    cases: list[dict], arms: list[str], repeats: int, concurrency: int
) -> Iterator[list[tuple[dict, str, int]]]:
    for repeat in range(repeats):
        if concurrency == 1:
            for position, case in enumerate(cases):
                offset = (position + repeat) % len(arms)
                for arm in arms[offset:] + arms[:offset]:
                    yield [(case, arm, repeat)]
        else:
            # Load experiments isolate each arm, including each local comparator.
            # No heavyweight local request can contend with Jev timing.
            offset = repeat % len(arms)
            for arm in arms[offset:] + arms[:offset]:
                for start in range(0, len(cases), concurrency):
                    yield [(case, arm, repeat) for case in cases[start : start + concurrency]]


def _reservation(case: dict, arm: str, repeat: int, config: dict) -> dict:
    calls = (
        3
        if arm == "jev-adaptive"
        else int(arm.startswith("jev-") or arm in ("local-original", "local-optimized"))
    )
    record = {
        "query_id": case["id"],
        "arm": arm,
        "repeat": repeat,
        "input_hash": case["input_hash"],
        "state_hash": digest(case["candidates"]),
        "order": [candidate["uuid"] for candidate in case["candidates"]],
        "status": "cancelled",
        "model": MODEL if arm.startswith("jev-") else config["local_model"] or "deterministic",
        "recipe_hash": digest({"arm": arm, "implementation": config["implementation_hashes"]}),
        "network_calls": calls,
        "network_calls_estimated": True,
        "accounted_tokens": calls * 160_000,
        "reserved_tokens": calls * 160_000,
        "usage": None,
        "usage_missing_reason": "interrupted_request",
        "elapsed_ms": None,
        "timing_origin": "interrupted_unmeasured",
        "filter_violations": None,
        "malformed_accepted": None,
        "cached": False,
    }
    record["record_hash"] = digest(record)
    return record


def load_inputs(
    path: Path,
    *,
    targets: Path | None = None,
    questions: Path | None = None,
    conn: sqlite3.Connection | None = None,
) -> list[dict]:
    """Freeze full result objects without exposing labels to the runtime model."""
    target_map = {row["qa_id"]: row for row in _read(targets)} if targets else {}
    query_map = {row["id"]: row for row in _read(questions)} if questions else {}
    known = set()
    records = []
    allowed = {field.name for field in fields(SearchResult)}
    for case in _read(path):
        identity = case.get("id")
        if not isinstance(identity, str) or not identity or identity in known:
            raise ValueError("case IDs must be nonempty and unique")
        known.add(identity)
        state = case.get("state", case)
        query = state.get("query", state.get("question"))
        candidates = state.get("candidates")
        if (
            not isinstance(query, str)
            or not query.strip()
            or not isinstance(candidates, list)
            or len(candidates) > 30
        ):
            raise ValueError("replay requires a query and a frozen pool of at most 30 candidates")
        target = target_map.get(identity)
        if targets and (target is None or target.get("candidate_hash") != digest(case)):
            raise ValueError("archived candidate hash does not match targets")
        if questions and (identity not in query_map or query_map[identity]["question"] != query):
            raise ValueError("case does not match frozen corpus question")
        uuids = (
            target["candidate_uuids"]
            if target
            else [row.get("uuid", row.get("id")) for row in candidates]
        )
        if (
            len(uuids) != len(candidates)
            or any(not isinstance(uid, str) or not uid for uid in uuids)
            or len(set(uuids)) != len(uuids)
        ):
            raise ValueError("candidate UUIDs must be nonempty, unique, and complete")
        details = {}
        if conn is not None and uuids:
            rows = conn.execute(
                f"SELECT id, uuid FROM knowledge_nodes WHERE uuid IN ({','.join('?' for _ in uuids)})",
                uuids,
            ).fetchall()
            if len(rows) != len(uuids):
                raise ValueError("frozen database is missing a candidate UUID")
            hydrated = _fetch_details(conn, [row[0] for row in rows])
            details = {row[1]: asdict(_to_result(0.0, hydrated[row[0]])) for row in rows}
        normalized = []
        for uid, candidate in zip(uuids, candidates, strict=True):
            if not isinstance(candidate, dict):
                raise ValueError("candidate must be an object")
            base = {
                "uuid": uid,
                "score": 0.0,
                "summary": "",
                "embed_text": "",
                "outcome": None,
                "tags": [],
                "cwd": "",
                "git_branch": "",
                "captured_at": 0,
                **details.get(uid, {}),
            }
            base.update({key: value for key, value in candidate.items() if key in allowed})
            base["uuid"] = uid
            if any(
                not isinstance(base.get(key, ""), str)
                for key in ("summary", "embed_text", "commands_raw")
            ):
                raise ValueError("candidate text must be strings")
            normalized.append(asdict(SearchResult(**base)))
        records.append(
            {
                "id": identity,
                "query": redact_state(query),
                "candidates": redact_state(normalized),
                "input_hash": digest(query_map[identity]) if questions else digest(case),
                "source_case_hash": digest(case),
                "filters": query_map.get(identity, {}).get(
                    "filters",
                    {"source": target.get("source_filter")} if target else case.get("filters", {}),
                ),
                "as_of_ms": query_map.get(identity, {}).get("as_of_ms"),
            }
        )
    if not records:
        raise ValueError("empty replay corpus")
    return records


class _LocalReplay:
    def __init__(self, client: httpx.AsyncClient, url: str, tokens: int) -> None:
        self.client, self.url, self.tokens = client, url, tokens
        self.response: dict | None = None
        self.calls = 0

    async def chat(self, messages: list[dict], model: str) -> str:
        self.calls += 1
        reply = await self.client.post(
            self.url.rstrip("/") + "/chat/completions",
            json={
                "model": model,
                "messages": messages,
                "temperature": 0.0,
                "max_tokens": self.tokens,
            },
        )
        reply.raise_for_status()
        self.response = reply.json()
        return self.response["choices"][0]["message"]["content"]


def _cached_local(path: Path | None, cases: list[dict]) -> dict[str, dict]:
    if path is None:
        return {}
    rows = {
        row["case_id"]: row
        for row in _read(path)
        if row.get("repeat") == 0 and row.get("task") == "ranking"
    }
    for case in cases:
        row = rows.get(case["id"])
        results = [SearchResult(**candidate) for candidate in case["candidates"]]
        if (
            row is None
            or row.get("status") != "ok"
            or row.get("request", {}).get("messages")
            != build_rerank_messages(case["query"], results)
        ):
            raise ValueError("cached local response does not match the frozen ranking request")
        order = row.get("canonical_order")
        if (
            not isinstance(order, list)
            or any(type(i) is not int for i in order)
            or sorted(order) != list(range(len(results)))
        ):
            raise ValueError("cached local ranking is not a complete permutation")
    return rows


async def _decision(
    case: dict,
    arm: str,
    *,
    client: JevClient | None,
    conn: sqlite3.Connection | None,
    http: httpx.AsyncClient,
    config: dict,
    cached: dict[str, dict],
) -> dict:
    results = [SearchResult(**candidate) for candidate in case["candidates"]]
    trace: dict = {}
    recipe = "single-v1" if arm == "jev-single" else "multi-v1"
    row = {
        "query_id": case["id"],
        "arm": arm,
        "input_hash": case["input_hash"],
        "state_hash": digest(case["candidates"]),
        "filter_violations": None,
        "malformed_accepted": None,
        "status": "ok",
        "network_calls": 0,
        "recipe_hash": digest(
            {
                "arm": arm,
                "recipe": recipe,
                "implementation": config["implementation_hashes"],
                "deadline_ms": config["deadline_ms"],
            }
        ),
        "usage": None,
        "usage_missing_reason": "no_inference",
        "cached": False,
    }
    started = time.monotonic()
    if arm == "local-cached":
        previous = cached[case["id"]]
        output = [results[i] for i in previous["canonical_order"]]
        row.update(
            {
                "model": previous["resolved_model"],
                "elapsed_ms": previous["elapsed_ms"],
                "cached": True,
                "cached_record_hash": digest(previous),
                "usage": previous.get("usage"),
                "usage_missing_reason": "historical_accounting",
                "timing_origin": "historical_cached_run",
            }
        )
    elif arm.startswith("local-"):
        adapter = _LocalReplay(
            http, config["local_url"], 512 if arm == "local-optimized" else 16384
        )
        try:
            async with asyncio.timeout(config["local_timeout"]):
                output = await rerank_results(
                    adapter, config["local_model"], case["query"], results, len(results)
                )
            from hippo_brain.rerank import parse_ranking

            if (
                adapter.response is None
                or parse_ranking(adapter.response["choices"][0]["message"]["content"], len(results))
                is None
            ):
                row["status"] = "fallback"
        except TimeoutError:
            row["status"], output = "fallback", results
        row.update(
            {
                "model": (adapter.response or {}).get("model", config["local_model"]),
                "network_calls": adapter.calls,
                "usage": (adapter.response or {}).get("usage"),
                "usage_missing_reason": None
                if (adapter.response or {}).get("usage")
                else "provider_omitted_or_failed",
            }
        )
    elif arm == "retrieval":
        output = results
        row["model"] = "deterministic"
    else:
        output = await rerank_results(
            None,
            "",
            case["query"],
            results,
            len(results),
            backend="rules" if arm == "rules" else "jev",
            adaptive=arm == "jev-adaptive",
            deadline_ms=config["deadline_ms"],
            conn=conn,
            jev_client=client,
            diagnostics=trace,
            recipe=recipe,
            effective_filters=Filters(**case["filters"]),
        )
        if trace.get("fallback_reason"):
            row["status"] = "fallback"
        row.update(
            {
                "model": "deterministic" if arm == "rules" else MODEL,
                "network_calls": sum(
                    (p.get("transport") or {}).get("network_calls", 0)
                    for p in trace.get("passes", [])
                ),
                "diagnostics": trace,
            }
        )
        usages = [p.get("usage") for p in trace.get("passes", [])]
        if usages and all(usage is not None for usage in usages):
            row["usage"] = {
                key: sum(usage[key] for usage in usages)
                for key in ("input_tokens", "output_tokens")
            }
            row["usage_missing_reason"] = None
        elif usages:
            row["usage_missing_reason"] = "one_or_more_attempts_unknown"
    row["order"] = [result.uuid for result in output]
    if sorted(row["order"]) != sorted(result.uuid for result in results):
        raise ValueError("runtime violated candidate permutation contract")
    row["malformed_accepted"] = 0
    if conn is not None:
        nodes = conn.execute(
            f"SELECT id FROM knowledge_nodes WHERE uuid IN ({','.join('?' for _ in output)})",
            row["order"],
        ).fetchall()
        allowed = _apply_filters(conn, [node[0] for node in nodes], Filters(**case["filters"]))
        row["filter_violations"] = len(output) - len(allowed)
    row.setdefault("elapsed_ms", (time.monotonic() - started) * 1000)
    row.setdefault("timing_origin", "fresh_wall_clock")
    return row


async def replay(args: argparse.Namespace) -> dict:
    """Run rotated single queries or isolated load batches with durable budgets."""
    out = external_path(args.out)
    concurrency = getattr(args, "concurrency", 1)
    if concurrency not in (1, 4):
        raise ValueError("replay concurrency must be one or four")
    arms = args.arms.split(",")
    if not arms or len(set(arms)) != len(arms) or set(arms) - set(ARMS):
        raise ValueError("invalid or repeated replay arms")
    if (
        not 1 <= args.repeats <= 100
        or not 1 <= args.deadline_ms <= 2000
        or args.max_requests < 0
        or args.max_tokens < 0
    ):
        raise ValueError("invalid replay budget")
    if not math.isfinite(args.local_timeout) or not 0 < args.local_timeout <= 3600:
        raise ValueError("invalid local timeout")
    if any(arm in arms for arm in ("local-original", "local-optimized")) and (
        not args.local_url or not args.local_model
    ):
        raise ValueError("local arms require --local-url and --local-model")
    if "local-cached" in arms and args.cached_local is None:
        raise ValueError("local-cached requires --cached-local")
    if "jev-adaptive" in arms and args.db is None:
        raise ValueError("adaptive replay requires a frozen --db")
    db = external_path(args.db) if args.db else None
    if db and Path(str(db) + "-wal").exists() and Path(str(db) + "-wal").stat().st_size:
        raise ValueError("checkpoint the external snapshot WAL before replay")
    conn = sqlite3.connect(db.as_uri() + "?mode=ro", uri=True) if db else None
    client = None
    try:
        if conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
        cases = load_inputs(args.cases, targets=args.targets, questions=args.questions, conn=conn)
        cached = _cached_local(args.cached_local, cases)
        config = {
            "schema_version": 1,
            "arms": arms,
            "repeats": args.repeats,
            "corpus_hash": digest(cases),
            "case_count": len(cases),
            "database_hash": _hash_file(db) if db else None,
            "cached_local_hash": _hash_file(args.cached_local) if args.cached_local else None,
            "deadline_ms": args.deadline_ms,
            "max_requests": args.max_requests,
            "max_tokens": args.max_tokens,
            "local_model": args.local_model,
            "local_url": args.local_url,
            "local_timeout": args.local_timeout,
            "concurrency": concurrency,
            "implementation_hashes": {
                name: _hash_file(Path(__file__).parents[1] / name)
                for name in (
                    "jev.py",
                    "rerank.py",
                    "decision_rules.py",
                    "retrieval.py",
                    "redaction.py",
                    "evidence_packets.py",
                    "source_filters.py",
                    "retrieval_eligibility.py",
                    "bench/knowledge_replay.py",
                )
            },
            "transport_policy": "one_attempt_no_retries_sequential_rotated_arms"
            if concurrency == 1
            else "one_attempt_no_retries_isolated_arm_load_batches",
        }
        if out.exists() and not args.resume:
            raise ValueError("output exists; use --resume for an unchanged run")
        out.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(child.is_symlink() for child in out.iterdir()):
            raise ValueError("replay output may not contain symlinks")
        with (out / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            invocation_started = time.monotonic()
            invocation_cpu = time.process_time()
            manifest = out / "manifest.json"
            if manifest.exists():
                if json.loads(manifest.read_text()) != config:
                    raise ValueError("replay inputs, implementation, or budgets changed")
                for name, expected in config["implementation_hashes"].items():
                    source = out / "source" / name
                    if (
                        not source.is_file()
                        or source.resolve() != source.absolute()
                        or _hash_file(source) != expected
                    ):
                        raise ValueError("replay source snapshot changed")
            else:
                for name, expected in config["implementation_hashes"].items():
                    body = (Path(__file__).parents[1] / name).read_bytes()
                    if hashlib.sha256(body).hexdigest() != expected:
                        raise ValueError("implementation changed while freezing replay")
                    source = out / "source" / name
                    source.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    fd = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(body)
                _write(manifest, config)
                _write(out / "inputs.json", cases)
            rows = _checkpoints(out / "results.jsonl")
            keys = {(row["query_id"], row["arm"], row["repeat"]) for row in rows}
            if len(keys) != len(rows):
                raise ValueError("duplicate checkpoint rows")
            pending = out / "inflight.json"
            if pending.exists():
                abandoned_rows = json.loads(pending.read_text())
                if isinstance(abandoned_rows, dict):
                    abandoned_rows = [abandoned_rows]
                for abandoned in abandoned_rows:
                    key = (abandoned["query_id"], abandoned["arm"], abandoned["repeat"])
                    if key not in keys:
                        fd = os.open(
                            out / "results.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
                        )
                        with os.fdopen(fd, "a") as stream:
                            stream.write(canonical(abandoned) + "\n")
                            stream.flush()
                            os.fsync(stream.fileno())
                        rows.append(abandoned)
                        keys.add(key)
                pending.unlink()
            by_id = {case["id"]: case for case in cases}
            for row in rows:
                case = by_id.get(row["query_id"])
                if (
                    case is None
                    or row["arm"] not in arms
                    or type(row["repeat"]) is not int
                    or not 0 <= row["repeat"] < args.repeats
                    or row["input_hash"] != case["input_hash"]
                    or sorted(row["order"]) != sorted(c["uuid"] for c in case["candidates"])
                    or row.get("record_hash")
                    != digest({k: v for k, v in row.items() if k != "record_hash"})
                ):
                    raise ValueError("checkpoint record identity or hash changed")
            used_calls = sum(row["network_calls"] for row in rows)
            used_tokens = sum(row["accounted_tokens"] for row in rows)
            if any(arm.startswith("jev-") for arm in arms):
                client = JevClient.from_env()
            stopped = None
            fd = os.open(out / "results.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            async with httpx.AsyncClient(
                timeout=args.local_timeout, follow_redirects=False, trust_env=False
            ) as http:
                with os.fdopen(fd, "a") as stream:

                    async def execute(case, arm, repeat, reservation):
                        row = await _decision(
                            case,
                            arm,
                            client=client,
                            conn=conn,
                            http=http,
                            config=config,
                            cached=cached,
                        )
                        row.update(
                            {
                                "repeat": repeat,
                                "reserved_tokens": reservation["reserved_tokens"],
                                "accounted_tokens": _account_usage(
                                    row["usage"], reservation["reserved_tokens"]
                                )
                                if not row["cached"]
                                else 0,
                                "load_concurrency": concurrency,
                                "batch_size": len(jobs),
                            }
                        )
                        row["record_hash"] = digest(row)
                        return row

                    for batch in _job_batches(cases, arms, args.repeats, concurrency):
                        jobs, reservations = [], []
                        for case, arm, repeat in batch:
                            if (case["id"], arm, repeat) in keys:
                                continue
                            reservation = _reservation(case, arm, repeat, config)
                            calls = (
                                sum(r["network_calls"] for r in reservations)
                                + reservation["network_calls"]
                            )
                            tokens = (
                                sum(r["reserved_tokens"] for r in reservations)
                                + reservation["reserved_tokens"]
                            )
                            if (
                                used_calls + calls > args.max_requests
                                or used_tokens + tokens > args.max_tokens
                            ):
                                stopped = "request_or_token_budget"
                                break
                            jobs.append((case, arm, repeat, reservation))
                            reservations.append(reservation)
                        if jobs:
                            # Reserve the whole batch before scheduling any network operation.
                            _write(pending, reservations)
                            tasks = [asyncio.create_task(execute(*job)) for job in jobs]
                            try:
                                for finished in asyncio.as_completed(tasks):
                                    row = await finished
                                    stream.write(canonical(row) + "\n")
                                    stream.flush()
                                    os.fsync(stream.fileno())
                                    rows.append(row)
                                    key = (row["query_id"], row["arm"], row["repeat"])
                                    keys.add(key)
                                    used_calls += row["network_calls"]
                                    used_tokens += row["accounted_tokens"]
                                    reservations = [
                                        r
                                        for r in reservations
                                        if (r["query_id"], r["arm"], r["repeat"]) != key
                                    ]
                                    if reservations:
                                        _write(pending, reservations)
                                    else:
                                        pending.unlink()
                            finally:
                                for task in tasks:
                                    if not task.done():
                                        task.cancel()
                                await asyncio.gather(*tasks, return_exceptions=True)
                        if stopped:
                            break
            expected = len(cases) * len(arms) * args.repeats
            summary = {
                "expected_rows": expected,
                "observed_rows": len(rows),
                "complete": len(rows) == expected
                and all(row["status"] != "cancelled" for row in rows),
                "stop_reason": stopped or "complete",
                "network_calls": used_calls,
                "accounted_tokens": used_tokens,
                "fallbacks": sum(row["status"] != "ok" for row in rows),
                "manifest_hash": digest(config),
                "results_hash": _hash_file(out / "results.jsonl"),
            }
            _write(out / "completion.json", summary)
            # The graded report consumes one quality observation per query/arm.
            _write(out / "rankings.json", [row for row in rows if row["repeat"] == 0])
            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            sample = {
                "recorded_at_ms": time.time_ns() // 1_000_000,
                "client_wall_ms": (time.monotonic() - invocation_started) * 1000,
                "client_cpu_ms": (time.process_time() - invocation_cpu) * 1000,
                "client_peak_rss_bytes": peak if sys.platform == "darwin" else peak * 1024,
                "scope": "this invocation, including checkpoint IO; excludes input preparation and provider compute; RSS is process lifetime peak",
                "concurrency": concurrency,
                "resumed": args.resume,
            }
            fd = os.open(out / "resources.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as stream:
                stream.write(canonical(sample) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            return summary
    finally:
        if client is not None:
            await client.aclose()
        if conn is not None:
            conn.close()


def add_parser(commands: Any) -> None:
    parser = commands.add_parser("replay", help="Replay frozen pools through production ranking")
    parser.add_argument("--cases", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--targets", type=Path)
    parser.add_argument("--questions", type=Path)
    parser.add_argument("--db", type=Path)
    parser.add_argument("--cached-local", type=Path)
    parser.add_argument("--arms", default="retrieval,rules,jev-single,jev-multi,jev-adaptive")
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--concurrency", type=int, choices=(1, 4), default=1)
    parser.add_argument("--deadline-ms", type=int, default=2000)
    parser.add_argument("--max-requests", type=int, default=300)
    parser.add_argument("--max-tokens", type=int, default=50_000_000)
    parser.add_argument("--local-url", default="")
    parser.add_argument("--local-model", default="")
    parser.add_argument("--local-timeout", type=float, default=180)
    parser.add_argument("--resume", action="store_true")
