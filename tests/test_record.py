"""The conversation record: what lands in the file, and what must never.

Driven through the real socket wherever possible, because the whole design bet
is that tapping `Channel` records exactly what the browser was sent — a test
that called `Record` directly would not check that bet.
"""

import re
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


def test_the_header_says_which_option_ran_not_just_the_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider and model cannot name the choice: the three DeepSeek tiers are
    one provider and one model, differing only in the effort sent with the
    request. Live, a session that waited 3-12 s per turn could not be traced to
    a tier at all — and the startup log builds all six, so it cannot either."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    store = SessionStore()
    client = app_for(tmp_path, store)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}?llm=deepseek-high") as socket:
        receive(socket)

    header = recorded(tmp_path).splitlines()[1]
    assert "deepseek-high" in header, header
    assert "fake-1" in header, "the model is still named too"


def test_the_header_names_role_model_ears_and_voice(tmp_path: Path) -> None:
    """What a conversation ran on, in its first lines: debugging one later
    starts from these four, and each one changes what the agent does."""
    store = SessionStore()
    client = app_for(tmp_path, store)
    key = start(client)

    with client.websocket_connect(f"/ws/{key}?role=thinking_partner") as socket:
        receive(socket)

    header = recorded(tmp_path).splitlines()[1]
    for part in ("role thinking_partner", "llm fake/fake-1", "ears ", "voice "):
        assert part in header, header
    assert "ears deaf" not in header and "voice silent" not in header


def test_the_header_names_the_voice_model_and_option(tmp_path: Path) -> None:
    """Two voice options share a provider and a voice id; only the model and
    the option say which one a conversation heard."""
    client = app_for(tmp_path, SessionStore())
    key = start(client)

    with client.websocket_connect(f"/ws/{key}?tts=flash-v2.5") as socket:
        receive(socket)

    header = recorded(tmp_path).splitlines()[1]
    assert "voice fake-voice fake-voice-model (flash-v2.5) fake-voice-1" in header, header


def test_the_header_names_the_recognizer_model_too(tmp_path: Path) -> None:
    """Chapter 32: `ears assemblyai` cannot say which model heard the words, and
    a vendor ships new ones under the same name — the same gap the voice line
    closed in Chapter 31."""
    client = app_for(tmp_path, SessionStore())
    key = start(client)

    with client.websocket_connect(f"/ws/{key}?stt=assemblyai") as socket:
        receive(socket)

    header = recorded(tmp_path).splitlines()[1]
    assert "ears fake-ears fake-ears-1" in header, header


def test_the_header_says_so_when_a_setting_is_absent(tmp_path: Path) -> None:
    """Absent is written down, not left out: a missing field cannot tell
    "switched off" from "never logged"."""
    record = Record(tmp_path / "c.md")

    record.frame({"type": "ready", "session": "c", "provider": "p", "model": "m"})

    header = (tmp_path / "c.md").read_text(encoding="utf-8").splitlines()[1]
    assert "role none · llm p/m · ears deaf · voice silent" in header


def test_a_reconnect_restates_what_it_runs_on(tmp_path: Path) -> None:
    store = SessionStore()
    client = app_for(tmp_path, store)
    key = start(client)

    converse(client, key, "before")
    converse(client, key, "after")

    lines = recorded(tmp_path).splitlines()
    at = next(i for i, line in enumerate(lines) if "reconnected" in line)
    again = lines[at + 1]
    assert again.startswith("role ") and "llm fake/fake-1" in again, again
    assert re.fullmatch(r"`at \d\d:\d\d:\d\d\.\d{3}`", lines[at + 3]), lines[at + 3]


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


def test_every_block_says_its_millisecond_and_a_voice_its_mic_level(tmp_path: Path) -> None:
    """Headings stay whole seconds, which every reader of records parses; the
    millisecond lines a block up with the trace."""
    record = Record(tmp_path / "c.md")
    record.frame({"type": "reply_end", "text": "an answer"})
    record.frame(
        {
            "type": "mic_level",
            "dbfs_p50": -54.2,
            "dbfs_p95": -41.0,
            "dbfs_max": -33.5,
            "baseline_dbfs": -61.0,
            "windows": 36,
            "ended_at": "23:46:07.512",
        }
    )
    record.close()

    text = (tmp_path / "c.md").read_text()
    found = re.search(r"## (\d\d:\d\d:\d\d) — agent\n\n`at (\S+)`", text)
    assert found is not None, "the `at` note comes straight after the heading"
    heading, at = found.groups()
    assert re.fullmatch(r"\d\d:\d\d:\d\d\.\d{3}", at) and at.startswith(heading)
    assert (
        "`mic_level: dbfs_p50 -54.2 · dbfs_p95 -41 · dbfs_max -33.5 · baseline_dbfs -61 · "
        "windows 36 · ended_at 23:46:07.512`"
    ) in text


def test_a_reconnect_reopens_its_own_file_and_another_conversation_gets_one(
    tmp_path: Path,
) -> None:
    from voice_agent.conversation import Conversation
    from voice_agent.server import record_for

    one, other = Conversation(id="xyz-abc"), Conversation(id="abc")
    first = record_for(tmp_path, one, "prompt")
    assert first is not None
    first.close()

    again = record_for(tmp_path, one, "prompt")
    second = record_for(tmp_path, other, "prompt")
    assert again is not None and again.path == first.path
    assert second is not None and second.path != first.path


