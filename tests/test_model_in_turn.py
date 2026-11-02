"""W14 — the model and the variant in TurnEvent and in EmfTurnObserver."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from types import SimpleNamespace
from typing import Any

import loom
import pytest
from loom import LoomSession, ModelSettings
from loom.schemas.config.agent import AgentConfig
from loom.schemas.config.app import AppConfig
from loom.schemas.config.models import CustomModelConfig

from weave import EmfTurnObserver, TurnEvent
from weave.adapters.driven.loom_engine import LoomAgentEngine
from weave.application.errors import HarnessError
from weave.application.observe import AgentDescription
from weave.application.run import run_agent, run_workflow

SETTINGS = ModelSettings(max_tokens=8000, streaming=False, effort="low")
DESCRIPTION = AgentDescription(
    model_id="us.anthropic.claude-haiku-5-5", model_settings=SETTINGS, variant="effort-low"
)


def _result() -> SimpleNamespace:
    return SimpleNamespace(
        message={"content": [{"text": "hi"}]},
        structured_output=None,
        stop_reason="end_turn",
        interrupts=[],
        metrics=SimpleNamespace(get_summary=lambda: {}),
    )


class _Agent:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.cleaned_up = False

    async def invoke_async(self, prompt, *, invocation_state=None):
        if self.fail:
            raise RuntimeError("boom")
        return _result()

    def cleanup(self) -> None:
        self.cleaned_up = True


class _Engine:
    """Builds a fake agent; ``describe`` is the optional capability under test."""

    def __init__(self, *, fail: bool = False, build_error: Exception | None = None) -> None:
        self.agent = _Agent(fail=fail)
        self.build_error = build_error
        self.described: list[Any] = []

    def build_agent(self, name, session, *, model_params=None):
        if self.build_error is not None:
            raise self.build_error
        return self.agent

    def build_workflow(self, name, session, *, model_params=None):
        async def invoke_async(prompt, *, invocation_state=None):
            return SimpleNamespace(status=None, results={}, interrupts=[])

        return SimpleNamespace(invoke_async=invoke_async), [self.agent]

    def discover(self):
        return []

    def describe(self, agent):
        # Called before the teardown: the agent still holds its model.
        assert not agent.cleaned_up
        self.described.append(agent)
        return DESCRIPTION


class _PlainEngine(_Engine):
    describe = None  # type: ignore[assignment]


class _Recorder:
    def __init__(self) -> None:
        self.events: list[TurnEvent] = []

    def on_turn(self, event: TurnEvent) -> None:
        self.events.append(event)


def _session() -> LoomSession:
    return LoomSession(session_id="s", user_id="u")


def _run(engine, rec, **kw):
    return asyncio.run(run_agent(engine, "support", "hi", _session(), observers=[rec], **kw))


class TestUseCases:
    def test_ok_turn_carries_the_model(self):
        rec = _Recorder()
        _run(_Engine(), rec)

        (event,) = rec.events
        assert (event.model_id, event.variant, event.model_fallbacks) == (
            "us.anthropic.claude-haiku-5-5",
            "effort-low",
            0,
        )
        assert event.model_settings == SETTINGS

    def test_failed_execution_still_carries_the_model(self):
        rec = _Recorder()
        with pytest.raises(HarnessError):
            _run(_Engine(fail=True), rec)

        assert rec.events[0].model_id == "us.anthropic.claude-haiku-5-5"

    def test_failed_build_has_no_model(self):
        rec = _Recorder()
        with pytest.raises(ValueError):
            _run(_Engine(build_error=ValueError("bad")), rec)

        assert rec.events[0].model_id is None

    def test_engine_without_describe(self):
        rec = _Recorder()
        reply = _run(_PlainEngine(), rec)

        assert reply.text == "hi"
        assert rec.events[0].model_id is None

    def test_describe_that_raises_is_logged_and_ignored(self, caplog):
        class Broken(_Engine):
            def describe(self, agent):
                raise RuntimeError("describe failed")

        rec = _Recorder()
        with caplog.at_level(logging.WARNING, logger="weave.application.observe"):
            reply = _run(Broken(), rec)

        assert reply.text == "hi"
        assert rec.events[0].model_id is None
        assert "describe" in caplog.text

    def test_no_observers_do_not_describe(self):
        engine = _Engine()
        asyncio.run(run_agent(engine, "support", "hi", _session()))

        assert engine.described == []

    def test_workflow_has_no_single_model(self):
        rec = _Recorder()
        asyncio.run(run_workflow(_Engine(), "flow", "hi", _session(), observers=[rec]))

        assert rec.events[0].model_id is None


class TestLoomEngine:
    def test_describe_reads_loom(self, monkeypatch):
        config = AgentConfig(
            name="support",
            system_prompt={"prompt": "hi", "is_resolved": True},
            model=CustomModelConfig(import_path="loom.testing.MockModel"),
            labels={"metrics.variant": "v1"},
        )
        agent = loom.build(config, _session(), app_config=AppConfig(conversation="none"))

        description = LoomAgentEngine().describe(agent)

        assert (description.model_id, description.variant) == ("mock", "v1")
        assert isinstance(description.model_settings, ModelSettings)


def _event(**kw) -> TurnEvent:
    base: dict[str, Any] = dict(
        target="support",
        kind="agent",
        operation="run",
        session_id="s",
        user_id="u",
        is_anonymous=False,
        outcome="ok",
        duration_ms=10.0,
        model_id="us.anthropic.claude-haiku-5-5",
        model_settings=SETTINGS,
        variant="effort-low",
    )
    return TurnEvent(**{**base, **kw})


def _line(observer, event) -> dict:
    out = io.StringIO()
    observer._stream = out
    observer.on_turn(event)
    return json.loads(out.getvalue())


class TestEmf:
    def test_three_dimension_sets(self):
        doc = _line(EmfTurnObserver("A", service="acme"), _event())

        assert doc["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [
            ["target", "service"],
            ["target", "service", "model"],
            ["target", "service", "variant"],
        ]
        assert (doc["model"], doc["variant"]) == ("us.anthropic.claude-haiku-5-5", "effort-low")
        assert doc["model_config"] == {"max_tokens": 8000, "streaming": False, "effort": "low"}
        assert doc["model_fallbacks"] == 0

    def test_custom_base_gets_the_extra_sets(self):
        observer = EmfTurnObserver("A", dimensions=lambda e: {"agent": e.target, "service": "acme"})

        doc = _line(observer, _event(variant=None))

        assert doc["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [
            ["agent", "service"],
            ["agent", "service", "model"],
        ]
        assert "target" not in doc

    def test_dimension_flags(self):
        observer = EmfTurnObserver("A", model_dimension=False, variant_dimension=False)

        doc = _line(observer, _event())

        assert doc["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["target"]]
        assert doc["model"] == "us.anthropic.claude-haiku-5-5"  # still a property

    def test_without_model_one_set_and_no_model_config(self):
        doc = _line(EmfTurnObserver("A"), _event(model_id=None, model_settings=None, variant=None))

        assert doc["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["target"]]
        assert "model_config" not in doc and "model" not in doc
