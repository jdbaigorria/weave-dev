"""W18–W21 — user feedback: merge in two steps, stores, EMF and the Lambda handler."""

from __future__ import annotations

import io
import json
import logging
from typing import Any

import pytest
from loom import LoomSession
from pydantic import ValidationError

from weave.adapters.driven.emf import EmfTurnObserver
from weave.adapters.driven.feedback_stores import InMemoryFeedbackStore, S3FeedbackStore
from weave.adapters.driving.chat import ChatChannel, ChatRequest
from weave.adapters.driving.identity import mint_if_absent
from weave.adapters.driving.lambda_handler import lambda_feedback_handler
from weave.application.feedback import (
    Feedback,
    FeedbackEvent,
    FeedbackPolicy,
    FeedbackRecord,
    IssueType,
    submit_feedback,
)

POLICY = FeedbackPolicy(
    issue_types=(
        IssueType(id="informacion_incorrecta", label="Información incorrecta"),
        IssueType(id="datos_incorrectos", label="Datos incorrectos"),
        IssueType(id="otro", label="Otro"),
    )
)
SESSION = LoomSession(session_id="s1", user_id="u1")


class _Recorder:
    def __init__(self) -> None:
        self.events: list[FeedbackEvent] = []

    def on_feedback(self, event: FeedbackEvent) -> None:
        self.events.append(event)


def _feedback(**kw: Any) -> Feedback:
    return Feedback(**{"session_id": "s1", "turn_id": "t1", "rating": "down", **kw})


def _submit(store, rec=None, **kw) -> FeedbackRecord:
    return submit_feedback(
        store,
        _feedback(**kw),
        target="billing",
        session=SESSION,
        policy=POLICY,
        observers=[rec] if rec else [],
    )


class TestSubmit:
    def test_two_steps_complete_each_other(self):
        store, rec = InMemoryFeedbackStore(), _Recorder()

        first = _submit(store, rec, issues=("informacion_incorrecta",))
        second = _submit(
            store,
            rec,
            issues=("informacion_incorrecta", "datos_incorrectos"),
            comment="  Me mostró datos de otra cuenta ",
        )

        assert rec.events[0].first is True
        assert rec.events[0].new_issues == ("informacion_incorrecta",)
        assert rec.events[1].first is False
        assert rec.events[1].new_issues == ("datos_incorrectos",)
        assert rec.events[1].comment_added is True
        assert second.created_at == first.created_at
        assert second.comment == "Me mostró datos de otra cuenta"

    def test_a_later_step_without_comment_keeps_it(self):
        store = InMemoryFeedbackStore()
        _submit(store, comment="mal")
        assert _submit(store).comment == "mal"

    def test_a_rating_change_is_reported(self):
        store, rec = InMemoryFeedbackStore(), _Recorder()
        _submit(store, rec, rating="up")
        _submit(store, rec, rating="down")
        assert rec.events[1].previous_rating == "up"

    def test_unknown_issues_are_dropped_and_the_thumb_kept(self, caplog):
        store = InMemoryFeedbackStore()
        with caplog.at_level(logging.WARNING, logger="weave.application.feedback"):
            record = _submit(store, issues=("inventado", "otro", "otro"))
        assert record.issues == ("otro",)
        assert record.rating == "down"
        assert "inventado" in caplog.text

    def test_a_long_comment_is_cut(self):
        policy = FeedbackPolicy(max_comment=2000)
        record = submit_feedback(
            InMemoryFeedbackStore(),
            _feedback(comment="x" * 3000),
            target="support",
            session=SESSION,
            policy=policy,
        )
        assert len(record.comment) == 2000

    def test_the_first_target_sticks(self):
        store = InMemoryFeedbackStore()
        _submit(store)
        again = submit_feedback(
            store, _feedback(), target="support", session=SESSION, policy=POLICY
        )
        assert again.target == "billing"

    def test_a_failing_observer_does_not_lose_the_feedback(self, caplog):
        class Broken:
            def on_feedback(self, event):
                raise RuntimeError("down")

        store = InMemoryFeedbackStore()
        with caplog.at_level(logging.WARNING):
            _submit(store, Broken())
        assert store.get("s1", "t1") is not None

    def test_the_event_has_no_comment_text(self):
        rec = _Recorder()
        _submit(InMemoryFeedbackStore(), rec, comment="mi DNI es 123")
        assert "123" not in rec.events[0].model_dump_json()


