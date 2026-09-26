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
from voice_agent.llm.registry import CHOICES, DEFAULT_CHOICE
from voice_agent.llm.registry import available as llm_available
from voice_agent.llm.registry import offered as menu_offered
from voice_agent.stt.registry import EARS, NO_EARS, describe
from voice_agent.stt.registry import available as stt_available


def offered(engine: FakeLLM | None = None, ears: FakeSTT | None = None) -> Backends:
    return Backends("assemblyai", engine=engine, ears=ears)


def test_an_engine_is_built_once_and_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the whole module: the second conversation on a model
    must get the first one's warm connection, not a cold adapter."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    pool = offered()

    assert pool.engine("deepseek-low") is pool.engine("deepseek-low")


def test_different_models_are_different_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    pool = offered()

    assert pool.engine("deepseek-low") is not pool.engine("haiku-4-5")


def test_one_model_at_two_efforts_is_two_instances(monkeypatch: pytest.MonkeyPatch) -> None:
    """Effort is per conversation now, and the pool keys on the option rather
    than on the model, so the same model at two efforts is two adapters with two
    connections. One shared between `low` and `max` would answer at whichever
    of the two happened to be built first."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    pool = offered()

    assert pool.engine("deepseek-low") is not pool.engine("deepseek-max")
    assert pool.engine("deepseek-low").model == pool.engine("deepseek-max").model


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
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    pool = offered()

    assert pool.engine("deepseek-low") is not None  # the one with a key is fine
    with pytest.raises(ConfigError):
        pool.engine("haiku-4-5")  # and the one without only fails when asked


def test_an_injected_engine_serves_every_name() -> None:
    """How a test injects one fake and has it answer whichever stack the code
    under test chooses, so the selection is inert rather than special-cased."""
    fake = FakeLLM()
    pool = offered(engine=fake)

    assert pool.engine("haiku-4-5") is pool.engine("deepseek-low")
    assert pool.engine("whatever-name").provider == fake.provider


def test_an_injected_recognizer_serves_every_name() -> None:
    fake = FakeSTT()
    pool = offered(ears=fake)

    assert pool.ears("assemblyai") is fake
    assert pool.ears("elevenlabs") is fake


def test_the_deaf_name_builds_nothing() -> None:
    assert offered().ears(NO_EARS) is None


def test_each_option_builds_its_own_provider_and_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The menu is data, and every entry of it has to survive being built: an
    Anthropic tier differing only by model, and a DeepSeek tier only by effort.
    What each option sends as an effort is asserted against the wire in
    `test_llm_http.py`; here the option is what the pool is keyed on."""
    for name in ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "sk-test")
    pool = Backends("assemblyai")

    built = {choice.name: pool.engine(choice.name) for choice in CHOICES}

    for choice in CHOICES:
        assert built[choice.name].provider == choice.provider
        assert built[choice.name].model == choice.model

    assert built["haiku-4-5"].model == "claude-haiku-4-5"
    assert built["deepseek-max"].model == "deepseek-flash"


def test_the_menu_offers_only_what_holds_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert menu_offered() == (CHOICES[0],), "no key at all still offers the default"

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    assert {c.provider for c in menu_offered()} == {"anthropic"}

    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    assert {c.provider for c in menu_offered()} == {"anthropic", "deepseek"}

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert {c.provider for c in menu_offered()} == {"anthropic", "deepseek"}, (
        "OpenAI holds a key and offers no models: the menu is the six, not every engine"
    )


def test_the_default_is_v4_1_flash_max(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("DEEPSEEK_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(name, "sk-test")

    pool = Backends("assemblyai")

    assert pool.default_engine == DEFAULT_CHOICE == "deepseek-max"
    assert pool.menu.index(CHOICES[0]) == 0, "the menu keeps its order"

    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    assert Backends("assemblyai").default_engine == "haiku-4-5", (
        "without a DeepSeek key the default is the first model that is offered"
    )


def test_an_option_that_cannot_be_run_is_not_offered(monkeypatch: pytest.MonkeyPatch) -> None:
    """A URL is something anyone can type. An unavailable option falls back to
    the default rather than refusing, and the `ready` frame says what ran."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    pool = Backends("assemblyai")
    conversation = Conversation(id="t")

    chosen, _, _ = pool.choose(conversation, {"llm": "opus-5-5"})

    assert chosen == "deepseek-max"

    kept, _, _ = pool.choose(Conversation(id="u"), {"llm": "deepseek-low"})
    assert kept == "deepseek-low"


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
    backends = Backends("assemblyai", roles=(DEVIL,))
    conversation = Conversation(id="t")

    _, _, first = backends.choose(conversation, {"role": "devils_advocate"})
    _, _, again = backends.choose(conversation, {"role": "none"})

    assert first == again == "devils_advocate"


def test_an_unknown_role_is_the_default_not_an_error() -> None:
    backends = Backends("assemblyai", roles=(DEVIL,), default_role="devils_advocate")

    _, _, role = backends.choose(Conversation(id="t"), {"role": "nobody"})

    assert role == "devils_advocate"


def test_a_preselected_role_that_is_not_a_card_is_the_first_card() -> None:
    assert Backends("assemblyai", roles=(DEVIL,), default_role="nobody").default_role == DEVIL.slug
    assert Backends("assemblyai", default_role="nobody").default_role == "none", (
        "with no cards at all there is nothing else to run"
    )


def test_only_cards_are_offered_as_roles() -> None:
    pool = Backends("assemblyai", roles=(DEVIL,), default_role="devils_advocate")
    offered = pool.choices("haiku-4-5", "assemblyai", "devils_advocate")["role"]
    alone = Backends("assemblyai").choices("haiku-4-5", "assemblyai")["role"]

    assert [(o["name"], o["default"]) for o in offered] == [("devils_advocate", True)]
    assert offered[0]["title"] == DEVIL.name
    assert alone == []
