"""Environment-driven configuration, read once at startup."""

import os
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

from voice_agent import prompts
from voice_agent.errors import ConfigError
from voice_agent.roles import DEFAULT_ROLE

DEFAULT_PROVIDER = "anthropic"
"""The reasoning engine a conversation runs on unless it picks another: the
fastest to first token in `--bench-llm`. Its default *model* is not the one
measured; a deployment sets `VOICE_AGENT_MODEL` (`fly.toml` does)."""
DEFAULT_VOICE_PROVIDER = "elevenlabs"
DEFAULT_EARS_PROVIDER = "assemblyai"

DEFAULT_GREETING = "Hi, I'm a voice agent. What can I help you with?"
"""What the agent says when a conversation opens with no role; empty to open in
silence. A role opens with its own line unless `VOICE_AGENT_GREETING` is set.
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

DEFAULT_LOGS = "off"
"""Where the log is also written, as rotating files, or `off`. The public
instance points it at the volume: Fly's own log buffer holds ~100 lines and is
gone after a deploy."""

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

# The caps (`VOICE_AGENT_MAX_LIVE` etc.) are off unless set; `fly.toml` sets
# them for the public instance. `limits.py` says what each bounds.


@dataclass(frozen=True, slots=True)
class Settings:
    provider: str
    model: str | None
    voice_provider: str
    voice: str | None
    ears_provider: str
    vad_silence: float | None
    greeting: str | None
    """`None` means the role's opening, or `DEFAULT_GREETING` with no role."""
    role: str
    initiative: tuple[float, ...]
    voice_gender: str
    sessions: Path | None
    trace: Path | None
    logs: Path | None
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
        greeting=os.environ.get("VOICE_AGENT_GREETING"),
        role=os.environ.get("VOICE_AGENT_ROLE", DEFAULT_ROLE).strip() or DEFAULT_ROLE,
        sessions=parse_directory(os.environ.get("VOICE_AGENT_SESSIONS", DEFAULT_SESSIONS)),
        trace=parse_directory(os.environ.get("VOICE_AGENT_TRACE", DEFAULT_TRACE)),
        logs=parse_directory(os.environ.get("VOICE_AGENT_LOGS", DEFAULT_LOGS)),
        voice_gender=_voice_gender(
            os.environ.get("VOICE_AGENT_VOICE_GENDER", DEFAULT_VOICE_GENDER)
        ),
        initiative=parse_delays(
            os.environ.get("VOICE_AGENT_INITIATIVE", DEFAULT_INITIATIVE_DELAYS)
        ),
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


def parse_directory(raw: str) -> Path | None:
    value = raw.strip()
    return None if value.casefold() in ("", "off", "none") else Path(value)


def _voice_gender(raw: str) -> str:
    value = raw.strip().casefold()
    if value not in VOICE_GENDERS:
        raise ConfigError(
            f"VOICE_AGENT_VOICE_GENDER must be one of {', '.join(VOICE_GENDERS)}: {raw!r}"
        )
    return value


def with_voice_gender(prompt: str, gender: str) -> str:
    """Append which gender the agent speaks about itself in."""
    rules = prompts.rules()
    return f"{prompt}\n\n{rules[f'Voice: {gender}'].format(scope=rules['Voice scope'])}"


def with_reply_language(prompt: str) -> str:
    return f"{prompt}\n\n{prompts.rules()['Language']}"


def with_languages(prompt: str, languages: tuple[str, ...]) -> str:
    """Append which languages the agent can hear. Nothing for a recognizer
    that reports none."""
    if not languages:
        return prompt
    hearing = prompts.rules()["Hearing"]
    return f"{prompt}\n\n{hearing.format(languages=', '.join(languages))}"


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
    clock = prompts.rules()["Clock"]
    return f"{prompt}\n\n{clock.format(delays=f'{spoken} seconds')}"


def parse_delays(raw: str) -> tuple[float, ...]:
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


def build_prompt(
    languages: tuple[str, ...],
    gender: str,
    delays: tuple[float, ...],
    base: str | None = None,
    greeting: str = "",
    role: str = "",
) -> str:
    """The whole system prompt for one conversation.

    The appended facts (voice, hearing, clock) are fixed for the conversation
    and sit below the persona, so the provider's cached prefix stays stable.
    `role` is a role card's "When speaking" section: directly under the
    persona, which it narrows, and above the facts.
    """
    rules = prompts.rules()
    prompt = prompts.load("system_prompt") if base is None else base
    identity = rules.get(f"Identity: {gender}", "")
    if identity:
        prompt = f"{identity}\n\n{prompt}"
    if role:
        prompt = f"{prompt}\n\n# Your role in this conversation\n\n{role}"
    prompt = with_initiative(with_languages(prompt, languages), delays)
    if greeting:
        prompt = f"{prompt}\n\n{rules['Opened'].format(greeting=greeting)}"
    # The two rules a fast model kept breaking go last, nearest the conversation:
    # buried under the hearing and clock notes, the gender line was ignored.
    return with_voice_gender(with_reply_language(prompt), gender)


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(
            f"{name} is not set. Put it in .env and run with `uv run --env-file .env ...`, "
            f"or export it into your shell."
        )
    return value
