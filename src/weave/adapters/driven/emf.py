"""weave.adapters.driven.emf — Turn metrics as CloudWatch Embedded Metric Format.

EMF is one JSON line on stdout that CloudWatch Logs turns into metrics: no API call,
no added latency, no ``cloudwatch:PutMetricData`` permission. The format is plain JSON,
so it is built with the standard library — Weave keeps zero dependencies here and does
not pick the product's logging stack.

The line goes to stdout directly, not through :mod:`logging`: EMF needs the JSON to be
the whole line, and a logging handler (Lambda's, Powertools') would prefix or wrap it.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Mapping
from typing import Any, TextIO

from weave.application.observe import TurnEvent

_MAX_DIMENSIONS = 30


class EmfTurnObserver:
    """A :class:`~weave.ports.observer.TurnObserver` that writes one EMF line per turn.

    Args:
        namespace: CloudWatch namespace (required, non-empty).
        service: Adds a ``service`` dimension to the default dimensions.
        dimensions: ``event -> {name: value}`` replacing the default ``{"target": ...}``
            (plus ``service``). Keep it low-cardinality: every combination is a metric.
        properties: ``event -> {name: value}`` replacing the default properties —
            fields on the line that are **not** dimensions (searchable in Logs Insights
            without paying for cardinality).
        stream: Where to write; ``None`` resolves ``sys.stdout`` at write time.

    Metrics per turn: ``Turns`` (1), ``TurnErrors`` (0/1, emitted on every turn so the
    error rate has no gaps), ``TurnDuration`` and ``BuildDuration`` (whole ms), and on
    ``ok`` ``InputTokens``, ``OutputTokens``, ``TotalTokens``, ``CacheReadInputTokens``,
    ``CacheWriteInputTokens`` and ``ToolCalls``. On Bedrock ``TotalTokens`` includes the
    cache tokens; for cost, price each of the four token metrics on its own.
    """

    def __init__(
        self,
        namespace: str,
        *,
        service: str | None = None,
        dimensions: Callable[[TurnEvent], Mapping[str, str]] | None = None,
        properties: Callable[[TurnEvent], Mapping[str, Any]] | None = None,
        stream: TextIO | None = None,
    ) -> None:
        if not namespace:
            raise ValueError("EmfTurnObserver needs a non-empty namespace.")
        self.namespace = namespace
        self.service = service
        self._dimensions = dimensions or self._default_dimensions
        self._properties = properties or _default_properties
        self._stream = stream

    def on_turn(self, event: TurnEvent) -> None:
        dimensions = dict(self._dimensions(event))
        if len(dimensions) > _MAX_DIMENSIONS:
            raise ValueError(
                f"EMF allows at most {_MAX_DIMENSIONS} dimensions, got {len(dimensions)}."
            )
        values: list[tuple[str, str, float]] = [
            ("Turns", "Count", 1),
            ("TurnErrors", "Count", 1 if event.outcome == "error" else 0),
            ("TurnDuration", "Milliseconds", _whole_ms(event.duration_ms)),
        ]
        if event.build_ms is not None:
            values.append(("BuildDuration", "Milliseconds", _whole_ms(event.build_ms)))
        if event.outcome == "ok" and event.usage is not None:
            values += [
                ("InputTokens", "Count", event.usage.input_tokens),
                ("OutputTokens", "Count", event.usage.output_tokens),
                ("TotalTokens", "Count", event.usage.total_tokens),
                ("CacheReadInputTokens", "Count", event.usage.cache_read_input_tokens),
                ("CacheWriteInputTokens", "Count", event.usage.cache_write_input_tokens),
                ("ToolCalls", "Count", len(event.usage.tool_calls)),
            ]
        doc = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self.namespace,
                        "Dimensions": [list(dimensions)],
                        "Metrics": [{"Name": name, "Unit": unit} for name, unit, _ in values],
                    }
                ],
            },
            **{k: v for k, v in self._properties(event).items() if v is not None},
            **dimensions,
            **{name: value for name, _, value in values},
        }
        print(json.dumps(doc), file=self._stream or sys.stdout, flush=True)

    def _default_dimensions(self, event: TurnEvent) -> dict[str, str]:
        dims = {"target": event.target}
        if self.service:
            dims["service"] = self.service
        return dims


def _whole_ms(value: float) -> int:
    """Half-up, not Python's banker's rounding: 4210.5 ms reads as 4211 in a dashboard."""
    return int(value + 0.5)


def _default_properties(event: TurnEvent) -> dict[str, Any]:
    return {
        "session_id": event.session_id,
        "user_id": event.user_id,
        "kind": event.kind,
        "operation": event.operation,
        "outcome": event.outcome,
        "stop_reason": event.stop_reason,
        "error_type": event.error_type,
    }
