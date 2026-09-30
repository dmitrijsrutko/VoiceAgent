"""Name -> recognition backend. The only place that knows which ears exist."""

import os
from collections.abc import Callable
from dataclasses import dataclass

from voice_agent.errors import ConfigError
from voice_agent.stt import assemblyai_stt, elevenlabs_stt
from voice_agent.stt.assemblyai_stt import AssemblyAISTT
from voice_agent.stt.base import STT
from voice_agent.stt.elevenlabs_stt import ElevenLabsSTT

NO_EARS = "none"
"""Not a backend: the explicit way to run the agent deaf, so typing still works
for anyone without a recognition key."""


@dataclass(frozen=True, slots=True)
class Ears:
    key: str
    model: str
    """The vendor's model id, named here so the page, the record and the trace
    can say *which* recognizer ran — two vendors are two models, and a vendor
    ships new ones under the same name."""
    title: str
    """What the page calls the model, without its vendor: the page supplies that
    from `PROVIDERS`, as it does for the voice menu."""
    hint: str
    """The one thing worth knowing when choosing, in place of a spec sheet."""
    languages: tuple[str, ...]
    """What it can hear, from the module's own constant rather than an
    instance: the start screen says it before a choice is made, and possibly
    without holding every key."""
    sample_rate: int
    build: Callable[[float | None], STT]


EARS: dict[str, Ears] = {
    "assemblyai": Ears(
        key="ASSEMBLYAI_API_KEY",
        model=assemblyai_stt.DEFAULT_MODEL,
        title="Universal-3.6 Pro Realtime",
        hint="native code-switching",
        languages=assemblyai_stt.LANGUAGES,
        sample_rate=assemblyai_stt.SAMPLE_RATE,
        build=lambda silence: AssemblyAISTT(silence_seconds=silence),
    ),
    "elevenlabs": Ears(
        # Scribe v2 Realtime is billed on the same key as synthesis.
        key="ELEVENLABS_API_KEY",
        model=elevenlabs_stt.DEFAULT_MODEL,
        title="Scribe v2 Realtime",
        hint="widest language cover",
        languages=elevenlabs_stt.LANGUAGES,
        sample_rate=elevenlabs_stt.SAMPLE_RATE,
        build=lambda silence: ElevenLabsSTT(silence_seconds=silence),
    ),
}
"""In the order the start screen draws them, whichever is the default."""


def available() -> tuple[str, ...]:
    """The recognizers this deployment could actually use, in `EARS` order.
    An empty key counts as absent, as in `llm.registry.available`."""
    return tuple(name for name, e in EARS.items() if os.environ.get(e.key, "").strip())


def describe(name: str) -> dict[str, object]:
    """What a choice of ears would mean, without building one. The model and its
    title travel with it for the same reason the LLM and voice menus carry
    theirs: what a choice is called is the server's to say, so the page cannot
    advertise a model that will not run."""
    ears = EARS[name]
    return {
        "name": name,
        "provider": name,
        "model": ears.model,
        "title": ears.title,
        "hint": ears.hint,
        "languages": list(ears.languages),
        "sample_rate": ears.sample_rate,
    }


def create_stt(provider: str, silence_seconds: float | None = None) -> STT | None:
    if provider == NO_EARS:
        return None
    try:
        ears = EARS[provider]
    except KeyError:
        known = ", ".join([*sorted(EARS), NO_EARS])
        raise ConfigError(f"unknown ears {provider!r}; expected one of: {known}") from None
    return ears.build(silence_seconds)
