"""Shared fakes. No test in the default run touches a real provider."""

import asyncio
from collections.abc import AsyncIterator, Sequence

import pytest

from voice_agent.conversation import Message
from voice_agent.errors import ProviderError
from voice_agent.llm.base import Warmth
from voice_agent.stt.base import Transcript
from voice_agent.tts.base import AudioClip, Voice


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

    async def stream(self, system: str, messages: Sequence[Message]) -> AsyncIterator[str]:
        self.systems.append(system)
        self.seen.append(list(messages))
        if self.fail:
            raise ProviderError("provider exploded")
        if self.delay:
            await asyncio.sleep(self.delay)
        reply = self.replies[min(len(self.seen) - 1, len(self.replies) - 1)]
        self.active += 1
        try:
            for word in reply.split(" "):
                if word:  # an empty reply must stream nothing, not one blank fragment
                    yield word + " "
                await asyncio.sleep(self.pace)
        finally:
            self.active -= 1


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


class FakeTTS:
    """A synthesizer that returns deterministic bytes and records its input.

    `spoken` is the point of it: the assertion that the agent speaks exactly
    the reply it recorded — not a truncated or re-rendered version — is made
    against this.
    """

    def __init__(self, fail: bool = False) -> None:
        self.provider = "fake-voice"
        self.voice = "fake-voice-1"
        self.fail = fail
        self.spoken: list[str] = []

    async def synthesize(self, text: str) -> AudioClip:
        self.spoken.append(text)
        if self.fail:
            raise ProviderError("synthesizer exploded")
        return AudioClip(data=b"ID3" + text.encode(), media_type="audio/mpeg")

    async def list_voices(self) -> list[Voice]:
        return [Voice(id="fake-voice-1", name="Fake", usable=True)]


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
