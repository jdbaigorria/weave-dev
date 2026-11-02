"""W15 — the turn id, end to end: request → stamped history → event/EMF → reply."""

from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace
from typing import Any, ClassVar

import pytest
from loom import LoomSession
from loom.testing import MockModel

from tests.conftest import FileLoomEngine
from weave.adapters.driven.emf import EmfTurnObserver
from weave.adapters.driven.mutable_file_session import MutableFileSessionManager
from weave.adapters.driving.chat import ChatChannel, ChatRequest
from weave.adapters.driving.lambda_handler import lambda_handler
from weave.application.errors import RequestError
from weave.application.run import run_agent, run_workflow, stream_agent
from weave.application.turn_id import TURN_ID_KEY, mark_input, turn_id_of


def _result() -> SimpleNamespace:
    return SimpleNamespace(
        message={"content": [{"text": "ok"}]},
        structured_output=None,
        stop_reason="end_turn",
        interrupts=[],
        metrics=SimpleNamespace(get_summary=lambda: {}),
    )


class _Agent:
    def __init__(self) -> None:
        self.prompt: Any = None
        self.messages: list[Any] = []

    async def invoke_async(self, prompt, *, invocation_state=None):
        self.prompt = prompt
        return _result()

    async def stream_async(self, prompt, *, invocation_state=None):
        self.prompt = prompt
        yield {"data": "o"}
        yield {"result": _result()}

    def cleanup(self) -> None:
        pass


class _Engine:
    def __init__(self, *, build_error: Exception | None = None) -> None:
        self.agent = _Agent()
        self.build_error = build_error

    def build_agent(self, name, session, *, model_params=None):
        if self.build_error is not None:
            raise self.build_error
        return self.agent

    def build_workflow(self, name, session, *, model_params=None):
        workflow = SimpleNamespace(invoke_async=self._workflow_invoke)
        return workflow, []

    async def _workflow_invoke(self, task, invocation_state=None):
        self.workflow_task = task
        return SimpleNamespace(
            status=SimpleNamespace(name="COMPLETED"),
            results={},
            accumulated_usage={},
            accumulated_metrics={},
            interrupts=[],
        )


class _Recorder:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def on_turn(self, event) -> None:
        self.events.append(event)


def _session() -> LoomSession:
    return LoomSession(session_id="s-1", user_id="u-1")


class TestUseCases:
    def test_minted_when_absent(self):
        rec = _Recorder()
        reply = asyncio.run(run_agent(_Engine(), "support", "hi", _session(), observers=[rec]))

        assert len(reply.turn_id) == 32
        int(reply.turn_id, 16)
        assert rec.events[0].turn_id == reply.turn_id

    def test_the_callers_id_is_kept_and_stamped(self):
        engine, rec = _Engine(), _Recorder()
        reply = asyncio.run(
            run_agent(engine, "support", "hi", _session(), observers=[rec], turn_id="m-1")
        )

        assert reply.turn_id == rec.events[0].turn_id == "m-1"
        assert engine.agent.prompt == [
            {
                "role": "user",
                "content": [{"text": "hi"}],
                "metadata": {"custom": {TURN_ID_KEY: "m-1"}},
            }
        ]

    @pytest.mark.parametrize("bad", ["a b", "x" * 65, "", "ñ"])
    def test_a_malformed_id_is_a_request_error(self, bad):
        with pytest.raises(RequestError):
            asyncio.run(run_agent(_Engine(), "support", "hi", _session(), turn_id=bad))

    def test_a_failed_build_still_reports_the_id(self):
        rec = _Recorder()
        with pytest.raises(RuntimeError):
            asyncio.run(
                run_agent(
                    _Engine(build_error=RuntimeError("yaml")),
                    "support",
                    "hi",
                    _session(),
                    observers=[rec],
                    turn_id="m-2",
                )
            )
        assert (rec.events[0].outcome, rec.events[0].turn_id) == ("error", "m-2")

    def test_stream_done_carries_the_id(self):
        async def consume():
            return [
                e async for e in stream_agent(_Engine(), "support", "hi", _session(), turn_id="m-3")
            ]

        events = asyncio.run(consume())
        assert events[-1].reply.turn_id == "m-3"

    def test_a_cancelled_stream_reports_the_id(self):
        rec = _Recorder()

        async def abandon():
            stream = stream_agent(
                _Engine(), "support", "hi", _session(), observers=[rec], turn_id="m-4"
            )
            await stream.__anext__()
            await stream.aclose()

        asyncio.run(abandon())
        assert (rec.events[0].outcome, rec.events[0].turn_id) == ("cancelled", "m-4")

    def test_workflows_report_the_id_without_stamping(self):
        engine = _Engine()
        reply = asyncio.run(run_workflow(engine, "flow", "hi", _session(), turn_id="m-5"))

        assert reply.turn_id == "m-5"
        assert engine.workflow_task == "hi"


