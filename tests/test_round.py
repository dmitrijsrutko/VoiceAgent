"""A judged round over the real app: its length, the End button, the ruling."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketTestSession

from tests.conftest import FakeLLM
from voice_agent import timing
from voice_agent.conversation import Message
from voice_agent.llm.base import Usage
from voice_agent.server import create_app, round_budget
from voice_agent.sessions import SessionStore

FIXTURES = Path(__file__).parent / "fixtures"
VERDICT = json.dumps(
    json.loads((FIXTURES / "ruling_sample.json").read_text(encoding="utf-8"))["verdict"],
    ensure_ascii=False,
)
LINES = (
    "Cities need traffic lights at every busy junction: people on foot need a protected phase.",
    "A roundabout gives a pedestrian nothing: they wait for a gap that never comes at rush hour.",
)


def app(
    role: str,
    judge: FakeLLM | None = None,
    sessions: Path | None = None,
    store: SessionStore | None = None,
    fake_judge: bool = True,
) -> Any:
    return create_app(
        llm=FakeLLM(["Roundabouts move more cars."]),
        store=store if store is not None else SessionStore(),
        voice=False,
        ears=False,
        role=role,
        judge=(judge or FakeLLM([VERDICT])) if fake_judge else None,
        sessions_dir=sessions,
        record=sessions is not None,
    )


def mint(client: TestClient) -> str:
    return str(client.get("/", follow_redirects=False).headers["location"].removeprefix("/c/"))


def until(socket: WebSocketTestSession, kind: str) -> dict[str, Any]:
    while True:
        frame: dict[str, Any] = socket.receive_json()
        if frame["type"] == kind:
            return frame


def argue_and_end(socket: WebSocketTestSession) -> None:
    for line in LINES:
        socket.send_text(json.dumps({"type": "user_message", "text": line}))
        until(socket, "reply_end")
    socket.send_text(json.dumps({"type": "end"}))
    until(socket, "ended")


def ruling(client: TestClient, key: str) -> dict[str, Any]:
    for _ in range(100):
        response = client.get(f"/c/{key}/verdict")
        if response.status_code == 200:
            return dict(response.json())
        assert response.status_code == 202
        time.sleep(0.02)
    raise AssertionError("the judge never ruled")


@pytest.mark.parametrize(
    ("deployment", "minutes", "expected"),
    [(None, 6.0, 360.0), (1800.0, 6.0, 360.0), (120.0, 6.0, 120.0), (1800.0, None, 1800.0)],
)
def test_a_round_is_the_shorter_of_the_role_and_the_deployment(
    deployment: float | None, minutes: float | None, expected: float
) -> None:
    from voice_agent import roles

    role = roles.load("devils_advocate")
    card = roles.Role(
        **{**{f: getattr(role, f) for f in role.__dataclass_fields__}, "minutes": minutes}
    )
    assert round_budget(deployment, card) == expected


def test_a_devil_s_advocate_round_lasts_six_minutes_and_names_its_judge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VOICE_AGENT_SESSION_BUDGET", raising=False)
    with (
        TestClient(app("devils_advocate")) as client,
        client.websocket_connect(f"/ws/{mint(client)}") as socket,
    ):
        ready = socket.receive_json()
    assert ready["budget_seconds"] == 360
    assert ready["judge"] == {"name": "deepseek-high", "title": "DeepSeek V4.1 Flash"}


def test_an_unjudged_role_names_no_judge(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_AGENT_SESSION_BUDGET", "1800")
    with (
        TestClient(app("thinking_partner")) as client,
        client.websocket_connect(f"/ws/{mint(client)}") as socket,
    ):
        ready = socket.receive_json()
    assert ready["budget_seconds"] == 1800 and ready["judge"] is None


def test_end_rules_on_the_round_and_a_reload_finds_the_ruling(tmp_path: Path) -> None:
    judge = FakeLLM([VERDICT])
    with TestClient(app("devils_advocate", judge, tmp_path)) as client:
        key = mint(client)
        with client.websocket_connect(f"/ws/{key}") as socket:
            _ = socket.receive_json()
            argue_and_end(socket)
        got = ruling(client, key)
        assert client.get(f"/c/{key}/verdict").json() == got  # asked again, same answer
    assert got["status"] == "done" and got["verdict"]["split"] == {"you": 30, "advocate": 70}
    assert got["stats"]["your_turns"] == 2
    seen = judge.seen[0][0].content
    assert "YOU (answered after" in seen and "typed" in seen and LINES[1] in seen
    record = next(tmp_path.glob("*.md")).read_text(encoding="utf-8")
    assert "judge deepseek-high" in record.splitlines()[1]
    assert "— judge (deepseek-high)" in record and "**LOSE** 30/70" in record
    # The whole review, not just its headline: readable without the page.
    assert record.count("/10 — ") == 10
    for part in (
        "**Position**",
        "**Moments**",
        "**How to improve**",
        "**Rematch**",
        "Пил по кругу",
    ):
        assert part in record, part


def test_ending_before_saying_enough_is_no_contest() -> None:
    judge = FakeLLM([VERDICT])
    with TestClient(app("devils_advocate", judge)) as client:
        key = mint(client)
        with client.websocket_connect(f"/ws/{key}") as socket:
            _ = socket.receive_json()
            socket.send_text(json.dumps({"type": "end"}))
            until(socket, "ended")
        assert ruling(client, key)["status"] == "no_contest"
    assert judge.seen == []


def test_the_judge_asked_for_is_pinned_and_an_unknown_one_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    with TestClient(app("devils_advocate")) as client:
        with client.websocket_connect(f"/ws/{mint(client)}?judge=opus-5-5") as socket:
            ready = socket.receive_json()
        assert ready["judge"]["name"] == "opus-5-5"
        with client.websocket_connect(f"/ws/{mint(client)}?judge=nobody") as socket:
            ready = socket.receive_json()
        assert ready["judge"]["name"] == "deepseek-high"


def test_there_is_no_ruling_to_wait_for_on_an_unjudged_or_unknown_conversation() -> None:
    with TestClient(app("thinking_partner")) as client:
        key = mint(client)
        with client.websocket_connect(f"/ws/{key}") as socket:
            _ = socket.receive_json()
            argue_and_end(socket)
        assert client.get(f"/c/{key}/verdict").status_code == 404
        assert client.get("/c/nobody/verdict").status_code == 404


def test_a_reload_resumes_the_round_s_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VOICE_AGENT_SESSION_BUDGET", raising=False)
    store = SessionStore()
    with TestClient(app("devils_advocate", store=store)) as client:
        key = mint(client)
        store.get(key).started = timing.now() - 100  # first opened 100 s ago
        with client.websocket_connect(f"/ws/{key}") as socket:
            ready = socket.receive_json()
    assert 259 <= ready["budget_seconds"] <= 260


def test_a_judge_that_cannot_be_built_still_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with TestClient(app("devils_advocate", fake_judge=False)) as client:
        key = mint(client)
        with client.websocket_connect(f"/ws/{key}") as socket:
            socket.receive_json()
            argue_and_end(socket)
        got = ruling(client, key)
    assert got["status"] == "failed" and "DEEPSEEK_API_KEY" in got["error"]


class Crowded(FakeLLM):
    """Counts how many rulings run at once."""

    peak = 0
    running = 0

    async def stream(
        self, system: str, messages: Sequence[Message], usage: Usage | None = None
    ) -> AsyncIterator[str]:
        Crowded.running += 1
        Crowded.peak = max(Crowded.peak, Crowded.running)
        try:
            await asyncio.sleep(0.2)
            async for fragment in super().stream(system, messages, usage):
                yield fragment
        finally:
            Crowded.running -= 1


def test_rulings_wait_their_turn_under_the_live_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_AGENT_MAX_LIVE", "1")
    Crowded.peak = Crowded.running = 0
    with TestClient(app("devils_advocate", Crowded([VERDICT]))) as client:
        keys = [mint(client), mint(client)]
        for key in keys:
            with client.websocket_connect(f"/ws/{key}") as socket:
                socket.receive_json()
                argue_and_end(socket)
        rulings = [ruling(client, key) for key in keys]
    assert [r["status"] for r in rulings] == ["done", "done"]
    assert Crowded.peak == 1


def facts(client: TestClient, key: str) -> dict[str, Any]:
    page = client.get(f"/c/{key}").text
    block = page.split('<script id="facts" type="application/json">', 1)[1]
    return dict(json.loads(block.split("</script>", 1)[0]))


def test_an_ended_round_s_page_carries_its_record_without_a_socket(tmp_path: Path) -> None:
    with TestClient(app("devils_advocate", sessions=tmp_path)) as client:
        key = mint(client)
        assert "ended" not in facts(client, key)  # live: the start screen, as before
        with client.websocket_connect(f"/ws/{key}") as socket:
            _ = socket.receive_json()
            argue_and_end(socket)
        known = facts(client, key)
    assert known["ended"] is True
    assert known["judge"] == {"name": "deepseek-high", "title": "DeepSeek V4.1 Flash"}
    assert [m["content"] for m in known["history"] if m["role"] == "user"] == list(LINES)
