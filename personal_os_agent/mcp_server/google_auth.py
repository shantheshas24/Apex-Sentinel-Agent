"""Gmail OAuth credentials shared by the dashboard and MCP tools."""
from functools import partial
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from personal_os_agent.config.settings import get_settings

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

_lock = Lock()
_cached: tuple[Path, int, Credentials] | None = None


class GmailConnectionRequired(RuntimeError):
    """The local Gmail grant is missing or needs to be renewed."""

def _require_read_only(credentials: Credentials) -> None:
    scopes = credentials.granted_scopes or credentials.scopes or []
    if not credentials.refresh_token or set(scopes) != set(SCOPES):
        raise GmailConnectionRequired("Reconnect Gmail with read-only access in the dashboard.")


def _write_credentials(path: Path, credentials: Credentials) -> None:
    global _cached
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        pending = Path(file.name)
        try:
            file.write(credentials.to_json())
            file.close()
            pending.replace(path)
        finally:
            file.close()
            pending.unlink(missing_ok=True)
    _cached = (path, path.stat().st_mtime_ns, credentials)


def store_credentials(credentials: Credentials) -> None:
    """Replace the saved grant after a successful account connection."""
    _require_read_only(credentials)
    with _lock:
        _write_credentials(Path(get_settings().google_token_path), credentials)


def get_credentials() -> Credentials:
    """Load or refresh the saved grant without launching a browser."""
    global _cached
    path = Path(get_settings().google_token_path)
    with _lock:
        try:
            stamp = path.stat().st_mtime_ns
            if _cached and _cached[:2] == (path, stamp):
                credentials = _cached[2]
            else:
                credentials = Credentials.from_authorized_user_file(path)
        except FileNotFoundError as exc:
            raise GmailConnectionRequired("Connect Gmail in the dashboard.") from exc
        except ValueError as exc:
            raise GmailConnectionRequired("Reconnect Gmail in the dashboard.") from exc

        _require_read_only(credentials)
        if not credentials.valid:
            try:
                credentials.refresh(partial(Request(), timeout=10))
            except RefreshError as exc:
                response = exc.args[1] if len(exc.args) > 1 else None
                if isinstance(response, dict) and response.get("error") == "invalid_grant":
                    raise GmailConnectionRequired("Reconnect Gmail in the dashboard.") from exc
                raise
            _require_read_only(credentials)
            _write_credentials(path, credentials)
        else:
            _cached = (path, stamp, credentials)
        return credentials
