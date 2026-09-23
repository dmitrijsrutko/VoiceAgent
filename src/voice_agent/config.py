"""Environment-driven configuration, read once at startup."""

import os
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from voice_agent.errors import ConfigError

DEFAULT_PROVIDER = "anthropic"
"""The reasoning engine a conversation runs on unless it picks another: the
fastest to first token in `--bench-llm`. Its default *model* is not the one
measured; a deployment sets `VOICE_AGENT_MODEL` (`fly.toml` does)."""
DEFAULT_VOICE_PROVIDER = "elevenlabs"
DEFAULT_EARS_PROVIDER = "assemblyai"

DEFAULT_GREETING = "Hi, I'm a voice agent. What can I help you with?"
"""What the agent says when a conversation opens; empty to open in silence.
Synthesised at startup, which also absorbs the synthesizer's cold start."""
DEFAULT_INITIATIVE_DELAYS = "5,15,28"
"""Seconds of silence at which the agent considers speaking unprompted, as
positions in one silence, counted from when its own voice stops. `off` for a
purely reactive agent. The last must land inside `mic.IDLE_TIMEOUT_SECONDS`,
or listening stops before the agent can withdraw."""
DEFAULT_VOICE_GENDER = "female"
"""Which grammatical gender the agent uses about itself, in languages that mark
it. Set to match the voice (it cannot be inferred from a voice id)."""
VOICE_GENDERS = ("female", "male", "neutral")

DEFAULT_SESSIONS = "sessions"
"""Where conversations are written down, or `off`. Kept until `--purge-sessions`;
personal data, and gitignored."""

DEFAULT_TRACE = "traces"
"""Where the span-tree trace is written, or `off`. Holds whole prompts and
replies (keys are redacted by `trace.scrub`)."""

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# The caps (`VOICE_AGENT_MAX_LIVE` etc.) are off unless set; `fly.toml` sets
# them for the public instance. `limits.py` says what each bounds.

SYSTEM_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "system_prompt.md"
"""Resolved relative to the source checkout, which the server runs from."""

PREAMBLE_SEPARATOR = "\n---\n"
"""Only what follows the first rule in the prompt file is sent; above it is a
note for developers."""


@dataclass(frozen=True, slots=True)
class Settings:
    provider: str
    model: str | None
    voice_provider: str
    voice: str | None
    ears_provider: str
    vad_silence: float | None
    greeting: str
    initiative: tuple[float, ...]
    voice_gender: str
    sessions: Path | None
    trace: Path | None
    host: str
    port: int
    max_live: int | None
    session_budget: float | None
    mints_per_ip: int | None
    max_stored: int | None


def load_settings() -> Settings:
    return Settings(
        provider=os.environ.get("VOICE_AGENT_PROVIDER", DEFAULT_PROVIDER),
        model=os.environ.get("VOICE_AGENT_MODEL") or None,
        voice_provider=os.environ.get("VOICE_AGENT_TTS", DEFAULT_VOICE_PROVIDER),
        voice=os.environ.get("VOICE_AGENT_VOICE") or None,
        ears_provider=os.environ.get("VOICE_AGENT_STT", DEFAULT_EARS_PROVIDER),
        vad_silence=_optional_float(os.environ.get("VOICE_AGENT_VAD_SILENCE")),
        greeting=os.environ.get("VOICE_AGENT_GREETING", DEFAULT_GREETING),
        sessions=_directory(os.environ.get("VOICE_AGENT_SESSIONS", DEFAULT_SESSIONS)),
        trace=_directory(os.environ.get("VOICE_AGENT_TRACE", DEFAULT_TRACE)),
        voice_gender=_voice_gender(
            os.environ.get("VOICE_AGENT_VOICE_GENDER", DEFAULT_VOICE_GENDER)
        ),
        initiative=_delays(os.environ.get("VOICE_AGENT_INITIATIVE", DEFAULT_INITIATIVE_DELAYS)),
        host=os.environ.get("VOICE_AGENT_HOST", DEFAULT_HOST),
        port=int(os.environ.get("VOICE_AGENT_PORT", DEFAULT_PORT)),
        max_live=_positive_int("VOICE_AGENT_MAX_LIVE"),
        session_budget=_positive_float("VOICE_AGENT_SESSION_BUDGET"),
        mints_per_ip=_positive_int("VOICE_AGENT_MINTS_PER_IP"),
        max_stored=_positive_int("VOICE_AGENT_MAX_STORED"),
    )


