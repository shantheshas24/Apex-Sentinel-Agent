"""Regression coverage for the dashboard's Gmail OAuth state and callback."""
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient

from personal_os_agent.api.app import app
from personal_os_agent.api.routers import google
from personal_os_agent.mcp_server import google_auth

pytestmark = pytest.mark.unit
ORIGIN = "http://localhost:8080"


@pytest.fixture
def browser(tmp_path, monkeypatch):
    config = tmp_path / "credentials.json"
    config.write_text(json.dumps({"installed": {
        "client_id": "test-client",
        "client_secret": "test-secret",
        "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }}), encoding="utf-8")
    settings = SimpleNamespace(
        dashboard_port=8080,
        google_credentials_path=config,
        google_token_path=tmp_path / "token.json",
    )
    monkeypatch.setattr(google, "get_settings", lambda: settings)
    monkeypatch.setattr(app.state, "google_flows", {}, raising=False)
    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as client:
        yield client


def test_oauth_requires_same_origin_and_browser(browser, monkeypatch):
    exchanges = []
    monkeypatch.setattr(google, "_save_grant", lambda flow, code: exchanges.append((flow, code)))

    denied = browser.post("/api/google/connect", headers={"Origin": "https://example.test"})
    assert denied.status_code == 403
    assert browser.get("/api/google/status").json()["status"] == "disconnected"

    start = browser.post("/api/google/connect", headers={"Origin": ORIGIN})
    assert start.status_code == 200
    assert "httponly" in start.headers["set-cookie"].lower()
    authorization = urlparse(start.json()["authorization_url"])
    query = parse_qs(authorization.query)
    assert authorization.netloc == "accounts.google.com"
    assert query["redirect_uri"] == [ORIGIN + "/"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["scope"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert query["include_granted_scopes"] == ["false"]
    params = {"state": query["state"][0], "code": "callback-test"}

    with TestClient(app, base_url=ORIGIN, follow_redirects=False) as other_browser:
        assert other_browser.get("/", params=params).headers["location"] == "/?gmail=expired"
    assert exchanges == []
    assert browser.get("/", params=params).headers["location"] == "/?gmail=connected"
    assert browser.get("/", params=params).headers["location"] == "/?gmail=expired"
    assert [code for _, code in exchanges] == ["callback-test"]


def test_oauth_cancelled_without_saving_grant(browser, monkeypatch):
    def unexpected_exchange(flow, code):
        pytest.fail("A cancelled flow must not exchange credentials")

    monkeypatch.setattr(google, "_save_grant", unexpected_exchange)
    start = browser.post("/api/google/connect", headers={"Origin": ORIGIN})
    state = parse_qs(urlparse(start.json()["authorization_url"]).query)["state"][0]

    callback = browser.get("/", params={"state": state, "error": "access_denied"})
    assert callback.headers["location"] == "/?gmail=cancelled"
    assert callback.headers["cache-control"] == "no-store"


def test_saved_send_grant_requires_reconnection(browser, monkeypatch):
    settings = google.get_settings()
    monkeypatch.setattr(google_auth, "get_settings", lambda: settings)
    settings.google_token_path.write_text(json.dumps({
        "token": "test-access",
        "refresh_token": "test-refresh",
        "client_id": "test-client",
        "client_secret": "test-secret",
        "expiry": "2099-01-01T00:00:00Z",
        "scopes": [*google_auth.SCOPES, "https://www.googleapis.com/auth/gmail.send"],
    }), encoding="utf-8")
    assert browser.get("/api/google/status").json()["status"] == "reconnect"
