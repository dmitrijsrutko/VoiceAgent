"""Which backends a conversation may choose, and one shared instance of each.

**Shared, because the connection is the asset.** `llm/http.py` keeps an idle
HTTP connection on the adapter's own client; an adapter per conversation would
pay DNS and TLS again on every visitor's first question. An `STT` holds no
session (`stream()` opens one per listening turn), so it is shared too.

**Lazy, because a missing key raises.** Every adapter calls `require_env` in its
constructor, so nothing is built until somebody picks it.
"""

import logging
from collections.abc import Callable, Mapping, Sequence

from voice_agent.conversation import Conversation
from voice_agent.llm import LLM, create_llm
from voice_agent.llm.registry import DEFAULT_MODELS
from voice_agent.llm.registry import available as llm_available
from voice_agent.llm.traced import Traced
from voice_agent.roles import NO_ROLE, Role
from voice_agent.stt import STT, create_stt
from voice_agent.stt.registry import NO_EARS, describe
from voice_agent.stt.registry import available as stt_available

logger = logging.getLogger(__name__)


class Stack:
    """The engines and ears this deployment offers, and each conversation's pick.

    Only what holds a key is offered. With no key for anything, the configured
    default is offered anyway, so a misconfigured deployment fails at the first
    call naming the missing variable rather than with an empty page.
    """

    def __init__(
        self,
        provider: str,
        model: str | None,
        ears_provider: str,
        *,
        hears: bool = True,
        roles: Sequence[Role] = (),
        default_role: str = NO_ROLE,
    ) -> None:
        self._provider = provider
        self.roles = tuple(roles)
        names = {role.slug for role in self.roles}
        self.default_role = default_role if default_role in names else NO_ROLE
        self._model = model
        self.engines: tuple[str, ...] = llm_available() or (provider,)
        # `none` configured means deaf, even where recognizer keys are present.
        hears = hears and ears_provider != NO_EARS
        self.listeners: tuple[str, ...] = (stt_available() or (ears_provider,)) if hears else ()
        self.default_engine = provider if provider in self.engines else self.engines[0]
        self.default_ears = (
            ears_provider
            if ears_provider in self.listeners
            else next(iter(self.listeners), NO_EARS)
        )

    def model_for(self, provider: str) -> str | None:
        """The model override, for the provider it was configured alongside
        only: a Claude model name means nothing to DeepSeek."""
        return self._model if provider == self._provider else None

    def model_named(self, provider: str) -> str:
        return self.model_for(provider) or DEFAULT_MODELS[provider]

    def choose(self, conversation: Conversation, asked: Mapping[str, str]) -> tuple[str, str, str]:
        """The stack and role this conversation runs, pinned on its first connect.

        A reconnect keeps what the history was made with. An unknown or
        unavailable name falls back to the default rather than refusing: this is
        a URL anyone can type, and the `ready` frame says what actually ran.
        """
        if conversation.engine is None:
            wanted = asked.get("llm", "")
            conversation.engine = wanted if wanted in self.engines else self.default_engine
            heard = asked.get("stt", "")
            conversation.ears = heard if heard in self.listeners else self.default_ears
            wanted_role = asked.get("role", "")
            known = {role.slug for role in self.roles} | {NO_ROLE}
            conversation.role = wanted_role if wanted_role in known else self.default_role
        return conversation.engine, conversation.ears or NO_EARS, conversation.role or NO_ROLE

    def choices(
        self, engine: str, ears: str, role: str = NO_ROLE
    ) -> dict[str, list[dict[str, object]]]:
        """What the page may offer, with `engine`, `ears` and `role` marked as
        chosen. Recognizers are described, not built, so no key is needed to
        list one. The plain assistant is always the first role; no cards at all
        means no role to choose."""
        roles: list[dict[str, object]] = (
            [
                {
                    "name": NO_ROLE,
                    "title": "None",
                    "summary": "a plain assistant",
                    "default": role == NO_ROLE,
                },
                *(
                    {
                        "name": r.slug,
                        "title": r.name,
                        "summary": r.summary,
                        "default": r.slug == role,
                    }
                    for r in self.roles
                ),
            ]
            if self.roles
            else []
        )
        return {
            "role": roles,
            "llm": [
                {"name": name, "model": self.model_named(name), "default": name == engine}
                for name in self.engines
            ],
            "stt": [{**describe(name), "default": name == ears} for name in self.listeners],
        }


class Pool:
    """The backends this process has been asked for so far.

    `engine` and `ears`, when given, serve every name: a test injects one fake
    and the selection logic still runs unchanged.
    """

    def __init__(
        self,
        model_for: Callable[[str], str | None],
        silence: float | None = None,
        engine: LLM | None = None,
        ears: STT | None = None,
    ) -> None:
        self._model_for = model_for
        self._silence = silence
        # Wrapped here, so an injected fake is traced exactly like a real one.
        self._fixed_engine = Traced(engine) if engine is not None else None
        self._fixed_ears = ears
        self._engines: dict[str, LLM] = {}
        self._ears: dict[str, STT] = {}

    def engine(self, name: str) -> LLM:
        if self._fixed_engine is not None:
            return self._fixed_engine
        if name not in self._engines:
            logger.info("building the %s engine", name)
            self._engines[name] = Traced(create_llm(name, self._model_for(name)))
        return self._engines[name]

    def ears(self, name: str) -> STT | None:
        if self._fixed_ears is not None:
            return self._fixed_ears
        if name not in self._ears:
            listener = create_stt(name, self._silence)
            if listener is None:
                return None  # `none`: deaf on purpose, and nothing to keep
            logger.info("building the %s recognizer", name)
            self._ears[name] = listener
        return self._ears[name]
