"""Environment-driven configuration, read once at startup."""

import os
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from voice_agent.errors import ConfigError

DEFAULT_PROVIDER = "deepseek"
DEFAULT_VOICE_PROVIDER = "elevenlabs"
DEFAULT_EARS_PROVIDER = "assemblyai"

DEFAULT_GREETING = "Hi, I'm a voice agent. What can I help you with?"
"""What the agent says when a conversation opens. Set VOICE_AGENT_GREETING to
change it, or to empty to open in silence.

It earns its place twice. It is the greeting the roadmap asks for — the moment
that sets expectations about what this thing is — and it moves the synthesis
engine's cold start off the user's first real question. Measured: the first
synthesis of a process took 3.1 s against 250-290 ms for every one after."""
DEFAULT_INITIATIVE_DELAYS = "5,15,28"
"""Seconds of silence at which the agent considers speaking unprompted, as
positions in one silence rather than gaps between rungs. Set
VOICE_AGENT_INITIATIVE=off, or this to empty, to get the purely reactive agent
of every chapter before this one.

Three numbers since chapter 13. It was two, and the note here used to say that a
short pause is "simply not the agent's to fill" — drawn from a seven-second rung
that was tried and removed after never firing in eighteen considerations. That
conclusion was too broad. The seven-second rung failed because its *job* was to
invite the user in, and the rules forbid rewording an invitation the greeting has
already made; every move it had was illegal. The delay was never the problem, and
a rung that follows through on what was just said has legal moves at five seconds
(`initiative.LADDER`).

Fifteen also read far longer than it sounds. The clock is pushed forward by the
agent's own speech (`Mic.expect_silence`), so it counts from the moment the voice
stops — fifteen seconds after a twenty-second answer is thirty-five seconds of
nothing.

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
"""What the agent is told about the languages it can hear.

Appended for the same reason as the gender line, and in the same place. The
recognizer does not fail on a language it lacks — it returns confident nonsense,
which the agent then answers as though it were a question. Measured: spoken
Russian arrived as "Period." and the agent asked what was meant by it, twice.

Two jobs, because the failure has two halves. The agent must stop *promising* a
language it cannot hear — as a model it speaks Russian perfectly well, and
nothing before this told it that its ears do not. And it must recognise the
garbage for what it is when it arrives, because that is the only moment anything
in the running system can notice, the transcript being all the agent ever sees."""


def with_languages(prompt: str, languages: tuple[str, ...]) -> str:
    """Append which languages the agent can hear, if that is a limit at all.

    At the end, with the gender line, and fixed for the life of the process —
    the cached prefix is everything above it and never changes between turns.
    A backend that reports nothing (`--stt none`, a recognizer with no opinion)
    adds nothing, rather than telling the agent it hears an empty set.
    """
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
"""What the agent is told about its own clock.

Because it was asked and made a number up. Requested live — "what is the time
between your last message and your repeated message if there is silence?" — the
agent answered "A few seconds, typically", when the first rung was at fifteen.
Nothing had ever told it, so it did what a model does with a question about its
own body and invented a plausible answer. The same failure as claiming to hear
Russian, and the same fix: state the fact, once, where it cannot drift."""


def with_initiative(prompt: str, delays: tuple[float, ...]) -> str:
    """Append when the agent may speak unprompted, if it may at all.

    Fed from the ladder the session actually runs, not from the default, so the
    prompt cannot disagree with the clock. `--initiative off` adds nothing —
    an agent that never speaks first has nothing to say about when it does.
    """
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
