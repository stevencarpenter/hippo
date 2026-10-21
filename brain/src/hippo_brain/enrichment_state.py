"""Reader for the operator enrichment pause state file (written by the Rust CLI)."""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

STATE_FILE = "enrichment-state.json"

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StateRead:
    paused: bool
    changed_at_ms: int | None
    valid: bool


def _reject_duplicates(pairs):
    # Rust serde rejects duplicate keys; Python keeps the last by default.
    keys = [k for k, _ in pairs]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate key")
    return dict(pairs)


def read_state(data_dir: str | Path) -> StateRead:
    """Never raises. Missing file means running; invalid content fails closed (paused)."""
    path = Path(data_dir) / STATE_FILE
    invalid = StateRead(paused=True, changed_at_ms=None, valid=False)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return StateRead(paused=False, changed_at_ms=None, valid=True)
    except (OSError, ValueError) as e:  # ValueError covers UnicodeDecodeError
        logger.warning("enrichment state unreadable at %s: %s", path, e)
        return invalid
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicates)
    except ValueError as e:
        logger.warning("enrichment state invalid at %s: %s", path, e)
        return invalid
    paused = data.get("paused") if isinstance(data, dict) else None
    changed = data.get("changed_at_ms") if isinstance(data, dict) else None
    # bool is an int subclass in Python; reject it for the timestamp.
    # Range matches Rust i64.
    if (
        not isinstance(paused, bool)
        or not isinstance(changed, int)
        or isinstance(changed, bool)
        or not -(2**63) <= changed < 2**63
    ):
        logger.warning("enrichment state invalid at %s: wrong shape", path)
        return invalid
    return StateRead(paused=paused, changed_at_ms=changed, valid=True)
