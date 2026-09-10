"""In-memory session store: one conversation per unique link.

Everything lives in this process. Restarting the server loses every
conversation, which is the intended chapter 1 behavior — persistence is a
later chapter, and pretending otherwise would hide the cost of not having it.
"""

import secrets

from voice_agent.conversation import Conversation
from voice_agent.errors import SessionNotFoundError

KEY_BYTES = 16
"""128 bits of entropy. The key is the only thing protecting a conversation,
so it has to be unguessable, not merely unique."""


class SessionStore:
    def __init__(self) -> None:
        self._conversations: dict[str, Conversation] = {}

    def create(self) -> Conversation:
        key = secrets.token_urlsafe(KEY_BYTES)
        conversation = Conversation(id=key)
        self._conversations[key] = conversation
        return conversation

    def get(self, key: str) -> Conversation:
        try:
            return self._conversations[key]
        except KeyError:
            raise SessionNotFoundError(key) from None

    def __contains__(self, key: object) -> bool:
        return key in self._conversations

    def __len__(self) -> int:
        return len(self._conversations)
