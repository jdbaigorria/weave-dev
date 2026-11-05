"""W23 — S3TranscriptObserver: one JSON object per agent turn, partitioned by day."""

from __future__ import annotations

import asyncio
import builtins
import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from loom import LoomSession

from weave.adapters.driven.s3_transcripts import S3TranscriptObserver
from weave.application import run as _run
from weave.application.errors import HarnessError
from weave.application.observe import TurnTranscript
from weave.application.transcript import ToolCall


class _FakeS3:
    def __init__(self, error: Exception | None = None) -> None:
        self.objects: dict[str, dict[str, Any]] = {}
        self.error = error

    def put_object(self, **kwargs: Any) -> None:
        if self.error is not None:
            raise self.error
        self.objects[kwargs["Key"]] = kwargs


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def _transcript(**kw: Any) -> TurnTranscript:
    base: dict[str, Any] = dict(
        turn_id="t1",
        session_id="s-1",
        user_id="u-1",
        is_anonymous=False,
        target="assistant",
        operation="run",
        outcome="ok",
        started_at=datetime(2026, 11, 4, 23, 59, 30, tzinfo=UTC),
        duration_ms=1200.0,
        question="¿Cuáles son mis pedidos?",
        answer="Dos.",
        tool_calls=(
            ToolCall(
                tool_use_id="tu-1",
                name="search_orders",
                input={"customer_id": 1},
                status="success",
                result=[{"json": {"customer_id": 1, "phone": "555-0100"}}],
            ),
        ),
    )
    return TurnTranscript(**{**base, **kw})


class TestKey:
    def test_day_partition_in_utc(self):
        s3 = _FakeS3()
        observer = S3TranscriptObserver("bucket", client=s3)

        observer.on_transcript(_transcript())
        observer.on_transcript(
            _transcript(turn_id="t2", started_at=datetime(2026, 11, 5, 0, 1, tzinfo=UTC))
        )

        assert sorted(s3.objects) == [
            "turns/dt=2026-11-04/s-1/t1.json",
            "turns/dt=2026-11-05/s-1/t2.json",
        ]

    def test_custom_prefix(self):
        s3 = _FakeS3()
        S3TranscriptObserver("bucket", prefix="audit/turns/", client=s3).on_transcript(
            _transcript()
        )

        assert list(s3.objects) == ["audit/turns/dt=2026-11-04/s-1/t1.json"]


