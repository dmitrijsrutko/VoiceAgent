"""Environment-driven configuration, read once at startup."""

import os
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from voice_agent.errors import ConfigError

DEFAULT_PROVIDER = "deepseek"
DEFAULT_VOICE_PROVIDER = "elevenlabs"
DEFAULT_EARS_PROVIDER = "elevenlabs"

DEFAULT_GREETING = "Hi, I'm a voice agent. What can I help you with?"
"""What the agent says when a conversation opens. Set VOICE_AGENT_GREETING to
change it, or to empty to open in silence.

It earns its place twice. It is the greeting the roadmap asks for — the moment
that sets expectations about what this thing is — and it moves the synthesis
engine's cold start off the user's first real question. Measured: the first
synthesis of a process took 3.1 s against 250-290 ms for every one after."""
DEFAULT_INITIATIVE_DELAYS = "15,28"
"""Seconds of silence at which the agent considers speaking unprompted, as
positions in one silence rather than gaps between rungs. Set
VOICE_AGENT_INITIATIVE=off, or this to empty, to get the purely reactive agent
of every chapter before this one.

Two numbers, not three. A rung at seven seconds was tried and removed: it never
fired once in eighteen measured considerations, because its job was to invite
the user in and the rules forbid rewording an invitation the greeting has
already made. A short pause is simply not the agent's to fill.

**The last one has to land inside the microphone's idle window**
(`mic.IDLE_TIMEOUT_SECONDS`, 30 s). Measured live at 45 s: listening stopped at
32 s with "no speech for 30s" and the withdrawal never arrived, so the ears
closed without the agent ever saying goodbye. Speaking the withdrawal
restarts that window, so 28 s leaves room for the line and then gives the user
a fresh thirty seconds of quiet to break."""
DEFAULT_VOICE_GENDER = "female"
"""Which grammatical gender the agent uses about itself, matching how its voice
sounds. `female`, `male`, or `neutral` to avoid the choice where a language
allows it.

Only audible in languages that mark the speaker's gender — Russian, Polish,
Hebrew, Arabic and many others put it on past-tense verbs and adjectives. Heard
live: a woman's voice saying «я понял» rather than «я поняла». In text this
would be a typo; spoken, the voice and the grammar disagree in the same
sentence, which is a much louder mistake.

Not derived from the voice id, because which voice sounds like what is not
inferable from the API and this project has been bitten by assuming it is
(`--list-voices` exists for the same reason). The default matches the default
voice; change it when you change `--voice`."""
VOICE_GENDERS = ("female", "male", "neutral")

DEFAULT_SESSIONS = "sessions"
"""Where conversations are written down, or `off` to write none.

Kept rather than expired: this is an archive to look back over, and a retention
rule that deletes the conversation you wanted is worse than a folder that grows.
`--purge-sessions` is the delete. Gitignored, and personal data — a transcript
is what somebody said out loud."""

DEFAULT_TRACE = "traces"
"""Where the technical trace is written, or `off` for none.

Holds whole prompts and whole replies, so it is as sensitive as the conversation
record and gitignored for the same reason. API keys are redacted on the way out
(`trace.scrub`)."""

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

SYSTEM_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "system_prompt.md"
"""Resolved relative to the source checkout. Chapter 1 assumes the server runs
from this repo; packaging the prompt as installed data is a later concern."""

PREAMBLE_SEPARATOR = "\n---\n"
"""`prompts/system_prompt.md` opens with a note explaining the file to a
developer, separated from the prompt itself by a horizontal rule. Only what
follows the first rule is sent to the model — the note is about the file, not
instructions to the agent."""


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
    )


def _optional_float(raw: str | None) -> float | None:
    return float(raw) if raw else None


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
        "Your speaking voice is a woman's. When you speak a language that marks "
        "the speaker's gender, use feminine forms about yourself — «я поняла», "
        "not «я понял»."
    ),
    "male": (
        "Your speaking voice is a man's. When you speak a language that marks "
        "the speaker's gender, use masculine forms about yourself — «я понял», "
        "not «я поняла»."
    ),
    "neutral": (
        "Your speaking voice does not clearly read as a man's or a woman's. "
        "When you speak a language that marks the speaker's gender, prefer "
        "wordings that avoid the choice; where one is unavoidable, pick one and "
        "stay with it for the whole conversation."
    ),
}


def with_voice_gender(prompt: str, gender: str) -> str:
    """Append which gender the agent speaks about itself in.

    At the end, and fixed for the life of the process, so it costs nothing:
    the prefix the provider caches is everything above it, and this never
    changes between turns (ROADMAP §2C).
    """
    return f"{prompt}\n\n{SELF_REFERENCE[gender]}"


def _delays(raw: str) -> tuple[float, ...]:
    """Comma-separated seconds, or `off` for none.

    Rejected rather than repaired if it is malformed: a typo that silently
    produced a mute agent, or one that speaks every half second, is worse than
    a server that will not start and says why.
    """
    if raw.strip().casefold() in ("", "off", "none", "0"):
        return ()
    try:
        delays = tuple(float(part) for part in raw.split(",") if part.strip())
    except ValueError as exc:
        raise ConfigError(f"VOICE_AGENT_INITIATIVE is not a list of seconds: {raw!r}") from exc
    if any(a >= b for a, b in pairwise(delays)) or any(d <= 0 for d in delays):
        raise ConfigError(f"VOICE_AGENT_INITIATIVE must increase and be positive: {raw!r}")
    return delays


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
