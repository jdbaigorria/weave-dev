---
title: Fork (edit / regenerate / explore)
description: Rewind a conversation to a point and continue with a new input.
---

# Fork

One primitive behind **regenerate / edit / explore** (overwrite style, no compare):
rewind a conversation to message `at` and continue with a new `input`.

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

store = MutableFileSessionManager(session_id="s1")   # or your Dynamo/Postgres store

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

### `MutableSession`

The capability the backend implements on top of its Strands `SessionManager`:

```python
class MutableSession(Protocol):
    def truncate(self, session_id: str, *, keep: int, agent_id: str = "default") -> None:
        ...   # delete persisted messages so only the first `keep` remain
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
