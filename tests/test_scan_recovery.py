"""Regression coverage for Gmail checkpoints, retries, and diagnostic logging."""
import hashlib
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from googleapiclient.errors import HttpError

from mcp_server.gmail_tools import GmailCursor, gmail_sync_page
from memory import redis_client
from memory.redis_client import RedisClient

pytestmark = pytest.mark.unit


def test_gmail_pages_preserve_baseline_and_read_inbox_messages():
    users = MagicMock()
    address = "test@example.com"
    users.getProfile.return_value.execute.side_effect = [
        {"emailAddress": address, "historyId": "100"},
        {"emailAddress": address, "historyId": "110"},
        {"emailAddress": address, "historyId": "112"},
    ]
    users.messages.return_value.list.return_value.execute.side_effect = [
        {"messages": [{"id": "earlier"}], "nextPageToken": "page-2"},
        {"messages": []},
    ]
    users.messages.return_value.get.return_value.execute.side_effect = [
        {"id": "earlier", "labelIds": ["INBOX"]},
        {"id": "later", "labelIds": ["INBOX"]},
        {"id": "outbound", "labelIds": ["SENT"]},
    ]
    users.history.return_value.list.return_value.execute.return_value = {
        "historyId": "112",
        "history": [{
            "messagesAdded": [{"message": {"id": "later"}}, {"message": {"id": "outbound"}}],
            "labelsAdded": [{"message": {"id": "later"}, "labelIds": ["INBOX"]}],
        }],
    }
    first = gmail_sync_page(users, GmailCursor())
    assert first.messages[0]["unread"] is False
    assert first.cursor.page_token == "page-2"
    second = gmail_sync_page(users, first.cursor)
    assert users.messages.return_value.list.call_args.kwargs["pageToken"] == "page-2"
    assert second.cursor.history_id == "100"
    third = gmail_sync_page(users, second.cursor)
    assert users.history.return_value.list.call_args.kwargs["startHistoryId"] == "100"
    assert [message["id"] for message in third.messages] == ["later"]
    assert third.cursor.history_id == "112"


def test_expired_gmail_history_backfills_from_last_saved_time():
    users = MagicMock()
    address = "test@example.com"
    mailbox = hashlib.sha256(address.encode()).hexdigest()
    users.getProfile.return_value.execute.return_value = {"emailAddress": address, "historyId": "120"}
    users.history.return_value.list.return_value.execute.side_effect = HttpError(
        SimpleNamespace(status=404, reason="Not Found"), b"{}",
    )
    users.messages.return_value.list.return_value.execute.return_value = {"messages": []}
    page = gmail_sync_page(users, GmailCursor(mailbox=mailbox, history_id="100", since=1234567890))
    assert "after:1234567890" in users.messages.return_value.list.call_args.kwargs["q"]
    assert page.cursor.history_id == "120"


@pytest.mark.asyncio
async def test_scan_job_lease_recovery_retry_and_deduplication(monkeypatch):
    url = os.getenv("PERSONALOS_TEST_REDIS_URL")
    if not url:
        pytest.skip("Set PERSONALOS_TEST_REDIS_URL to an isolated Redis database")
    redis = RedisClient(url)
    scope = f"test-scan:{uuid4().hex}:"
    keys = ("READY", "ACTIVE", "DONE", "IN_FLIGHT", "LEASES", "RETRIES", "GMAIL_SYNC")
    for key in keys:
        monkeypatch.setattr(redis_client, key, scope + key)
    now = 1000
    monkeypatch.setattr(redis_client, "time", lambda: now)
    earlier = {"event_id": scope + "earlier", "type": "email"}
    newer = {"event_id": scope + "newer", "type": "email"}
    try:
        assert await redis.enqueue_unseen_email(earlier)
        abandoned = await redis.reserve_event(lease_seconds=1)
        assert abandoned.event == earlier
        assert not await redis.enqueue_unseen_email(earlier)
        now += 2
        recovered = await redis.reserve_event()
        assert recovered.event == earlier
        assert not await redis.finish_event(abandoned)
        assert await redis.finish_event(recovered, retry_in=60)

        assert await redis.enqueue_unseen_email(newer)
        fresh = await redis.reserve_event()
        assert fresh.event == newer
        assert await redis.finish_event(fresh)
        assert await redis.reserve_event() is None
        now += 60
        retry = await redis.reserve_event()
        assert retry.event == {**earlier, "scan_attempt": 1}
        assert await redis.finish_event(retry)
        assert not await redis.enqueue_unseen_email(earlier)
        assert await redis.scan_queue_depths() == {"events": 0, "processing": 0, "retrying": 0}

        assert await redis.save_gmail_sync({"history_id": "110"}, previous={})
        assert not await redis.save_gmail_sync({"history_id": "100"}, previous={})
        assert await redis.get_gmail_sync() == {"history_id": "110"}
    finally:
        await redis._redis.delete(*(scope + key for key in keys))
        await redis.close()


def test_new_logs_exclude_provider_payloads(tmp_path):
    log = tmp_path / "runtime.log"
    script = """
import logging
import sys
from utils.logger import get_logger, setup_logging
setup_logging(sys.argv[1])
logging.getLogger("openai._base_client").warning("private model payload")
logging.getLogger("mcp.client.sse").error("private Gmail payload")
get_logger("Planner").info("deadline_detected", count=1)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(log)], capture_output=True, text=True, check=True,
    )
    output = log.read_text(encoding="utf-8") + completed.stdout
    assert "deadline_detected" in output
    assert "private" not in output
