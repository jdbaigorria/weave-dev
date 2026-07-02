---
title: API cookbook
description: Copy-paste examples for Weave's public API surface.
---

# API cookbook

Small, concrete examples for the public API exported from `weave`. The feature guides
explain the concepts; this page is the quick reference.

All examples assume a Loom target named `assistant` exists and a `LoomSession` is
available:

```python
from loom import LoomSession

session = LoomSession(user_id="u1", session_id="s1")
```

## Configure the runtime

### `configure(source=...)`

Use a non-filesystem config source for all top-level functions:

```python
import weave
from weave.adapters.driven.dynamo_config_source import DynamoConfigSource

weave.configure(source=DynamoConfigSource("my-agents-table"))
```

### `configure(engine=...)`

Install a custom engine, typically in tests or a bespoke composition root:

```python
import weave

class MyEngine:
    def build_agent(self, name, session): ...
    def build_workflow(self, name, session): ...
    def discover(self): return []
    def get_target_kind(self, name, kind=None): return kind or "agent"

weave.configure(engine=MyEngine())
```

### Reset to the default engine

```python
import weave

weave.configure()  # Loom filesystem default
```

## One-shot runs

### `run_agent`

```python
from weave import run_agent

reply = await run_agent("assistant", "hello", session)
print(reply.text)
```

Forward request context to tools through Strands `invocation_state`:

```python
reply = await run_agent(
    "assistant",
    "hello",
    session,
    extras={"tenant_id": "tenant-a", "request_id": "req-123"},
)
```

### `run_agent_sync`

```python
from weave import run_agent_sync

reply = run_agent_sync("assistant", "hello", session)
print(reply.text)
```

### `run_workflow`

```python
from weave import run_workflow

reply = await run_workflow("triage_workflow", "ticket text", session)
print(reply.status, reply.execution_order, reply.text)
```

### `run_workflow_sync`

```python
from weave import run_workflow_sync

reply = run_workflow_sync("triage_workflow", "ticket text", session)
```

### `run`

Dispatch by target kind when you do not know whether the name is an agent or workflow:

```python
from weave import run

reply = await run("assistant", "hello", session)               # kind discovered
reply = await run("assistant", "hello", session, kind="agent") # explicit
```

### `run_sync`

```python
from weave import run_sync

reply = run_sync("assistant", "hello", session)
```

### `catalog`

```python
from weave import catalog

for target in catalog():
    print(target.name, target.kind, target.description)
```

## Replies and errors

### `AgentReply`, `Usage`

```python
from weave import AgentReply, Usage

reply: AgentReply = await run_agent("assistant", "hello", session)
usage: Usage = reply.usage

print(reply.text)
print(reply.output)        # structured output, if present
print(reply.interrupts)    # pending HITL interrupts
print(usage.total_tokens)
```

### `WorkflowReply`

```python
from weave import WorkflowReply

reply: WorkflowReply = await run_workflow("triage_workflow", "ticket", session)
print(reply.status)
print(reply.execution_order)
print(reply.interrupts)
```

### `HarnessError`, `RequestError`, `RoutingError`

```python
from weave import HarnessError, RequestError, RoutingError

try:
    reply = await run("assistant", "hello", session)
except RoutingError:
    ...  # request did not map to a target
except RequestError:
    ...  # invalid request/session identity
except HarnessError:
    ...  # execution failed after the target was built
```

## Token streaming

### `stream_agent`

```python
from weave import stream_agent

async for event in stream_agent("assistant", "tell me a story", session):
    if event.type == "delta":
        print(event.text, end="", flush=True)
    elif event.type == "done":
        print(event.reply.usage.total_tokens)
```

### `ChatStreamEvent`

```python
from weave import ChatStreamEvent

async for event in stream_agent("assistant", "hi", session):
    item: ChatStreamEvent = event
    print(item.type, item.text)
```

## Bidi / voice streaming

### `open_stream`, `BidiStream`, `StreamEvent`

```python
from weave import BidiStream, StreamEvent, open_stream

async with open_stream("voice", session) as stream:
    live: BidiStream = stream
    await live.send_text("hello")

    async for event in live.receive():
        item: StreamEvent = event
        if item.type == "text":
            print(item.text)
        elif item.type == "audio":
            play_audio(item.audio)
```

### `BidiStream.send_audio`

```python
async with open_stream("voice", session) as stream:
    await stream.send_audio(
        audio_chunk_base64,
        format="pcm",
        sample_rate=16000,
        channels=1,
    )
```

## Forking conversations

### `fork`

