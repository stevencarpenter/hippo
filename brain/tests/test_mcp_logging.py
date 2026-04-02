import logging

from hippo_brain.mcp_logging import setup_logging, MetricsCollector


def test_setup_logging_returns_logger():
    logger = setup_logging("test-mcp")
    assert isinstance(logger, logging.Logger)
    assert logger.name == "hippo.mcp"
    assert logger.level == logging.INFO


def test_setup_logging_writes_to_stderr(capsys):
    # Clear any cached handlers so setup_logging creates a fresh one
    # that points to the capsys-wrapped sys.stderr.
    logger = logging.getLogger("hippo.mcp")
    logger.handlers.clear()

    logger = setup_logging("test-mcp")
    logger.info("hello from test")
    captured = capsys.readouterr()
    assert captured.out == "", "logging must not write to stdout (reserved for MCP stdio)"
    assert "hello from test" in captured.err


def test_metrics_collector_counters():
    m = MetricsCollector()
    assert m.tool_calls == 0
    assert m.tool_errors == 0
    m.tool_calls += 1
    m.semantic_searches += 1
    assert m.tool_calls == 1


def test_metrics_collector_snapshot():
    m = MetricsCollector()
    m.tool_calls = 5
    m.tool_errors = 1
    m.semantic_searches = 3
    m.lexical_searches = 2
    m.lexical_fallbacks = 1
    m.lmstudio_errors = 1
    m.events_searched = 100
    m.entities_returned = 50
    snap = m.snapshot()
    assert snap == {
        "tool_calls": 5,
        "tool_errors": 1,
        "semantic_searches": 3,
        "lexical_searches": 2,
        "lexical_fallbacks": 1,
        "lmstudio_errors": 1,
        "events_searched": 100,
        "entities_returned": 50,
    }
