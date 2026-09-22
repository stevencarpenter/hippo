"""Opt-in, bounded input capture. Never invokes inference or opens Hippo's DB."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import time
import tomllib
from dataclasses import asdict, is_dataclass
from pathlib import Path

from hippo_brain.redaction import redact

logger = logging.getLogger(__name__)


def external_path(path: Path | str) -> Path:
    """Resolve an experiment output path and reject repository/production trees."""
    xdg = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    root = Path(path).expanduser().resolve()
    for parent in (*Path(__file__).resolve().parents, Path.cwd(), *Path.cwd().parents):
        if (parent / ".git").exists() and (root == parent or parent in root.parents):
            raise ValueError("decision artifacts must be outside the repository")
    production = [xdg / "hippo", Path.home() / ".local/share/hippo"]
    config_path = Path.home() / ".config/hippo/config.toml"
    if config_path.exists():
        with config_path.open("rb") as stream:
            configured = tomllib.load(stream).get("storage", {}).get("data_dir")
        if configured:
            production.append(Path(configured).expanduser())
    for prod in (p.resolve() for p in production):
        if root == prod or prod in root.parents:
            raise ValueError("decision artifacts must be outside production data")
    return root


def decision_root() -> Path:
    xdg = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return external_path(xdg / "hippo-bench" / "decisions")


def _write_capture(kind: str, record: dict, *, identity: str | None = None) -> None:
    body = (json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n").encode()
    if len(body) > 128_000:
        return
    root = decision_root()
    directory = (root / kind).resolve()
    if directory.parent != root:
        raise ValueError("capture directory must remain inside decision artifacts")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # ponytail: bounded scan of 1000 records; use a spool if capture volume demands it.
    if sum(1 for _ in directory.iterdir()) >= 1000:
        return
    name = identity or hashlib.sha256(body).hexdigest()
    try:
        fd = os.open(directory / f"{name}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    with os.fdopen(fd, "wb") as stream:
        stream.write(body)


def _redacted(value):
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(k): _redacted(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redacted(v) for v in value]
    return value


def capture_query(question: str, results: list, *, filters=None, origin="interactive") -> None:
    """Capture ordinary queries independently of reranking; never fail a query."""
    if os.environ.get("HIPPO_QUERY_CAPTURE") != "1" or origin not in ("interactive", "http", "mcp"):
        return
    try:
        if not isinstance(question, str) or not question.strip() or len(question) > 16_000:
            return
        candidates = []
        for result in results[:60]:
            row = asdict(result) if is_dataclass(result) else result
            if not isinstance(row, dict):
                continue
            candidates.append(
                {
                    "uuid": row.get("uuid", ""),
                    **{
                        key: redact(str(row.get(key, "")))[:1200]
                        for key in ("summary", "embed_text", "commands_raw")
                    },
                    "captured_at": row.get("captured_at"),
                    "linked_source_ids": row.get("linked_source_ids", []),
                }
            )
        state = _redacted(
            {
                "question": question,
                "filters": asdict(filters) if is_dataclass(filters) else filters,
                "candidates": candidates,
            }
        )
        identity = hashlib.sha256(
            json.dumps(state, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
        _write_capture(
            "query-captures",
            {
                "schema_version": 1,
                "id": identity,
                "input_hash": identity,
                "origin": origin,
                "captured_at_ms": int(time.time() * 1000),
                **state,
            },
            identity=identity,
        )
    except Exception as exc:
        logger.warning("query capture failed (%s)", type(exc).__name__)


def capture_decision(diagnostics: dict, *, origin="interactive") -> None:
    if os.environ.get("HIPPO_DECISION_CAPTURE") != "1" or origin not in (
        "interactive",
        "http",
        "mcp",
    ):
        return
    try:
        _write_capture(
            "decision-traces",
            _redacted(
                {
                    "schema_version": 1,
                    "captured_at_ms": int(time.time() * 1000),
                    "origin": origin,
                    **diagnostics,
                }
            ),
        )
    except Exception as exc:
        logger.warning("decision trace capture failed (%s)", type(exc).__name__)


def capture_rerank(question: str, results: list) -> None:
    if os.environ.get("HIPPO_DECISION_CAPTURE") != "1":
        return
    try:
        from hippo_brain.rerank import _clip

        if not 2 <= len(results) <= 100 or len(question) > 16_000:
            return
        state = {
            "query": question,
            "candidates": [
                {
                    key: _clip(redact(getattr(result, key)), 300)
                    or (" " if getattr(result, key) else "")
                    for key in ("summary", "embed_text", "commands_raw")
                }
                for result in results
            ],
        }
        state = _redacted(state)
        serialized = json.dumps(state, sort_keys=True, ensure_ascii=False)
        case_id = hashlib.sha256(serialized.encode()).hexdigest()
        case = {"id": case_id, "task": "ranking", "state": state}
        _write_capture("captures", case, identity=case_id)
    except Exception as exc:
        # Capture is advisory. Even a bad path or full disk cannot fail a query.
        logger.warning("decision capture failed (%s)", type(exc).__name__)
