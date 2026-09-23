"""In-memory session store: one conversation per unique link.

Everything lives in this process: a restart loses every conversation.
"""

import secrets

from voice_agent.conversation import Conversation
from voice_agent.errors import SessionNotFoundError

KEY_BYTES = 16
"""128 bits of entropy. The key is the only thing protecting a conversation,
so it has to be unguessable, not merely unique."""


class SessionStore:
    """`cap` bounds how many conversations are kept, oldest dropped first.

    Unset (a local run), the store grows forever. With a public address that
    is not safe: `GET /` mints a conversation on every load, so a crawler would
    grow this dict until the process is killed for it.

    A dropped conversation loses its link; reloading it gets the 404 page
    instead of its history. That is precisely what restarting the server already
    does to every conversation, and it is a better failure than being OOM-killed
    mid-sentence. The cap is set well above `VOICE_AGENT_MAX_LIVE` so that
    eviction reaches abandoned conversations long before it reaches a held one.
    """

    def __init__(self, cap: int | None = None) -> None:
        self._conversations: dict[str, Conversation] = {}
        self._cap = cap

    def create(self) -> Conversation:
        key = secrets.token_urlsafe(KEY_BYTES)
        conversation = Conversation(id=key)
        self._conversations[key] = conversation
        self._evict()
        return conversation

    def _evict(self) -> None:
        """Oldest first, by insertion order — which a dict keeps for us."""
        if self._cap is None:
            return
        while len(self._conversations) > self._cap:
            del self._conversations[next(iter(self._conversations))]

    def get(self, key: str) -> Conversation:
        try:
            return self._conversations[key]
        except KeyError:
            raise SessionNotFoundError(key) from None

    def __contains__(self, key: object) -> bool:
        return key in self._conversations

    def __len__(self) -> int:
        return len(self._conversations)
