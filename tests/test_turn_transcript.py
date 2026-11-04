"""W22 — TurnTranscript and TranscriptObserver: one record per agent turn, with its content."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC
from types import SimpleNamespace
from typing import Any

import pytest
from loom import LoomSession
from loom.errors import LoomError

import weave
from weave.application import run as _run
from weave.application.errors import HarnessError
from weave.application.observe import TurnTranscript
from weave.application.transcript import ToolCall, question_of, tool_calls_of, turn_messages
from weave.application.turn_id import TURN_ID_KEY

SUMMARY = {
    "accumulated_usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
    "total_duration": 0.2,
    "tool_usage": {"search_orders": {}},
}

SESSION = LoomSession(session_id="s-1", user_id="u-1")


def _result(text: str = "Tenés dos pedidos abiertos.") -> SimpleNamespace:
    return SimpleNamespace(
        message={"role": "assistant", "content": [{"text": text}]},
        structured_output=None,
        stop_reason="end_turn",
        interrupts=[],
        metrics=SimpleNamespace(get_summary=lambda: SUMMARY),
    )


def _tool_use(tool_use_id: str = "tu-1", name: str = "search_orders") -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [
            {"toolUse": {"toolUseId": tool_use_id, "name": name, "input": {"customer_id": 1}}}
        ],
    }


def _tool_result(tool_use_id: str = "tu-1", status: str = "success") -> dict[str, Any]:
    return {
        "role": "user",
        "content": [
            {
                "toolResult": {
                    "toolUseId": tool_use_id,
                    "status": status,
                    "content": [{"json": {"orders": [1, 2]}}],
                }
            }
        ],
    }


class _Agent:
    """Appends the stamped input to ``messages`` like Strands, then what ``steps`` add."""

    def __init__(
        self,
        *,
        steps: list[dict[str, Any]] | None = None,
        fail: Exception | None = None,
        history: list[Any] | None = None,
    ) -> None:
        self.steps = [_tool_use(), _tool_result()] if steps is None else steps
        self.fail = fail
        self.messages: list[Any] = list(history or [])
        self.cleaned_up = False

    async def invoke_async(self, prompt, *, invocation_state=None):
        self.messages.extend(prompt)
        self.messages.extend(self.steps)
        if self.fail is not None:
            raise self.fail
        result = _result()
        self.messages.append(result.message)
        return result

    async def stream_async(self, prompt, *, invocation_state=None):
        self.messages.extend(prompt)
        self.messages.extend(self.steps)
        yield {"data": "Tenés"}
        result = _result()
        self.messages.append(result.message)
        yield {"result": result}

    def cleanup(self) -> None:
        self.cleaned_up = True


class _Engine:
    def __init__(self, agent: Any = None, *, build_error: Exception | None = None) -> None:
        self.agent = agent if agent is not None else _Agent()
        self.build_error = build_error

    def build_agent(self, name, session, *, model_params=None):
        if self.build_error is not None:
            raise self.build_error
        self.agent.agent_id = name
        return self.agent

    def build_workflow(self, name, session, *, model_params=None):
        async def invoke(prompt, *, invocation_state=None):
            return SimpleNamespace(
                status="COMPLETED",
                results={},
                execution_order=[],
                accumulated_usage={"inputTokens": 1, "outputTokens": 2, "totalTokens": 3},
                accumulated_metrics={"latencyMs": 5},
                execution_time=7,
                interrupts=[],
            )

        return SimpleNamespace(invoke_async=invoke), [self.agent]

    def discover(self):
        return [SimpleNamespace(name="assistant", kind="agent")]


class _Transcripts:
    def __init__(self) -> None:
        self.records: list[TurnTranscript] = []

    def on_transcript(self, transcript: TurnTranscript) -> None:
        self.records.append(transcript)


class _Turns:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def on_turn(self, event) -> None:
        self.events.append(event)


def _run_agent(engine=None, input: Any = "¿Cuáles son mis pedidos?", observers=None, **kw):
    return asyncio.run(
        _run.run_agent(
            engine or _Engine(),
            "assistant",
            input,
            SESSION,
            observers=observers if observers is not None else [],
            turn_id=kw.pop("turn_id", "t1"),
            **kw,
        )
    )


@pytest.fixture
def rec() -> _Transcripts:
    return _Transcripts()


class TestRunAgent:
    def test_ok_with_a_tool(self, rec):
        reply = _run_agent(observers=[rec])

        (record,) = rec.records
        assert (record.turn_id, record.session_id, record.user_id) == ("t1", "s-1", "u-1")
        assert (record.target, record.operation, record.outcome) == ("assistant", "run", "ok")
        assert record.question == "¿Cuáles son mis pedidos?"
        assert record.answer == reply.text == "Tenés dos pedidos abiertos."
        assert record.tool_calls == (
            ToolCall(
                tool_use_id="tu-1",
                name="search_orders",
                input={"customer_id": 1},
                status="success",
                result=[{"json": {"orders": [1, 2]}}],
            ),
        )
        assert record.usage == reply.usage
        assert record.started_at.tzinfo is UTC
        assert record.duration_ms >= 0
        assert record.error_type is None and record.replaces is None

    def test_build_error(self, rec):
        engine = _Engine(build_error=LoomError("bad yaml"))
        with pytest.raises(LoomError):
            _run_agent(engine, observers=[rec])

        (record,) = rec.records
        assert (record.outcome, record.error_type) == ("error", "LoomError")
        assert record.question == "¿Cuáles son mis pedidos?"
        assert record.tool_calls == () and record.answer is None

    def test_model_error_after_a_tool(self, rec):
        engine = _Engine(_Agent(steps=[_tool_use()], fail=RuntimeError("throttled")))
        with pytest.raises(HarnessError):
            _run_agent(engine, observers=[rec])

        (record,) = rec.records
        assert (record.outcome, record.error_type, record.answer) == ("error", "RuntimeError", None)
        (call,) = record.tool_calls
        assert (call.name, call.status, call.result) == ("search_orders", None, None)
        assert engine.agent.cleaned_up

    def test_only_the_messages_of_this_turn(self, rec):
        old_turn = {
            "role": "user",
            "content": [{"text": "antes"}],
            "metadata": {"custom": {TURN_ID_KEY: "t0"}},
        }
        history = [old_turn, _tool_use("tu-0", "old_tool"), _tool_result("tu-0"), _result().message]
        _run_agent(_Engine(_Agent(history=history)), observers=[rec])

        assert [call.tool_use_id for call in rec.records[0].tool_calls] == ["tu-1"]

    def test_an_image_is_an_attachment_without_bytes(self, rec):
        blocks = [
            {"text": "¿Qué dice este documento?"},
            {"image": {"format": "png", "source": {"bytes": b"\x89PNG-secret"}}},
        ]
        _run_agent(input=blocks, observers=[rec])

        (record,) = rec.records
        assert record.question == "¿Qué dice este documento?"
        assert record.attachments == ("image/png",)
        assert "PNG-secret" not in record.model_dump_json()


class TestOtherUseCases:
    def test_stream(self, rec):
        async def go():
            stream = _run.stream_agent(
                _Engine(), "assistant", "hola", SESSION, observers=[rec], turn_id="t1"
            )
            async for _ in stream:
                pass

        asyncio.run(go())

        (record,) = rec.records
        assert (record.operation, record.outcome, record.question) == ("stream", "ok", "hola")
        assert [call.name for call in record.tool_calls] == ["search_orders"]

    def test_stream_closed_before_done_is_cancelled(self, rec):
        async def go():
            stream = _run.stream_agent(
                _Engine(), "assistant", "hola", SESSION, observers=[rec], turn_id="t1"
            )
            async for _ in stream:
                await stream.aclose()
                return

        asyncio.run(go())

        (record,) = rec.records
        assert (record.outcome, record.answer) == ("cancelled", None)

    def test_regenerate_records_the_new_turn_only(self, rec):
        old_turn = {
            "role": "user",
            "content": [{"text": "pregunta"}],
            "metadata": {"custom": {TURN_ID_KEY: "t1"}},
        }
        store = SimpleNamespace(truncate=lambda *a, **k: None, turn_start=lambda *a, **k: 0)
        agent = _Agent(history=[old_turn, _tool_use("tu-0"), _tool_result("tu-0")])
        asyncio.run(
            _run.regenerate(
                _Engine(agent),
                store,
                "assistant",
                "pregunta",
                SESSION,
                kind="agent",
                replaces="t1",
                turn_id="t2",
                observers=[rec],
            )
        )

        (record,) = rec.records
        assert (record.operation, record.turn_id, record.replaces) == ("regenerate", "t2", "t1")
        assert [call.tool_use_id for call in record.tool_calls] == ["tu-1"]

    def test_fork_and_fork_stateless(self, rec):
        store = SimpleNamespace(truncate=lambda *a, **k: None)
        asyncio.run(
            _run.fork(_Engine(), store, "assistant", "hola", SESSION, at=0, observers=[rec])
        )
        asyncio.run(
            _run.fork_stateless(_Engine(), "assistant", [], "hola", SESSION, observers=[rec])
        )

        assert [(r.operation, r.outcome) for r in rec.records] == [("fork", "ok"), ("fork", "ok")]

    def test_a_workflow_has_no_transcript(self, rec):
        asyncio.run(_run.run_workflow(_Engine(), "flujo", "hola", SESSION, observers=[rec]))

        assert rec.records == []


class TestObserverRules:
    def test_without_transcript_observers_messages_are_not_read(self):
        class Untouchable(_Agent):
            @property
            def messages(self):  # type: ignore[override]
                raise AssertionError("messages read without a transcript observer")

            @messages.setter
            def messages(self, value):
                pass

            async def invoke_async(self, prompt, *, invocation_state=None):
                return _result()

        turns = _Turns()
        _run_agent(_Engine(Untouchable()), observers=[turns])

        assert len(turns.events) == 1

    def test_unreadable_messages_still_emit_the_record(self, rec, caplog):
        class Broken(_Agent):
            async def invoke_async(self, prompt, *, invocation_state=None):
                self.messages = None  # not a list: the capture fails
                return _result()

        with caplog.at_level(logging.WARNING, logger="weave.application.observe"):
            _run_agent(_Engine(Broken()), observers=[rec])

        (record,) = rec.records
        assert record.tool_calls == () and record.answer == "Tenés dos pedidos abiertos."
        assert "transcript" in caplog.text

    def test_a_failing_transcript_observer_changes_nothing(self, caplog):
        class Boom:
            def on_transcript(self, transcript):
                raise ValueError("s3 down")

        turns = _Turns()
        with caplog.at_level(logging.WARNING, logger="weave.application.observe"):
            reply = _run_agent(observers=[Boom(), turns])

        assert reply.text == "Tenés dos pedidos abiertos."
        assert len(turns.events) == 1
        assert "Boom" in caplog.text

    def test_each_observer_hears_only_its_method(self, rec):
        turns = _Turns()

        class Both(_Turns, _Transcripts):
            def __init__(self) -> None:
                _Turns.__init__(self)
                _Transcripts.__init__(self)

        both = Both()
        _run_agent(observers=[rec, turns, both])

        assert len(rec.records) == len(turns.events) == 1
        assert len(both.records) == len(both.events) == 1

    def test_emf_has_no_on_transcript(self):
        assert not hasattr(weave.EmfTurnObserver("A/B"), "on_transcript")


class TestConfigure:
    def test_a_transcript_observer_reaches_the_wired_use_cases(self, rec):
        try:
            weave.configure(engine=_Engine(), observers=[rec])
            asyncio.run(weave.run_agent("assistant", "hola", SESSION))
        finally:
            weave.configure()

        (record,) = rec.records
        assert record.question == "hola"


class TestHelpers:
    def test_question_of_each_input_shape(self):
        assert question_of("hola") == ("hola", ())
        assert question_of([{"text": "a"}, {"text": "b"}]) == ("a\nb", ())
        messages = [
            {"role": "assistant", "content": [{"text": "no"}]},
            {"role": "user", "content": [{"text": "sí"}, {"document": {"format": "pdf"}}]},
        ]
        assert question_of(messages) == ("sí", ("document/pdf",))
        assert question_of([{"interruptResponse": {"interruptId": "i", "response": 1}}]) == ("", ())
        assert question_of(None) == ("", ())

    def test_turn_messages_without_stamp_is_empty(self):
        assert turn_messages([{"role": "user", "content": [{"text": "x"}]}], "t1") == []

    def test_tool_calls_of_an_error_result(self):
        (call,) = tool_calls_of([_tool_use(), _tool_result(status="error")])
        assert call.status == "error"


class TestWithLoom:
    def test_the_stamp_survives_in_the_agent_messages(self, file_engine, rec):
        """W15 stamps the input; Strands keeps it in ``agent.messages``, which ``capture`` reads.

        Two turns over the same session: the second one's messages start at its own
        question, not at the history the agent re-loaded from the first.
        """
        loom_engine, _ = file_engine
        seen: list[list[Any]] = []

        class SpyEngine:
            def build_agent(self, name, session, *, model_params=None):
                return loom_engine.build_agent(name, session, model_params=model_params)

            def describe(self, agent):  # called before the teardown, like capture
                seen.append(turn_messages(agent.messages, f"t{len(seen) + 1}"))

        for turn_id, text in (("t1", "uno"), ("t2", "dos")):
            asyncio.run(
                _run.run_agent(
                    SpyEngine(), "assistant", text, SESSION, turn_id=turn_id, observers=[rec]
                )
            )

        assert [m[0]["content"][0]["text"] for m in seen] == ["uno", "dos"]
        assert [len(m) for m in seen] == [2, 2]  # question + answer, without the history
        assert [r.answer is not None for r in rec.records] == [True, True]
