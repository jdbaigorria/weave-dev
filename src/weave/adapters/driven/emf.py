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
        model_dimension: Also aggregate by model: adds the ``[base + model]`` dimension
            set when the turn has a ``model_id``.
        variant_dimension: Also aggregate by variant (the agent's ``metrics.variant``
            label): adds ``[base + variant]`` when the turn has one.
        stream: Where to write; ``None`` resolves ``sys.stdout`` at write time.

    ``base`` is what ``dimensions`` returns, so a custom base gets the extra sets too.
    Each extra set is one more series per metric.

    Metrics per turn: ``Turns`` (1), ``TurnErrors`` (0/1, emitted on every turn so the
    error rate has no gaps), ``TurnDuration`` and ``BuildDuration`` (whole ms), and on
    ``ok`` ``InputTokens``, ``OutputTokens``, ``TotalTokens``, ``CacheReadInputTokens``,
    ``CacheWriteInputTokens`` and ``ToolCalls``; ``Regenerations`` (1) on a regenerated
    turn. On Bedrock ``TotalTokens`` includes the
    cache tokens; for cost, price each of the four token metrics on its own.
    """

    def __init__(
        self,
        namespace: str,
        *,
        service: str | None = None,
        dimensions: Callable[[TurnEvent], Mapping[str, str]] | None = None,
        properties: Callable[[TurnEvent], Mapping[str, Any]] | None = None,
        model_dimension: bool = True,
        variant_dimension: bool = True,
        stream: TextIO | None = None,
    ) -> None:
        if not namespace:
            raise ValueError("EmfTurnObserver needs a non-empty namespace.")
        self.namespace = namespace
        self.service = service
        self._dimensions = dimensions or self._default_dimensions
        self._properties = properties or _default_properties
        self._model_dimension = model_dimension
        self._variant_dimension = variant_dimension
        self._stream = stream

    def on_turn(self, event: TurnEvent) -> None:
        base = dict(self._dimensions(event))
        sets = [base]
        if self._model_dimension and event.model_id:
            sets.append({**base, "model": event.model_id})
        if self._variant_dimension and event.variant:
            sets.append({**base, "variant": event.variant})
        for dims in sets:
            if len(dims) > _MAX_DIMENSIONS:
                raise ValueError(
                    f"EMF allows at most {_MAX_DIMENSIONS} dimensions, got {len(dims)}."
                )
        # EMF needs each dimension's value at the root; model and variant also travel as
        # properties when they are not dimensions.
        dimensions = {k: v for dims in sets for k, v in dims.items()}
        model_fields = {"model": event.model_id, "variant": event.variant}
        values: list[tuple[str, str, float]] = [
            ("Turns", "Count", 1),
            ("TurnErrors", "Count", 1 if event.outcome == "error" else 0),
            ("TurnDuration", "Milliseconds", _whole_ms(event.duration_ms)),
        ]
        if event.build_ms is not None:
            values.append(("BuildDuration", "Milliseconds", _whole_ms(event.build_ms)))
        if event.operation == "regenerate":
            # A rejection signal even when the user never touches the thumbs.
            values.append(("Regenerations", "Count", 1))
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
                        "Dimensions": [list(dims) for dims in sets],
                        "Metrics": [{"Name": name, "Unit": unit} for name, unit, _ in values],
                    }
                ],
            },
            **{k: v for k, v in self._properties(event).items() if v is not None},
            **{k: v for k, v in model_fields.items() if v is not None},
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
        "turn_id": event.turn_id,
        "replaces": event.replaces,
        "kind": event.kind,
        "operation": event.operation,
        "outcome": event.outcome,
        "stop_reason": event.stop_reason,
        "error_type": event.error_type,
        "model_config": (
            event.model_settings.model_dump(exclude_none=True) if event.model_settings else None
        ),
        "model_fallbacks": event.model_fallbacks if event.model_id else None,
    }
