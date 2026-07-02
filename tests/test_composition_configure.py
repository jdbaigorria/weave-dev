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
