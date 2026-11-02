"""weave.adapters.driven.feedback_stores — :class:`~weave.ports.feedback.FeedbackStore` adapters.

- :class:`InMemoryFeedbackStore` — tests and local development.
- :class:`S3FeedbackStore` — one JSON object per turn at
  ``{prefix}{session_id}/{turn_id}.json``. Two feedbacks on different turns never
  overwrite each other, and the key is built without knowing when the turn happened
  (the second step may arrive after midnight). No date partitions: feedback volume is
  small and Athena reads the whole prefix. Needs ``boto3`` (``weave[aws]``).
"""

from __future__ import annotations

from typing import Any

from weave.application.errors import HarnessError
from weave.application.feedback import FeedbackRecord


class InMemoryFeedbackStore:
    """A dict keyed by ``(session_id, turn_id)``."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], FeedbackRecord] = {}

    def get(self, session_id: str, turn_id: str) -> FeedbackRecord | None:
        return self.records.get((session_id, turn_id))

    def put(self, record: FeedbackRecord) -> None:
        self.records[(record.session_id, record.turn_id)] = record


class S3FeedbackStore:
    """One object per turn in S3. ``client`` is injectable; defaults to ``boto3.client("s3")``."""

    def __init__(self, bucket: str, prefix: str = "feedback/", *, client: Any = None) -> None:
        if client is None:
            try:
                import boto3
            except ImportError as exc:  # pragma: no cover - depends on the optional extra
                raise HarnessError(
                    "S3FeedbackStore needs boto3; install the extra: weave[aws]."
                ) from exc
            client = boto3.client("s3")
        self.bucket = bucket
        self.prefix = prefix
        self.client = client

    def key(self, session_id: str, turn_id: str) -> str:
        return f"{self.prefix}{session_id}/{turn_id}.json"

    def get(self, session_id: str, turn_id: str) -> FeedbackRecord | None:
        try:
            response = self.client.get_object(Bucket=self.bucket, Key=self.key(session_id, turn_id))
        except Exception as exc:
            # Only a missing object is "no feedback yet"; anything else (AccessDenied,
            # throttling) must not be read as absent, or the record would be overwritten.
            if _error_code(exc) == "NoSuchKey":
                return None
            raise
        return FeedbackRecord.model_validate_json(response["Body"].read())

    def put(self, record: FeedbackRecord) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=self.key(record.session_id, record.turn_id),
            Body=record.model_dump_json().encode("utf-8"),
            ContentType="application/json",
        )


def _error_code(exc: BaseException) -> str | None:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None
