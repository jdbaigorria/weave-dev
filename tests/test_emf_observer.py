"""W8 — EmfTurnObserver: one Embedded Metric Format line per turn, no dependencies."""

from __future__ import annotations

import io
import json
import sys

import pytest
from strands.types.exceptions import ContextWindowOverflowException

from weave import EmfTurnObserver, TurnEvent, Usage
from weave.application.errors import HarnessError


def _event(**kw) -> TurnEvent:
    base = dict(
        target="billing",
        kind="agent",
        operation="run",
        session_id="s-1",
        user_id="u-1",
        is_anonymous=False,
        outcome="ok",
        duration_ms=4210.5,
        build_ms=120.0,
        usage=Usage(
            input_tokens=10,
            output_tokens=5,
            total_tokens=23,
            cache_read_input_tokens=3,
            cache_write_input_tokens=5,
            tool_calls=["a", "b"],
        ),
        stop_reason="end_turn",
    )
    return TurnEvent(**{**base, **kw})


def _line(observer: EmfTurnObserver, event: TurnEvent, stream: io.StringIO) -> dict:
    observer.on_turn(event)
    (line,) = stream.getvalue().splitlines()
    return json.loads(line)


@pytest.fixture
def out() -> io.StringIO:
    return io.StringIO()


def test_ok_turn(out):
    doc = _line(EmfTurnObserver("Acme/Agents", service="acme", stream=out), _event(), out)

    spec = doc["_aws"]["CloudWatchMetrics"][0]
    assert spec["Namespace"] == "Acme/Agents"
    assert spec["Dimensions"] == [["target", "service"]]
    assert isinstance(doc["_aws"]["Timestamp"], int) and doc["_aws"]["Timestamp"] > 10**12
    units = {m["Name"]: m["Unit"] for m in spec["Metrics"]}
    assert units == {
        "Turns": "Count",
        "TurnErrors": "Count",
        "TurnDuration": "Milliseconds",
        "BuildDuration": "Milliseconds",
        "InputTokens": "Count",
        "OutputTokens": "Count",
        "TotalTokens": "Count",
        "CacheReadInputTokens": "Count",
        "CacheWriteInputTokens": "Count",
        "ToolCalls": "Count",
    }
    assert all(name in doc for name in units)
    assert (doc["Turns"], doc["TurnErrors"], doc["TotalTokens"], doc["ToolCalls"]) == (1, 0, 23, 2)
    assert (doc["CacheReadInputTokens"], doc["CacheWriteInputTokens"]) == (3, 5)
    # Whole milliseconds: sub-ms precision is noise in a dashboard.
    assert (doc["TurnDuration"], doc["BuildDuration"]) == (4211, 120)
    assert isinstance(doc["TurnDuration"], int)
    assert (doc["target"], doc["service"]) == ("billing", "acme")
    assert (doc["session_id"], doc["outcome"], doc["operation"]) == ("s-1", "ok", "run")


def test_error_turn(out):
    try:
        raise HarnessError("failed") from ContextWindowOverflowException("long")
    except HarnessError as exc:
        error = exc

    doc = _line(
        EmfTurnObserver("A", stream=out),
        _event(outcome="error", usage=None, stop_reason=None, error=error),
        out,
    )

    assert doc["TurnErrors"] == 1
    assert doc["error_type"] == "ContextWindowOverflowException"
    assert "TotalTokens" not in doc and "ToolCalls" not in doc
    assert "CacheReadInputTokens" not in doc


def test_no_build_duration_when_the_build_did_not_finish(out):
    doc = _line(
        EmfTurnObserver("A", stream=out), _event(outcome="error", usage=None, build_ms=None), out
    )

    assert "BuildDuration" not in doc
    names = [m["Name"] for m in doc["_aws"]["CloudWatchMetrics"][0]["Metrics"]]
    assert "BuildDuration" not in names


def test_custom_dimensions_replace_the_default_and_session_is_never_one(out):
    observer = EmfTurnObserver(
        "A", dimensions=lambda e: {"agent": e.target, "area": "ventas"}, stream=out
    )

    doc = _line(observer, _event(), out)

    assert doc["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["agent", "area"]]
    assert "target" not in doc
    assert doc["session_id"] == "s-1"  # still a property


def test_custom_properties(out):
    doc = _line(EmfTurnObserver("A", properties=lambda e: {"area": "x"}, stream=out), _event(), out)

    assert doc["area"] == "x"
    assert "session_id" not in doc


def test_default_stream_is_stdout_at_write_time(monkeypatch):
    observer = EmfTurnObserver("A")
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)

    observer.on_turn(_event())

    assert '"_aws"' in out.getvalue()


def test_empty_namespace_is_rejected():
    with pytest.raises(ValueError, match="namespace"):
        EmfTurnObserver("")


def test_more_than_30_dimensions_is_rejected_at_emit(out):
    observer = EmfTurnObserver(
        "A", dimensions=lambda e: {f"d{i}": "x" for i in range(31)}, stream=out
    )

    with pytest.raises(ValueError, match="30"):
        observer.on_turn(_event())
