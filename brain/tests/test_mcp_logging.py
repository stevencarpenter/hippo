import logging

from hippo_brain.mcp_logging import setup_logging


def test_setup_logging_creates_logger():
    logger = setup_logging("test-mcp")
    assert logger.name == "test-mcp"
    assert logger.level == logging.INFO


def test_setup_logging_writes_to_stderr(capsys):
    logger = setup_logging("test-mcp-stderr")
    logger.info("test message")
    captured = capsys.readouterr()
    assert "test message" in captured.err