class TestBody:
    def test_round_trip_without_tool_results(self):
        s3 = _FakeS3()
        S3TranscriptObserver("bucket", client=s3).on_transcript(_transcript())

        (put,) = s3.objects.values()
        assert (put["Bucket"], put["ContentType"]) == ("bucket", "application/json")
        stored = TurnTranscript.model_validate_json(put["Body"])
        assert stored.question == "¿Cuáles son mis pedidos?"
        (call,) = stored.tool_calls
        assert (call.name, call.input, call.status, call.result) == (
            "search_orders",
            {"customer_id": 1},
            "success",
            None,
        )
        assert "555-0100" not in put["Body"].decode("utf-8")

    def test_with_tool_results(self):
        s3 = _FakeS3()
        S3TranscriptObserver("bucket", include_tool_results=True, client=s3).on_transcript(
            _transcript()
        )

        (put,) = s3.objects.values()
        body = json.loads(put["Body"])
        assert body["tool_calls"][0]["result"] == [
            {"json": {"customer_id": 1, "phone": "555-0100"}}
        ]

    def test_policy_decides_per_call(self):
        """W24: tool_result keeps what it returns for each call, and drops it on None."""
        s3 = _FakeS3()
        transcript = _transcript(
            tool_calls=(
                ToolCall(
                    tool_use_id="tu-1",
                    name="search_docs",
                    input={"query": "refunds"},
                    status="success",
                    result=[{"text": "long passage with 555-0100"}],
                ),
                ToolCall(
                    tool_use_id="tu-2",
                    name="search_orders",
                    input={"customer_id": 1},
                    status="success",
                    result=[{"json": {"phone": "555-0100"}}],
                ),
            )
        )

        def policy(call: ToolCall):
            if call.name == "search_docs":
                return [{"json": {"sources": ["s3://docs/refunds.pdf"]}}]
            return None

        S3TranscriptObserver("bucket", tool_result=policy, client=s3).on_transcript(transcript)

        (put,) = s3.objects.values()
        body = json.loads(put["Body"])
        assert [c["result"] for c in body["tool_calls"]] == [
            [{"json": {"sources": ["s3://docs/refunds.pdf"]}}],
            None,
        ]
        assert [c["input"] for c in body["tool_calls"]] == [
            {"query": "refunds"},
            {"customer_id": 1},
        ]
        assert "555-0100" not in put["Body"].decode("utf-8")

    def test_policy_sees_the_whole_call(self):
        seen: list[ToolCall] = []
        S3TranscriptObserver(
            "bucket", tool_result=lambda c: seen.append(c), client=_FakeS3()
        ).on_transcript(_transcript())

        (call,) = seen
        assert (call.name, call.input, call.status, call.result) == (
            "search_orders",
            {"customer_id": 1},
            "success",
            [{"json": {"customer_id": 1, "phone": "555-0100"}}],
        )

    def test_policy_that_raises_drops_that_result_only(self, caplog):
        s3 = _FakeS3()
        transcript = _transcript(
            tool_calls=(
                ToolCall(
                    tool_use_id="tu-1", name="broken", status="success", result=[{"text": "a"}]
                ),
                ToolCall(tool_use_id="tu-2", name="fine", status="success", result=[{"text": "b"}]),
            )
        )

        def policy(call: ToolCall):
            if call.name == "broken":
                raise KeyError("boom")
            return call.result

        with caplog.at_level(logging.WARNING, logger="weave.adapters.driven.s3_transcripts"):
            S3TranscriptObserver("bucket", tool_result=policy, client=s3).on_transcript(transcript)

        (put,) = s3.objects.values()
        body = json.loads(put["Body"])
        assert [c["result"] for c in body["tool_calls"]] == [None, [{"text": "b"}]]
        assert "'broken'" in caplog.text

    @pytest.mark.parametrize("wrong", [{"sources": []}, "text", [1, 2]])
    def test_policy_with_the_wrong_shape_drops_the_result(self, wrong, caplog):
        s3 = _FakeS3()
        with caplog.at_level(logging.WARNING, logger="weave.adapters.driven.s3_transcripts"):
            S3TranscriptObserver("bucket", tool_result=lambda c: wrong, client=s3).on_transcript(
                _transcript()
            )

        (put,) = s3.objects.values()
        assert json.loads(put["Body"])["tool_calls"][0]["result"] is None
        assert "did not return a list of blocks" in caplog.text

    def test_policy_and_include_tool_results_exclude_each_other(self):
        with pytest.raises(ValueError, match="not both"):
            S3TranscriptObserver(
                "bucket", include_tool_results=True, tool_result=lambda c: None, client=_FakeS3()
            )

    def test_utf8(self):
        s3 = _FakeS3()
        S3TranscriptObserver("bucket", client=s3).on_transcript(_transcript())

        (put,) = s3.objects.values()
        assert "¿Cuáles son mis pedidos?" in put["Body"].decode("utf-8")


class TestInTheTurn:
    def test_access_denied_does_not_break_the_turn(self, caplog):
        from tests.test_turn_transcript import _Engine

        observer = S3TranscriptObserver("bucket", client=_FakeS3(_ClientError("AccessDenied")))
        with caplog.at_level(logging.WARNING, logger="weave.application.observe"):
            reply = asyncio.run(
                _run.run_agent(
                    _Engine(),
                    "assistant",
                    "hola",
                    LoomSession(session_id="s-1", user_id="u-1"),
                    observers=[observer],
                )
            )

        assert reply.text == "Tenés dos pedidos abiertos."
        assert "S3TranscriptObserver" in caplog.text

    def test_a_regenerated_turn_does_not_overwrite_the_replaced_one(self):
        s3 = _FakeS3()
        observer = S3TranscriptObserver("bucket", client=s3)

        observer.on_transcript(_transcript(turn_id="t1"))
        observer.on_transcript(
            _transcript(turn_id="t2", operation="regenerate", replaces="t1", answer="Otra.")
        )

        assert len(s3.objects) == 2


class TestBoto3:
    def test_without_boto3(self, monkeypatch):
        real_import = builtins.__import__

        def no_boto3(name, *args, **kwargs):
            if name == "boto3":
                raise ImportError("no boto3")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", no_boto3)
        with pytest.raises(HarnessError, match=r"weave\[aws\]"):
            S3TranscriptObserver("bucket")
