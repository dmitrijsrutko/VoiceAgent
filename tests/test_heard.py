"""Turning "how much audio played" into "which words were heard"."""

from voice_agent.conversation import Conversation, Message
from voice_agent.heard import Spoken, truncated
from voice_agent.tts.base import Alignment, AudioChunk


def voiced(*segments: tuple[str, int]) -> Spoken:
    """A voice of timed segments: each text spoken evenly over its milliseconds,
    timed from the start of the chunk it arrives with, as ElevenLabs times it."""
    voice = Spoken()
    for text, duration_ms in segments:
        step = duration_ms / len(text)
        timing = Alignment(text, tuple(step * (i + 1) for i in range(len(text))))
        voice.add(AudioChunk(b"\x00" * (duration_ms * 48), timing))
    return voice


def test_a_word_heard_in_part_is_not_heard() -> None:
    voice = voiced(("one two three", 1300))  # 100 ms a character

    assert voice.heard(1000) == "one two"  # "one two th"


def test_a_word_heard_to_its_last_letter_is_heard() -> None:
    voice = voiced(("one two three", 1300))

    assert voice.heard(700) == "one two"
    assert voice.heard(800) == "one two"


def test_speech_that_stopped_mid_word_does_not_count_the_half_word() -> None:
    """A generated segment can end mid-word ("The Millennium Priz"), and a reply
    interrupted there has voiced only half of it."""
    voice = voiced(("one two thre", 1200))
    voice.message = Message("assistant", "one two three four")

    assert voice.heard(5000) == "one two"


def test_speech_that_voiced_the_whole_reply_keeps_its_last_word() -> None:
    voice = voiced(("one two three", 1300))
    voice.message = Message("assistant", "one two three")

    assert voice.heard(5000) == "one two three"


def test_punctuation_before_the_cut_stays() -> None:
    voice = voiced(("Riga, the capital", 1700))

    assert voice.heard(700) == "Riga,"


def test_segments_are_timed_from_the_audio_before_them() -> None:
    """The second segment's timing starts at zero again; read naively, it would
    say its words were heard before the first segment's."""
    voice = voiced(("one ", 400), ("two three", 900))

    assert voice.heard(450) == "one"
    assert voice.heard(700) == "one two"


def test_playing_past_the_end_hears_everything() -> None:
    voice = voiced(("one two", 700))

    assert voice.heard(5000) == "one two"


def test_nothing_played_hears_nothing() -> None:
    assert voiced(("one two", 700)).heard(0) == ""


def test_without_timing_the_text_is_assumed_spread_evenly() -> None:
    voice = Spoken(message=Message("assistant", "abcd efgh"))
    voice.add(AudioChunk(b"\x00" * 48_000))  # one second, no timing

    assert voice.heard(500) == "abcd"
    assert voice.heard(1000) == "abcd efgh"


def test_an_interrupted_reply_is_recorded_as_heard_and_nothing_more() -> None:
    """Seen live: with "… [interrupted]" appended, the model started writing
    that marker at the end of its own replies, and the voice read it out."""
    assert truncated("one two three", "one two") == "one two"


def test_a_reply_heard_in_full_is_left_alone() -> None:
    assert truncated("one two three ", "one two three") == "one two three "


def test_a_reply_nobody_heard_is_removed() -> None:
    assert truncated("one two three", "") is None


def test_a_message_is_replaced_by_identity_not_by_equal_content() -> None:
    conversation = Conversation(id="t")
    first = conversation.add_assistant("same")
    conversation.add_user("q")
    second = conversation.add_assistant("same")

    conversation.replace(second, "cut")
    conversation.replace(first, None)

    assert conversation.messages == [Message("user", "q"), Message("assistant", "cut")]
