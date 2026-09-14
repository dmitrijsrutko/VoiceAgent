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

    def replace(self, message: Message, content: str | None) -> None:
        """Rewrite a message already recorded, or remove it with `None`.

        By identity, not by position: a reply is recorded when its text is
        written, and learns how much of it was heard only later — after other
        messages may have followed it.
        """
        for index, existing in enumerate(self.messages):
            if existing is message:
                if content is None:
                    del self.messages[index]
                else:
                    self.messages[index] = Message(role=message.role, content=content)
                return

    def end(self) -> None:
        self.ended = True