def _optional_float(raw: str | None) -> float | None:
    return float(raw) if raw else None


def _positive_int(name: str) -> int | None:
    """A cap, or `None` (unset or `off`). A malformed value is an error, not
    an unlimited cap."""
    value = _positive_float(name)
    if value is None:
        return None
    if value != int(value):
        raise ConfigError(f"{name} must be a whole number: {os.environ[name]!r}")
    return int(value)


def _positive_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if raw.casefold() in ("", "off", "none"):
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} is not a number: {raw!r}") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive, or 'off' for no limit: {raw!r}")
    return value


def _directory(raw: str) -> Path | None:
    value = raw.strip()
    return None if value.casefold() in ("", "off", "none") else Path(value)


def _voice_gender(raw: str) -> str:
    value = raw.strip().casefold()
    if value not in VOICE_GENDERS:
        raise ConfigError(
            f"VOICE_AGENT_VOICE_GENDER must be one of {', '.join(VOICE_GENDERS)}: {raw!r}"
        )
    return value


SELF_REFERENCE = {
    "female": (
        "Voice rule, which also overrides everything above: your speaking voice is "
        "a woman's, so in a language that marks the speaker's gender, every form "
        "about yourself is feminine — «я поняла», «я не совсем поняла», «я рада», "
        "«я уверена», never «понял», «рад» or «уверен». " + "{scope}"
    ),
    "male": (
        "Voice rule, which also overrides everything above: your speaking voice is "
        "a man's, so in a language that marks the speaker's gender, every form "
        "about yourself is masculine — «я понял», «я не совсем понял», «я рад», "
        "«я уверен», never «поняла», «рада» or «уверена». " + "{scope}"
    ),
    "neutral": (
        "Voice rule: your speaking voice does not clearly read as a man's or a woman's. "
        "When you speak a language that marks the speaker's gender, prefer "
        "wordings that avoid the choice; where one is unavoidable, pick one and "
        "stay with it for the whole conversation. " + "{scope}"
    ),
}


IDENTITY = {
    "female": (
        "You are a woman, and you speak with a woman's voice. In every language "
        "that marks it, you speak about yourself in the feminine, as a woman "
        "naturally would: «я поняла», «я рада», «я сделала»."
    ),
    "male": (
        "You are a man, and you speak with a man's voice. In every language that "
        "marks it, you speak about yourself in the masculine, as a man naturally "
        "would: «я понял», «я рад», «я сделал»."
    ),
    "neutral": "",
}
"""Who the agent is, first thing it reads. A rule at the end about grammar was
followed less reliably than an identity at the start."""

SELF_ONLY = (
    "This is only about how you refer to yourself. Never change how you refer to "
    "anyone else — an author, a person being discussed, the user. If someone "
    "corrects your grammar, fix only your own forms. If an earlier reply of yours "
    "used the other gender, that was a mistake, not a precedent: do not repeat it."
)
"""Scope for the gender line. Corrected on its own gender, a model once 'fixed'
a third person instead («написала Достоевская»)."""


def with_voice_gender(prompt: str, gender: str) -> str:
    """Append which gender the agent speaks about itself in."""
    return f"{prompt}\n\n{SELF_REFERENCE[gender].format(scope=SELF_ONLY)}"


LANGUAGE = (
    "Language rule, which overrides everything above: reply in the same language "
    "as the user's last message, even when your own previous reply was in another "
    "language — including when you did not understand them, and including when you "
    "say so. A question in Russian gets a Russian answer, one in Spanish a Spanish "
    "answer, one in Japanese a Japanese answer — straight after an English reply "
    "too. Answering in a different language from theirs is the worst mistake you "
    "can make here."
)
"""The language rule, repeated last and nearest the conversation. Stated only in
the persona, Haiku answered a Russian question in English 11 of 16 times in a
replay of a live conversation; with this, 0 of 16. Concrete examples mattered:
the same rule without them still failed 2 of 16."""


def with_reply_language(prompt: str) -> str:
    return f"{prompt}\n\n{LANGUAGE}"


