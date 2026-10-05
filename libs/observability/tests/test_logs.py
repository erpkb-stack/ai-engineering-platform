import json

import pytest

from aeoi_common import set_correlation_id
from aeoi_observability import configure_logging, get_logger


def test_json_log_has_service_correlation_and_redaction(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("test-svc", level="INFO", environment="test")
    set_correlation_id("corr-7")
    get_logger("t").info("tool_called", tool="search_logs", api_key="sk-ant-should-not-leak-1234")
    set_correlation_id(None)
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["event"] == "tool_called"
    assert line["service"] == "test-svc"
    assert line["env"] == "test"
    assert line["correlation_id"] == "corr-7"
    assert line["api_key"] == "[REDACTED]"
    assert "should-not-leak" not in json.dumps(line)


def test_level_filtering(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("test-svc", level="WARNING")
    get_logger().info("hidden")
    get_logger().warning("shown")
    out = capsys.readouterr().out
    assert "hidden" not in out
    assert "shown" in out


def test_unknown_level_rejected() -> None:
    with pytest.raises(ValueError, match="unknown log level"):
        configure_logging("x", level="LOUD")
