"""Tests for weave.configure — set the engine the wired top-level functions use.

configure() mutates the composition-root engine, so each test restores the default
afterwards to avoid leaking global state.
"""

from __future__ import annotations

import pytest

import weave
from weave import composition
from weave.adapters.driven.loom_engine import LoomAgentEngine
from weave.application.errors import HarnessError


@pytest.fixture(autouse=True)
def _restore_default_engine():
    yield
    weave.configure()  # reset to the Loom filesystem default


class _FakeEngine:
    """Records which wired calls reach the engine."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def discover(self):
        self.calls.append("discover")
        return []

    def build_agent(self, name, session):  # pragma: no cover - not exercised here
        self.calls.append("build_agent")

    def build_workflow(self, name, session):  # pragma: no cover - not exercised here
        self.calls.append("build_workflow")


def test_default_engine_is_loom_filesystem():
    assert isinstance(composition._engine, LoomAgentEngine)
    assert composition._engine._source is None


def test_configure_with_source_builds_loom_engine_around_it():
    source = object()
    weave.configure(source=source)

    assert isinstance(composition._engine, LoomAgentEngine)
    assert composition._engine._source is source


def test_configure_with_engine_installs_it_for_wired_functions():
    engine = _FakeEngine()
    weave.configure(engine=engine)

    assert weave.catalog() == []
    assert engine.calls == ["discover"]  # the wired catalog() reached our engine


def test_configure_resets_to_default_with_no_args():
    weave.configure(engine=_FakeEngine())
    weave.configure()

    assert isinstance(composition._engine, LoomAgentEngine)
    assert composition._engine._source is None


def test_configure_rejects_both_source_and_engine():
    with pytest.raises(HarnessError, match="either source or engine"):
        weave.configure(source=object(), engine=_FakeEngine())


# --- W7: configure(observers=...) ---------------------------------------------


class _Recorder:
    def __init__(self) -> None:
        self.events: list = []

    def on_turn(self, event) -> None:
        self.events.append(event)


class _TurnEngine:
    """Builds a fake agent for any target and lists two agents in the catalog."""

    def discover(self):
        from types import SimpleNamespace

        return [SimpleNamespace(name=n, kind="agent", labels={}) for n in ("sales", "support")]

    def build_agent(self, name, session, *, model_params=None):
        from types import SimpleNamespace

        result = SimpleNamespace(
            message={"content": [{"text": "ok"}]},
            structured_output=None,
            stop_reason="end_turn",
            interrupts=[],
            metrics=SimpleNamespace(get_summary=lambda: {}),
        )

        async def invoke_async(prompt, *, invocation_state=None):
            return result

        return SimpleNamespace(invoke_async=invoke_async, cleanup=lambda: None)


def test_observers_hear_a_turn_through_lambda_handler_with_the_resolved_target():
    import json

    from weave import lambda_handler, routed_channel

    obs = _Recorder()
    weave.configure(engine=_TurnEngine(), observers=[obs])
    handler = lambda_handler(routed_channel(lambda request: "support"))

    resp = handler({"body": json.dumps({"message": "hi", "session_id": "s-1"})}, None)

    assert resp["statusCode"] == 200
    (event,) = obs.events
    assert (event.target, event.outcome, event.session_id) == ("support", "ok", "s-1")


def test_configure_without_args_drops_the_observers():
    weave.configure(engine=_TurnEngine(), observers=[_Recorder()])
    weave.configure()

    assert composition._observers == ()


def test_configure_with_source_and_observers_keeps_both():
    source = object()
    obs = _Recorder()
    weave.configure(source=source, observers=[obs])

    assert composition._engine._source is source
    assert composition._observers == (obs,)
