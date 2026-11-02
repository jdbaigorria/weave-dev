---
title: Fork (edit / regenerate / explore)
description: Rewind a conversation to a point and continue with a new input.
---

# Fork

## Regenerate the last turn (`regenerate`)

The common case — "try that answer again", or "edit my last message" — needs no
positions. Name the turn by its [`turn_id`](chat-channel.md#the-turn-id):

```python
from weave import regenerate, S3SessionStore

store = S3SessionStore("my-sessions-bucket", "session/")  # same bucket/prefix as app.yml

reply = await regenerate(
    store,
    "assistant",
    "the same question",      # or an edited one
    session,
    replaces=last.turn_id,    # the reply being overwritten
)
reply.turn_id                 # a new id: feedback on the old answer stays on the old answer
```

Weave finds where `replaces` starts (the user message stamped with it), truncates the
history from there, rebuilds the agent and runs the input. Only the **last** turn can
be regenerated; any other id is a `RequestError` (400) and nothing changes. The new
turn always has a new id. Observers hear `operation="regenerate"` with `replaces`, and
`EmfTurnObserver` counts it in `Regenerations` — a rejection signal even when the user
never rates the answer.

Over a channel, send `replaces` in the request and build the channel with a
`session_store` (`chat_channel(..., session_store=store)`); without one, `replaces` is a
400. Streaming a regeneration is not supported yet.

The store implements [`RegenerableSession`](#mutablesession): `truncate` plus
`turn_start`. `MutableFileSessionManager` (file sessions) and `S3SessionStore` (Loom's
`s3` provider) ship with Weave. `S3SessionStore` is not a session manager: it reuses
Strands' S3 key layout but never reads or creates a session, so one instance serves
every `session_id`. Build it with the **same bucket and prefix** as `session.config` in
`app.yml`.

A regenerate is not a fork in the "new conversation" sense: it rewrites the same
session. Branching into a new session is not built yet.

## Rewind by position (`fork`)

The lower-level primitive behind **regenerate / edit / explore** (overwrite style, no
compare): rewind a conversation to message `at` and continue with a new `input`.

- **regenerate** — `at` before the last exchange, same user message.
- **edit K** — `at = K`, the edited message.
- **explore K** — `at = K`, a different message.

There are two variants, by who owns the transcript.

## Session-backed (`fork`)

When **Strands owns the conversation** (a `session_manager` is configured in Loom).
Strands sessions are *append-only* — no delete/truncate by index — so a backend
supplies the rewind via the [`MutableSession`](#mutablesession) capability.

```python
from weave import fork, MutableFileSessionManager

# The same directory as `session.config.path` in app.yml: without `storage_dir`,
# Strands uses its own temp default and would truncate another folder.
store = MutableFileSessionManager(session_id="s1", storage_dir="./sessions")

reply = await fork(
    store,
    "assistant",
    "Actually, make it formal.",   # the new input
    session,
    at=4,                          # keep the first 4 messages
)
```

It truncates the persisted session to its first `at` messages, **rebuilds** the agent
(which re-initialises cleanly from the now-truncated session — re-syncing messages and
the next-id cursor), runs the input, and Strands persists the new turn.

Strands keeps each agent's history in its own folder,
`session_<session_id>/agents/agent_<agent_id>/`, and Loom names it after the agent
(or the `agent_id` its YAML declares). `fork` truncates `agent_id=name` unless you pass
`agent_id`, and checks the agent it builds against it: a mismatch raises
`HarnessError` before invoking, instead of running over the untruncated history. Pass
`agent_id` when the YAML declares one.

### `MutableSession`

The capability the backend implements on top of its Strands `SessionManager`:

```python
class MutableSession(Protocol):
    def truncate(self, session_id: str, *, keep: int, agent_id: str) -> None:
        ...   # delete persisted messages so only the first `keep` remain
```

`regenerate` needs one more method, in its own protocol so a `fork`-only store keeps
working:

```python
class RegenerableSession(MutableSession, Protocol):
    def turn_start(self, session_id: str, *, agent_id: str, turn_id: str) -> int | None:
        ...   # message_id opening turn_id, only if it is the last stamped turn
```

`keep` may be negative (counted from the end, like a list slice): `-1` keeps all but
the last, `0` clears the history. `MutableFileSessionManager` is the reference impl
(`FileSessionManager` + `truncate`); a Dynamo/Postgres store subclasses the relevant
Strands manager and adds `truncate`.

!!! warning "Serialize calls per `session_id`"
    Truncation destructively rewrites the append-only session, and Strands' per-instance
    sequentiality guard doesn't span the instances rebuild-per-request creates for one
    session. A fork racing a concurrent `run_agent`/`fork` on the same `session_id`
    corrupts it. Weave imposes no locking — serialize concurrent turns on a conversation
    yourself (a per-`session_id` lock or queue).

## Stateless (`fork_stateless`)

When **the caller owns the transcript** (session persistence is off). Seeds the agent
with `history[:at]`, runs the input, and returns the reply **and the new full
transcript** for you to persist.

```python
from weave import fork_stateless

reply, new_transcript = await fork_stateless(
    "assistant",
    history,            # the messages you hold
    "Try again, shorter.",
    session,
    at=6,               # optional; defaults to the whole history
)
# overwrite your stored transcript with `new_transcript`
```

## Which one?

| | `fork` | `fork_stateless` |
| --- | --- | --- |
| Conversation owned by | Strands (session persisted) | the caller |
| Needs | a `MutableSession` store | nothing |
| Returns | `AgentReply` | `(AgentReply, new_transcript)` |
