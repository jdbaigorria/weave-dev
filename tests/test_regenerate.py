"""W16/W17 — regenerate the last turn by its turn_id, over files and over S3."""

from __future__ import annotations

import asyncio
import io
import json
from functools import partial
from typing import Any

import pytest
from loom import LoomSession
from strands.types.exceptions import SessionException
from strands.types.session import SessionMessage

from weave.adapters.driven.emf import EmfTurnObserver
from weave.adapters.driven.mutable_file_session import MutableFileSessionManager
from weave.adapters.driven.s3_session_store import S3SessionStore
from weave.adapters.driving.chat import ChatChannel, ChatRequest
from weave.adapters.driving.lambda_handler import lambda_handler
from weave.application import run as _run
from weave.application.errors import HarnessError, RequestError
from weave.application.turn_id import TURN_ID_KEY, turn_id_of
from weave.ports.session import MutableSession, RegenerableSession

SESSION = LoomSession(session_id="s1", user_id="u1")


class _Recorder:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def on_turn(self, event) -> None:
        self.events.append(event)


def _two_turns(engine) -> None:
    asyncio.run(_run.run_agent(engine, "assistant", "pregunta 1", SESSION, turn_id="t1"))
    asyncio.run(_run.run_agent(engine, "assistant", "pregunta 2", SESSION, turn_id="t2"))


def _history(store) -> list[tuple[str | None, str]]:
    return [
        (turn_id_of(m.message), m.message["content"][0]["text"])
        for m in store.list_messages("s1", "assistant")
    ]


def _regenerate(engine, store, **kw):
    return asyncio.run(
        _run.regenerate(
            engine, store, "assistant", kw.pop("input", "pregunta 2"), SESSION, kind="agent", **kw
        )
    )


class TestWithLoom:
    def test_overwrites_the_last_turn(self, file_engine):
        engine, store_for = file_engine
        _two_turns(engine)

        reply = _regenerate(engine, store_for("s1"), replaces="t2", turn_id="t3")

        history = _history(store_for("s1"))
        assert [h[0] for h in history] == ["t1", None, "t3", None]
        assert history[2][1] == "pregunta 2"
        assert (reply.turn_id, reply.replaces) == ("t3", "t2")

    def test_editing_is_regenerating_with_another_text(self, file_engine):
        engine, store_for = file_engine
        _two_turns(engine)

        _regenerate(engine, store_for("s1"), replaces="t2", turn_id="t3", input="otra cosa")

        assert _history(store_for("s1"))[2] == ("t3", "otra cosa")

    def test_only_the_last_turn(self, file_engine):
        engine, store_for = file_engine
        _two_turns(engine)
        before = _history(store_for("s1"))

        with pytest.raises(RequestError, match="not the last turn"):
            _regenerate(engine, store_for("s1"), replaces="t1", turn_id="t3")
        with pytest.raises(RequestError, match="not the last turn"):
            _regenerate(engine, store_for("s1"), replaces="nope", turn_id="t3")
        assert _history(store_for("s1")) == before

    def test_needs_a_new_turn_id(self, file_engine):
        engine, store_for = file_engine
        _two_turns(engine)
        with pytest.raises(RequestError, match="new turn_id"):
            _regenerate(engine, store_for("s1"), replaces="t2", turn_id="t2")

    def test_observers_hear_a_regenerate_and_emf_counts_it(self, file_engine):
        engine, store_for = file_engine
        _two_turns(engine)
        rec = _Recorder()

        _regenerate(engine, store_for("s1"), replaces="t2", turn_id="t3", observers=[rec])

        event = rec.events[0]
        assert (event.operation, event.turn_id, event.replaces) == ("regenerate", "t3", "t2")
        out = io.StringIO()
        EmfTurnObserver("A", stream=out).on_turn(event)
        doc = json.loads(out.getvalue())
        assert (doc["Regenerations"], doc["replaces"]) == (1, "t2")

    def test_a_rejected_request_is_not_a_turn(self, file_engine):
        engine, store_for = file_engine
        _two_turns(engine)
        rec = _Recorder()
        with pytest.raises(RequestError):
            _regenerate(engine, store_for("s1"), replaces="t1", observers=[rec])
        assert rec.events == []

    def test_only_agents(self, file_engine):
        engine, store_for = file_engine
        with pytest.raises(RequestError, match="not an agent"):
            asyncio.run(
                _run.regenerate(
                    engine, store_for("s1"), "flow", "q", SESSION, replaces="t1", kind="workflow"
                )
            )


