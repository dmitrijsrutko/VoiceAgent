"""The conversation: the messages exchanged so far, and nothing else.

This is the "context" that is resent to the reasoning engine on every call.
It is deliberately a plain in-memory list — no trimming, no summarization, no
persistence. Those are later chapters, and each needs this to exist first.
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

    def add_user(self, content: str) -> Message:
        return self._add("user", content)

    def add_assistant(self, content: str) -> Message:
        return self._add("assistant", content)

    def _add(self, role: Role, content: str) -> Message:
        message = Message(role=role, content=content)
        self.messages.append(message)
        return message

    def end(self) -> None:
        self.ended = True
