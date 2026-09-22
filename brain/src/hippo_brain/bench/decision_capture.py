"""Compatibility exports for the shared bounded capture implementation."""

from hippo_brain.decision_capture import (
    capture_decision,
    capture_query,
    capture_rerank,
    decision_root,
    external_path,
)

__all__ = ["capture_decision", "capture_query", "capture_rerank", "decision_root", "external_path"]
