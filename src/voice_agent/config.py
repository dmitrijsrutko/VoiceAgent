"""Environment-driven configuration, read once at startup."""

import os
from dataclasses import dataclass
from pathlib import Path

from voice_agent.errors import ConfigError

DEFAULT_PROVIDER = "deepseek"
DEFAULT_VOICE_PROVIDER = "elevenlabs"
DEFAULT_EARS_PROVIDER = "elevenlabs"
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
        host=os.environ.get("VOICE_AGENT_HOST", DEFAULT_HOST),
        port=int(os.environ.get("VOICE_AGENT_PORT", DEFAULT_PORT)),
    )


def _optional_float(raw: str | None) -> float | None:
    return float(raw) if raw else None


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
