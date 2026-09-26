"""Shared fakes. No test in the default run touches a real provider."""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest

from voice_agent.conversation import Message
from voice_agent.errors import ProviderError
from voice_agent.llm.base import Usage
from voice_agent.stt.base import Transcript
from voice_agent.tts.base import Alignment, AudioChunk


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
        # Generations currently running. A speculation that outlives its socket
        # keeps billing, so "nothing is still generating" is a thing to assert.
        self.active = 0
        self.systems: list[str] = []
        self.connects = 0
        self.connect_ms: int | None = None

    async def connect(self) -> None:
        self.connects += 1
        if self.fail:
            raise ProviderError("provider exploded")

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
                usage.connect_ms = self.connect_ms
                usage.accepted_ms = 40
                usage.attempts = 1
        finally:
            self.active -= 1


@pytest.fixture(autouse=True)
def _sessions_in_tmp(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test writes a conversation or a trace into the working tree.

    Recording is on by default, so without this every test that opens a socket
    leaves files in `sessions/` — 64 of them on the first run that had it.
    A test that wants to look at one points at its own `tmp_path`.
    """
    monkeypatch.setenv("VOICE_AGENT_SESSIONS", str(tmp_path / "sessions"))
    monkeypatch.setenv("VOICE_AGENT_TRACE", str(tmp_path / "traces"))


@pytest.fixture(autouse=True)
def _plain_assistant(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pipeline tests run the plain assistant: no role greeting, no thinker.
    The page's default is a role card now, so this is said here rather than
    assumed; a test about roles pre-selects one, or deletes this variable."""
    monkeypatch.setenv("VOICE_AGENT_ROLE", "none")


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


def pcm_for(text: str) -> bytes:
    """Deterministic "speech" for a text: its bytes, padded to whole samples."""
    data = text.encode()
    return data + b"\x00" * (len(data) % 2)


def ms_for(text: str) -> float:
    """How far into the fake voice's audio `text` has been fully spoken.

    The fake "speaks" one byte of PCM per character, so a character ends where
    its byte does: at 48 bytes a millisecond of 24 kHz 16-bit audio.
    """
    return len(text.encode("latin-1")) / 48


def timed(data: bytes) -> AudioChunk:
    """A chunk of the fake voice, timed the way ElevenLabs times its own:
    from the start of the chunk the timing arrives with."""
    return AudioChunk(
        data + b"\x00" * (len(data) % 2),
        Alignment(data.decode("latin-1"), tuple((i + 1) / 48 for i in range(len(data)))),
    )


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

    async def stream(self, text: AsyncIterator[str]) -> AsyncIterator[AudioChunk]:
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
                    yield timed(pending[:4])
                    pending = pending[4:]
                    sent += 1
            if pending:
                yield timed(pending)
        finally:
            self.active -= 1


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

    def __init__(
        self,
        script: Sequence[Transcript] | None = None,
        fail: bool = False,
        languages: tuple[str, ...] = (),
    ) -> None:
        self.provider = "fake-ears"
        self.model = "fake-ears-1"
        self.sample_rate = 16000
        # Empty by default: a recognizer with no stated limit adds nothing to
        # the system prompt, so every test that is not *about* languages keeps
        # seeing the prompt it was written against.
        self.languages = languages
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
