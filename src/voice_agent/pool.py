"""One backend per name, built on first use and kept for the life of the process.

Chapter 15 lets a conversation choose its reasoning engine and its ears, which
raises the question of when those get built. Per conversation is the obvious
answer and the wrong one.

**Shared, because the connection is the asset.** `llm/http.py` keeps an idle
HTTP connection for 300 seconds, on the adapter's own client — that is what the
"connections kept between turns" measurement bought, after finding that every
streamed call had been reopening one. An adapter per conversation would hand
each new visitor a cold client and pay DNS and TLS again on their first
question, quietly undoing that chapter. Recognizers are shared for a plainer
reason: an `STT` holds no session, `stream()` opens one per listening turn, so
there is nothing per-conversation to keep apart.

**Lazy, because a missing key raises.** Every adapter calls `require_env` in its
constructor, so building the full set at startup would crash any deployment that
holds some keys and not others — which is most of them, and both of mine.
Nothing is built until somebody picks it, and `registry.available()` is what
decides whether they may.
"""

import logging
from collections.abc import Callable

from voice_agent.llm import LLM, create_llm
from voice_agent.llm.traced import Traced
from voice_agent.stt import STT, create_stt

logger = logging.getLogger(__name__)


class Pool:
    """The backends this process has been asked for so far.

    `engine` and `ears` stand in for every name when they are given — that is
    how a test injects one fake and has it serve whichever stack the code under
    test chooses, so the selection is inert rather than special-cased.
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
        # Wrapped once, here: a faked provider injected by a test is traced
        # exactly like a real one, which is why this has never lived in
        # `create_llm`.
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

    def built(self) -> tuple[LLM, ...]:
        """The engines actually built so far — what there is to warm or close."""
        if self._fixed_engine is not None:
            return (self._fixed_engine,)
        return tuple(self._engines.values())
