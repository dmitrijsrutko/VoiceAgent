"""The caps a public address needs — and, as much as anything, that they are off.

The second half matters more than the first. Chapters 1-13 were built and
measured on localhost, and a cap that quietly engaged there would mean the thing
being developed was no longer the thing being deployed.
"""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from tests.conftest import FakeLLM, receive
from voice_agent.config import load_settings
from voice_agent.errors import ConfigError
from voice_agent.limits import MINT_WINDOW, Live, MintLimit, budget_reason, client_address
from voice_agent.server import create_app
from voice_agent.sessions import SessionStore


def test_no_cap_admits_anything() -> None:
    live = Live(None)

    assert all(live.take() for _ in range(1000))


def test_a_cap_refuses_past_it_and_lets_go_again() -> None:
    live = Live(2)

    assert live.take() and live.take()
    assert not live.take()

    live.release()

    assert live.take()


def test_releasing_more_than_was_taken_does_not_bank_slots() -> None:
    """A double release would otherwise make the counter negative, and the cap
    would admit one extra conversation for every stray one."""
    live = Live(1)
    live.release()
    live.release()

    assert live.take()
    assert not live.take()


def test_an_unset_mint_limit_allows_everything() -> None:
    mints = MintLimit(None)

    assert all(mints.allow("1.2.3.4", now=0.0) for _ in range(100))


def test_the_allowance_is_spent_and_then_refills() -> None:
    mints = MintLimit(3, window=30.0)

    assert [mints.allow("a", now=0.0) for _ in range(4)] == [True, True, True, False]
    # A third of the window is one token back, no more.
    assert mints.allow("a", now=10.0)
    assert not mints.allow("a", now=10.0)


def test_addresses_are_counted_apart() -> None:
    mints = MintLimit(1, window=30.0)

    assert mints.allow("a", now=0.0)
    assert not mints.allow("a", now=0.0)
    assert mints.allow("b", now=0.0)


def test_hammering_the_door_does_not_reset_the_clock() -> None:
    """A refusal writes nothing. Recording the recomputed balance on every one
    instead accumulates the float error of a hundred small additions, and this
    bucket sat at 0.999... after its full window — refusing forever."""
    mints = MintLimit(1, window=10.0)

    assert mints.allow("a", now=0.0)
    for second in range(1, 10):
        assert not mints.allow("a", now=float(second))

    assert mints.allow("a", now=10.0)


def test_spent_addresses_are_forgotten_once_they_have_refilled() -> None:
    mints = MintLimit(1, window=1.0)
    for n in range(2000):
        assert mints.allow(f"addr-{n}", now=0.0)

    # A window later every one of them is back to full, and an entry saying so
    # says nothing an absent entry does not.
    mints.allow("late", now=5.0)

    assert len(mints._spent) < 2000


def test_the_address_comes_from_the_proxy_before_the_socket() -> None:
    assert client_address({"fly-client-ip": "9.9.9.9"}, "172.16.0.1") == "9.9.9.9"
    assert client_address({"x-forwarded-for": "9.9.9.9, 10.0.0.1"}, "172.16.0.1") == "9.9.9.9"
    assert client_address({}, "127.0.0.1") == "127.0.0.1"
    assert client_address({}, None) == "unknown"


def test_a_blank_header_falls_through_rather_than_becoming_the_address() -> None:
    """An empty `X-Forwarded-For` would otherwise put every caller behind one
    bucket named "", and the first of them would spend everybody's allowance."""
    assert client_address({"fly-client-ip": "", "x-forwarded-for": " "}, "127.0.0.1") == "127.0.0.1"


def test_the_budget_reason_names_the_length_it_enforced() -> None:
    assert "5 minutes" in budget_reason(300)
    assert "30 seconds" in budget_reason(30)


def test_the_store_keeps_everything_when_uncapped() -> None:
    store = SessionStore()
    for _ in range(50):
        store.create()

    assert len(store) == 50


def test_a_capped_store_drops_the_oldest_first() -> None:
    store = SessionStore(cap=3)
    keys = [store.create().id for _ in range(5)]

    assert len(store) == 3
    assert keys[0] not in store and keys[1] not in store
    assert all(key in store for key in keys[2:])


