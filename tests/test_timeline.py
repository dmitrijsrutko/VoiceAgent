"""The round's timings: turns paired from frames, pauses measured, echo left out."""

from voice_agent import timing
from voice_agent.timeline import Timeline, stats


def run(timeline: Timeline, script: list[tuple[float, object]]) -> None:
    """Frames, "quiet" (the page's playback ended) or typed text, at virtual
    times. The clock starts at the first, as it does live."""
    now = [0.0]
    with timing.using(lambda: now[0]):
        for when, item in script:
            now[0] = when
            if isinstance(item, dict):
                timeline.frame(item)
            elif item == "quiet":
                timeline.playback(False)
            else:
                timeline.typed(str(item))


def test_a_reply_takes_its_own_audio_and_the_answer_its_pause() -> None:
    timeline = Timeline()
    run(
        timeline,
        [
            (0.0, {"type": "greeting", "text": "Give me a position."}),
            (0.2, {"type": "audio_start"}),
            (0.3, {"type": "audio_end", "seconds": 2.0}),
            (2.2, "quiet"),
            (5.2, {"type": "floor", "state": "speaking", "agent": False}),
            (
                8.0,
                {
                    "type": "transcript",
                    "final": True,
                    "text": "Cities need traffic lights.",
                    "speech_end_ms": 800,
                },
            ),
            (8.1, {"type": "reply_start"}),
            (9.0, {"type": "audio_start"}),
            (9.1, {"type": "reply_end", "text": "Roundabouts move more cars."}),
            (9.2, {"type": "audio_end", "seconds": 4.0}),
            (13.0, "quiet"),
        ],
    )
    greeting, answer, reply = timeline.turns()
    assert (greeting.who, greeting.at, greeting.spoken) == ("advocate", 0.2, 2.0)
    assert answer.who == "you" and answer.at == 5.2
    assert answer.think == 3.0  # onset 5.2 minus the greeting's end 2.2
    assert round(answer.spoken or 0, 1) == 2.0  # onset to when the VAD heard the stop
    assert (reply.text, reply.at, reply.spoken) == ("Roundabouts move more cars.", 9.0, 4.0)


def test_cutting_the_agent_off_is_a_talk_over() -> None:
    timeline = Timeline()
    run(
        timeline,
        [
            (0.0, {"type": "reply_start"}),
            (0.1, {"type": "audio_start"}),
            (0.2, {"type": "reply_end", "text": "A long objection."}),
            (0.3, {"type": "audio_end", "seconds": 8.0}),
            (3.0, {"type": "floor", "state": "speaking", "agent": True}),
            (3.4, {"type": "truncated", "played_ms": 3300}),
            (5.0, {"type": "transcript", "final": True, "text": "No, wait.", "speech_end_ms": 500}),
        ],
    )
    reply, answer = timeline.turns()
    assert reply.cut == 3.3 and reply.of == 8.0
    assert answer.think == -0.4  # onset 3.0 minus cut-off end 3.4
    assert stats(timeline.turns())["talk_overs"] == 1


def test_a_sound_during_the_agent_that_did_not_cut_it_is_not_an_onset() -> None:
    timeline = Timeline()
    run(
        timeline,
        [
            (0.0, {"type": "reply_start"}),
            (0.1, {"type": "audio_start"}),
            (0.2, {"type": "reply_end", "text": "Say something."}),
            (1.0, {"type": "floor", "state": "speaking", "agent": True}),  # its own voice
            (2.1, "quiet"),
            (4.0, {"type": "floor", "state": "speaking", "agent": False}),
            (
                6.0,
                {"type": "transcript", "final": True, "text": "Here goes.", "speech_end_ms": 400},
            ),
        ],
    )
    _, answer = timeline.turns()
    assert answer.at == 4.0 and answer.think == 1.9


def test_echo_the_agent_declined_to_answer_is_not_the_user() -> None:
    timeline = Timeline()
    run(
        timeline,
        [
            (0.0, {"type": "greeting", "text": "Hi."}),
            (1.0, {"type": "transcript", "final": True, "text": "Hi."}),
            (1.1, {"type": "echo_ignored", "stage": "final", "text": "Hi."}),
            (3.0, "I think so."),
        ],
    )
    assert [t.who for t in timeline.turns()] == ["advocate", "you"]
    assert timeline.turns()[1].typed


def test_a_reply_cancelled_before_its_first_word_is_no_turn() -> None:
    timeline = Timeline()
    run(timeline, [(0.0, {"type": "reply_start"}), (0.5, {"type": "reply_end", "text": ""})])
    assert timeline.turns() == []


def test_stats_measure_share_pauses_and_words() -> None:
    timeline = Timeline()
    run(
        timeline,
        [
            (0.0, {"type": "greeting", "text": "Go."}),
            (0.0, {"type": "audio_start"}),
            (0.0, {"type": "audio_end", "seconds": 1.0}),
            (1.0, "quiet"),
            (3.0, {"type": "floor", "state": "speaking", "agent": False}),
            (
                6.0,
                {
                    "type": "transcript",
                    "final": True,
                    "text": "one two three four",
                    "speech_end_ms": 0,
                },
            ),
        ],
    )
    measured = stats(timeline.turns())
    assert measured["your_turns"] == 1 and measured["your_words"] == 4
    assert measured["talk_share"] == 75  # 3 s of 4 s
    assert measured["think_median_s"] == 2.0


def test_an_echo_of_something_else_leaves_the_user_s_turn_alone() -> None:
    timeline = Timeline()
    run(
        timeline,
        [
            (0.0, {"type": "greeting", "text": "Hi."}),
            (1.0, {"type": "transcript", "final": True, "text": "Traffic lights save lives."}),
            (1.1, {"type": "echo_ignored", "stage": "final", "text": "Hi."}),
        ],
    )
    assert [t.text for t in timeline.turns()] == ["Hi.", "Traffic lights save lives."]
