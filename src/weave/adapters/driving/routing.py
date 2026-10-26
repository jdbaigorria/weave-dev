"""weave.adapters.driving.routing — Target resolvers for the pre-agent entrypoint.

A :data:`~weave.adapters.driving.chat.TargetResolver` is ``(ChatRequest) -> str``:
given an inbound request, it returns the name of the target to run. The *policy*
(where the key comes from, which table) is the product's — these are just the
common shapes wired up so a backend doesn't rewrite them:

- :func:`by_key` — deterministic table lookup (key-based / identity-based routing).
- :func:`by_rules` — first matching predicate wins (compound, ordered conditions).
- :func:`by_label` — like ``by_key``, but the table is the targets' own catalog labels
  (declared in each agent's YAML) instead of a dict kept in the product's code.

Pass either to :func:`weave.composition.routed_channel`. A plain ``str`` target on a
:class:`~weave.adapters.driving.chat.ChatChannel` is the single-agent degenerate case
(a constant resolver), so single- and multi-agent share one channel.

Routing by *intent* (an LLM picks the target) is **not** a resolver: model it as a
Loom Swarm / supervisor agent — i.e. a target of its own — and run that. These
resolvers are synchronous on purpose (they read keys off the request, no model call).
"""

from __future__ import annotations

from collections.abc import Callable

from loom import TargetInfo

from weave.adapters.driving.chat import ChatRequest, TargetResolver
from weave.application.errors import HarnessError, RoutingError


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


def _label_index(label: str, targets: list[TargetInfo]) -> dict[str, str]:
    """Map each value of ``label`` to the one target that declares it.

    A label value is a string or a list of strings (``departments: [SALES, SUPPORT]``);
    other scalar types can't match a string key and are ignored. Two *different*
    targets claiming the same value is a configuration error and fails loudly.
    """
    index: dict[str, str] = {}
    for target in targets:
        raw = target.labels.get(label)
        values = [raw] if isinstance(raw, str) else raw if isinstance(raw, (list, tuple)) else []
        for value in values:
            if not isinstance(value, str):
                continue
            owner = index.setdefault(value, target.name)
            if owner != target.name:
                raise HarnessError(
                    f"label {label!r}={value!r} is declared by both {owner!r} and "
                    f"{target.name!r}; each value must map to a single target."
                )
    return index


def by_label(
    label: str,
    *,
    key: Callable[[ChatRequest], str | None],
    default: str | None = None,
    catalog: Callable[[], list[TargetInfo]] | None = None,
) -> TargetResolver:
    """Route to the target whose catalog label ``label`` carries the request's key.

    The index ``value → target`` is built from the ``label`` of every target in the
    catalog (a value may be a string or a list of strings), **on the first request** and
    then cached — not at construction, because :func:`weave.configure` may run later.
    Two targets declaring the same value raise
    :class:`~weave.application.errors.HarnessError`: a configuration error that should
    fail loudly, not route to whichever came first.

    ``key`` pulls the value from the request (``None`` = no key). A missing or
    unmatched key falls back to ``default``, or raises
    :class:`~weave.application.errors.RoutingError` (→ 404) when none is set — the same
    as :func:`by_key`. ``catalog`` defaults to :func:`weave.composition.catalog`
    (imported lazily); inject one in tests.
    """
    index: dict[str, str] | None = None

    def resolve(request: ChatRequest) -> str:
        nonlocal index
        if index is None:
            if catalog is not None:
                targets = catalog()
            else:
                from weave.composition import catalog as default_catalog  # lazy: cycle

                targets = default_catalog()
            index = _label_index(label, targets)
        value = key(request)
        target = index.get(value) if value is not None else None
        target = target or default
        if target is None:
            raise RoutingError(f"no target labelled {label}={value!r}")
        return target

    return resolve
