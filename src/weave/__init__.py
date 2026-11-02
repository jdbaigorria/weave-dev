"""weave — Conversation runtime/harness for Loom-built agents.

Weave owns how an agent lives over time (history/branching, sessions, interrupts,
channels, assets). It depends on :mod:`loom` (the declarative engine) and uses it
through its public API only — the dependency is unidirectional.

Internal architecture is hexagonal (see the subpackages): ``domain`` is pure
conversation logic, ``application`` holds use cases that depend only on ``ports``,
and ``adapters`` implement those ports (``driven`` for storage/engine, ``driving``
for channels). ``composition`` wires it all together.

Public surface::

    from weave import catalog, run, run_agent, run_workflow, open_stream
    from weave import AgentReply, WorkflowReply, StreamEvent, Usage
    from weave import TurnEvent, TurnObserver, EmfTurnObserver   # turn metrics
    reply = await run_agent("assistant", "hello", LoomSession(...))

Conversation persistence is Strands' job (a ``session_manager`` configured in Loom),
not Weave's — so there is no conversation/history type here. Branching (a value-add
over Strands' native ``take_snapshot``) is deferred.
"""

import logging

from loom import TargetInfo

from weave.adapters.driven.emf import EmfTurnObserver
from weave.adapters.driven.feedback_stores import InMemoryFeedbackStore, S3FeedbackStore
from weave.adapters.driven.file_registry import FileConversationRegistry
from weave.adapters.driven.local_assets import LocalAssetStore
from weave.adapters.driven.mutable_file_session import MutableFileSessionManager
from weave.adapters.driven.s3_assets import S3AssetStore
from weave.adapters.driven.s3_session_store import S3SessionStore
from weave.adapters.driving.assets import asset_router
from weave.adapters.driving.chat import (
    ChatChannel,
    ChatRequest,
    SessionPolicy,
    TargetResolver,
    chat_router,
)
from weave.adapters.driving.identity import mint_if_absent, require
from weave.adapters.driving.lambda_handler import (
    decode_body,
    header,
    lambda_feedback_handler,
    lambda_handler,
)
from weave.adapters.driving.routing import by_key, by_label, by_rules
from weave.application.content import (
    build_content,
    document_block,
    file_block,
    image_block,
    text_block,
    video_block,
)
from weave.application.conversations import list_conversations
from weave.application.errors import HarnessError, RequestError, RoutingError
from weave.application.feedback import (
    Feedback,
    FeedbackEvent,
    FeedbackPolicy,
    FeedbackRecord,
    IssueType,
    PolicyFor,
)
from weave.application.observe import AgentDescription, TurnEvent
from weave.application.reply import AgentReply, ChatStreamEvent, Usage, WorkflowReply
from weave.application.stream import BidiStream, StreamEvent
from weave.composition import (
    catalog,
    chat_channel,
    configure,
    fork,
    fork_stateless,
    fork_stateless_sync,
    fork_sync,
    labels_of,
    open_stream,
    regenerate,
    regenerate_sync,
    routed_channel,
    run,
    run_agent,
    run_agent_sync,
    run_sync,
    run_workflow,
    run_workflow_sync,
    stream_agent,
    submit_feedback,
)
from weave.ports.assets import AssetRef, AssetStore, PresignedAssetStore
from weave.ports.feedback import FeedbackStore
from weave.ports.observer import FeedbackObserver, TurnObserver
from weave.ports.registry import ConversationRef, ConversationRegistry
from weave.ports.session import MutableSession, RegenerableSession

__version__ = "0.0.1"

# Library logging convention: Weave emits under the ``weave`` namespace (mirroring
# Loom's ``loom.*``) and attaches a NullHandler so importing it produces no output
# and no "no handlers" warning. The *application* owns the handlers — e.g. an AWS
# Lambda Powertools logger captures both ``loom`` and ``weave`` with one bridge::
#
#     for name in ("loom", "weave"):
#         lib = logging.getLogger(name)
#         lib.handlers, lib.propagate = powertools_logger.handlers, False
#
# Weave never picks a sink (no loguru, no basicConfig); see docs/observability.md.
logging.getLogger("weave").addHandler(logging.NullHandler())

__all__ = [
    "AgentDescription",
    "AgentReply",
    "AssetRef",
    "AssetStore",
    "BidiStream",
    "ChatChannel",
    "ChatRequest",
    "ChatStreamEvent",
    "ConversationRef",
    "ConversationRegistry",
    "EmfTurnObserver",
    "Feedback",
    "FeedbackEvent",
    "FeedbackObserver",
    "FeedbackPolicy",
    "FeedbackRecord",
    "FeedbackStore",
    "FileConversationRegistry",
    "HarnessError",
    "InMemoryFeedbackStore",
    "IssueType",
    "LocalAssetStore",
    "MutableFileSessionManager",
    "MutableSession",
    "PolicyFor",
    "PresignedAssetStore",
    "RegenerableSession",
    "RequestError",
    "RoutingError",
    "S3AssetStore",
    "S3FeedbackStore",
    "S3SessionStore",
    "SessionPolicy",
    "StreamEvent",
    "TargetInfo",
    "TargetResolver",
    "TurnEvent",
    "TurnObserver",
    "Usage",
    "WorkflowReply",
    "asset_router",
    "build_content",
    "by_key",
    "by_label",
    "by_rules",
    "catalog",
    "chat_channel",
    "chat_router",
    "configure",
    "decode_body",
    "document_block",
    "file_block",
    "fork",
    "fork_stateless",
    "fork_stateless_sync",
    "fork_sync",
    "header",
    "image_block",
    "labels_of",
    "lambda_feedback_handler",
    "lambda_handler",
    "list_conversations",
    "mint_if_absent",
    "open_stream",
    "regenerate",
    "regenerate_sync",
    "require",
    "routed_channel",
    "run",
    "run_agent",
    "run_agent_sync",
    "run_sync",
    "run_workflow",
    "run_workflow_sync",
    "stream_agent",
    "submit_feedback",
    "text_block",
    "video_block",
]
