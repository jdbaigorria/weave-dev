"""Tests for LoomAgentEngine — the default AgentEngine over Loom's build surface.

These assert the engine threads its injected ``ConfigSource`` into every Loom call
(``build_agent`` / ``build_workflow`` / ``discover``) and defaults to ``None`` (so
Loom uses its filesystem source). Loom's builders are patched — this is wiring, not
a real build.
"""

from __future__ import annotations

from unittest.mock import patch

from loom import LoomSession

from weave.adapters.driven.loom_engine import LoomAgentEngine


def _session() -> LoomSession:
    return LoomSession(user_id="u1", session_id="s1")


def test_defaults_to_no_source():
    engine = LoomAgentEngine()
    with patch("loom.build_agent") as build_agent:
        engine.build_agent("assistant", _session())
    _, kwargs = build_agent.call_args
    assert kwargs["source"] is None


def test_threads_injected_source_to_build_agent():
    source = object()
    engine = LoomAgentEngine(source=source)
    with patch("loom.build_agent") as build_agent:
        engine.build_agent("assistant", _session())
    _, kwargs = build_agent.call_args
    assert kwargs["source"] is source


def test_threads_model_params_to_build_agent():
    engine = LoomAgentEngine()
    overlay = {"boto_session": object()}
    with patch("loom.build_agent") as build_agent:
        engine.build_agent("assistant", _session(), model_params=overlay)
    _, kwargs = build_agent.call_args
    assert kwargs["model_params"] is overlay


def test_defaults_model_params_to_none_on_build_agent():
    engine = LoomAgentEngine()
    with patch("loom.build_agent") as build_agent:
        engine.build_agent("assistant", _session())
    _, kwargs = build_agent.call_args
    assert kwargs["model_params"] is None


def test_threads_injected_source_to_build_workflow():
    source = object()
    engine = LoomAgentEngine(source=source)
    with patch("loom.build_workflow", return_value=(None, [])) as build_workflow:
        engine.build_workflow("triage", _session())
    _, kwargs = build_workflow.call_args
    assert kwargs["source"] is source


def test_threads_model_params_to_build_workflow():
    engine = LoomAgentEngine()
    overlay = {"boto_session": object()}
    with patch("loom.build_workflow", return_value=(None, [])) as build_workflow:
        engine.build_workflow("triage", _session(), model_params=overlay)
    _, kwargs = build_workflow.call_args
    assert kwargs["model_params"] is overlay


def test_threads_injected_source_to_discover():
    source = object()
    engine = LoomAgentEngine(source=source)
    with patch("loom.discover", return_value=[]) as discover:
        engine.discover()
    args, _ = discover.call_args
    assert args == (source,)
