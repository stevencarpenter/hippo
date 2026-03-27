"""Extended tests for hippo_brain.embeddings — table-already-exists path, pad/truncate."""

import tempfile
from unittest.mock import patch, MagicMock

from hippo_brain.embeddings import (
    get_or_create_table,
    open_vector_db,
    _pad_or_truncate,
)


def test_get_or_create_table_opens_existing():
    """When list_tables returns 'knowledge', get_or_create_table opens it instead of creating."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = open_vector_db(tmpdir)
        # First call creates the table
        table1 = get_or_create_table(db)
        assert table1.name == "knowledge"

        # Now mock list_tables to return a plain list containing "knowledge"
        # so the "knowledge" in existing check on line 38 succeeds
        mock_open = MagicMock(return_value=table1)
        with patch.object(db, "list_tables", return_value=["knowledge"]):
            with patch.object(db, "open_table", mock_open):
                get_or_create_table(db)
        mock_open.assert_called_once_with("knowledge")


def test_pad_or_truncate_pad():
    """Shorter vector is zero-padded to target dimension."""
    vec = [1.0, 2.0, 3.0]
    result = _pad_or_truncate(vec, 5)
    assert result == [1.0, 2.0, 3.0, 0.0, 0.0]


def test_pad_or_truncate_truncate():
    """Longer vector is truncated to target dimension."""
    vec = [1.0, 2.0, 3.0, 4.0, 5.0]
    result = _pad_or_truncate(vec, 3)
    assert result == [1.0, 2.0, 3.0]


def test_pad_or_truncate_exact():
    """Exact-length vector is returned unchanged."""
    vec = [1.0, 2.0, 3.0]
    result = _pad_or_truncate(vec, 3)
    assert result == [1.0, 2.0, 3.0]
