"""Connect a Gmail account through the local dashboard."""
import asyncio
import logging
from pathlib import Path
from secrets import compare_digest
from time import monotonic, perf_counter

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from google.auth.transport.requests import AuthorizedSession
from google_auth_oauthlib.flow import Flow

from personal_os_agent.config.settings import get_settings
from personal_os_agent.mcp_server.google_auth import GmailConnectionRequired, SCOPES, get_credentials, store_credentials

router = APIRouter(prefix="/api/google")
_COOKIE = "personalos_gmail_oauth"
_FLOW_TTL = 600
_MAX_FLOWS = 32
_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
logger = logging.getLogger(__name__)


def _dashboard_origin(request: Request) -> str | None:
    port = get_settings().dashboard_port
    if request.url.scheme != "http" or request.url.netloc not in {
        f"localhost:{port}", f"127.0.0.1:{port}",
    }:
        return None
    return f"http://{request.url.netloc}"


def _gmail_profile() -> dict:
    credentials = get_credentials()
    with AuthorizedSession(credentials) as session:
        result = session.get("https://gmail.googleapis.com/gmail/v1/users/me/profile", timeout=8)
        result.raise_for_status()
        return result.json()


async def connection_status(request: Request) -> dict:
    cfg = get_settings()
    if getattr(request.app.state, "dashboard_only", False):
        return {"status": "disabled", "polling": False}
    result = {
        "polling": getattr(request.app.state, "gmail_polling", True),
        "can_connect": bool(_dashboard_origin(request)) and Path(cfg.google_credentials_path).is_file(),
    }
    if not Path(cfg.google_token_path).is_file():
        return {**result, "status": "disconnected"}

    start = perf_counter()
    try:
        profile = await asyncio.to_thread(_gmail_profile)
    except GmailConnectionRequired:
        return {**result, "status": "reconnect"}
    except Exception as exc:
        logger.debug("Gmail profile check failed (%s)", type(exc).__name__)
        return {**result, "status": "error"}
    return {
        **result, "status": "ok", "account": profile.get("emailAddress", ""),
        "latency_ms": round((perf_counter() - start) * 1000, 1),
    }


@router.get("/status")
async def google_status(request: Request) -> JSONResponse:
    return JSONResponse(await connection_status(request), headers=_HEADERS)


@router.post("/connect")
async def start_connection(request: Request) -> JSONResponse:
    origin = _dashboard_origin(request)
    if not origin or request.headers.get("origin") != origin:
        raise HTTPException(403, "Open the dashboard on localhost to connect Gmail.")
    if getattr(request.app.state, "dashboard_only", False):
        raise HTTPException(409, "Gmail is unavailable in dashboard-only mode.")
    config = Path(get_settings().google_credentials_path)
    if not config.is_file():
        raise HTTPException(409, "Add your Google Desktop OAuth client credentials to connect Gmail.")
    try:
        flow = Flow.from_client_secrets_file(config, SCOPES, autogenerate_code_verifier=True)
        if flow.client_type != "installed":
            raise ValueError("Expected a desktop client")
        flow.redirect_uri = origin + "/"
        authorization_url, state = flow.authorization_url(
            prompt="consent select_account", include_granted_scopes="false",
        )
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(409, "Google OAuth requires a valid Desktop app client configuration.") from exc

    now = monotonic()
    pending = getattr(request.app.state, "google_flows", {})
    pending = {key: attempt for key, attempt in pending.items() if attempt[0] > now}
    pending.pop(request.cookies.get(_COOKIE), None)
    if len(pending) >= _MAX_FLOWS:
        pending.pop(next(iter(pending)))
    pending[state] = (now + _FLOW_TTL, flow)
    request.app.state.google_flows = pending

    response = JSONResponse({"authorization_url": authorization_url}, headers=_HEADERS)
    response.set_cookie(_COOKIE, state, max_age=_FLOW_TTL, httponly=True, samesite="lax")
    return response


def _save_grant(flow: Flow, code: str) -> None:
    flow.fetch_token(code=code, timeout=15)
    store_credentials(flow.credentials)


async def finish_connection(request: Request) -> RedirectResponse:
    state = request.query_params.get("state", "")
    cookie = request.cookies.get(_COOKIE, "")
    origin = _dashboard_origin(request)
    outcome = "expired"
    if origin and state and compare_digest(state.encode(), cookie.encode()):
        pending = getattr(request.app.state, "google_flows", {})
        attempt = pending.pop(state, None)
        if attempt and attempt[0] > monotonic() and attempt[1].redirect_uri == origin + "/":
            code = request.query_params.get("code")
            if request.query_params.get("error") == "access_denied":
                outcome = "cancelled"
            elif code and not request.query_params.get("error"):
                try:
                    await asyncio.to_thread(_save_grant, attempt[1], code)
                    outcome = "connected"
                except Exception as exc:
                    logger.warning("Google OAuth connection failed (%s)", type(exc).__name__)
                    outcome = "failed"
            else:
                outcome = "failed"
    response = RedirectResponse(f"/?gmail={outcome}", status_code=303, headers=_HEADERS)
    if outcome != "expired":
        response.delete_cookie(_COOKIE, httponly=True, samesite="lax")
    return response
