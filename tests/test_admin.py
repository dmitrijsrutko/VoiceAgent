"""`/admin`: invisible without the key, and the record fields it reads."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_record import app_for, converse, recorded
from tests.test_server import start
from voice_agent import admin
from voice_agent.config import load_settings
from voice_agent.errors import ConfigError
from voice_agent.sessions import SessionStore

KEY = "owner-key-long-enough-for-the-rule"


@pytest.fixture(autouse=True)
def no_vendors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never a real balance call, whatever keys the shell has."""
    monkeypatch.setattr("voice_agent.quotas.VENDORS", {})


@pytest.fixture
def keyed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_AGENT_ADMIN_KEY", KEY)


def signed_in(client: TestClient) -> None:
    response = client.get(f"/admin?key={KEY}", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin", "the key leaves the address bar"
    cookie = response.headers["set-cookie"]
    assert KEY not in cookie, "the cookie holds a token, never the key"
    assert "HttpOnly" in cookie


def test_without_a_configured_key_there_is_no_admin(tmp_path: Path) -> None:
    client = app_for(tmp_path, SessionStore())

    assert client.get("/admin").status_code == 404
    assert client.get("/admin?key=anything").status_code == 404
    assert client.get("/admin/api/stats").status_code == 404


@pytest.mark.usefixtures("keyed")
def test_a_wrong_key_or_no_cookie_is_a_404(tmp_path: Path) -> None:
    client = app_for(tmp_path, SessionStore())

    assert client.get("/admin?key=guess").status_code == 404
    assert client.get("/admin").status_code == 404
    assert client.get("/admin/api/stats").status_code == 404
    client.cookies.set(admin.COOKIE, KEY, path="/admin")
    assert client.get("/admin").status_code == 404, "the raw key is not the token"


@pytest.mark.usefixtures("keyed")
def test_the_key_signs_in_and_the_stats_list_the_session(tmp_path: Path) -> None:
    store = SessionStore()
    client = app_for(tmp_path, store)
    key = start(client)
    converse(client, key, "cats are better than dogs")

    signed_in(client)
    assert "voice-agent · admin" in client.get("/admin").text
    stats = client.get("/admin/api/stats").json()

    [row] = stats["sessions"]
    assert row["id"] == key
    assert row["measured"]
    assert (row["you"], row["replies"]) == (1, 1)
    assert row["topic"] == "cats are better than dogs"
    assert row["link"] == f"/admin/sessions/{row['file']}", (
        "in progress: its own page would open on the start screen and join it"
    )
    store.get(key).ended = True
    [row] = client.get("/admin/api/stats").json()["sessions"]
    assert row["link"] == f"/c/{key}" and row["review"], "ended: its own page, read-only"
    assert stats["windows"]["deploy"]["sessions"] == 1
    sections = stats["windows"]["all"]["breakdown"]
    assert [s["title"] for s in sections] == ["Role", "Reasoning", "Ears", "Voice", "Judge"]
    roles = {r["name"]: r for r in sections[0]["rows"]}
    assert len(roles) > 1, "every role card, not only the one used"
    assert any(r["offered"] and r["sessions"] == 0 for r in roles.values()), "unused shown as zero"
    assert stats["health"]["records"] == 1
    assert stats["quotas"]["anthropic"]["console"]


@pytest.mark.usefixtures("keyed")
def test_a_session_gone_from_memory_links_to_its_record(tmp_path: Path) -> None:
    store = SessionStore()
    client = app_for(tmp_path, store)
    converse(client, start(client), "hello")
    fresh = app_for(tmp_path, SessionStore())  # a restart: the file stays, the link does not
    signed_in(fresh)

    [row] = fresh.get("/admin/api/stats").json()["sessions"]
    assert row["link"] == f"/admin/sessions/{row['file']}"
    page = fresh.get(row["link"])
    assert page.status_code == 200
    assert "hello" in page.text


@pytest.mark.usefixtures("keyed")
def test_the_record_view_escapes_and_refuses_paths(tmp_path: Path) -> None:
    (tmp_path / "x.md").write_text("# Conversation x\n\n<script>alert(1)</script>\n")
    (tmp_path.parent / "secret.md").write_text("secret")
    client = app_for(tmp_path, SessionStore())
    signed_in(client)

    page = client.get("/admin/sessions/x.md").text
    assert "<script>alert" not in page
    assert "&lt;script&gt;" in page
    assert client.get("/admin/sessions/..%2Fsecret.md").status_code == 404
    assert client.get("/admin/sessions/nope.md").status_code == 404


@pytest.mark.usefixtures("keyed")
def test_the_record_holds_a_visitor_hash_and_totals_never_the_address(tmp_path: Path) -> None:
    client = app_for(tmp_path, SessionStore())
    converse(client, start(client), "one", "two")

    text = recorded(tmp_path)
    header = text.splitlines()[1]
    assert f"visitor {admin.visitor(KEY, 'testclient')}" in header
    assert "testclient" not in text
    [totals] = [line for line in text.splitlines() if line.startswith("`totals")]
    assert "replies 2" in totals and "you 2" in totals


@pytest.mark.usefixtures("keyed")
def test_a_non_ascii_key_or_cookie_is_a_404_not_a_500(tmp_path: Path) -> None:
    client = app_for(tmp_path, SessionStore())

    assert client.get("/admin?key=é").status_code == 404
    # A cookie takes the same path; the test client cannot send a non-ASCII one.
    assert not admin.same("é", admin.token(KEY))


def test_a_short_key_stops_the_server(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOICE_AGENT_ADMIN_KEY", "too-short")

    with pytest.raises(ConfigError, match="at least 24 characters"):
        load_settings()


def test_no_key_no_visitor() -> None:
    assert admin.visitor(None, "1.2.3.4") == ""
    assert admin.visitor(KEY, "1.2.3.4") == admin.visitor(KEY, "1.2.3.4")
    assert admin.visitor(KEY, "1.2.3.4") != admin.visitor("other", "1.2.3.4")
    assert len(admin.visitor(KEY, "1.2.3.4")) == admin.VISITOR_CHARS