```python
from weave import MutableFileSessionManager, fork

store = MutableFileSessionManager(session_id="s1")

reply = await fork(
    store,
    "assistant",
    "Regenerate that answer, shorter.",
    session,
    at=-1,  # keep all but the last persisted message
)
```

### `fork_sync`

```python
from weave import fork_sync

reply = fork_sync(store, "assistant", "Try again.", session, at=-1)
```

### `fork_stateless`

```python
from weave import fork_stateless

history = [
    {"role": "user", "content": [{"text": "hello"}]},
    {"role": "assistant", "content": [{"text": "hi"}]},
]

reply, new_history = await fork_stateless(
    "assistant",
    history,
    "Answer differently.",
    session,
    at=1,
)
```

### `fork_stateless_sync`

```python
from weave import fork_stateless_sync

reply, new_history = fork_stateless_sync("assistant", history, "Try again.", session)
```

### `MutableSession`

Implement this protocol when your own Strands session store can truncate history:

```python
from weave import MutableSession

class MySessionStore:
    def truncate(self, session_id: str, *, keep: int, agent_id: str = "default") -> None:
        delete_messages_after(session_id, keep=keep, agent_id=agent_id)

store: MutableSession = MySessionStore()
```

## Chat channels

### `ChatRequest`

```python
from weave import ChatRequest

request = ChatRequest(
    message="hello",
    session_id="s1",
    user_id="u1",
    extras={"request_id": "req-123"},
)
```

### `chat_channel`, `ChatChannel`

```python
from weave import ChatChannel, ChatRequest, chat_channel

channel: ChatChannel = chat_channel("assistant")
reply = await channel.handle(ChatRequest(message="hello", session_id="s1", user_id="u1"))
```

### `ChatChannel.handle_sync`

```python
request = ChatRequest(message="hello", session_id="s1", user_id="u1")
reply = channel.handle_sync(request)
```

### `chat_router`

```python
from fastapi import FastAPI
from weave import chat_channel, chat_router

app = FastAPI()
app.include_router(chat_router(chat_channel("assistant")))
```

Custom route paths:

```python
app.include_router(
    chat_router(
        chat_channel("assistant"),
        path="/assistant/chat",
        stream_path="/assistant/chat/stream",
    )
)
```

### `lambda_handler`

```python
from weave import chat_channel, lambda_handler

handler = lambda_handler(chat_channel("assistant"))
```

## Session policies

### `require`

Strict mode: the request must include `session_id`.

```python
from weave import chat_channel, require

channel = chat_channel("assistant", session_policy=require())
```

### `mint_if_absent`

Generate a session id when the first request has none:

```python
from weave import chat_channel, mint_if_absent

channel = chat_channel("assistant", session_policy=mint_if_absent(anon_prefix="anon_"))
reply = await channel.handle(ChatRequest(message="hello"))
print(reply.session_id)  # send this back on the next request
```

### `SessionPolicy`

A custom policy is a function from `ChatRequest` to `LoomSession`:

```python
from loom import LoomSession
from weave import ChatRequest, SessionPolicy, chat_channel

def session_from_request(request: ChatRequest) -> LoomSession:
    user_id = request.extras["identity"]["user_id"]
    return LoomSession(user_id=user_id, session_id=request.session_id or user_id)

policy: SessionPolicy = session_from_request
channel = chat_channel("assistant", session_policy=policy)
```

## Routing

### `routed_channel`

```python
from weave import by_key, routed_channel

channel = routed_channel(
    by_key(
        lambda req: req.extras["profile"]["department"],
        {"SUPPORT": "support_agent", "SALES": "sales_agent"},
    )
)
```

### `by_key`

```python
from weave import by_key

resolve = by_key(
    lambda req: req.extras.get("department"),
    {"support": "support_agent", "sales": "sales_agent"},
    default="fallback_agent",
)
```

### `by_rules`

```python
from weave import by_rules

resolve = by_rules(
    [
        (lambda req: req.extras.get("priority") == "high", "priority_agent"),
        (lambda req: req.extras.get("department") == "sales", "sales_agent"),
    ],
    default="support_agent",
)
```

### `TargetResolver`

```python
from weave import ChatRequest, TargetResolver

def resolve_target(request: ChatRequest) -> str:
    return "support_agent" if request.user_id else "public_agent"

resolver: TargetResolver = resolve_target
channel = routed_channel(resolver)
```

## Input shaping

### `text_block`

```python
from weave import text_block

block = text_block("Summarise this attachment.")
```

### `image_block`

```python
from weave import image_block

block = image_block(png_bytes, format="png")
```

### `document_block`

```python
from weave import document_block

block = document_block(pdf_bytes, format="pdf", name="Quarterly report")
```

