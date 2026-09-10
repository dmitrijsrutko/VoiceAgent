import pytest

from voice_agent.errors import SessionNotFoundError
from voice_agent.sessions import SessionStore


def test_each_conversation_gets_its_own_unguessable_key() -> None:
    store = SessionStore()
    keys = {store.create().id for _ in range(50)}

    assert len(keys) == 50
    assert all(len(key) >= 20 for key in keys)


def test_get_returns_the_same_live_conversation() -> None:
    store = SessionStore()
    created = store.create()
    created.add_user("remember this")

    assert store.get(created.id) is created
    assert store.get(created.id).messages[0].content == "remember this"


def test_unknown_key_is_an_error_not_a_new_conversation() -> None:
    store = SessionStore()
    with pytest.raises(SessionNotFoundError):
        store.get("nope")
    assert len(store) == 0
