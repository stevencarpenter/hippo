import pytest

from hippo_brain.bench.cli import main


def test_cli_help_smoke(capsys):
    """hippo-bench --help exits 0 and mentions the three subcommands."""
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "run" in out
    assert "corpus" in out
    assert "summary" in out
