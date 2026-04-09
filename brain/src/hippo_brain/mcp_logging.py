"""Structured logging for the Hippo MCP server.

Logging goes to stderr (stdout is reserved for MCP stdio transport).
"""

import logging
import sys


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
