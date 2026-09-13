"""Starting the reply before the turn ends, and being wrong cheaply."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeLLM, FakeSTT, FakeTTS, receive
from voice_agent.server import create_app
from voice_agent.sessions import SessionStore
from voice_agent.speculation import Speculation
from voice_agent.stt.base import Transcript

FRAME = b"\x00\x01" * 800


@pytest.fixture
def store() -> SessionStore:
    return SessionStore()


def build(store: SessionStore, script: list[Transcript], llm: FakeLLM | None = None) -> TestClient:
    return TestClient(
        create_app(
            llm=llm or FakeLLM(),
            tts=FakeTTS(),
            stt=FakeSTT(script=script),
            store=store,
            voice=False,
            greeting="",
        )
    )


def converse(client: TestClient, frames: int) -> tuple[str, list[dict[str, object]]]:
    key = client.get("/", follow_redirects=False).headers["location"].removeprefix("/c/")
    seen: list[dict[str, object]] = []
    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "listen_start"})
        socket.receive_json()
        for _ in range(frames):
            socket.send_bytes(FRAME)
        while True:
            frame = receive(socket)
            seen.append(frame)
            if frame["type"] == "reply_end":
                return key, seen


# A partial that repeats is the signal that speech has stopped.
SETTLES = [
    Transcript("what is the capital", is_final=False),
    Transcript("what is the capital of Latvia", is_final=False),
    Transcript("what is the capital of Latvia", is_final=False),  # adds nothing -> guess
    Transcript("What is the capital of Latvia?", is_final=True),
]


def test_a_settled_prefix_starts_the_reply_before_the_turn_ends(store: SessionStore) -> None:
    llm = FakeLLM()
    _, frames = converse(build(store, SETTLES, llm), 4)

    reply_end = next(f for f in frames if f["type"] == "reply_end")
    assert reply_end["speculated"] is True
    assert reply_end["speculations_discarded"] == 0
    # One generation, not two: the speculation became the reply.
    assert len(llm.seen) == 1
    assert llm.seen[0][-1].content == "what is the capital of Latvia"
    # The adopted guess brings its own usage: the turn made no call to count.
    fragments = int(reply_end["fragments"])  # type: ignore[call-overload]
    assert reply_end["output_tokens"] == 2 * fragments > 0


def test_the_recorded_turn_is_the_committed_text_not_the_guess(store: SessionStore) -> None:
    """The model answered a question without its question mark. What goes into
    the conversation is what the user actually said."""
    key, frames = converse(build(store, SETTLES), 4)

    assert any(f["type"] == "reply_end" for f in frames)
    assert store.get(key).messages[0].content == "What is the capital of Latvia?"


def test_a_guess_the_user_talks_through_is_discarded(store: SessionStore) -> None:
    """The whole safety argument: a reply generated for a question that was
    still being asked must never reach the user."""
    llm = FakeLLM(replies=["Riga.", "Riga is the capital and it has 600,000 people."])
    script = [
        Transcript("what is the capital", is_final=False),
        Transcript("what is the capital", is_final=False),  # settles -> guess "Riga."
        Transcript("what is the capital of Latvia and how big", is_final=False),  # kept talking
        Transcript("what is the capital of Latvia and how big", is_final=False),  # settles again
        Transcript("What is the capital of Latvia and how big?", is_final=True),
    ]
    _, frames = converse(build(store, script, llm), 5)

    reply_end = next(f for f in frames if f["type"] == "reply_end")
    spoken = "".join(str(f["text"]) for f in frames if f["type"] == "delta")

    assert reply_end["speculations_discarded"] == 1
    assert "600,000" in spoken, "the reply should answer the question that was finished"
    assert spoken.strip() != "Riga.", "the abandoned guess reached the user"
    # The first guess was cancelled, which is what freed the slot for a second
    # one to be started and adopted. Without cancellation the turn generates
    # from scratch and this is False.
    assert reply_end["speculated"] is True


def test_the_cost_of_guessing_wrong_is_reported(store: SessionStore) -> None:
    """Chapter 4 shipped seven redundant warms and only the on-screen count
    caught it. A discarded guess is billed output, so it gets counted too."""
    llm = FakeLLM(replies=["Riga.", "Riga, and about 600,000 people."])
    script = [
        Transcript("what is the capital", is_final=False),
        Transcript("what is the capital", is_final=False),
        Transcript("what is the capital of Latvia", is_final=False),
        Transcript("what is the capital of Latvia", is_final=False),
        Transcript("What is the capital of Latvia?", is_final=True),
    ]
    _, frames = converse(build(store, script, llm), 5)

    reply_end = next(f for f in frames if f["type"] == "reply_end")
    assert reply_end["speculations_discarded"] == 1
    assert reply_end["speculation_wasted_chars"] != 0


def test_a_turn_nobody_guessed_at_behaves_exactly_as_before(store: SessionStore) -> None:
    """No partial ever repeats, so nothing is speculated and the turn is the
    plain one from chapter 3."""
    llm = FakeLLM()
    script = [
        Transcript("what is", is_final=False),
        Transcript("what is the capital", is_final=False),
        Transcript("What is the capital?", is_final=True),
    ]
    _, frames = converse(build(store, script, llm), 3)

    reply_end = next(f for f in frames if f["type"] == "reply_end")
    assert reply_end["speculated"] is False
    assert reply_end["speculations_discarded"] == 0
    assert len(llm.seen) == 1


def test_only_one_guess_is_ever_in_flight(store: SessionStore) -> None:
    llm = FakeLLM()
    script = [
        Transcript("hello there", is_final=False),
        Transcript("hello there", is_final=False),  # settles -> guess
        Transcript("hello there", is_final=False),  # settles again -> must not start another
        Transcript("Hello there.", is_final=True),
    ]
    _, frames = converse(build(store, script, llm), 4)

    reply_end = next(f for f in frames if f["type"] == "reply_end")
    assert reply_end["speculated"] is True
    assert reply_end["speculations_discarded"] == 0
    assert len(llm.seen) == 1, "a second guess was started while one was in flight"


def test_a_guess_that_fails_leaves_the_turn_to_do_the_work(store: SessionStore) -> None:
    """A speculation must fail the way a wrong one does: quietly."""
    llm = FakeLLM(fail=True)
    client = build(store, SETTLES, llm)
    key = client.get("/", follow_redirects=False).headers["location"].removeprefix("/c/")

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "listen_start"})
        socket.receive_json()
        for _ in range(4):
            socket.send_bytes(FRAME)
        while (frame := receive(socket))["type"] not in ("error", "reply_end"):
            pass

    assert frame["type"] == "error"
    assert "provider exploded" in str(frame["message"])


def test_a_guess_is_discarded_when_the_commit_says_something_else(
    store: SessionStore,
) -> None:
    """The commit can differ from every partial that preceded it, with nothing
    in between to cancel on. Then the only thing standing between the user and
    an answer to a question they did not ask is checking what they actually
    said."""
    llm = FakeLLM(replies=["Hello to you.", "Hello there friend, nice to meet you."])
    script = [
        Transcript("hello", is_final=False),
        Transcript("hello", is_final=False),  # settles -> guess on "hello"
        Transcript("Hello there friend.", is_final=True),  # the turn was longer
    ]
    _, frames = converse(build(store, script, llm), 3)

    reply_end = next(f for f in frames if f["type"] == "reply_end")
    spoken = "".join(str(f["text"]) for f in frames if f["type"] == "delta")

    assert reply_end["speculated"] is False, "a guess at 'hello' answered 'Hello there friend.'"
    assert reply_end["speculations_discarded"] == 1
    assert "nice to meet you" in spoken
    assert llm.seen[-1][-1].content == "Hello there friend."


async def test_abandoning_a_guess_actually_stops_the_generation() -> None:
    """Cancelling the task is not enough: it leaves the generator suspended at
    its yield, still holding the provider's stream, until the garbage collector
    gets to it. It has to be closed from outside the cancelled task, because
    inside one the `await` that would close it is cancelled too.

    Tested here rather than through a socket. The server abandons a guess when
    the browser disconnects, and that path is *not* covered: the test client
    either tears down the event loop on disconnect — killing the task whether
    or not the code cancelled it, which looks like a pass — or holds the
    handler open so its cleanup never runs at all. Neither can tell the fix
    from its absence, so this covers the mechanism and the caller is covered by
    reading it.
    """
    reply = "one two three four five six seven eight"
    whole = sum(len(word) + 1 for word in reply.split())
    llm = FakeLLM(replies=[reply], pace=0.05)
    guess = Speculation(llm, "system", [], "hello")
    await asyncio.sleep(0.12)
    assert llm.active == 1, "nothing was generating to abandon"

    started = time.monotonic()
    wasted = await guess.abandon()
    elapsed = time.monotonic() - started
    await asyncio.sleep(0.05)

    assert llm.active == 0, "the generator was left holding the provider's stream"
    # The discriminating assertions. Simply awaiting the task would also end
    # with nothing generating and every character counted — that is waiting for
    # the guess to finish, which is the opposite of abandoning it.
    assert 0 < wasted < whole, f"the guess ran to completion: {wasted} of {whole} chars"
    assert elapsed < 0.1, f"abandoning waited {elapsed:.2f}s for the guess to finish"


def test_endpointing_is_measured_from_the_last_word_not_the_last_message(
    store: SessionStore,
) -> None:
    """A repeated partial carries no new word; it is the recognizer idling. If
    it resets the endpoint clock then `endpoint_ms` measures the gap between the
    recognizer's last two *messages* — which makes it identical to the
    speculation lead on every single turn, because a repeat is exactly what
    starts a speculation.
    """
    client = build(store, SETTLES)
    key = client.get("/", follow_redirects=False).headers["location"].removeprefix("/c/")

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "listen_start"})
        socket.receive_json()
        socket.send_bytes(FRAME)  # "what is the capital"
        socket.receive_json()
        socket.send_bytes(FRAME)  # "...of Latvia" — the last new words
        socket.receive_json()
        time.sleep(0.25)  # the speaker has stopped; the recognizer idles
        socket.send_bytes(FRAME)  # a repeat: no new words, starts a guess
        socket.receive_json()
        socket.send_bytes(FRAME)  # the commit
        commit = socket.receive_json()
        while (frame := receive(socket))["type"] != "reply_end":
            pass

    assert commit["final"] is True
    assert int(commit["endpoint_ms"]) >= 250, (
        f"the idle gap was not counted: endpoint_ms={commit['endpoint_ms']}"
    )
    assert int(frame["speculation_lead_ms"]) < int(commit["endpoint_ms"]), (
        "the two are measuring the same instant again"
    )


def test_a_spoken_exit_does_not_claim_the_guess_made_for_it(store: SessionStore) -> None:
    """`exit` produces no reply, so a guess at it answers nothing. Claimed, it
    would run to completion unread and uncounted."""
    llm = FakeLLM()
    script = [
        Transcript("exit", is_final=False),
        Transcript("exit", is_final=False),  # settles -> a guess is made
        Transcript("Exit.", is_final=True),
    ]
    client = build(store, script, llm)
    key = client.get("/", follow_redirects=False).headers["location"].removeprefix("/c/")

    with client.websocket_connect(f"/ws/{key}") as socket:
        socket.receive_json()
        socket.send_json({"type": "listen_start"})
        socket.receive_json()
        for _ in range(3):
            socket.send_bytes(FRAME)
        while (frame := receive(socket))["type"] != "ended":
            pass

    assert frame["type"] == "ended"
    assert store.get(key).ended
