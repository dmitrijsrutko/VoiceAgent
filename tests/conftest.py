"""Shared fakes. No test in the default run touches a real provider."""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from voice_agent.conversation import Message
from voice_agent.errors import ProviderError
from voice_agent.llm.base import Usage, Warmth
from voice_agent.stt.base import Transcript
from voice_agent.tts.base import Voice


class FakeLLM:
    """A reasoning engine that streams a canned reply and records what it saw.

    `seen` is the point of it: the assertion that the whole conversation is
    resent as context on every call is made against this, not against a mock's
    call count.
    """

    def __init__(
        self,
        replies: Sequence[str] | None = None,
        fail: bool = False,
        delay: float = 0.0,
        pace: float = 0.0,
    ) -> None:
        self.provider = "fake"
        self.model = "fake-1"
        self.replies = list(replies) if replies else ["Sure thing."]
        self.fail = fail
        self.delay = delay
        self.pace = pace
        self.seen: list[list[Message]] = []
        self.warmed: list[list[Message]] = []
        # Generations currently running. A speculation that outlives its socket
        # keeps billing, so "nothing is still generating" is a thing to assert.
        self.active = 0
        self.systems: list[str] = []

    async def warm(self, system: str, messages: Sequence[Message]) -> Warmth:
        """Records what it was asked to warm. `warmed` is the point of it: the
        assertion that only *stable* text is ever prefilled is made here."""
        self.warmed.append(list(messages))
        if self.fail:
            raise ProviderError("provider exploded")
        return Warmth(prompt_tokens=1200, cached_tokens=1024)

    async def stream(
        self, system: str, messages: Sequence[Message], usage: Usage | None = None
    ) -> AsyncIterator[str]:
        self.systems.append(system)
        self.seen.append(list(messages))
        if self.fail:
            raise ProviderError("provider exploded")
        if self.delay:
            await asyncio.sleep(self.delay)
        reply = self.replies[min(len(self.seen) - 1, len(self.replies) - 1)]
        self.active += 1
        try:
            words = [word for word in reply.split(" ") if word]
            for word in words:  # an empty reply must stream nothing, not one blank fragment
                yield word + " "
                await asyncio.sleep(self.pace)
            if usage is not None:
                # Deliberately not equal to the fragment count, so a report that
                # confuses the two fails.
                usage.output_tokens = 2 * len(words)
                usage.prompt_tokens = sum(len(m.content.split()) for m in messages) + 100
                usage.cached_tokens = 64
        finally:
            self.active -= 1


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


def pcm_for(text: str) -> bytes:
    """Deterministic "speech" for a text: its bytes, padded to whole samples."""
    data = text.encode()
    return data + b"\x00" * (len(data) % 2)


class FakeTTS:
    """A synthesizer that streams deterministic PCM and records its input.

    `spoken` is the point of it: the assertion that the agent speaks exactly
    the reply it recorded — not a truncated or re-rendered version — is made
    against this. Audio is produced as the text arrives, in several chunks,
    because a consumer that only ever sees one chunk after all of the text
    cannot be told apart from a batched one.
    """

    def __init__(self, fail: bool = False, fail_after: int | None = None) -> None:
        self.provider = "fake-voice"
        self.voice = "fake-voice-1"
        self.fail = fail
        self.fail_after = fail_after
        """Raise after this many chunks: a synthesis that dies mid-reply."""
        self.spoken: list[str] = []
        """The whole text of each synthesis, as far as it was read."""
        self.active = 0
        """Syntheses still running — one outliving its turn keeps billing."""

    async def stream(self, text: AsyncIterator[str]) -> AsyncIterator[bytes]:
        index = len(self.spoken)
        self.spoken.append("")
        if self.fail:
            raise ProviderError("synthesizer exploded")
        self.active += 1
        try:
            pending = b""
            sent = 0
            async for fragment in text:
                self.spoken[index] += fragment
                pending += fragment.encode()
                while len(pending) >= 4:
                    if self.fail_after is not None and sent == self.fail_after:
                        raise ProviderError("synthesizer exploded mid-reply")
                    yield pending[:4]
                    pending = pending[4:]
                    sent += 1
            if pending:
                yield pending + b"\x00" * (len(pending) % 2)
        finally:
            self.active -= 1

    async def list_voices(self) -> list[Voice]:
        return [Voice(id="fake-voice-1", name="Fake", usable=True)]


def receive(socket: object) -> dict[str, Any]:
    """The next frame of either kind. Speech is now a run of binary frames of
    no fixed count, so a test can no longer read JSON and skip one frame."""
    message = socket.receive()  # type: ignore[attr-defined]
    socket._raise_on_close(message)  # type: ignore[attr-defined]
    if message.get("bytes") is not None:
        return {"type": "audio_bytes", "data": message["bytes"]}
    return dict(json.loads(message["text"]))


@pytest.fixture
def tts() -> FakeTTS:
    return FakeTTS()


class FakeSTT:
    """A recognizer that emits one scripted transcript per audio frame it is
    fed, so a test can drive the volatile -> committed sequence by sending a
    known number of frames rather than by sleeping.
    """

    def __init__(self, script: Sequence[Transcript] | None = None, fail: bool = False) -> None:
        self.provider = "fake-ears"
        self.model = "fake-ears-1"
        self.sample_rate = 16000
        self.fail = fail
        self.script = (
            list(script)
            if script is not None
            else [
                Transcript("what is", is_final=False),
                Transcript("what is the capital", is_final=False),
                Transcript("What is the capital of Latvia?", is_final=True),
            ]
        )
        self.heard: list[bytes] = []
        # Shared across `stream()` calls, so a session that restarts — the user
        # pressing listen again, or a reconnect — continues the script rather
        # than replaying it. A recognizer does not rewind when its socket does.
        self._remaining = iter(self.script)

    async def stream(self, audio: AsyncIterator[bytes]) -> AsyncIterator[Transcript]:
        if self.fail:
            raise ProviderError("recognizer exploded")
        async for chunk in audio:
            self.heard.append(chunk)
            transcript = next(self._remaining, None)
            if transcript is not None:
                yield transcript


@pytest.fixture
def stt() -> FakeSTT:
    return FakeSTT()
