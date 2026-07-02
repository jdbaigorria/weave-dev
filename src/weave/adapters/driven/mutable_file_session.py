"""weave.adapters.driven.mutable_file_session — Reference mutable session store.

Shows the intended pattern for editable history: **inherit Strands' session
manager and add only what's missing** (truncation). Reuses all of Strands'
file-session machinery (restore, append, AgentState, conversation_manager_state,
interrupt_state) and adds ``truncate`` so a conversation can be rewound for
edit/regenerate/explore. A DynamoDB/Postgres store would do the same: subclass the
relevant Strands manager (or implement the repository) and add ``truncate``.
"""

from __future__ import annotations

import os

from strands.session import FileSessionManager


class MutableFileSessionManager(FileSessionManager):
    """``FileSessionManager`` plus ``truncate`` — implements :class:`weave.ports.session.MutableSession`."""

    def truncate(self, session_id: str, *, keep: int, agent_id: str = "default") -> None:
        """Delete persisted messages with id >= the cutoff (keep the first ``keep``).

        ``keep`` may be negative (counted from the end). Operates on the on-disk
        ``message_<id>.json`` files; a subsequent agent build re-initialises cleanly
        from the truncated session (re-syncing both ``agent.messages`` and the
        manager's next-id cursor).
        """
        messages = self.list_messages(session_id, agent_id)
        total = len(messages)
        cutoff = keep if keep >= 0 else max(0, total + keep)
        for session_message in messages:
            if session_message.message_id >= cutoff:
                path = self._get_message_path(session_id, agent_id, session_message.message_id)
                if os.path.exists(path):
                    os.remove(path)
