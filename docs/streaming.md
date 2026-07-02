---
title: Streaming
description: Token streaming over SSE, and full-duplex voice over bidi.
---

# Streaming

Two distinct shapes:

- **Token streaming** (`stream_agent`) — incremental *text* for a normal chat turn,
  delivered over Server-Sent Events.
- **Bidi** (`open_stream`) — full-duplex *audio* (voice), a persistent connection.

## Token streaming (`stream_agent`)

The streaming sibling of `run_agent`: instead of awaiting the whole turn, it yields
events as text is generated, then a final event with the projected `AgentReply`.

```python
from loom import LoomSession
from weave import stream_agent

async for event in stream_agent("assistant", "Tell me a story.",
                                LoomSession(user_id="u1", session_id="s1")):
    if event.type == "delta":
        print(event.text, end="", flush=True)
    elif event.type == "done":
        print("\n", event.reply.usage.total_tokens)
```

### `ChatStreamEvent`

```python
class ChatStreamEvent:
    type: Literal["delta", "done"]
    text: str | None = None        # delta: an incremental text chunk
    reply: AgentReply | None = None  # done: the final projected reply
    raw: Any                        # the original Strands event; excluded from dump
```

Same lifecycle discipline as `run_agent`: the agent (and its MCP connections) is
**always** torn down — including when the consumer abandons the stream early
(`aclose` runs the `finally`).

!!! tip "Tool-use detail lives in `raw`"
    The minimal contract is `delta` + `done`. Lifecycle and tool-use events ride along
    in each event's `raw` if a UI wants them.

### Over HTTP (SSE)

`chat_router` exposes a streaming route at `POST /chat/stream`, one event per SSE
frame:

```python
from fastapi import FastAPI
from weave import chat_channel, chat_router

app = FastAPI()
app.include_router(chat_router(chat_channel("assistant")))
```

```text
POST /chat/stream   {"message": "hi", "session_id": "s1", "user_id": "u1"}

data: {"type":"delta","text":"Par"}

data: {"type":"delta","text":"is"}

data: {"type":"done","reply":{...}}
```

!!! warning "Errors mid-stream are a frame, not a status"
    Once the response has started its HTTP status can't change, so a failure mid-stream
    is delivered as a final `data: {"type":"error","detail":...}` frame instead of a
    502.

## Voice (bidi)

For full-duplex audio, `open_stream` builds a bidi target and gives you an async
context manager over a live connection.

```python
from weave import open_stream

async with open_stream("voice", session) as stream:
    await stream.send_text("hello")
    async for event in stream.receive():
        if event.type == "audio":
            ...   # base64 audio in the model's format
        elif event.type == "text":
            ...
        elif event.type == "control":
            ...   # lifecycle, usage, barge-in (real-time interruption)
```

- `__aenter__` opens the connection; `__aexit__` stops it and runs `agent.cleanup()`.
- `send_audio(chunk, format=..., sample_rate=..., channels=...)` speaks the
  **model's** format — transcoding (e.g. μ-law 8k ↔ PCM 16k) is the **channel
  adapter's** job, not the stream's.
- Supported `send_audio` values are `format="pcm" | "wav" | "opus" | "mp3"`,
  `sample_rate=16000 | 24000 | 48000`, and `channels=1 | 2`.
- `StreamEvent.type` is `text | audio | tool_call | control`; **barge-in** arrives as a
  `control` event.

!!! note "Transport plumbing is a channel adapter"
    `open_stream` is the transport-agnostic half. A WebSocket pump and a telephony
    protocol (e.g. Twilio Media Streams) live in a *channel adapter* that wraps this —
    a future addition extracted from working code.