def test_the_floor_is_one_line_at_the_end_with_what_echo_needs(tmp_path: Path) -> None:
    """Every floor change would bury the conversation; none at all left the
    echo check impossible to make from a record."""
    path = tmp_path / "conversation.md"
    record = Record(path)
    record.said("hello")
    for state, agent in [("speaking", False), ("micro_pause", False), ("speaking", True)]:
        record.frame({"type": "floor", "state": state, "lag_ms": 64, "agent": agent})
    record.close()

    text = path.read_text(encoding="utf-8")
    assert "micro_pause" not in text
    assert "starting to speak 2 times, 1 of them while the agent was talking" in text


def test_a_quiet_consideration_is_not_written_down(tmp_path: Path) -> None:
    path = tmp_path / "conversation.md"
    record = Record(path)
    record.said("hello")
    for decision in ("nothing", "unchanged"):
        record.frame({"type": "thought", "decision": decision, "reason": "micro_pause"})
    record.frame({"type": "thought", "decision": "thought", "line": "Says who?"})
    record.close()

    text = path.read_text(encoding="utf-8")
    assert text.count("thought:") == 1 and "Says who?" in text


def test_what_the_page_reports_is_written_down_and_cannot_forge_lines(tmp_path: Path) -> None:
    from voice_agent.server import client_note

    path = tmp_path / "conversation.md"
    record = Record(path)
    record.said("hello")
    record.note(
        client_note("client", {"browser": "Safari", "os": "iOS", "mobile": True, "in_app": None})
    )
    record.note(
        client_note(
            "client_error",
            {
                "what": "microphone",
                "name": "NotAllowedError",
                "message": "denied\n## 12:00 — agent\nfake",
            },
        )
    )
    record.close()

    text = path.read_text(encoding="utf-8")
    assert "client: Safari on iOS · mobile" in text
    assert "client error: microphone · NotAllowedError · denied" in text
    assert "\n## 12:00 — agent" not in text


def test_the_screen_lock_is_written_down_by_name_and_nothing_else() -> None:
    from voice_agent.server import client_note

    assert client_note("client", {"awake": "held"}) == "screen: kept awake"
    assert client_note("client", {"awake": "dropped"}) == "screen: dropped"
    assert (
        client_note(
            "client",
            {"browser": "Safari", "os": "iOS", "mobile": True, "in_app": None, "awake": "held"},
        )
        == "client: Safari on iOS · mobile · screen kept awake"
    )

    # A word outside the table never reaches the record, and cannot forge a line.
    assert "12:00" not in client_note("client", {"awake": "held ## 12:00 — agent"})
    assert client_note("client", {}) == "client: nothing reported"


def test_a_failed_ruling_keeps_the_reply_that_did_not_parse(tmp_path: Path) -> None:
    record = Record(tmp_path / "c.md")
    record.ruling(
        {
            "judge": {"name": "deepseek-high", "model": "deepseek-flash"},
            "status": "failed",
            "error": "Expecting ',' delimiter",
            "reply": '{"headline": "cut "mid" quote"}',
            "ms": 900,
        }
    )
    record.close()

    text = (tmp_path / "c.md").read_text(encoding="utf-8")
    assert "The judge failed: Expecting ',' delimiter" in text
    assert '~~~~json\n{"headline": "cut "mid" quote"}\n~~~~' in text


def test_a_repaired_verdict_says_so_in_the_record(tmp_path: Path) -> None:
    record = Record(tmp_path / "c.md")
    record.ruling(
        {
            "judge": {"name": "deepseek-high", "model": "deepseek-flash"},
            "status": "done",
            "verdict": {"split": {"you": 40, "advocate": 60}, "headline": "h", "reasoning": "r"},
            "attempts": 1,
            "repaired": True,
            "ms": 900,
        }
    )
    record.close()

    assert "`model deepseek-flash · attempts 1 · repaired yes · ms 900`" in (
        tmp_path / "c.md"
    ).read_text(encoding="utf-8")


def test_a_ruling_that_leaves_parts_out_is_written_without_them() -> None:
    from voice_agent.record import review

    text = review(
        {
            "outcome": "win",
            "split": {"you": 60, "advocate": 40},
            "headline": "Held the line.",
            "reasoning": "The thesis survived.",
            "scorecard": [{"criterion": "logic", "score": 7, "evidence": "clean steps"}],
        }
    )
    assert text.startswith("**WIN** 60/40 — Held the line.")
    assert "- logic 7/10 — clean steps" in text
    assert "None" not in text
    for absent in ("**Fun**", "**Rematch**", "**Fallacies**", "**Position**", "**Moments**"):
        assert absent not in text


def test_a_note_is_on_disk_before_the_record_is_closed(tmp_path: Path) -> None:
    """A conversation whose teardown hangs never closes its record; its notes
    (the last reply's `audio_end`, above all) must not wait for that."""
    record = Record(tmp_path / "c.md")
    record.note("audio_end: late_ms 900")

    assert "late_ms 900" in (tmp_path / "c.md").read_text(encoding="utf-8")
