import subprocess


def test_cli_help_smoke():
    """hippo-bench --help exits 0 and mentions the three subcommands."""
    result = subprocess.run(
        ["uv", "run", "--project", "brain", "hippo-bench", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "run" in result.stdout
    assert "corpus" in result.stdout
    assert "summary" in result.stdout
