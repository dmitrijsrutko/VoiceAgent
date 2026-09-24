"""Prompt rules written in response to live sessions: each pins the wording
that fixed something, so a later edit cannot quietly undo it."""

from voice_agent import prompts
from voice_agent.roles import load

RULES = prompts.rules()
HEARING = RULES["Hearing"]
SELF_ONLY = RULES["Voice scope"]


def test_not_catching_something_asks_again_instead_of_reading_out_every_language() -> None:
    """Live, "didn't catch that" read ~100 languages aloud: 624 characters."""
    assert "name the languages you can understand" not in HEARING
    assert "ask them to say it again" in HEARING
    assert "name at most five" in HEARING


def test_the_female_voice_names_its_one_word_acknowledgements() -> None:
    """Live, a bare «Понял.» from the female voice, talking to a man."""
    rule = RULES["Voice: female"]
    assert "«Поняла.»" in rule and "never «Понял.»" in rule
    assert "a man talking to you does not make you" in rule
    assert "«Поняла.»" in RULES["Identity: female"]
    assert "never «Поняла.»" in RULES["Voice: male"]


def test_the_user_is_never_given_a_gender_they_have_not_shown() -> None:
    assert "never give the user a gender they have not shown you" in SELF_ONLY


def test_the_thinking_partner_answers_when_asked() -> None:
    """Live, asked «я спрашиваю…» ("I'm asking you"), it refused to suggest anything."""
    speaking = load("thinking_partner").when_speaking
    assert "спрашиваю" in speaking
    assert "answer it" in speaking
