"""Pre-mutation backup checkpoint for maintenance scripts."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

# `hippo backup now` exits 3 when [backup] is not configured.
NOT_CONFIGURED = 3


def checkpoint_before_mutation(label: str, db_path: Path) -> None:
    """Run `hippo backup now --label LABEL` when db_path is the configured DB.

    Exit 3 (backup not configured) logs a warning. Any other failure raises
    SystemExit, so the caller does not mutate an unprotected database.
    """
    from hippo_brain import _load_runtime_settings

    configured = Path(_load_runtime_settings()["db_path"]).expanduser()
    if Path(db_path).resolve() != configured.resolve():
        return
    result = subprocess.run(
        ["hippo", "backup", "now", "--label", label],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == NOT_CONFIGURED:
        log.warning("backup not configured; continuing without a checkpoint")
    elif result.returncode != 0:
        raise SystemExit(
            f"backup checkpoint failed (exit {result.returncode}): {result.stderr.strip()}"
        )
