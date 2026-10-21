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


def read_state(data_dir: str | Path) -> StateRead:
    """Never raises. Missing file means running; invalid content fails closed (paused)."""
    path = Path(data_dir) / STATE_FILE
    invalid = StateRead(paused=True, changed_at_ms=None, valid=False)
    try:
        text = path.read_text()
    except FileNotFoundError:
        return StateRead(paused=False, changed_at_ms=None, valid=True)
    except OSError as e:
        logger.warning("enrichment state unreadable at %s: %s", path, e)
        return invalid
    try:
        data = json.loads(text)
    except ValueError as e:
        logger.warning("enrichment state invalid at %s: %s", path, e)
        return invalid
    paused = data.get("paused") if isinstance(data, dict) else None
    changed = data.get("changed_at_ms") if isinstance(data, dict) else None
    # bool is an int subclass in Python; reject it for the timestamp.
    if not isinstance(paused, bool) or not isinstance(changed, int) or isinstance(changed, bool):
        logger.warning("enrichment state invalid at %s: wrong shape", path)
        return invalid
    return StateRead(paused=paused, changed_at_ms=changed, valid=True)