class TestChannel:
    def _handler(self, engine, store):
        regenerator = partial(_run.regenerate, engine)

        async def runner(name, input, session, *, kind=None, extras=None, turn_id=None):
            return await _run.run_agent(engine, name, input, session, turn_id=turn_id)

        channel = ChatChannel(
            "assistant", runner, kind="agent", session_store=store, regenerator=regenerator
        )
        return lambda_handler(channel)

    def test_replaces_regenerates_through_lambda(self, file_engine):
        engine, store_for = file_engine
        _two_turns(engine)
        handler = self._handler(engine, store_for("s1"))

        body = {
            "message": "pregunta 2",
            "session_id": "s1",
            "user_id": "u1",
            "turn_id": "t3",
            "replaces": "t2",
        }
        response = handler({"body": json.dumps(body)})

        assert response["statusCode"] == 200
        assert json.loads(response["body"])["replaces"] == "t2"

    def test_a_channel_without_store_rejects_replaces(self, file_engine):
        engine, _ = file_engine
        handler = self._handler(engine, None)
        body = {"message": "q", "session_id": "s1", "replaces": "t2"}

        assert handler({"body": json.dumps(body)})["statusCode"] == 400

    def test_streaming_a_regeneration_is_rejected(self, file_engine):
        engine, store_for = file_engine
        channel = ChatChannel(
            "assistant",
            None,
            streamer=lambda *a, **k: None,
            session_store=store_for("s1"),
            regenerator=partial(_run.regenerate, engine),
        )
        with pytest.raises(RequestError):
            channel.stream(ChatRequest(message="q", session_id="s1", replaces="t2"))


# --- turn_start over files (seeded histories) -----------------------------------


def _seed(mgr: MutableFileSessionManager, messages: list[dict[str, Any]]) -> None:
    import os

    for index, message in enumerate(messages):
        path = mgr._get_message_path("s1", "assistant", index)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mgr._write_file(path, SessionMessage.from_message(message, index).to_dict())


def _user(text: str, turn: str | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "user", "content": [{"text": text}]}
    if turn:
        message["metadata"] = {"custom": {TURN_ID_KEY: turn}}
    return message


def _assistant(text: str) -> dict[str, Any]:
    return {"role": "assistant", "content": [{"text": text}]}


TOOL_TURN = [
    _user("q1", "t1"),
    _assistant("a1"),
    _user("q2", "t2"),
    {"role": "assistant", "content": [{"toolUse": {"toolUseId": "x", "name": "f", "input": {}}}]},
    {"role": "user", "content": [{"toolResult": {"toolUseId": "x", "content": [{"text": "r"}]}}]},
    _assistant("a2"),
]


