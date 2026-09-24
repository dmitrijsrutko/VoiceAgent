"""One backend per name, shared — because the connection is the asset.

`llm/http.py` keeps an idle HTTP connection for 300 s on the adapter's own
client, which is what the "connections kept between turns" chapter bought after
finding every streamed call reopening one. A pool that handed each conversation
its own adapter would undo that silently, and nothing downstream would notice.
"""

import pytest

from tests.conftest import FakeLLM, FakeSTT
from voice_agent import roles as roles_module
from voice_agent.backends import Backends
from voice_agent.conversation import Conversation
from voice_agent.errors import ConfigError
from voice_agent.llm.registry import ENGINES
from voice_agent.llm.registry import available as llm_available
from voice_agent.stt.registry import EARS, NO_EARS, describe
from voice_agent.stt.registry import available as stt_available


def offered(engine: FakeLLM | None = None, ears: FakeSTT | None = None) -> Backends:
    return Backends("anthropic", None, "assemblyai", engine=engine, ears=ears)


def test_an_engine_is_built_once_and_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the whole module: the second conversation on a provider
    must get the first one's warm connection, not a cold adapter."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    pool = offered()

    assert pool.engine("deepseek") is pool.engine("deepseek")


def test_different_engines_are_different_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    pool = offered()

    assert pool.engine("deepseek") is not pool.engine("openai")


def test_recognizers_are_shared_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """An `STT` holds no session — `stream()` opens one per listening turn — so
    there is nothing per-conversation to keep apart."""
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "sk-test")
    pool = offered()

    assert pool.ears("assemblyai") is pool.ears("assemblyai")


def test_nothing_is_built_until_it_is_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every adapter calls `require_env` in its constructor, so a pool that
    built eagerly would crash any deployment holding some keys and not others —
    which is most of them."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    pool = offered()

    assert pool.engine("deepseek") is not None  # the one with a key is fine
    with pytest.raises(ConfigError):
        pool.engine("openai")  # and the one without only fails when asked


def test_an_injected_engine_serves_every_name() -> None:
    """How a test injects one fake and has it answer whichever stack the code
    under test chooses, so the selection is inert rather than special-cased."""
    fake = FakeLLM()
    pool = offered(engine=fake)

    assert pool.engine("anthropic") is pool.engine("deepseek")
    assert pool.engine("whatever-name").provider == fake.provider


def test_an_injected_recognizer_serves_every_name() -> None:
    fake = FakeSTT()
    pool = offered(ears=fake)

    assert pool.ears("assemblyai") is fake
    assert pool.ears("elevenlabs") is fake


def test_the_deaf_name_builds_nothing() -> None:
    assert offered().ears(NO_EARS) is None


def test_the_model_override_is_asked_per_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """`VOICE_AGENT_MODEL` names a model, and a model belongs to one provider.
    Handing the deployment's `claude-haiku-4-5` to DeepSeek would ask DeepSeek
    for a model it has never heard of."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    pool = Backends("anthropic", "claude-haiku-4-5", "assemblyai")

    assert pool.engine("anthropic").model == "claude-haiku-4-5"
    assert pool.engine("deepseek").model == ENGINES["deepseek"].default_model


# --- what the registries can answer without building anything ---------------


def test_availability_follows_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    assert llm_available() == ()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert llm_available() == ("anthropic",)


def test_a_key_set_but_empty_counts_as_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """`.env.example` ships every name with nothing after the `=`, so treating
    "" as configured would offer every provider on a machine that has none."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.setenv("OPENAI_API_KEY", "   ")

    assert "anthropic" not in llm_available()
    assert "openai" not in llm_available()


def test_ears_can_be_described_without_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The start screen says what each recognizer hears before one is chosen,
    and a deployment need not hold every key to say it. `describe` reads the
    modules' own constants; `STT.languages` would need an instance."""
    for name in ("ASSEMBLYAI_API_KEY", "ELEVENLABS_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    assert stt_available() == ()
    assert len(describe("assemblyai")["languages"]) == 18  # type: ignore[arg-type]
    assert len(describe("elevenlabs")["languages"]) == 100  # type: ignore[arg-type]


def test_the_recognizers_differ_in_the_way_chapter_12_cared_about() -> None:
    """The fact worth showing at the moment of choosing: Scribe hears Russian
    and AssemblyAI does not, and a language it lacks becomes confident nonsense
    rather than an error."""
    assert "ru" not in EARS["assemblyai"].languages
    assert "rus" in EARS["elevenlabs"].languages


DEVIL = roles_module.load("devils_advocate")


def test_a_conversation_picks_its_role_and_keeps_it() -> None:
    backends = Backends("anthropic", None, "assemblyai", roles=(DEVIL,))
    conversation = Conversation(id="t")

    _, _, first = backends.choose(conversation, {"role": "devils_advocate"})
    _, _, again = backends.choose(conversation, {"role": "none"})

    assert first == again == "devils_advocate"


def test_an_unknown_role_is_the_default_not_an_error() -> None:
    backends = Backends(
        "anthropic", None, "assemblyai", roles=(DEVIL,), default_role="devils_advocate"
    )

    _, _, role = backends.choose(Conversation(id="t"), {"role": "nobody"})

    assert role == "devils_advocate"


def test_a_preselected_role_that_is_not_a_card_is_no_role() -> None:
    assert Backends("anthropic", None, "assemblyai", default_role="nobody").default_role == "none"


def test_the_plain_assistant_is_offered_first_and_only_with_a_card_to_choose() -> None:
    offered = Backends("anthropic", None, "assemblyai", roles=(DEVIL,)).choices(
        "anthropic", "assemblyai"
    )["role"]
    alone = Backends("anthropic", None, "assemblyai").choices("anthropic", "assemblyai")["role"]

    assert [(o["name"], o["default"]) for o in offered] == [
        ("none", True),
        ("devils_advocate", False),
    ]
    assert offered[1]["title"] == DEVIL.name
    assert alone == []
