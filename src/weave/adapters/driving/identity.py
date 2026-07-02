"""weave.adapters.driving.identity — Session policies for the chat channel.

A :data:`~weave.adapters.driving.chat.SessionPolicy` is ``(ChatRequest) -> LoomSession``:
it turns an inbound request's identity fields (``session_id`` / ``user_id``) into the
run's :class:`~loom.LoomSession`. Session *lifetime* is the harness's domain — the line
that divides Weave (how a conversation lives over time) from Loom (how an agent is
built) — so **Weave owns the minting mechanism; the product injects the policy**. Same
shape as routing (``routed_channel`` dispatches, you inject :func:`by_key`): here the
channel builds the session, you choose whether a missing id is rejected or generated.

- :func:`require` — strict (the channel default): a request must carry its own
  ``session_id``, else :class:`~weave.application.errors.RequestError` (→ 400).
- :func:`mint_if_absent` — permissive: generate a ``session_id`` (and an anonymous
  ``user_id``, optionally prefixed) when the request omits them.

Pass either to :func:`weave.composition.chat_channel` /
:func:`~weave.composition.routed_channel` via ``session_policy=...``.
"""

from __future__ import annotations

import uuid

from loom import LoomSession

from weave.adapters.driving.chat import ChatRequest, SessionPolicy, _strict_session


def _new_id() -> str:
    return str(uuid.uuid4())


def require() -> SessionPolicy:
    """The strict default policy: reject a request that carries no ``session_id``.

    Returns the channel's built-in :func:`~weave.adapters.driving.chat._strict_session`
    — passing ``session_policy=require()`` is just making the default explicit. A
    missing ``session_id`` raises :class:`~weave.application.errors.RequestError`
    (→ 400) rather than silently starting a new conversation; an anonymous visitor (no
    ``user_id``) is keyed by ``session_id``.
    """
    return _strict_session


def mint_if_absent(*, anon_prefix: str = "") -> SessionPolicy:
    """A permissive policy: generate identity fields the request omits.

    - No ``session_id`` → a fresh UUID (a new conversation). The effective id is always
      returned on the :class:`~weave.application.reply.AgentReply` (``session_id``), so
      the caller reads it from the response and echoes it on the next turn.
    - No ``user_id`` → an anonymous user with a freshly minted id, prefixed with
      ``anon_prefix`` (e.g. ``"anon_"``) so a backend can spot anonymous rows. With a
      ``user_id`` present the turn is identified as usual.

    For a public, gateway-less frontend that may not send ids on the first hit. Behind
    an authenticated gateway (ids always present) prefer the strict :func:`require`.
    """

    def policy(request: ChatRequest) -> LoomSession:
        session_id = request.session_id or _new_id()
        if request.user_id is not None:
            return LoomSession(user_id=request.user_id, session_id=session_id, is_anonymous=False)
        return LoomSession(
            user_id=f"{anon_prefix}{_new_id()}", session_id=session_id, is_anonymous=True
        )

    return policy