@pytest.mark.parametrize(
    "name",
    ["VOICE_AGENT_MAX_LIVE", "VOICE_AGENT_MINTS_PER_IP", "VOICE_AGENT_MAX_STORED"],
)
def test_a_malformed_cap_stops_the_server_rather_than_becoming_no_cap(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure mode being avoided: a typo in a spend ceiling that reads as
    "unlimited" and is only discovered on the invoice."""
    monkeypatch.setenv(name, "four")

    with pytest.raises(ConfigError):
        load_settings()


@pytest.mark.parametrize("value", ["0", "-1", "2.5"])
def test_a_cap_must_be_a_positive_whole_number(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_AGENT_MAX_LIVE", value)

    with pytest.raises(ConfigError):
        load_settings()


@pytest.mark.parametrize("value", ["", "off", "none"])
def test_off_and_unset_mean_the_same_no_cap(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_AGENT_MAX_LIVE", value)
    monkeypatch.setenv("VOICE_AGENT_SESSION_BUDGET", value)

    settings = load_settings()

    assert settings.max_live is None
    assert settings.session_budget is None


def test_nothing_is_capped_by_default(llm: FakeLLM) -> None:
    """The whole point: a local run is chapters 1-13 exactly, untouched."""
    settings = load_settings()

    assert (settings.max_live, settings.session_budget) == (None, None)
    assert (settings.mints_per_ip, settings.max_stored) == (None, None)

    client = TestClient(create_app(llm=llm, voice=False, ears=False, greeting=""))
    with client:
        keys = [client.get("/", follow_redirects=False).headers["location"] for _ in range(30)]

    assert len(set(keys)) == 30


def test_the_mint_window_is_a_constant_rather_than_a_knob() -> None:
    assert MintLimit(1)._window == MINT_WINDOW


# --- the caps as the server actually applies them ---------------------------


def capped(llm: FakeLLM, monkeypatch: pytest.MonkeyPatch, **caps: str) -> TestClient:
    """A deaf, silent agent with some caps turned on, as `fly.toml` turns them
    on: through the environment, which is the one way configuration enters."""
    for name, value in caps.items():
        monkeypatch.setenv(f"VOICE_AGENT_{name.upper()}", value)
    return TestClient(create_app(llm=llm, voice=False, ears=False, greeting=""))


def mint(client: TestClient) -> str:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"].removeprefix("/c/")


def test_too_many_conversations_at_once_are_turned_away_at_the_socket(
    llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = capped(llm, monkeypatch, max_live="1")
    with client:
        first, second = mint(client), mint(client)
        with client.websocket_connect(f"/ws/{first}") as socket:
            assert receive(socket)["type"] == "ready"

            # Read, rather than merely connect. Since `refuse` accepts before
            # closing — which is what makes the reason reach a browser at all —
            # the handshake succeeds and the refusal arrives as the first thing
            # on the socket. A test that only connected would now pass a server
            # that had stopped refusing anything.
            with (
                pytest.raises(WebSocketDisconnect) as refused,
                client.websocket_connect(f"/ws/{second}") as busy,
            ):
                receive(busy)

    # Refused with a reason, so the page can say why rather than showing the
    # blank "disconnected" a dropped network shows. That the reason survives to
    # a *browser* is not testable from here — the test client surfaces it either
    # way, which is exactly how `refuse` closing before accepting went unnoticed.
    # See the CHANGELOG's verification: it took a real client to see it.
    assert refused.value.code == 4429
    assert "busy" in str(refused.value.reason)
    assert len(str(refused.value.reason).encode()) <= 123, "close frames cap the reason"


def test_the_slot_comes_back_when_a_conversation_hangs_up(
    llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = capped(llm, monkeypatch, max_live="1")
    with client:
        for _ in range(3):
            key = mint(client)
            with client.websocket_connect(f"/ws/{key}") as socket:
                assert receive(socket)["type"] == "ready"


def test_a_conversation_ends_itself_when_its_budget_runs_out(
    llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = capped(llm, monkeypatch, session_budget="0.2")
    with client:
        key = mint(client)
        with client.websocket_connect(f"/ws/{key}") as socket:
            assert receive(socket)["type"] == "ready"
            ended = receive(socket)

    assert ended["type"] == "ended"
    # Named, because an agent that hangs up without saying why reads as broken.
    assert "public demo" in str(ended["reason"])


def test_the_budget_hangs_up_as_well_as_ending(
    llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ending alone would leave the socket — and the slot it holds — occupied
    by a conversation that is over: the receive loop only re-reads `ended`
    after another frame arrives, and a silent browser sends none."""
    client = capped(llm, monkeypatch, session_budget="0.2", max_live="1")
    with client:
        key = mint(client)
        with client.websocket_connect(f"/ws/{key}") as socket:
            assert receive(socket)["type"] == "ready"
            assert receive(socket)["type"] == "ended"
            with pytest.raises(WebSocketDisconnect):
                receive(socket)

        # The slot is free again, which it would not be if the socket were held.
        with client.websocket_connect(f"/ws/{mint(client)}") as socket:
            assert receive(socket)["type"] == "ready"


def test_one_address_cannot_mint_conversations_without_end(
    llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = capped(llm, monkeypatch, mints_per_ip="3")
    with client:
        assert [client.get("/", follow_redirects=False).status_code for _ in range(4)] == [
            303,
            303,
            303,
            429,
        ]


def test_a_link_already_held_still_works_once_the_door_is_shut(
    llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The limit is on *minting*, not on talking. Somebody rate-limited while
    mid-conversation would be the cap breaking the thing it exists to protect."""
    client = capped(llm, monkeypatch, mints_per_ip="1")
    with client:
        key = mint(client)
        assert client.get("/", follow_redirects=False).status_code == 429
        assert client.get(f"/c/{key}").status_code == 200
        with client.websocket_connect(f"/ws/{key}") as socket:
            assert receive(socket)["type"] == "ready"


def test_the_page_is_told_whether_it_is_being_written_down(
    llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A public instance records, and says so before anybody speaks. The flag
    travels on the frame the page already reads to learn what it can hear."""
    client = TestClient(create_app(llm=llm, voice=False, ears=False, greeting=""))
    with client, client.websocket_connect(f"/ws/{mint(client)}") as socket:
        assert receive(socket)["recording"] is True

    silent = TestClient(create_app(llm=llm, voice=False, ears=False, greeting="", record=False))
    with silent, silent.websocket_connect(f"/ws/{mint(silent)}") as socket:
        assert receive(socket)["recording"] is False


def test_healthz_is_ready_rather_than_merely_alive(llm: FakeLLM) -> None:
    """503 until the greeting is synthesised and the engine connected, so a
    platform routing on this check never hands anybody a cold machine."""
    app = create_app(llm=llm, voice=False, ears=False, greeting="")
    client = TestClient(app)

    assert client.get("/healthz").status_code == 503  # lifespan has not run

    with client:
        assert client.get("/healthz").status_code == 200
