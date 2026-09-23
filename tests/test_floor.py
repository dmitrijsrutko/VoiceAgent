"""The floor state machine, on made-up probabilities: every transition, and
the things it must not do — take a click for speech, or a dip for a pause."""

from voice_agent.floor import MICRO_PAUSE_MS, PAUSE_MS, YIELD_MS, Floor, Transition

WINDOW = 32
SPEECH = 0.9
SILENCE = 0.05


def run(*runs: tuple[float, int]) -> list[Transition]:
    """Feed `(probability, milliseconds)` runs, a window at a time."""
    floor = Floor(WINDOW)
    changes: list[Transition] = []
    for probability, ms in runs:
        for _ in range(ms // WINDOW):
            changes.extend(floor.push(probability))
    return changes


def test_speech_then_silence_walks_every_state() -> None:
    changes = run((SILENCE, 320), (SPEECH, 640), (SILENCE, 1600))

    assert [c.state for c in changes] == ["speaking", "micro_pause", "pause", "yielded"]
    speaking, micro, pause, yielded = changes
    assert speaking.at_ms == 320
    # Every pause state dates from the same moment: when the speech stopped.
    assert micro.at_ms == pause.at_ms == yielded.at_ms == 960
    assert micro.lag_ms >= MICRO_PAUSE_MS
    assert pause.lag_ms >= PAUSE_MS
    assert yielded.lag_ms >= YIELD_MS


def test_onset_is_dated_from_the_first_speech_window_not_the_decision() -> None:
    (speaking,) = run((SILENCE, 96), (SPEECH, 128))

    assert speaking.at_ms == 96
    assert speaking.lag_ms == 2 * WINDOW


def test_a_single_window_click_does_not_claim_the_floor() -> None:
    assert run((SILENCE, 320), (SPEECH, WINDOW), (SILENCE, 320)) == []


def test_a_gap_inside_a_word_is_not_a_pause() -> None:
    """Stop consonants silence a word for up to ~100 ms."""
    changes = run((SPEECH, 320), (SILENCE, 128), (SPEECH, 320))

    assert [c.state for c in changes] == ["speaking"]


def test_a_dip_between_the_thresholds_keeps_the_speaker_speaking() -> None:
    """Hysteresis: 0.4 is not speech enough to start, and not quiet enough to stop."""
    changes = run((SPEECH, 320), (0.4, 640), (SPEECH, 320))

    assert [c.state for c in changes] == ["speaking"]


def test_speech_after_a_pause_takes_the_floor_back() -> None:
    changes = run((SPEECH, 320), (SILENCE, 704), (SPEECH, 320))

    assert [c.state for c in changes] == ["speaking", "micro_pause", "pause", "speaking"]


def test_a_blip_inside_a_pause_does_not_restart_its_clock() -> None:
    """A click mid-pause is neither speech nor a reason to measure the pause
    from after it."""
    changes = run((SPEECH, 320), (SILENCE, 320), (SPEECH, WINDOW), (SILENCE, 1600))

    assert [c.state for c in changes] == ["speaking", "micro_pause", "pause", "yielded"]
    assert {c.at_ms for c in changes[1:]} == {320}
