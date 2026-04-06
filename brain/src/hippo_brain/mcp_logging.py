"""Structured logging and metrics for the Hippo MCP server.

Logging goes to stderr (stdout is reserved for MCP stdio transport).
MetricsCollector holds counters suitable for future OTel export.
"""

import logging
import sys
from dataclasses import dataclass


def setup_logging(server_name: str) -> logging.Logger:
    """Configure structured logging to stderr for the MCP server."""
    logger = logging.getLogger(server_name)
    logger.setLevel(logging.INFO)

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        logger.addHandler(handler)

    logger.propagate = False
    return logger


@dataclass
class MetricsCollector:
    """Counters for MCP server observability.

    Designed for future OTel gauge/counter export. Each field maps to a
    metric name like `hippo.mcp.tool_calls`.
    """

    tool_calls: int = 0
    tool_errors: int = 0
    semantic_searches: int = 0
    lexical_searches: int = 0
    lexical_fallbacks: int = 0
    lmstudio_errors: int = 0
    events_searched: int = 0
    entities_returned: int = 0

    def snapshot(self) -> dict[str, int]:
        """Return all metrics as a dict (for health checks or OTel export)."""
        return {
            "tool_calls": self.tool_calls,
            "tool_errors": self.tool_errors,
            "semantic_searches": self.semantic_searches,
            "lexical_searches": self.lexical_searches,
            "lexical_fallbacks": self.lexical_fallbacks,
            "lmstudio_errors": self.lmstudio_errors,
            "events_searched": self.events_searched,
            "entities_returned": self.entities_returned,
        }
