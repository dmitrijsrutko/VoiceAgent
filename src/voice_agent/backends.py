"""Which backends a conversation may choose, and one shared instance of each.

**Shared, because the connection is the asset.** `llm/http.py` keeps an idle
HTTP connection on the adapter's own client; an adapter per conversation would
pay DNS and TLS again on every visitor's first question. An `STT` holds no
session (`stream()` opens one per listening turn), and a `TTS` opens a socket
per reply, so both are shared too.

**Lazy, because a missing key raises.** Every adapter calls `require_env` in its
constructor, so nothing is built until somebody picks it.

**Only what holds a key is offered.** With no key for anything, the default
model is offered anyway, so a misconfigured deployment fails at the first call
naming the missing variable rather than with an empty page. Voices are the
exception: every option shares the backend's one key, so all are offered and
`create_app` builds the default at startup, where a missing key stops the server.
"""

import logging
from collections.abc import Mapping, Sequence

from voice_agent.conversation import Conversation
from voice_agent.llm import LLM, create_llm
from voice_agent.llm.registry import BY_NAME, Choice, default_choice, offered
from voice_agent.llm.traced import Traced
from voice_agent.roles import NO_ROLE, Role
from voice_agent.stt import STT, create_stt
from voice_agent.stt.registry import NO_EARS, describe
from voice_agent.stt.registry import available as stt_available
from voice_agent.tts import TTS
from voice_agent.tts import registry as tts_registry

logger = logging.getLogger(__name__)


class Backends:
    """The engines, ears, voices and roles this deployment offers, each
    conversation's pick, and the one instance of each backend every
    conversation shares.

    `engine`, `ears` and `speaker`, when given, serve every name: a test injects one fake
    and the selection logic still runs unchanged.
    """

    def __init__(
        self,
        ears_provider: str,
        *,
        silence: float | None = None,
        hears: bool = True,
        roles: Sequence[Role] = (),
        default_role: str = NO_ROLE,
        engine: LLM | None = None,
        ears: STT | None = None,
        voice_provider: str = tts_registry.NO_VOICE,
        voice: str | None = None,
        speaker: TTS | None = None,
    ) -> None:
        self._silence = silence
        self.roles = tuple(roles)
        names = {role.slug for role in self.roles}
        # A card that is not there falls back to the first there is. The plain
        # assistant is never offered, but an operator (or a test) may still run
        # every conversation as one by pre-selecting it.
        self.default_role = (
            default_role
            if default_role in names or default_role == NO_ROLE
            else next((role.slug for role in self.roles), NO_ROLE)
        )
        self.menu: tuple[Choice, ...] = offered()
        """The models this deployment can run, fastest first: what the page
        offers and what a conversation may be pinned to."""
        self._names = {choice.name for choice in self.menu}
        self.default_engine = default_choice(self.menu)
        # `none` configured means deaf, even where recognizer keys are present.
        hears = hears and ears_provider != NO_EARS
        self.listeners: tuple[str, ...] = (stt_available() or (ears_provider,)) if hears else ()
        self.default_ears = (
            ears_provider
            if ears_provider in self.listeners
            else next(iter(self.listeners), NO_EARS)
        )
        offered_voices = tts_registry.offered(voice_provider)
        self.voices: tuple[tts_registry.Option, ...] = (
            offered_voices if speaker is None else offered_voices or tts_registry.MENU
        )
        """The voice models the page offers; none when silent."""
        self.default_voice = tts_registry.default_choice(self.voices) or tts_registry.NO_VOICE
        self._voice = voice
        self._fixed_speaker = speaker
        self._built_speakers: dict[str, TTS] = {}
        # Wrapped here, so an injected fake is traced exactly like a real one.
        self._fixed_engine = Traced(engine) if engine is not None else None
        self._fixed_ears = ears
        self._built_engines: dict[str, LLM] = {}
        self._built_ears: dict[str, STT] = {}

    def choose(
        self, conversation: Conversation, asked: Mapping[str, str]
    ) -> tuple[str, str, str, str]:
        """The stack and role this conversation runs, pinned on its first connect.

        A reconnect keeps what the history was made with. An unknown or
        unavailable name falls back to the default rather than refusing: this is
        a URL anyone can type, and the `ready` frame says what actually ran.
        """
        if conversation.engine is None:
            wanted = asked.get("llm", "")
            conversation.engine = wanted if wanted in self._names else self.default_engine
            heard = asked.get("stt", "")
            conversation.ears = heard if heard in self.listeners else self.default_ears
            wanted_role = asked.get("role", "")
            known = {role.slug for role in self.roles}
            conversation.role = wanted_role if wanted_role in known else self.default_role
            spoken = asked.get("tts", "")
            names = {option.name for option in self.voices}
            conversation.voice = spoken if spoken in names else self.default_voice
        return (
            conversation.engine,
            conversation.ears or NO_EARS,
            conversation.role or NO_ROLE,
            conversation.voice or tts_registry.NO_VOICE,
        )

    def choices(
        self, engine: str, ears: str, role: str = NO_ROLE, voice: str = tts_registry.NO_VOICE
    ) -> dict[str, list[dict[str, object]]]:
        """What the page may offer, with `engine`, `ears`, `role` and `voice` marked as
        chosen. Recognizers are described, not built, so no key is needed to
        list one. Only the cards are roles; the plain assistant is not offered."""
        roles: list[dict[str, object]] = [
            {"name": r.slug, "title": r.name, "summary": r.summary, "default": r.slug == role}
            for r in self.roles
        ]
        return {
            "role": roles,
            "llm": [
                {
                    "name": choice.name,
                    "title": choice.title,
                    "provider": choice.provider,
                    "model": choice.model,
                    "default": choice.name == engine,
                }
                for choice in self.menu
            ],
            "stt": [{**describe(name), "default": name == ears} for name in self.listeners],
            "tts": [
                {
                    "name": option.name,
                    "title": option.title,
                    "hint": option.hint,
                    "model": option.model,
                    "default": option.name == voice,
                }
                for option in self.voices
            ],
        }

    def engine(self, name: str) -> LLM:
        if self._fixed_engine is not None:
            return self._fixed_engine
        if name not in self._built_engines:
            choice = BY_NAME[name]
            logger.info("building the %s engine", choice.name)
            self._built_engines[name] = Traced(
                create_llm(choice.provider, choice.model, choice.effort)
            )
        return self._built_engines[name]

    def ears(self, name: str) -> STT | None:
        if self._fixed_ears is not None:
            return self._fixed_ears
        if name not in self._built_ears:
            listener = create_stt(name, self._silence)
            if listener is None:
                return None  # `none`: deaf on purpose, and nothing to keep
            logger.info("building the %s recognizer", name)
            self._built_ears[name] = listener
        return self._built_ears[name]

    def speaker(self, name: str) -> TTS | None:
        if name not in tts_registry.BY_NAME:
            return None  # silent on purpose
        if self._fixed_speaker is not None:
            return self._fixed_speaker
        if name not in self._built_speakers:
            option = tts_registry.BY_NAME[name]
            logger.info("building the %s voice", name)
            # The builder, not `create_tts`: an option is never `none`, and a
            # test keeps deprecated models off the menu.
            build = tts_registry.BUILDERS[option.provider]
            self._built_speakers[name] = build(self._voice, option.model)
        return self._built_speakers[name]
