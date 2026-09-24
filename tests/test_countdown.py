"""The page counts down the last minute, so it must be told the limit."""

import pytest
from fastapi.testclient import TestClient

from tests.conftest import FakeLLM
from voice_agent.server import create_app
from voice_agent.sessions import SessionStore


def ready_frame(monkeypatch: pytest.MonkeyPatch, budget: str | None) -> dict[str, object]:
    if budget is None:
        monkeypatch.delenv("VOICE_AGENT_SESSION_BUDGET", raising=False)
    else:
        monkeypatch.setenv("VOICE_AGENT_SESSION_BUDGET", budget)
    app = create_app(llm=FakeLLM(), store=SessionStore(), voice=False, ears=False, greeting="")
    with TestClient(app) as client:
        key = client.get("/", follow_redirects=False).headers["location"].removeprefix("/c/")
        with client.websocket_connect(f"/ws/{key}") as socket:
            frame: dict[str, object] = socket.receive_json()
    return frame


def test_ready_carries_the_time_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ready_frame(monkeypatch, "360")["budget_seconds"] == 360


def test_no_limit_means_no_countdown(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ready_frame(monkeypatch, None)["budget_seconds"] is None
