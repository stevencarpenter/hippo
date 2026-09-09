"""Tests for embedding padding and truncation."""

from __future__ import annotations

from hippo_brain.embeddings import _pad_or_truncate


def test_pad_or_truncate_pad():
    vec = [1.0, 2.0, 3.0]
    assert _pad_or_truncate(vec, 5) == [1.0, 2.0, 3.0, 0.0, 0.0]


def test_pad_or_truncate_truncate():
    vec = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _pad_or_truncate(vec, 3) == [1.0, 2.0, 3.0]


def test_pad_or_truncate_exact():
    vec = [1.0, 2.0, 3.0]
    assert _pad_or_truncate(vec, 3) == [1.0, 2.0, 3.0]
