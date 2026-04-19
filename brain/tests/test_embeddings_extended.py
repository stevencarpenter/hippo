"""Extended tests for hippo_brain.embeddings — idempotency + pad/truncate."""

from __future__ import annotations

import tempfile

from hippo_brain.embeddings import (
    _pad_or_truncate,
    get_or_create_table,
    open_vector_db,
)


def test_get_or_create_table_is_idempotent():
    """Calling twice must succeed; the vec0 table is reused."""
    with tempfile.TemporaryDirectory() as tmpdir:
        conn = open_vector_db(tmpdir)
        try:
            handle1 = get_or_create_table(conn)
            handle2 = get_or_create_table(conn)
            assert handle1 is handle2
            row = conn.execute(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='knowledge_vectors'"
            ).fetchone()
            assert row[0] == 1
        finally:
            conn.close()


def test_pad_or_truncate_pad():
    vec = [1.0, 2.0, 3.0]
    assert _pad_or_truncate(vec, 5) == [1.0, 2.0, 3.0, 0.0, 0.0]


def test_pad_or_truncate_truncate():
    vec = [1.0, 2.0, 3.0, 4.0, 5.0]
    assert _pad_or_truncate(vec, 3) == [1.0, 2.0, 3.0]


def test_pad_or_truncate_exact():
    vec = [1.0, 2.0, 3.0]
    assert _pad_or_truncate(vec, 3) == [1.0, 2.0, 3.0]
