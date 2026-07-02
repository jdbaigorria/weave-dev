"""weave.adapters.driving.routing — Target resolvers for the pre-agent entrypoint.

A :data:`~weave.adapters.driving.chat.TargetResolver` is ``(ChatRequest) -> str``:
given an inbound request, it returns the name of the target to run. The *policy*
(where the key comes from, which table) is the product's — these are just the two
common shapes wired up so a backend doesn't rewrite them:

- :func:`by_key` — deterministic table lookup (key-based / identity-based routing).
- :func:`by_rules` — first matching predicate wins (compound, ordered conditions).

Pass either to :func:`weave.composition.routed_channel`. A plain ``str`` target on a
:class:`~weave.adapters.driving.chat.ChatChannel` is the single-agent degenerate case
(a constant resolver), so single- and multi-agent share one channel.

Routing by *intent* (an LLM picks the target) is **not** a resolver: model it as a
Loom Swarm / supervisor agent — i.e. a target of its own — and run that. These
resolvers are synchronous on purpose (they read keys off the request, no model call).
"""

from __future__ import annotations

from collections.abc import Callable

from weave.adapters.driving.chat import ChatRequest, TargetResolver
from weave.application.errors import RoutingError


def by_key(
    key_of: Callable[[ChatRequest], str],
    routes: dict[str, str],
    *,
    default: str | None = None,
) -> TargetResolver:
    """Route by a single key extracted from the request, looked up in ``routes``.

    ``key_of`` pulls the routing key (a header, a JWT claim, a profile field) — the
    product owns where it comes from. An unmatched key falls back to ``default``, or
    raises :class:`~weave.application.errors.RoutingError` (→ 404) when none is set.
    """

    def resolve(request: ChatRequest) -> str:
        target = routes.get(key_of(request), default)
        if target is None:
            raise RoutingError(f"no route for key {key_of(request)!r}")
        return target

    return resolve


def by_rules(
    rules: list[tuple[Callable[[ChatRequest], bool], str]],
    *,
    default: str | None = None,
) -> TargetResolver:
    """Route by the first rule whose predicate matches (ordered, compound conditions).

    Each rule is ``(predicate, target)``; the first predicate returning ``True`` wins.
    No rule matches → ``default``, or :class:`~weave.application.errors.RoutingError`
    (→ 404) when none is set. More expressive than :func:`by_key` (a rule can combine
    several request fields); ``by_key`` is the single-equality special case.
    """

    def resolve(request: ChatRequest) -> str:
        for predicate, target in rules:
            if predicate(request):
                return target
        if default is None:
            raise RoutingError("no rule matched the request")
        return default

    return resolve