HEARING = (
    "Your hearing is a speech recognizer, and it transcribes these languages "
    "and no others: {languages}.\n\n"
    "You can read and write far more languages than that, but you cannot *hear* "
    "them. So never offer, promise or agree to listen in a language outside "
    "that list — if someone asks, say plainly which ones you can understand. "
    "Claiming one you cannot hear is the same mistake as inventing a fact.\n\n"
    "When a transcript reads as nonsense — words that do not make a sentence, "
    "or a mixture of scripts in one line — that is usually not someone talking "
    "nonsense. It is most often someone speaking a language your hearing does "
    "not have. Do not answer it as though it were a question, and do not guess "
    "at what it might have meant. Say briefly that you did not catch it and "
    "name the languages you can understand."
)
"""What the agent is told about the languages it can hear. A recognizer does not
refuse a language it lacks, it returns confident nonsense; the agent must neither
promise such a language nor answer the nonsense as a question."""


def with_languages(prompt: str, languages: tuple[str, ...]) -> str:
    """Append which languages the agent can hear. Nothing for a recognizer
    that reports none."""
    if not languages:
        return prompt
    return f"{prompt}\n\n{HEARING.format(languages=', '.join(languages))}"


CLOCK = (
    "When nobody has spoken for a while, you are asked whether to say something "
    "unprompted. That happens at {delays} of silence, counted from the moment "
    "your own voice stops — and you may decline at any of them, which is the "
    "usual answer at the shortest. After the last one you stay quiet until they "
    "speak.\n\n"
    "These are the real numbers. If someone asks how long you wait before "
    "speaking first, tell them plainly instead of guessing at it."
)
"""What the agent is told about its own clock, so that asked, it says the real
numbers rather than inventing some."""


def with_initiative(prompt: str, delays: tuple[float, ...]) -> str:
    """Append when the agent may speak unprompted, from the ladder the session
    actually runs. Nothing if it never does."""
    if not delays:
        return prompt
    seconds = [f"{d:g}" for d in delays]
    spoken = (
        " and ".join(seconds)
        if len(seconds) < 3
        else f"{', '.join(seconds[:-1])} and {seconds[-1]}"
    )
    return f"{prompt}\n\n{CLOCK.format(delays=f'{spoken} seconds')}"


def _delays(raw: str) -> tuple[float, ...]:
    """Comma-separated seconds, or `off` for none. Malformed is an error."""
    if raw.strip().casefold() in ("", "off", "none", "0"):
        return ()
    try:
        delays = tuple(float(part) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        raise ConfigError(f"VOICE_AGENT_INITIATIVE is not a list of seconds: {raw!r}") from exc
    if any(a >= b for a, b in pairwise(delays)) or any(d <= 0 for d in delays):
        raise ConfigError(f"VOICE_AGENT_INITIATIVE must increase and be positive: {raw!r}")
    return delays


OPENED = (
    "You opened this call by saying: \u201c{greeting}\u201d. That was said before "
    "anyone spoke, so do not greet again, and its language says nothing about theirs."
)
"""The greeting, as a fact rather than a turn: as a turn in the history, a fixed
English line anchored replies to a Russian question in English."""


def build_prompt(
    languages: tuple[str, ...],
    gender: str,
    delays: tuple[float, ...],
    base: str | None = None,
    greeting: str = "",
) -> str:
    """The whole system prompt for one conversation.

    The appended facts (voice, hearing, clock) are fixed for the conversation
    and sit below the persona, so the provider's cached prefix stays stable.
    """
    prompt = load_system_prompt() if base is None else base
    if IDENTITY[gender]:
        prompt = f"{IDENTITY[gender]}\n\n{prompt}"
    prompt = with_initiative(with_languages(prompt, languages), delays)
    if greeting:
        prompt = f"{prompt}\n\n{OPENED.format(greeting=greeting)}"
    # The two rules a fast model kept breaking go last, nearest the conversation:
    # buried under the hearing and clock notes, the gender line was ignored.
    return with_voice_gender(with_reply_language(prompt), gender)


def load_system_prompt(path: Path = SYSTEM_PROMPT_PATH) -> str:
    text = path.read_text(encoding="utf-8")
    _, separator, body = text.partition(PREAMBLE_SEPARATOR)
    return (body if separator else text).strip()


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(
            f"{name} is not set. Put it in .env and run with `uv run --env-file .env ...`, "
            f"or export it into your shell."
        )
    return value
