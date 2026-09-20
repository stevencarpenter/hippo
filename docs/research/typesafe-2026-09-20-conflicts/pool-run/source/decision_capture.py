"""Opt-in, bounded input capture. Never invokes inference or opens Hippo's DB."""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def decision_root() -> Path:
    xdg = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    root = (xdg / "hippo-bench" / "decisions").resolve()
    for prod in ((xdg / "hippo").resolve(), (Path.home() / ".local/share/hippo").resolve()):
        if root == prod or prod in root.parents:
            raise ValueError("decision artifacts must be outside production data")
    return root


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
                    key: _clip(getattr(result, key), 300) or (" " if getattr(result, key) else "")
                    for key in ("summary", "embed_text", "commands_raw")
                }
                for result in results
            ],
        }
        serialized = json.dumps(state, sort_keys=True, ensure_ascii=False)
        case_id = hashlib.sha256(serialized.encode()).hexdigest()
        case = {"id": case_id, "task": "ranking", "state": state}
        body = (json.dumps(case, ensure_ascii=False) + "\n").encode()
        if len(body) > 128_000:
            return
        root = decision_root()
        directory = (root / "captures").resolve()
        if directory.parent != root:
            raise ValueError("capture directory must remain inside decision artifacts")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        # ponytail: bounded directory scan, at most 1000 captures; use a spool
        # service if sustained capture throughput becomes material.
        if sum(1 for _ in directory.iterdir()) >= 1000:
            return
        try:
            fd = os.open(directory / f"{case_id}.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return
        with os.fdopen(fd, "wb") as stream:
            stream.write(body)
    except Exception as exc:
        # Capture is advisory. Even a bad path or full disk cannot fail a query.
        logger.warning("decision capture failed (%s)", type(exc).__name__)
