"""The conversation record: what lands in the file, and what must never.

Driven through the real socket wherever possible, because the whole design bet
is that tapping `Channel` records exactly what the browser was sent — a test
that called `Record` directly would not check that bet.
"""

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeLLM, FakeSTT, FakeTTS, receive
from tests.test_server import start
from voice_agent.record import Record, attributes
from voice_agent.server import create_app
from voice_agent.sessions import SessionStore


def recorded(directory: Path) -> str:
    files = sorted(directory.glob("*.md"))
    assert len(files) == 1, f"expected one conversation, found {[f.name for f in files]}"
    return files[0].read_text(encoding="utf-8")


def converse(client: TestClient, key: str, *texts: str) -> None:
    with client.websocket_connect(f"/ws/{key}") as socket:
        receive(socket)
        for text in texts:
            socket.send_json({"type": "user_message", "text": text})
            while receive(socket).get("type") != "reply_end":
                pass


def app_for(tmp_path: Path, store: SessionStore, **kwargs: Any) -> TestClient:
    return TestClient(
        create_app(
            llm=kwargs.pop("llm", FakeLLM()),
            tts=kwargs.pop("tts", FakeTTS()),
            stt=kwargs.pop("stt", FakeSTT(script=[])),
            store=store,
            greeting="",
            sessions_dir=tmp_path,
            **kwargs,
        )
    )


def test_a_conversation_is_written_down_in_order(tmp_path: Path) -> None:
    store = SessionStore()
    client = app_for(tmp_path, store)
    key = start(client)

    converse(client, key, "first question", "second question")

    text = recorded(tmp_path)
    assert text.index("first question") < text.index("second question")
    assert "Sure thing." in text
    assert f"# Conversation {key}" in text


def test_no_audio_ever_reaches_the_file(tmp_path: Path) -> None:
    """Speech is biometric data and a five-minute session is tens of megabytes.
    Binary frames are counted; the bytes themselves are never kept."""
    store = SessionStore()
    tts = FakeTTS()
    client = app_for(tmp_path, store, tts=tts)

    converse(client, start(client), "say something")

    text = recorded(tmp_path)
    assert tts.spoken, "the test needs a reply that was actually synthesized"
    assert "\x00" not in text
    assert "frames " in text, "the frames should be counted even though they are not kept"
    assert len(text) < 4000, "the file is the size of a transcript, not of audio"


def test_typed_and_spoken_turns_are_told_apart(tmp_path: Path) -> None:
    """Typed input is the one thing `Channel` cannot see, so it is recorded on
    its own path — and that path says which door the turn came through."""
    from voice_agent.stt.base import Transcript

    store = SessionStore()
    stt = FakeSTT(script=[Transcript("out loud", is_final=True)])
    client = app_for(tmp_path, store, stt=stt)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}") as socket:
        receive(socket)
        socket.send_json({"type": "user_message", "text": "in writing"})
        while receive(socket).get("type") != "reply_end":
            pass
        socket.send_json({"type": "listen_start"})
        socket.send_bytes(b"\x00" * 32)
        while receive(socket).get("type") != "reply_end":
            pass

    text = recorded(tmp_path)
    assert "you (typed)" in text and "in writing" in text
    assert "you (spoken)" in text and "out loud" in text


def test_resuming_a_conversation_appends_rather_than_starting_over(tmp_path: Path) -> None:
    """Reloading the link is the same conversation, so it is the same file —
    and a second header would read as a second conversation."""
    store = SessionStore()
    client = app_for(tmp_path, store)
    key = start(client)

    converse(client, key, "before")
    converse(client, key, "after")

    text = recorded(tmp_path)
    assert text.count("# Conversation") == 1
    assert "reconnected" in text
    assert "before" in text and "after" in text


def test_recording_off_writes_nothing(tmp_path: Path) -> None:
    store = SessionStore()
    client = TestClient(
        create_app(
            llm=FakeLLM(),
            tts=FakeTTS(),
            stt=FakeSTT(script=[]),
            store=store,
            greeting="",
            record=False,
        )
    )

    converse(client, start(client), "hello")

    assert not list(tmp_path.glob("*.md"))


def test_a_field_added_to_a_frame_appears_without_touching_the_recorder() -> None:
    """The property the whole design rests on. The page phrases each frame by
    hand; this renders whatever the frame carries, so the two cannot drift."""
    assert attributes({"type": "x", "invented_later_ms": 42}) == "invented_later_ms 42"


def test_the_noise_a_record_would_drown_in_is_left_out() -> None:
    """One frame per token and one per audio chunk. The trace keeps them."""
    record = Record(Path("/dev/null"))
    for kind in ("delta", "marks", "audio_start", "reply_start"):
        record.frame({"type": kind, "text": "x"})
    record.frame({"type": "transcript", "text": "still being rewritten", "final": False})


def test_falsy_fields_are_omitted_so_the_line_stays_readable() -> None:
    """`speculated no · speculation_lead_ms 0 · speculations_discarded 0` is
    three fields saying nothing happened, on every single turn."""
    rendered = attributes({"chars": 12, "speculated": False, "discarded": 0, "ttft_ms": 492})
    assert rendered == "chars 12 · ttft_ms 492"


def test_a_record_that_cannot_be_written_does_not_end_the_conversation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A full disk, or a directory that is not writable. Losing the archive is
    a nuisance; losing the conversation is a failure."""
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("")
    record = Record(blocked / "conversation.md")

    record.said("hello")
    record.frame({"type": "reply_end", "text": "hi"})
    record.flush()
    record.close()

    assert "not being recorded" in caplog.text


def test_a_long_value_is_cut_short_rather_than_dropped() -> None:
    """Absence has to mean "the frame did not carry it", never "it was too long
    to show". The unprompted line in one live run was 113 characters — seven
    under the limit that would have silently swallowed it."""
    from voice_agent.record import MAX_VALUE_CHARS

    rendered = attributes({"type": "initiative", "line": "word " * 60})

    assert rendered.startswith("line word")
    assert rendered.endswith("…")
    assert len(rendered) < MAX_VALUE_CHARS + 20


def test_the_audio_frame_count_belongs_to_one_reply(tmp_path: Path) -> None:
    """A reply whose audio never closes used to lend its count to the next."""
    record = Record(tmp_path / "c.md")
    record.audio(100)
    record.audio(100)  # ... and no audio_end arrives

    record.frame({"type": "reply_end", "text": "next reply"})
    record.audio(100)
    record.frame({"type": "audio_end", "bytes": 100})
    record.close()

    assert "frames 1" in (tmp_path / "c.md").read_text()


def test_a_conversation_whose_id_ends_with_another_gets_its_own_file(
    tmp_path: Path,
) -> None:
    """`*-{id}.md` also matched a conversation whose id merely ends with this
    one. Random base64 makes it a curiosity, but an exact stem costs nothing."""
    from voice_agent.server import record_for

    tmp_path.mkdir(exist_ok=True)
    long_id, short_id = "xyz-abc", "abc"
    first = record_for(tmp_path, long_id, "prompt")
    assert first is not None
    first.frame({"type": "ready", "session": long_id})
    first.close()

    second = record_for(tmp_path, short_id, "prompt")
    assert second is not None
    assert second.path != first.path