class TestMarkInput:
    def test_content_blocks_become_one_stamped_message(self):
        blocks = [{"text": "a"}, {"image": {"format": "png", "source": {"bytes": b""}}}]
        (message,) = mark_input(blocks, "t")
        assert message["content"] == blocks
        assert turn_id_of(message) == "t"

    def test_messages_stamp_the_first_user_one_without_mutating(self):
        original = [
            {"role": "assistant", "content": [{"text": "hola"}]},
            {"role": "user", "content": [{"text": "q"}], "metadata": {"custom": {"k": 1}}},
        ]
        snapshot = json.dumps(original)
        marked = mark_input(original, "t")

        assert json.dumps(original) == snapshot
        assert turn_id_of(marked[0]) is None
        assert marked[1]["metadata"]["custom"] == {"k": 1, TURN_ID_KEY: "t"}

    def test_interrupt_responses_pass_through(self):
        responses = [{"interruptResponse": {"interruptId": "i", "response": "yes"}}]
        assert mark_input(responses, "t") is responses

    def test_none_passes_through(self):
        assert mark_input(None, "t") is None


class CapturingModel(MockModel):
    """MockModel that remembers what the agent sent to the model."""

    seen: ClassVar[list[Any]] = []

    async def stream(self, messages, *args, **kwargs):
        CapturingModel.seen.append(messages)
        async for event in super().stream(messages, *args, **kwargs):
            yield event


def test_the_stamp_is_persisted_and_never_reaches_the_model(tmp_path):
    CapturingModel.seen.clear()
    engine = FileLoomEngine(str(tmp_path), model="tests.test_turn_id.CapturingModel")

    asyncio.run(run_agent(engine, "assistant", "hola", _session(), turn_id="m-9"))

    store = MutableFileSessionManager(session_id="s-1", storage_dir=str(tmp_path))
    first = store.list_messages("s-1", "assistant")[0].message
    assert turn_id_of(first) == "m-9"
    assert all("metadata" not in m for m in CapturingModel.seen[0])


class TestEdges:
    def test_emf_carries_the_id_as_a_property(self):
        rec = _Recorder()
        asyncio.run(run_agent(_Engine(), "support", "hi", _session(), observers=[rec], turn_id="m-6"))
        out = io.StringIO()
        EmfTurnObserver("A", stream=out).on_turn(rec.events[0])
        doc = json.loads(out.getvalue())

        assert doc["turn_id"] == "m-6"
        assert all(
            "turn_id" not in dims for dims in doc["_aws"]["CloudWatchMetrics"][0]["Dimensions"]
        )

    def _handler(self):
        async def runner(name, input, session, *, kind=None, extras=None, turn_id=None):
            return await run_agent(_Engine(), name, input, session, turn_id=turn_id)

        return lambda_handler(ChatChannel("support", runner))

    def test_lambda_returns_the_callers_id(self):
        body = {"message": "hi", "session_id": "s", "turn_id": "m-7"}
        response = self._handler()({"body": json.dumps(body)})

        assert response["statusCode"] == 200
        assert json.loads(response["body"])["turn_id"] == "m-7"

    def test_lambda_rejects_a_malformed_id(self):
        body = {"message": "hi", "session_id": "s", "turn_id": "no válido"}
        response = self._handler()({"body": json.dumps(body)})

        assert response["statusCode"] == 400

    def test_a_runner_without_turn_id_still_works_when_none_is_sent(self):
        async def old_runner(name, input, session, *, kind=None, extras=None):
            return await run_agent(_Engine(), name, input, session)

        reply = asyncio.run(
            ChatChannel("support", old_runner).handle(ChatRequest(message="hi", session_id="s"))
        )
        assert reply.turn_id
