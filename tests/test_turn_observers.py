"""W5/W6 — TurnEvent, TurnObserver and the use cases notifying observers once per turn."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from loom import LoomSession
from loom.errors import LoomError
from strands.types.exceptions import ContextWindowOverflowException

from weave.application.errors import HarnessError
from weave.application.observe import TurnEvent
from weave.application.run import (
    fork,
    fork_stateless,
    run,
    run_agent,
    run_workflow,
    stream_agent,
)

SUMMARY = {
    "accumulated_usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
    "total_duration": 0.2,
    "tool_usage": {"search": {}},
}


def _result(text: str = "hi") -> SimpleNamespace:
    return SimpleNamespace(
        message={"content": [{"text": text}]},
        structured_output=None,
        stop_reason="end_turn",
        interrupts=[],
        metrics=SimpleNamespace(get_summary=lambda: SUMMARY),
    )


class _Agent:
    def __init__(self, *, fail: bool = False, events: list[Any] | None = None) -> None:
        self.fail = fail
        self.events = events
        self.cleaned_up = False
        self.messages: list[Any] = []

    async def invoke_async(self, prompt, *, invocation_state=None):
        if self.fail:
            raise RuntimeError("boom")
        return _result()

    async def stream_async(self, prompt, *, invocation_state=None):
        for event in self.events or []:
            if isinstance(event, Exception):
                raise event
            yield event

    def cleanup(self) -> None:
        self.cleaned_up = True


class _Engine:
    def __init__(self, agent: _Agent | None = None, *, build_error: Exception | None = None):
        self.agent = agent or _Agent()
        self.build_error = build_error

    def build_agent(self, name, session, *, model_params=None):
        if self.build_error is not None:
            raise self.build_error
        self.agent.agent_id = name
        return self.agent

    def build_workflow(self, name, session, *, model_params=None):
        workflow = SimpleNamespace(invoke_async=self._invoke_workflow)
        return workflow, [self.agent]

    async def _invoke_workflow(self, prompt, *, invocation_state=None):
        return SimpleNamespace(
            status="COMPLETED",
            results={},
            execution_order=[],
            accumulated_usage={"inputTokens": 1, "outputTokens": 2, "totalTokens": 3},
            accumulated_metrics={"latencyMs": 5},
            execution_time=7,
            interrupts=[],
        )

    def discover(self):
        return [SimpleNamespace(name="assistant", kind="agent")]


class _Recorder:
    def __init__(self) -> None:
        self.events: list[TurnEvent] = []

    def on_turn(self, event: TurnEvent) -> None:
        self.events.append(event)


def _session() -> LoomSession:
    return LoomSession(session_id="s-1", user_id="u-1", is_anonymous=True)


@pytest.fixture
def rec() -> _Recorder:
    return _Recorder()


class TestTurnEvent:
    def _event(self, **kw) -> TurnEvent:
        base = dict(
            target="a",
            kind="agent",
            operation="run",
            session_id="s",
            user_id="u",
            is_anonymous=False,
            outcome="error",
            duration_ms=1.0,
        )
        return TurnEvent(**{**base, **kw})

    def test_error_type_unwraps_the_cause_chain(self):
        try:
            try:
                raise ContextWindowOverflowException("too long")
            except ContextWindowOverflowException as inner:
                raise HarnessError("agent failed") from inner
        except HarnessError as exc:
            error = exc

        assert self._event(error=error).error_type == "ContextWindowOverflowException"

    def test_error_type_without_cause_and_without_error(self):
        assert self._event(error=RuntimeError("x")).error_type == "RuntimeError"
        assert self._event(outcome="ok").error_type is None

    def test_frozen(self):
        event = self._event(outcome="ok")
        with pytest.raises(Exception):
            event.target = "b"  # type: ignore[misc]


class TestRunAgent:
    def test_ok(self, rec):
        reply = asyncio.run(run_agent(_Engine(), "assistant", "hi", _session(), observers=[rec]))

        (event,) = rec.events
        assert (event.target, event.kind, event.operation, event.outcome) == (
            "assistant",
            "agent",
            "run",
            "ok",
        )
        assert (event.session_id, event.user_id, event.is_anonymous) == ("s-1", "u-1", True)
        assert event.usage == reply.usage
        assert event.stop_reason == "end_turn"
        assert event.build_ms is not None and event.build_ms >= 0
        assert event.duration_ms >= event.build_ms
        assert event.error is None

    def test_execution_error(self, rec):
        engine = _Engine(_Agent(fail=True))
        with pytest.raises(HarnessError):
            asyncio.run(run_agent(engine, "assistant", "hi", _session(), observers=[rec]))

        (event,) = rec.events
        assert event.outcome == "error"
        assert isinstance(event.error, HarnessError)
        assert event.error_type == "RuntimeError"
        assert event.usage is None and event.stop_reason is None
        assert engine.agent.cleaned_up

    def test_build_error(self, rec):
        engine = _Engine(build_error=LoomError("bad yaml"))
        with pytest.raises(LoomError):
            asyncio.run(run_agent(engine, "assistant", "hi", _session(), observers=[rec]))

        (event,) = rec.events
        assert event.outcome == "error"
        assert event.build_ms is None
        assert event.error_type == "LoomError"


def _consume(engine, rec, *, close_after: int | None = None):
    async def go():
        stream = stream_agent(engine, "assistant", "hi", _session(), observers=[rec])
        seen = 0
        async for _ in stream:
            seen += 1
            if close_after is not None and seen == close_after:
                await stream.aclose()
                return

    asyncio.run(go())


class TestStreamAgent:
    def test_fully_consumed_is_ok(self, rec):
        _consume(_Engine(_Agent(events=[{"data": "h"}, {"result": _result()}])), rec)

        (event,) = rec.events
        assert (event.operation, event.outcome) == ("stream", "ok")
        assert event.usage is not None and event.usage.total_tokens == 15

    def test_error_midway(self, rec):
        with pytest.raises(HarnessError):
            _consume(_Engine(_Agent(events=[{"data": "h"}, RuntimeError("down")])), rec)

        (event,) = rec.events
        assert event.outcome == "error"

    def test_closed_before_done_is_cancelled(self, rec):
        _consume(
            _Engine(_Agent(events=[{"data": "h"}, {"data": "i"}, {"result": _result()}])),
            rec,
            close_after=1,
        )

        (event,) = rec.events
        assert event.outcome == "cancelled"
        assert event.usage is None

    def test_closed_after_done_is_ok(self, rec):
        _consume(
            _Engine(_Agent(events=[{"result": _result()}, {"data": "late"}])), rec, close_after=1
        )

        (event,) = rec.events
        assert event.outcome == "ok"


class TestOtherUseCases:
    def test_workflow(self, rec):
        asyncio.run(run_workflow(_Engine(), "flow", "hi", _session(), observers=[rec]))

        (event,) = rec.events
        assert (event.kind, event.operation, event.outcome) == ("workflow", "run", "ok")
        assert event.usage is not None and event.usage.total_tokens == 3

    def test_fork(self, rec):
        store = SimpleNamespace(truncate=lambda *a, **k: None)
        from weave.ports.session import MutableSession

        assert isinstance(store, MutableSession)
        asyncio.run(fork(_Engine(), store, "assistant", "hi", _session(), at=0, observers=[rec]))
        asyncio.run(fork_stateless(_Engine(), "assistant", [], "hi", _session(), observers=[rec]))

        assert [(e.operation, e.outcome) for e in rec.events] == [("fork", "ok"), ("fork", "ok")]

    def test_run_dispatch_emits_once(self, rec):
        asyncio.run(run(_Engine(), "assistant", "hi", _session(), observers=[rec]))

        assert len(rec.events) == 1


class TestObserverRules:
    def test_order_and_a_failing_observer(self, rec, caplog):
        calls: list[str] = []

        class Boom:
            def on_turn(self, event):
                calls.append("boom")
                raise ValueError("observer bug")

        class Second:
            def on_turn(self, event):
                calls.append("second")

        with caplog.at_level(logging.WARNING, logger="weave.application.observe"):
            reply = asyncio.run(
                run_agent(_Engine(), "assistant", "hi", _session(), observers=[Boom(), Second()])
            )

        assert reply.text == "hi"
        assert calls == ["boom", "second"]
        assert "observer" in caplog.text

    def test_a_failing_observer_does_not_mask_the_turn_error(self):
        class Boom:
            def on_turn(self, event):
                raise ValueError("observer bug")

        with pytest.raises(HarnessError):
            asyncio.run(
                run_agent(_Engine(_Agent(fail=True)), "a", "hi", _session(), observers=[Boom()])
            )

    def test_no_observers_builds_no_event(self, monkeypatch):
        import weave.application.observe as observe

        monkeypatch.setattr(observe, "TurnEvent", None)  # would explode if built
        asyncio.run(run_agent(_Engine(), "assistant", "hi", _session()))
