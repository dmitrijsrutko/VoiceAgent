"""`/admin`: the owner's private look at the server, its spend and its records.

It exists only when `VOICE_AGENT_ADMIN_KEY` is set, and every admin route
answers 404 to anyone without it, so the page is not advertised. The key is
typed once, as `/admin?key=…`, and traded for an HttpOnly cookie holding an
HMAC of it; the redirect takes the key out of the address bar and history.

Everything shown is read from the session records (`ledger`) and from the
vendors (`quotas`), never counted in memory: a deploy restarts the process,
and the records are what survive it.
"""

import asyncio
import hashlib
import hmac
import html
import logging
import re
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.responses import Response

from voice_agent import ledger
from voice_agent.quotas import Quotas

if TYPE_CHECKING:
    from voice_agent.server import Agent

logger = logging.getLogger(__name__)

PAGE_PATH = Path(__file__).parent / "web" / "admin.html"
COOKIE = "va_admin"
COOKIE_DAYS = 30
VISITOR_CHARS = 12
"""48 bits: enough to tell visitors apart. Pseudonymous, not anonymous: without
the key no hash links to an address, but whoever holds the key (the owner) can
hash every IPv4 address in minutes and read one back."""

BOLD = re.compile(r"\*\*(.+?)\*\*")
RECORD_NAME = re.compile(r"^[\w-]+\.md$")
NOT_FOUND = "<h1>404 — not found</h1>"


def visitor(key: str | None, address: str) -> str:
    """The address, hashed under the admin key: the same visitor gets the same
    hash across deploys, and the record never holds the address itself.
    Without a key there is nothing to hash under, so nothing is written."""
    if not key or not address or address == "unknown":
        return ""
    return hmac.new(key.encode(), address.encode(), hashlib.sha256).hexdigest()[:VISITOR_CHARS]


def token(key: str) -> str:
    """What the cookie holds: derived from the key, so the key itself never
    rides along on every request."""
    return hmac.new(key.encode(), b"admin", hashlib.sha256).hexdigest()


def same(presented: str, expected: str) -> bool:
    """Constant-time, and on bytes: `compare_digest` raises on a non-ASCII `str`,
    which would turn anyone's `?key=é` into a 500."""
    return hmac.compare_digest(presented.encode(), expected.encode())


def allowed(key: str | None, request: Request) -> bool:
    return bool(key) and same(request.cookies.get(COOKIE, ""), token(key or ""))


def render(text: str) -> str:
    """A record as a page: headings for turns, notes set apart, all escaped.
    Deliberately not a Markdown renderer — the record is a log, and a log shown
    verbatim can be trusted to show what was written."""
    out: list[str] = []
    for line in text.splitlines():
        safe = html.escape(line)
        if line.startswith("# "):
            out.append(f"<h1>{safe[2:]}</h1>")
        elif line.startswith("## "):
            out.append(f"<h2>{safe[3:]}</h2>")
        elif line.startswith("`") and line.endswith("`") and len(line) > 1:
            out.append(f'<p class="note">{safe[1:-1]}</p>')
        elif line.strip():
            out.append(f"<p>{BOLD.sub(r'<b>\1</b>', safe)}</p>")
    return "\n".join(out)