### `video_block`

```python
from weave import video_block

block = video_block(mp4_bytes, format="mp4")
```

### `file_block`

```python
from weave import file_block

block = file_block("report.pdf", pdf_bytes)  # infers document/pdf
```

### `build_content`

```python
from weave import build_content, file_block

content = build_content(
    "Summarise these files.",
    attachments=[
        file_block("report.pdf", pdf_bytes),
        file_block("chart.png", png_bytes),
    ],
)

reply = await run_agent("assistant", content, session)
```

## Conversation registry

### `FileConversationRegistry`

```python
from weave import FileConversationRegistry, chat_channel

registry = FileConversationRegistry()
channel = chat_channel("assistant", registry=registry)
```

### `list_conversations`

```python
from weave import list_conversations

for ref in list_conversations(registry, "u1"):
    print(ref.session_id, ref.title, ref.updated_at)
```

### `ConversationRef`

```python
from weave import ConversationRef

ref: ConversationRef = list_conversations(registry, "u1")[0]
print(ref.session_id, ref.metadata)
```

### `ConversationRegistry`

Implement the protocol for your own store:

```python
from weave import ConversationRegistry, ConversationRef

class MyRegistry:
    def record(self, user_id, session_id, *, title=None, metadata=None): ...
    def list(self, user_id) -> list[ConversationRef]: ...
    def delete(self, user_id, session_id): ...

registry: ConversationRegistry = MyRegistry()
```

## Assets

### `LocalAssetStore`

```python
from weave import LocalAssetStore

store = LocalAssetStore("./tmp-assets")
ref = store.put("u1", "note.txt", b"hello", content_type="text/plain")
print(store.get(ref))
```

### `S3AssetStore`

```python
from weave import S3AssetStore

store = S3AssetStore("my-bucket", prefix="uploads")
ref = store.put("u1", "report.pdf", pdf_bytes, content_type="application/pdf")
```

### `AssetRef`

```python
from weave import AssetRef

ref: AssetRef = store.list("u1")[0]
print(ref.asset_id, ref.filename, ref.size)
```

### `AssetStore`

Implement the core asset protocol for a custom backend:

```python
from weave import AssetRef, AssetStore

class MyAssetStore:
    def put(self, user_id, filename, data, *, content_type=None) -> AssetRef: ...
    def get(self, ref: AssetRef) -> bytes: ...
    def list(self, user_id) -> list[AssetRef]: ...
    def delete(self, ref: AssetRef) -> None: ...

store: AssetStore = MyAssetStore()
```

### `PresignedAssetStore`

Add presigned URL support when the browser should upload/download directly:

```python
from weave import PresignedAssetStore

class MyPresignedStore(MyAssetStore):
    def presign_upload(self, user_id, filename, *, content_type=None) -> tuple[str, AssetRef]: ...
    def presign_download(self, ref: AssetRef) -> str: ...

store: PresignedAssetStore = MyPresignedStore()
```

### `asset_router`

```python
from fastapi import FastAPI
from weave import S3AssetStore, asset_router

app = FastAPI()
app.include_router(asset_router(S3AssetStore("my-bucket")))
```

Custom prefix:

```python
app.include_router(asset_router(S3AssetStore("my-bucket"), prefix="/files"))
```

## Config source

### `DynamoConfigSource`

```python
from weave.adapters.driven.dynamo_config_source import DynamoConfigSource

source = DynamoConfigSource("my-agents-table")
agent_config = source.get_agent_config("assistant")
```

### Writer helpers

```python
source.put_app_config(app_config)
source.put_agent_config(agent_config)
source.put_workflow_config(workflow_config)
```

### `migrate_from_file_source`

```python
from loom.adapters.config import FileConfigSource
from weave.adapters.driven.dynamo_config_source import DynamoConfigSource, migrate_from_file_source

written = migrate_from_file_source(
    FileConfigSource("warp"),
    DynamoConfigSource("my-agents-table"),
)
```

## Lower-level engine port

### `TargetInfo`

```python
from weave import TargetInfo, catalog

target: TargetInfo = catalog()[0]
print(target.name, target.kind, target.description)
```

### Custom `AgentEngine`

The `AgentEngine` protocol is not exported at top level; import it from the port module
when building a dependency-injected composition:

```python
from weave.ports.agent_engine import AgentEngine

class MyEngine:
    def build_agent(self, name, session): ...
    def build_workflow(self, name, session): ...
    def discover(self): return []
    def get_target_kind(self, name, kind=None): return kind or "agent"

engine: AgentEngine = MyEngine()
```
