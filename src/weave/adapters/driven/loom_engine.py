"""weave.adapters.driven.loom_engine — The default ``AgentEngine``: Loom.

Delegates straight to Loom's public build surface. This is the only place Weave
names Loom's builders; everything above depends on the
:class:`~weave.ports.agent_engine.AgentEngine` port.

Where declarative config comes from is Loom's ``ConfigSource`` seam. By default
this engine passes none, so Loom falls back to its filesystem source (the ``warp/``
workspace). Inject a ``source`` to serve config from elsewhere — e.g.
:class:`~weave.adapters.driven.dynamo_config_source.DynamoConfigSource` — and the
same source backs ``build_agent``, ``build_workflow`` and ``discover``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import loom
from loom import LoomSession, TargetInfo

if TYPE_CHECKING:
    from loom.ports import ConfigSource


class LoomAgentEngine:
    """``AgentEngine`` backed by Loom's public ``build_agent`` / ``build_workflow`` / ``discover``.

    Args:
        source: Optional :class:`~loom.ports.ConfigSource` for declarative config.
            ``None`` (default) lets Loom use its filesystem source — the historical,
            batteries-included behaviour. A backend serving config from a document
            store constructs the engine with its own source.
    """

    def __init__(self, source: ConfigSource | None = None) -> None:
        self._source = source

    def build_agent(
        self, name: str, session: LoomSession, *, model_params: dict[str, Any] | None = None
    ) -> Any:
        return loom.build_agent(name, session, source=self._source, model_params=model_params)

    def build_workflow(
        self, name: str, session: LoomSession, *, model_params: dict[str, Any] | None = None
    ) -> tuple[Any, list[Any]]:
        return cast(
            "tuple[Any, list[Any]]",
            loom.build_workflow(name, session, source=self._source, model_params=model_params),
        )

    def discover(self) -> list[TargetInfo]:
        return cast("list[TargetInfo]", loom.discover(self._source))
