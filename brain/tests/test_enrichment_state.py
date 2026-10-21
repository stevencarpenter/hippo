import shutil
from pathlib import Path

import pytest

from hippo_brain.enrichment_state import STATE_FILE, StateRead, read_state

FIXTURE = Path(__file__).resolve().parents[2] / "tests/fixtures/enrichment-state-paused.json"


def test_missing_file_is_running(tmp_path):
    assert read_state(tmp_path) == StateRead(paused=False, changed_at_ms=None, valid=True)


def test_reads_shared_fixture(tmp_path):
    shutil.copy(FIXTURE, tmp_path / STATE_FILE)
    assert read_state(tmp_path) == StateRead(True, 1792500000000, True)


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "{",
        '{"paused":"yes"}',
        '{"changed_at_ms":1}',
        '{"paused":true,"changed_at_ms":9223372036854775808}',
        '{"paused": true, "paused": false, "changed_at_ms": 1}',
    ],
)
def test_invalid_contents_are_paused(tmp_path, bad):
    (tmp_path / STATE_FILE).write_text(bad)
    r = read_state(tmp_path)
    assert r.paused is True and r.valid is False


def test_non_utf8_contents_are_paused(tmp_path):
    (tmp_path / STATE_FILE).write_bytes(b"\xff\xfe")
    r = read_state(tmp_path)
    assert r == StateRead(paused=True, changed_at_ms=None, valid=False)
