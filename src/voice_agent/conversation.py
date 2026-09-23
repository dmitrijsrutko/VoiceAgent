"""The conversation: the messages exchanged so far, and nothing else.

This is the "context" that is resent to the reasoning engine on every call.
A plain in-memory list: no trimming, no summarization, no persistence.
"""

from dataclasses import dataclass, field
from typing import Literal

Role = Literal["user", "assistant"]


@dataclass(frozen=True, slots=True)
class Message:
    role: Role
    content: str


@dataclass(slots=True)
class Conversation:
    """One conversation, identified by the key in its URL."""

    id: str
    messages: list[Message] = field(default_factory=list)
    ended: bool = False
    engine: str | None = None
    ears: str | None = None
    """Which backends this conversation runs on, chosen when it was started.

    Pinned rather than re-read on every connection, because resuming a link has
    to resume the same agent: the history below was produced by that engine,
    and the system prompt names the languages *those* ears have. A reconnect
    that quietly swapped either would leave the agent contradicting its own
    transcript.
    """
    opening: Message | None = None
    """The greeting, kept in the history the page replays but out of what the
    model reads: a fixed line in one language anchored replies to it (the system
    prompt says it was said instead)."""

    @property
    def context(self) -> list[Message]:
        """What the reasoning engine reads: every message but the greeting."""
        return [m for m in self.messages if m is not self.opening]

    def add_user(self, content: str) -> Message:
        return self._add("user", content)

    def add_assistant(self, content: str) -> Message:
        return self._add("assistant", content)

    def _add(self, role: Role, content: str) -> Message:
        message = Message(role=role, content=content)
        self.messages.append(message)
        return message

    def replace(self, message: Message, content: str | None) -> None:
        """Rewrite a message already recorded, or remove it with `None`.

        By identity, not by position: a reply is recorded when its text is
        written, and learns how much of it was heard only later — after other
        messages may have followed it.
        """
        for index, existing in enumerate(self.messages):
            if existing is message:
                replacement = None if content is None else Message(message.role, content)
                if replacement is None:
                    del self.messages[index]
                else:
                    self.messages[index] = replacement
                if existing is self.opening:
                    self.opening = replacement
                return

    def end(self) -> None:
        self.ended = True