class TestModels:
    @pytest.mark.parametrize("bad", ["Info Incorrecta", "1x", "x" * 65])
    def test_issue_ids_are_validated_when_building_the_policy(self, bad):
        with pytest.raises(ValidationError):
            IssueType(id=bad, label="x")

    def test_duplicated_issue_ids_are_rejected(self):
        with pytest.raises(ValidationError):
            FeedbackPolicy(issue_types=(IssueType(id="a", label="A"), IssueType(id="a", label="B")))

    @pytest.mark.parametrize("field,value", [("turn_id", "a b"), ("session_id", "a/b")])
    def test_ids_that_end_in_a_key_are_validated(self, field, value):
        with pytest.raises(ValidationError):
            _feedback(**{field: value})


class TestS3Store:
    class _Client:
        def __init__(self, error: str | None = None) -> None:
            self.objects: dict[str, bytes] = {}
            self.error = error

        def get_object(self, **kw):
            if self.error:
                raise _ClientError(self.error)
            if kw["Key"] not in self.objects:
                raise _ClientError("NoSuchKey")
            return {"Body": io.BytesIO(self.objects[kw["Key"]])}

        def put_object(self, **kw):
            self.objects[kw["Key"]] = kw["Body"]

    def test_round_trip_under_the_expected_key(self):
        client = self._Client()
        store = S3FeedbackStore("b", client=client)
        assert store.get("s1", "t1") is None

        record = _submit(store)

        assert list(client.objects) == ["feedback/s1/t1.json"]
        assert store.get("s1", "t1") == record

    def test_any_other_error_propagates(self):
        store = S3FeedbackStore("b", client=self._Client(error="AccessDenied"))
        with pytest.raises(_ClientError):
            store.get("s1", "t1")


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


def _lines(observer: EmfTurnObserver, event: FeedbackEvent) -> list[dict]:
    out = io.StringIO()
    observer._stream = out
    observer.on_feedback(event)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def _event(**kw) -> FeedbackEvent:
    base = dict(
        target="billing",
        session_id="s1",
        user_id="u1",
        is_anonymous=False,
        turn_id="t1",
        rating="down",
        first=True,
    )
    return FeedbackEvent(**{**base, **kw})


