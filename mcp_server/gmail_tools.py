"""
Read-only Gmail MCP access.
Uses the Gmail grant saved by the dashboard or OAuth setup script.
"""
import base64
import hashlib
from binascii import Error as Base64Error
from email.message import Message
from html.parser import HTMLParser
from time import time
from typing import Any

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel

from mcp_server.google_auth import get_credentials

class GmailReadResult(BaseModel):
    result: list[dict[str, Any]]


class GmailCursor(BaseModel):
    mailbox: str = ""
    history_id: str = ""
    since: int = 0
    page_token: str = ""
    baseline: str = ""
    started_at: int = 0


class GmailSyncPage(BaseModel):
    messages: list[dict[str, Any]]
    cursor: GmailCursor



class EmailHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.chunks: list[str] = []
        self.hidden = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in {"style", "script", "head"}:
            self.hidden = True
        if tag in {"br", "p", "div", "li", "tr"}:
            self.chunks.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"style", "script", "head"}:
            self.hidden = False
        if tag in {"p", "div", "li", "tr"}:
            self.chunks.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.chunks.append(data)


def message_payload(message: dict[str, Any]) -> dict[str, Any]:
    """Normalize Gmail metadata and the inline message body for extraction."""
    payload = message.get("payload", {})
    headers = {entry["name"].lower(): entry["value"] for entry in payload.get("headers", [])}
    text = _message_text(payload)
    return {
        "id": message["id"],
        "from": headers.get("from", ""),
        "subject": headers.get("subject", "(no subject)"),
        "snippet": message.get("snippet", ""),
        "body": text[:12000],
        "date": headers.get("date", ""),
        "unread": "UNREAD" in message.get("labelIds", []),
    }


def _message_text(payload: dict[str, Any]) -> str:
    plain = []
    html = []
    parts = [payload]
    while parts:
        part = parts.pop()
        if part.get("filename") or part.get("mimeType") == "message/rfc822":
            continue
        parts.extend(reversed(part.get("parts", [])))
        mime_type = part.get("mimeType")
        encoded = part.get("body", {}).get("data")
        if mime_type not in {"text/plain", "text/html"} or not encoded:
            continue
        content_type = Message()
        for header in part.get("headers", []):
            if header["name"].lower() == "content-type":
                content_type["Content-Type"] = header["value"]
                break
        try:
            decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except (Base64Error, ValueError):
            continue
        try:
            text = decoded.decode(content_type.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            text = decoded.decode("utf-8", errors="replace")
        if text.strip():
            (plain if mime_type == "text/plain" else html).append(text)
    if plain:
        return " ".join(plain)
    parser = EmailHTML()
    parser.feed(" ".join(html))
    parser.close()
    return "".join(parser.chunks)


def _get_gmail_service():
    """Use the current grant and a fresh HTTP transport for each tool call."""
    return build("gmail", "v1", credentials=get_credentials())


def _read_page_messages(users, ids: list[str]) -> list[dict[str, Any]]:
    messages = []
    for message_id in dict.fromkeys(ids):
        try:
            raw = users.messages().get(userId="me", id=message_id, format="full").execute()
        except HttpError as exc:
            if exc.resp.status == 404:
                continue
            raise
        labels = set(raw.get("labelIds", []))
        if labels.intersection({"INBOX", "UNREAD"}) and not labels.intersection({"DRAFT", "SPAM", "TRASH"}):
            messages.append(message_payload(raw))
    return messages


def _history_ids(page: dict) -> list[str]:
    ids = []
    for item in page.get("history", []):
        ids.extend(entry["message"]["id"] for entry in item.get("messagesAdded", []))
        ids.extend(
            entry["message"]["id"] for entry in item.get("labelsAdded", []) if "INBOX" in entry.get("labelIds", [])
        )
    return ids


def gmail_sync_page(users, cursor: GmailCursor) -> GmailSyncPage:
    """Fetch one resumable page, replaying the last scan window on expired history."""
    profile = users.getProfile(userId="me").execute()
    mailbox = hashlib.sha256(profile["emailAddress"].lower().encode()).hexdigest()
    if cursor.mailbox != mailbox:
        cursor = GmailCursor(mailbox=mailbox)

    now = int(time())
    started_at = cursor.started_at or now
    params = {"userId": "me", "maxResults": 20}
    if cursor.page_token:
        params["pageToken"] = cursor.page_token

    if cursor.history_id:
        try:
            page = users.history().list(
                **params, startHistoryId=cursor.history_id, historyTypes=["messageAdded", "labelAdded"],
            ).execute()
        except HttpError as exc:
            if exc.resp.status != 404 and not (cursor.page_token and exc.resp.status == 400):
                raise
            params.pop("pageToken", None)
        else:
            next_page = page.get("nextPageToken", "")
            next_cursor = cursor.model_copy(update={"page_token": next_page, "started_at": started_at})
            if not next_page:
                next_cursor = GmailCursor(mailbox=mailbox, history_id=page["historyId"], since=started_at)
            return GmailSyncPage(messages=_read_page_messages(users, _history_ids(page)), cursor=next_cursor)

    baseline = cursor.baseline or profile["historyId"]
    since = cursor.since or now - 86400
    query = f"{{is:unread (in:inbox after:{since})}}"
    try:
        page = users.messages().list(**params, q=query).execute()
    except HttpError as exc:
        if exc.resp.status != 400 or "pageToken" not in params:
            raise
        params.pop("pageToken")
        page = users.messages().list(**params, q=query).execute()
    next_page = page.get("nextPageToken", "")
    if next_page:
        next_cursor = GmailCursor(
            mailbox=mailbox, since=since, baseline=baseline, started_at=started_at, page_token=next_page,
        )
    else:
        next_cursor = GmailCursor(mailbox=mailbox, history_id=baseline, since=started_at)
    ids = [item["id"] for item in page.get("messages", [])]
    return GmailSyncPage(messages=_read_page_messages(users, ids), cursor=next_cursor)


def register_gmail_tools(mcp: FastMCP) -> None:
    """Register the read-only Gmail tool."""

    @mcp.tool()
    def read_emails(max_results: int = 10, query: str = "") -> GmailReadResult:
        """
        Fetch recent emails from Gmail inbox.
        Returns list of {id, from, subject, snippet, body, date, unread}.
        query: optional Gmail search string (e.g. 'is:unread', 'from:boss@company.com')
        """
        service = _get_gmail_service()
        q = query if query else "in:inbox"
        result = (
            service.users()
            .messages()
            .list(userId="me", maxResults=max_results, q=q)
            .execute()
        )
        messages = result.get("messages", [])
        emails = []
        for msg in messages:
            full = (
                service.users()
                .messages()
                .get(userId="me", id=msg["id"], format="full")
                .execute()
            )
            emails.append(message_payload(full))
        return GmailReadResult(result=emails)

    @mcp.tool()
    def sync_gmail(cursor: GmailCursor | None = None) -> GmailSyncPage:
        """Read one page of inbox changes and return a checkpoint for the next page."""
        service = _get_gmail_service()
        try:
            return gmail_sync_page(service.users(), cursor or GmailCursor())
        except HttpError as exc:
            raise RuntimeError(f"Gmail API returned HTTP {exc.resp.status}") from None
        finally:
            service.close()