RECORD_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title}</title>
<style>
body {{ font: 15px/1.5 system-ui, sans-serif; max-width: 860px; margin: 0 auto; padding: 16px;
  background: #fff; color: #1a1a1a; }}
h1 {{ font-size: 18px; }} h2 {{ font-size: 14px; margin: 22px 0 4px; color: #555; }}
.note {{ font: 12px/1.4 ui-monospace, monospace; color: #777; margin: 2px 0;
  word-break: break-word; }}
a {{ color: inherit; }}
@media (prefers-color-scheme: dark) {{ body {{ background: #111; color: #e6e6e6; }}
  h2 {{ color: #aaa; }} .note {{ color: #888; }} }}
</style></head><body>
<p><a href="/admin">← admin</a></p>
{body}
</body></html>"""


def disk(directory: Path | None) -> dict[str, Any]:
    if directory is None or not directory.is_dir():
        return {}
    files = list(directory.glob("*.md"))
    usage = shutil.disk_usage(directory)
    return {
        "records": len(files),
        "records_bytes": sum(f.stat().st_size for f in files if f.exists()),
        "free_bytes": usage.free,
        "total_bytes": usage.total,
    }


def routes(app: FastAPI, agent: "Agent") -> None:
    quotas = Quotas()

    def key() -> str | None:
        return agent.settings.admin_key

    @app.get("/admin")
    async def admin_page(request: Request) -> Response:
        typed = request.query_params.get("key")
        if typed is not None:
            if not key() or not same(typed, key() or ""):
                logger.warning("admin: a wrong key was tried")
                return HTMLResponse(NOT_FOUND, status_code=404)
            response = RedirectResponse("/admin", status_code=303)
            response.set_cookie(
                COOKIE,
                token(typed),
                max_age=COOKIE_DAYS * 86400,
                path="/admin",
                httponly=True,
                samesite="strict",
                secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https",
            )
            return response
        if not allowed(key(), request):
            return HTMLResponse(NOT_FOUND, status_code=404)
        return HTMLResponse(
            PAGE_PATH.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"}
        )

    @app.get("/admin/api/stats")
    async def admin_stats(request: Request) -> Response:
        if not allowed(key(), request):
            return JSONResponse({}, status_code=404)
        now = datetime.now()
        # Off the loop: the first load after a deploy parses every record, and
        # the loop is shared with conversations in progress.
        rows = await asyncio.to_thread(ledger.read, agent.record_dir)
        # What the start screen offers, from the same call that builds it.
        backends = agent.backends
        menu = backends.choices(
            backends.default_engine,
            backends.default_ears,
            backends.default_role,
            backends.default_voice,
        )
        sessions = []
        for row in rows:
            listed = row.as_json()
            # The conversation's own page only once it has ended, when it opens
            # read-only. Before that it opens on the start screen, and starting
            # there would join the visitor's conversation in progress.
            review = row.id in agent.sessions and agent.sessions.get(row.id).ended
            listed["link"] = f"/c/{row.id}" if review else f"/admin/sessions/{row.file}"
            listed["review"] = review
            sessions.append(listed)
        return JSONResponse(
            {
                "now": now.isoformat(sep=" ", timespec="seconds"),
                "health": {
                    "ready": agent.ready,
                    "started_at": agent.started_at.isoformat(sep=" ", timespec="seconds"),
                    "uptime_s": round((now - agent.started_at).total_seconds()),
                    "image": agent.image,
                    "live": agent.live.held,
                    "max_live": agent.settings.max_live,
                    "stored": len(agent.sessions),
                    "recording": agent.record_dir is not None,
                    **await asyncio.to_thread(disk, agent.record_dir),
                },
                "windows": {
                    name: asdict(stats) | {"breakdown": ledger.breakdown(stats, menu)}
                    for name, stats in ledger.windows(rows, agent.started_at, now).items()
                },
                "quotas": await quotas.read(),
                "sessions": sessions,
            },
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/admin/sessions/{name}")
    async def admin_record(name: str, request: Request) -> Response:
        if not allowed(key(), request) or agent.record_dir is None:
            return HTMLResponse(NOT_FOUND, status_code=404)
        path = agent.record_dir / name
        if not RECORD_NAME.match(name) or not path.is_file():
            return HTMLResponse(NOT_FOUND, status_code=404)
        text = await asyncio.to_thread(path.read_text, encoding="utf-8", errors="replace")
        return HTMLResponse(
            RECORD_PAGE.format(title=html.escape(name), body=await asyncio.to_thread(render, text)),
            headers={"Cache-Control": "no-store"},
        )
