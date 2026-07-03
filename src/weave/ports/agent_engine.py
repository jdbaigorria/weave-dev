"""weave.ports.agent_engine — The ``AgentEngine`` port.

Wraps the act of turning a *named* declarative target into a runnable. The default
adapter (:mod:`weave.adapters.driven`) delegates to ``loom.build_agent`` /
``loom.build_workflow`` / ``loom.discover``. The port exists so the
application/domain can be exercised against a mock engine — not to abstract Loom
away (Weave depends on Loom; this is a test seam, not a vendor swap).
"""

from __future__ import annotations

from typing import Any, Protocol

from loom import LoomSession, TargetInfo


class AgentEngine(Protocol):
    """Builds runnable targets (native Strands objects) by name, and lists them."""

    def build_agent(
        self, name: str, session: LoomSession, *, model_params: dict[str, Any] | None = None
    ) -> Any:
        """Return a runnable ``strands.Agent`` (or ``BidiAgent``) for ``name``.

        ``model_params`` is an optional per-run overlay of model constructor params
        (merged on top of the declarative config) — the channel for injecting per-run
        secrets such as a Bedrock ``boto_session``.
        """
        ...

    def build_workflow(
        self, name: str, session: LoomSession, *, model_params: dict[str, Any] | None = None
    ) -> tuple[Any, list[Any]]:
        """Return ``(workflow, node_agents)`` — a Graph/Swarm plus its node agents.

        ``model_params`` is the per-run model-params overlay applied to every node
        (see :meth:`build_agent`).
        """
        ...

    def discover(self) -> list[TargetInfo]:
        """Return the catalog of buildable targets (name + kind + description)."""
        ...