class TestFileTurnStart:
    def test_a_turn_with_tools_starts_at_its_user_message(self, tmp_path):
        mgr = MutableFileSessionManager(session_id="s1", storage_dir=str(tmp_path))
        _seed(mgr, TOOL_TURN)

        assert mgr.turn_start("s1", agent_id="assistant", turn_id="t2") == 2
        assert mgr.turn_start("s1", agent_id="assistant", turn_id="t1") is None

        mgr.truncate("s1", keep=2, agent_id="assistant")
        assert len(mgr.list_messages("s1", "assistant")) == 2

    def test_a_history_without_stamps_has_no_turns(self, tmp_path):
        mgr = MutableFileSessionManager(session_id="s1", storage_dir=str(tmp_path))
        _seed(mgr, [_user("viejo"), _assistant("r")])

        assert mgr.turn_start("s1", agent_id="assistant", turn_id="t1") is None

    def test_is_a_regenerable_session(self, tmp_path):
        mgr = MutableFileSessionManager(session_id="s1", storage_dir=str(tmp_path))
        assert isinstance(mgr, RegenerableSession)

    def test_a_fork_only_store_is_still_a_mutable_session(self):
        class ForkOnly:
            def truncate(self, session_id, *, keep, agent_id):
                pass

        assert isinstance(ForkOnly(), MutableSession)
        assert not isinstance(ForkOnly(), RegenerableSession)
        with pytest.raises(HarnessError, match="RegenerableSession"):
            asyncio.run(
                _run.regenerate(None, ForkOnly(), "a", "q", SESSION, replaces="t1", kind="agent")
            )


# --- S3SessionStore with a fake client ------------------------------------------


class _Body:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data


class _FakeS3:
    def __init__(self, objects: dict[str, dict[str, Any]], *, delete_errors=None) -> None:
        self.objects = {k: json.dumps(v).encode() for k, v in objects.items()}
        self.gets: list[str] = []
        self.lists = 0
        self.deleted: list[str] = []
        self.delete_errors = delete_errors

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        fake = self

        class _Paginator:
            def paginate(self, **kw):
                fake.lists += 1
                keys = sorted(k for k in fake.objects if k.startswith(kw["Prefix"]))
                yield {"Contents": [{"Key": k} for k in keys]}

        return _Paginator()

    def get_object(self, **kw):
        self.gets.append(kw["Key"])
        return {"Body": _Body(self.objects[kw["Key"]])}

    def delete_objects(self, **kw):
        keys = [o["Key"] for o in kw["Delete"]["Objects"]]
        self.deleted += keys
        for key in keys:
            self.objects.pop(key, None)
        return {"Errors": self.delete_errors} if self.delete_errors else {}


def _s3_store(messages, **kw) -> tuple[S3SessionStore, _FakeS3]:
    store = S3SessionStore("bucket", "session/", client=object())
    objects = {
        store._get_message_path("s1", "assistant", i): SessionMessage.from_message(m, i).to_dict()
        for i, m in enumerate(messages)
    }
    fake = _FakeS3(objects, **kw)
    store.client = fake
    return store, fake


class TestS3Store:
    def test_turn_start_lists_once_and_stops_at_the_stamp(self):
        store, fake = _s3_store(TOOL_TURN)

        assert store.turn_start("s1", agent_id="assistant", turn_id="t2") == 2
        assert fake.lists == 1
        assert len(fake.gets) == 4  # messages 5, 4, 3 and the stamped 2 — never 1 or 0

    def test_keys_follow_strands_layout(self):
        store, _ = _s3_store([])
        assert store._get_message_path("s1", "assistant", 3) == (
            "session/session_s1/agents/agent_assistant/messages/message_3.json"
        )

    def test_truncate_deletes_from_keep(self):
        store, fake = _s3_store(TOOL_TURN)

        store.truncate("s1", keep=2, agent_id="assistant")

        assert sorted(k.rsplit("_", 1)[-1] for k in fake.deleted) == [
            "2.json",
            "3.json",
            "4.json",
            "5.json",
        ]

    def test_a_failed_delete_raises(self):
        store, _ = _s3_store(TOOL_TURN, delete_errors=[{"Key": "k", "Code": "AccessDenied"}])
        with pytest.raises(SessionException):
            store.truncate("s1", keep=0, agent_id="assistant")

    def test_is_a_regenerable_session_and_creates_no_session(self):
        store = S3SessionStore("bucket", client=object())
        assert isinstance(store, RegenerableSession)
