---
title: Conversation registry
description: List a user's chats — opt-in, metadata only.
---

# Conversation registry

An assistant needs to show a user "your chats". But Strands keys sessions by
`session_id` alone — its `Session` record has **no `user_id`** and there's no
list-by-user — so the user→conversations relationship lives nowhere Strands manages
it. Weave offers it as an **opt-in, metadata-only** capability.

!!! info "Metadata only — never content"
    The registry indexes `user_id → session_id` plus timestamps and an opaque
    title/metadata. It **never** stores message content: Strands stays the single
    source of truth. Opening a listed conversation = loading its Strands session by
    `session_id`, as usual. One write at turn time, no double persistence.

## Wire it into the channel

Pass a registry to `chat_channel` and every **identified** turn upserts the
conversation (anonymous turns are skipped; no registry = no-op):

```python
from weave import chat_channel, FileConversationRegistry

registry = FileConversationRegistry()        # reference impl; use Dynamo in prod
channel = chat_channel("assistant", registry=registry)
# ... the user chats; each turn records (user_id, session_id) ...
```

The upsert sets `created_at` once and bumps `updated_at` every turn — so listing by
recency comes for free.

## List a user's chats

```python
from weave import list_conversations

for ref in list_conversations(registry, "u1"):     # most-recent first
    print(ref.session_id, ref.title, ref.updated_at)
```

Your backend builds its own domain list from these refs; clicking one opens that chat
by its `session_id`.

### `ConversationRef`

```python
@dataclass(frozen=True)
class ConversationRef:
    session_id: str
    user_id: str
    created_at: str        # ISO-8601
    updated_at: str
    title: str | None = None
    metadata: dict = {}
```

## The port

```python
class ConversationRegistry(Protocol):
    def record(self, user_id, session_id, *, title=None, metadata=None) -> None: ...
    def list(self, user_id) -> list[ConversationRef]: ...   # most-recent first
    def delete(self, user_id, session_id) -> None: ...
```

- **`title` / `metadata` are the backend's** — the harness never fills them (titling is
  product/UX). Passing `None` on `record` **preserves** any previously stored value, so
  the per-turn auto-record never erases a title you set later.
- **`delete`** forgets one entry (delete the Strands session separately).
- Reference adapter `FileConversationRegistry` stores one JSON per `(user, session)`;
  a production store (e.g. DynamoDB partitioned on `user_id`, `session_id` as sort key)
  implements the same port directly.

## Setting a title

The harness won't title chats for you. Generate one in your backend and store it:

```python
registry.record("u1", "s1", title="Trip to Japan")
```
