"""weave.adapters.driven.s3_transcripts — A :class:`~weave.ports.observer.TranscriptObserver` over S3 (W23).

One JSON object per agent turn at ``{prefix}dt={YYYY-MM-DD}/{session_id}/{turn_id}.json``,
the date being the turn's start in UTC. Unlike feedback (``S3FeedbackStore``), a
transcript is written once, when the turn happens, so the day is known: the ``dt=``
partition lets Athena prune by date with partition projection. A regenerated turn has
its own ``turn_id`` (with ``replaces``), so the answer it replaced is never overwritten.

``session_id`` and ``turn_id`` were already validated (session policy, W15) before the
turn ran. The write is a synchronous ``PutObject``, like every observer: Lambda freezes
the process when the handler returns. An error propagates to Weave, which logs it and
lets the turn answer. Needs ``boto3`` (``weave[aws]``).
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

from weave.application.errors import HarnessError
from weave.application.observe import TurnTranscript


class S3TranscriptObserver:
    """Write each :class:`~weave.application.observe.TurnTranscript` to S3.

    Args:
        bucket: Destination bucket.
        prefix: Key prefix, ending in ``/``.
        include_tool_results: Keep each tool call's ``result``. Off by default: results
            can be large (listings) and carry personal data. The tool's ``input`` is
            always kept — it is small and says what the agent looked for.
        client: An S3 client; defaults to ``boto3.client("s3")``.
    """

    def __init__(
        self,
        bucket: str,
        prefix: str = "turns/",
        *,
        include_tool_results: bool = False,
        client: Any = None,
    ) -> None:
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
        self.client = client

    def key(self, transcript: TurnTranscript) -> str:
        day = transcript.started_at.astimezone(UTC).date().isoformat()
        return f"{self.prefix}dt={day}/{transcript.session_id}/{transcript.turn_id}.json"

    def on_transcript(self, transcript: TurnTranscript) -> None:
        stored = transcript if self.include_tool_results else _without_tool_results(transcript)
        self.client.put_object(
            Bucket=self.bucket,
            Key=self.key(transcript),
            Body=stored.model_dump_json().encode("utf-8"),
            ContentType="application/json",
        )


def _without_tool_results(transcript: TurnTranscript) -> TurnTranscript:
    calls = tuple(call.model_copy(update={"result": None}) for call in transcript.tool_calls)
    return transcript.model_copy(update={"tool_calls": calls})
