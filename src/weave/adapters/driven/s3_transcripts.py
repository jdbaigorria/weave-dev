"""weave.adapters.driven.s3_transcripts — A :class:`~weave.ports.observer.TranscriptObserver` over S3 (W23).

One JSON object per agent turn at ``{prefix}dt={YYYY-MM-DD}/{session_id}/{turn_id}.json``,
the date being the turn's start in UTC. Unlike feedback (``S3FeedbackStore``), a
transcript is written once, when the turn happens, so the day is known: the ``dt=``
partition lets Athena prune by date with partition projection. A regenerated turn has
its own ``turn_id`` (with ``replaces``), so the answer it replaced is never overwritten.

What is kept of each tool's result is a policy (W24): nothing by default, everything
with ``include_tool_results=True``, or whatever a ``tool_result`` callable returns for
each call — for instance only the sources of a search, without its text.

``session_id`` and ``turn_id`` were already validated (session policy, W15) before the
turn ran. The write is a synchronous ``PutObject``, like every observer: Lambda freezes
the process when the handler returns. An error propagates to Weave, which logs it and
lets the turn answer. Needs ``boto3`` (``weave[aws]``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC
from typing import Any

from weave.application.errors import HarnessError
from weave.application.observe import TurnTranscript
from weave.application.transcript import ToolCall

logger = logging.getLogger(__name__)

#: What to keep of one tool call's result: the content blocks to store, or ``None``.
ToolResultPolicy = Callable[[ToolCall], "list[dict[str, Any]] | None"]


class S3TranscriptObserver:
    """Write each :class:`~weave.application.observe.TurnTranscript` to S3.

    Args:
        bucket: Destination bucket.
        prefix: Key prefix, ending in ``/``.
        include_tool_results: Keep each tool call's ``result`` whole. Off by default:
            results can be large (listings) and carry personal data. The tool's
            ``input`` is always kept — it is small and says what the agent looked for.
        tool_result: Decide per call what to keep instead: it receives the
            :class:`~weave.application.transcript.ToolCall` (``name``, ``input``,
            ``status``, ``result``) and returns the content blocks to store, or ``None``
            to drop the result. A call it raises on is stored without result and logged
            as a ``WARNING``; the rest of the transcript is still written. Excludes
            ``include_tool_results``.
        client: An S3 client; defaults to ``boto3.client("s3")``.

    Raises:
        ValueError: If both ``include_tool_results`` and ``tool_result`` are given.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "turns/",
        *,
        include_tool_results: bool = False,
        tool_result: ToolResultPolicy | None = None,
        client: Any = None,
    ) -> None:
        if include_tool_results and tool_result is not None:
            raise ValueError("Pass include_tool_results or tool_result, not both.")
        if client is None:
            try:
                import boto3
            except ImportError as exc:
                raise HarnessError(
                    "S3TranscriptObserver needs boto3; install the extra: weave[aws]."
                ) from exc
            client = boto3.client("s3")
        self.bucket = bucket
        self.prefix = prefix
        self.include_tool_results = include_tool_results
        self.tool_result = tool_result
        self.client = client

    def key(self, transcript: TurnTranscript) -> str:
        day = transcript.started_at.astimezone(UTC).date().isoformat()
        return f"{self.prefix}dt={day}/{transcript.session_id}/{transcript.turn_id}.json"

    def on_transcript(self, transcript: TurnTranscript) -> None:
        stored = transcript if self.include_tool_results else self._apply_policy(transcript)
        self.client.put_object(
            Bucket=self.bucket,
            Key=self.key(transcript),
            Body=stored.model_dump_json().encode("utf-8"),
            ContentType="application/json",
        )

    def _apply_policy(self, transcript: TurnTranscript) -> TurnTranscript:
        calls = tuple(
            call.model_copy(update={"result": self._kept(call)}) for call in transcript.tool_calls
        )
        return transcript.model_copy(update={"tool_calls": calls})

    def _kept(self, call: ToolCall) -> list[dict[str, Any]] | None:
        if self.tool_result is None:
            return None
        try:
            kept = self.tool_result(call)
        except Exception:
            logger.warning(
                "S3TranscriptObserver: tool_result failed on %r; result dropped",
                call.name,
                exc_info=True,
            )
            return None
        if kept is None:
            return None
        # Validated like any ToolCall, so a policy that returns the wrong shape is
        # caught here (and dropped) instead of writing an object Athena can't read.
        try:
            return ToolCall.model_validate({**call.model_dump(), "result": kept}).result
        except ValueError:
            logger.warning(
                "S3TranscriptObserver: tool_result on %r did not return a list of blocks;"
                " result dropped",
                call.name,
            )
            return None