class TestEmf:
    def test_first_down_with_two_issues(self):
        lines = _lines(
            EmfTurnObserver("A", service="acme"),
            _event(new_issues=("informacion_incorrecta", "datos_incorrectos")),
        )

        main, *issues = lines
        assert (main["Feedback"], main["FeedbackNegative"]) == (1, 1)
        assert "FeedbackPositive" not in main
        assert main["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["target", "service"]]
        assert [(i["issue"], i["FeedbackIssue"]) for i in issues] == [
            ("informacion_incorrecta", 1),
            ("datos_incorrectos", 1),
        ]
        assert issues[0]["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [
            ["target", "service", "issue"]
        ]

    def test_a_second_step_only_adding_a_comment(self):
        (line,) = _lines(EmfTurnObserver("A"), _event(first=False, comment_added=True))
        metrics = [m["Name"] for m in line["_aws"]["CloudWatchMetrics"][0]["Metrics"]]
        assert metrics == ["FeedbackComments"]

    def test_a_change_counts_only_the_change(self):
        (line,) = _lines(EmfTurnObserver("A"), _event(first=False, previous_rating="up"))
        assert [m["Name"] for m in line["_aws"]["CloudWatchMetrics"][0]["Metrics"]] == [
            "FeedbackChanged"
        ]

    def test_nothing_new_writes_nothing(self):
        assert _lines(EmfTurnObserver("A"), _event(first=False, previous_rating="down")) == []

    def test_the_products_dimensions_apply(self):
        observer = EmfTurnObserver(
            "A", dimensions=lambda e: {"agent": e.target, "service": "acme-agents"}
        )
        (line,) = _lines(observer, _event())
        assert line["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["agent", "service"]]
        assert line["agent"] == "billing"


# --- W21: channel + Lambda --------------------------------------------------------


def _channel(store=None, policy: Any = POLICY, **kw) -> ChatChannel:
    def resolve(request: ChatRequest) -> str:
        area = ((request.extras or {}).get("headers") or {}).get("profile")
        return {"BILLING": "billing"}.get(area, "support")

    return ChatChannel(
        resolve,
        None,
        feedback_store=store if store is not None else InMemoryFeedbackStore(),
        feedback_policy=policy,
        **kw,
    )


def _post(handler, body: dict, headers: dict | None = None) -> dict:
    event = {
        "requestContext": {"http": {"method": "POST"}},
        "headers": headers or {},
        "body": json.dumps(body),
    }
    return handler(event)


def _parse(event):
    body = json.loads(event["body"])
    return Feedback(**body, extras={"headers": event["headers"]})


class TestLambda:
    def test_routes_like_the_chat(self):
        store = InMemoryFeedbackStore()
        handler = lambda_feedback_handler(_channel(store), parse=_parse)

        response = _post(
            handler,
            {"session_id": "s1", "turn_id": "t1", "rating": "down", "issues": ["otro"]},
            {"profile": "BILLING"},
        )

        assert response["statusCode"] == 200
        assert json.loads(response["body"]) == {
            "turn_id": "t1",
            "rating": "down",
            "issues": ["otro"],
        }
        assert store.get("s1", "t1").target == "billing"

    def test_a_channel_without_store_is_a_400(self):
        channel = ChatChannel("support", None)
        response = _post(
            lambda_feedback_handler(channel), {"session_id": "s1", "turn_id": "t1", "rating": "up"}
        )
        assert response["statusCode"] == 400

    def test_session_id_is_required_even_with_a_minting_policy(self):
        handler = lambda_feedback_handler(_channel(session_policy=mint_if_absent()))
        assert _post(handler, {"turn_id": "t1", "rating": "up"})["statusCode"] == 400

    def test_a_bad_turn_id_is_a_400(self):
        handler = lambda_feedback_handler(_channel())
        body = {"session_id": "s1", "turn_id": "no válido", "rating": "up"}
        assert _post(handler, body)["statusCode"] == 400

    def test_the_identity_follows_the_session_policy(self):
        store = InMemoryFeedbackStore()
        handler = lambda_feedback_handler(
            _channel(store, session_policy=mint_if_absent(anon_prefix="anon_"))
        )
        _post(handler, {"session_id": "s1", "turn_id": "t1", "rating": "up"})

        record = store.get("s1", "t1")
        assert (record.user_id, record.is_anonymous) == ("anon_s1", True)

    def test_policy_per_target(self):
        def policy_for(target: str) -> FeedbackPolicy:
            if target == "billing":
                return FeedbackPolicy(
                    issue_types=(IssueType(id="cuenta_cerrada", label="Cuenta cerrada"),)
                )
            return POLICY

        store = InMemoryFeedbackStore()
        handler = lambda_feedback_handler(_channel(store, policy_for), parse=_parse)
        body = {
            "session_id": "s1",
            "turn_id": "t1",
            "rating": "down",
            "issues": ["cuenta_cerrada"],
        }

        _post(handler, body)  # routed to support: unknown there
        assert store.get("s1", "t1").issues == ()

    def test_get_returns_the_panel_of_the_routed_target(self):
        handler = lambda_feedback_handler(
            _channel(),
            parse_options=lambda event: {"headers": event["headers"]},
        )
        response = handler(
            {"requestContext": {"http": {"method": "GET"}}, "headers": {"profile": "BILLING"}}
        )

        body = json.loads(response["body"])
        assert response["statusCode"] == 200
        assert body["target"] == "billing"
        assert [i["id"] for i in body["issue_types"]] == [
            "informacion_incorrecta",
            "datos_incorrectos",
            "otro",
        ]
        assert body["max_comment"] == 2000

    def test_get_without_policy_has_no_issues(self):
        handler = lambda_feedback_handler(_channel(policy=None))
        response = handler({"httpMethod": "GET", "headers": {}})
        assert json.loads(response["body"])["issue_types"] == []

    def test_other_methods_are_405(self):
        handler = lambda_feedback_handler(_channel())
        assert handler({"httpMethod": "DELETE"})["statusCode"] == 405


def test_configure_hands_feedback_to_observers_with_on_feedback():
    import weave
    from weave import composition

    class TurnsOnly:
        def on_turn(self, event):
            pass

    rec = _Recorder()
    try:
        weave.configure(engine=object(), observers=[TurnsOnly(), rec])
        assert composition._feedback_observers == (rec,)
        assert len(composition._observers) == 1

        channel = composition.routed_channel(
            lambda r: "support", feedback_store=InMemoryFeedbackStore(), feedback_policy=POLICY
        )
        channel.feedback(_feedback())
        assert rec.events[0].target == "support"
    finally:
        weave.configure()
