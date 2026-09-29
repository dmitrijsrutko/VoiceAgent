"""What the vendors say is left, for `/admin`.

Two read-only calls that nobody bills: ElevenLabs' subscription (characters
used against the plan) and DeepSeek's balance. Anthropic and AssemblyAI expose
no balance to an ordinary API key, so the page links to their consoles instead.

A vendor that fails — no key, a key without the permission, a timeout — is a
row saying so, never an exception: the page is for when things are going wrong.
"""

import asyncio
import os
from typing import Any

import httpx2

from voice_agent import timing

TIMEOUT_SECONDS = 5.0
CACHE_SECONDS = 60.0
"""A page reload should not be two vendor calls; a minute-old balance is fresh."""

ELEVENLABS_URL = "https://api.elevenlabs.io/v1/user/subscription"
DEEPSEEK_URL = "https://api.deepseek.com/user/balance"

CONSOLES = {
    "anthropic": "https://console.anthropic.com/settings/billing",
    "assemblyai": "https://www.assemblyai.com/dashboard/settings/billing",
}


async def elevenlabs(client: httpx2.AsyncClient, key: str) -> dict[str, Any]:
    response = await client.get(ELEVENLABS_URL, headers={"xi-api-key": key})
    response.raise_for_status()
    body = response.json()
    return {
        "used": body.get("character_count"),
        "limit": body.get("character_limit"),
        "resets": body.get("next_character_count_reset_unix"),
        "tier": body.get("tier"),
        "status": body.get("status"),
    }


async def deepseek(client: httpx2.AsyncClient, key: str) -> dict[str, Any]:
    response = await client.get(DEEPSEEK_URL, headers={"Authorization": f"Bearer {key}"})
    response.raise_for_status()
    body = response.json()
    return {
        "available": body.get("is_available"),
        "balances": [
            {"currency": info.get("currency"), "total": info.get("total_balance")}
            for info in body.get("balance_infos") or []
        ],
    }


VENDORS = {
    "elevenlabs": ("ELEVENLABS_API_KEY", elevenlabs),
    "deepseek": ("DEEPSEEK_API_KEY", deepseek),
}


async def one(client: httpx2.AsyncClient, name: str) -> dict[str, Any]:
    env, fetch = VENDORS[name]
    key = os.environ.get(env)
    if not key:
        return {"ok": False, "error": f"{env} is not set"}
    try:
        return {"ok": True, **await fetch(client, key)}
    except httpx2.HTTPStatusError as exc:
        return {"ok": False, "error": f"HTTP {exc.response.status_code}"}
    except (httpx2.HTTPError, ValueError) as exc:
        return {"ok": False, "error": type(exc).__name__}


class Quotas:
    """Every vendor at once, cached for `CACHE_SECONDS`."""

    def __init__(self, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._transport = transport
        self._cached: tuple[float, dict[str, Any]] | None = None

    async def read(self) -> dict[str, Any]:
        if self._cached is not None and timing.now() - self._cached[0] < CACHE_SECONDS:
            return self._cached[1]
        async with httpx2.AsyncClient(timeout=TIMEOUT_SECONDS, transport=self._transport) as client:
            names = list(VENDORS)
            results = await asyncio.gather(*(one(client, name) for name in names))
        found: dict[str, Any] = dict(zip(names, results, strict=True))
        found |= {name: {"ok": False, "console": url} for name, url in CONSOLES.items()}
        self._cached = (timing.now(), found)
        return found
