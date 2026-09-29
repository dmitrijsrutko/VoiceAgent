"""Vendor balances: read when they can be, a row saying why when they cannot."""

import httpx2
import pytest

from voice_agent.quotas import Quotas


def transport(status: int, body: dict[str, object]) -> httpx2.MockTransport:
    def answer(request: httpx2.Request) -> httpx2.Response:
        if "elevenlabs" in request.url.host:
            assert request.headers["xi-api-key"] == "el-key"
        else:
            assert request.headers["authorization"] == "Bearer ds-key"
        return httpx2.Response(status, json=body)

    return httpx2.MockTransport(answer)


@pytest.fixture(autouse=True)
def keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ELEVENLABS_API_KEY", "el-key")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")


async def test_both_vendors_are_read_and_the_consoles_linked() -> None:
    body = {
        "character_count": 100,
        "character_limit": 1000,
        "tier": "creator",
        "is_available": True,
        "balance_infos": [{"currency": "USD", "total_balance": "4.20"}],
    }
    found = await Quotas(transport(200, body)).read()

    assert found["elevenlabs"]["ok"]
    assert (found["elevenlabs"]["used"], found["elevenlabs"]["limit"]) == (100, 1000)
    assert found["deepseek"]["balances"] == [{"currency": "USD", "total": "4.20"}]
    assert found["anthropic"]["console"].startswith("https://")


async def test_a_refusing_vendor_is_a_row_saying_so() -> None:
    found = await Quotas(transport(401, {})).read()

    assert found["elevenlabs"] == {"ok": False, "error": "HTTP 401"}
    assert found["deepseek"] == {"ok": False, "error": "HTTP 401"}


async def test_a_missing_key_is_not_a_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DEEPSEEK_API_KEY")

    def never(request: httpx2.Request) -> httpx2.Response:
        if "deepseek" in request.url.host:
            raise AssertionError("called without a key")
        return httpx2.Response(200, json={})

    found = await Quotas(httpx2.MockTransport(never)).read()

    assert found["deepseek"] == {"ok": False, "error": "DEEPSEEK_API_KEY is not set"}


async def test_a_second_read_within_the_minute_is_cached() -> None:
    calls = 0

    def answer(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(200, json={})

    quotas = Quotas(httpx2.MockTransport(answer))
    await quotas.read()
    await quotas.read()

    assert calls == 2, "one call per vendor, once"
