"""Regression tests for deadline validation and the reminder lifecycle."""
import asyncio
import base64
import json
import os
import uuid
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from openai import RateLimitError

from agents.planner_agent import ModelCoolingDown, PlannerAgent
from api.app import app
from api.routers import deadlines
from config.settings import Settings
from core.deadlines import explicit_due_time, in_quiet_hours, task_state
from core.reminders import ReminderWorker
from mcp_server.gmail_tools import message_payload
from mcp_server.server import build_mcp_app
from memory.deadline_tasks import DeadlineTasks, REMINDERS, TASKS
from memory.redis_client import RedisClient
from utils.notifier import CallProgress

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value", [
    "Friday at 5 PM", "October 30", "2027-10-30T17:00",
    "2027-02-30T17:00Z", "2027-10-30T17:00+05:99",
    "2027-10-30T17:00Z or 2027-10-31T17:00Z",
])
def test_unclear_dates_require_confirmation(value):
    assert explicit_due_time(value) is None


def test_explicit_offset_and_overnight_quiet_hours():
    due = explicit_due_time("by 2027-10-30T17:00+05:30")
    assert due == datetime(2027, 10, 30, 11, 30, tzinfo=timezone.utc)
    late = datetime(2027, 10, 30, 18, tzinfo=timezone.utc)
    assert in_quiet_hours(late, "Asia/Kolkata", time(22), time(7))
    assert not in_quiet_hours(late, "Asia/Kolkata", time(0), time(0))


def test_legacy_date_outside_evidence_requires_review():
    plan = {"deadline": "Submit the assignment", "due": "2027-10-30T17:00Z", "evidence": "Submit when instructed."}
    assert task_state(plan)["due_at"] is None


def test_gmail_html_body_is_available_beyond_preview():
    html = "<p>Please <b>submit the report</b> by Friday.</p><script>ignore me</script>"
    payload = message_payload({
        "id": "message",
        "snippet": "Introduction",
        "payload": {"mimeType": "text/html", "body": {
            "data": base64.urlsafe_b64encode(html.encode()).decode().rstrip("="),
        }},
    })
    assert payload["snippet"] == "Introduction"
    assert "Please submit the report by Friday." in payload["body"]
    assert "ignore me" not in payload["body"]


@pytest.mark.asyncio
async def test_mcp_gmail_read_requests_inline_text(monkeypatch):
    gmail = MagicMock()
    messages = gmail.users.return_value.messages.return_value
    messages.list.return_value.execute.return_value = {"messages": [{"id": "sample"}]}
    gmail_message = "Please send the assignment by Friday."
    messages.get.return_value.execute.return_value = {
        "id": "sample",
        "payload": {"mimeType": "text/plain", "body": {"data": base64.urlsafe_b64encode(gmail_message.encode()).decode()}},
    }
    monkeypatch.setattr("mcp_server.gmail_tools._get_gmail_service", lambda: gmail)
    _, structured = await build_mcp_app().call_tool("read_emails", {"query": "is:unread"})
    messages.get.assert_called_once_with(userId="me", id="sample", format="full")
    assert structured["result"][0]["body"] == gmail_message


@pytest.mark.asyncio
async def test_model_rate_limit_falls_back_only_for_a_stated_deadline():
    planner = PlannerAgent.__new__(PlannerAgent)
    planner._logger = MagicMock()
    response = httpx.Response(429, request=httpx.Request("POST", "https://example.test"))
    planner._request_candidates = AsyncMock(side_effect=RateLimitError("Quota reached", response=response, body={}))
    due = "12th December 2027, 5:00 PM"
    event = {
        "event_id": "fallback-test",
        "payload": {
            "subject": "Project deadline",
            "body": f"The report must be completed and submitted by\n{due}.",
            "unread": False,
        },
    }
    [plan] = await planner._plan_event(event)
    assert plan["due"] == due
    assert plan["evidence"] in " ".join(event["payload"]["body"].split())
    assert task_state(plan)["due_at"] is None

    event["payload"] = {"subject": "Meeting", "body": "Would you like to meet tomorrow at 5 PM?"}
    with pytest.raises(ModelCoolingDown):
        await planner._plan_event(event)


@pytest.fixture
async def workflow_redis(monkeypatch):
    url = os.getenv("PERSONALOS_TEST_REDIS_URL")
    if not url:
        pytest.skip("Set PERSONALOS_TEST_REDIS_URL to an isolated Redis database")
    redis = RedisClient(url)
    marker = f"test-task-{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(RedisClient, "get_instance", lambda: redis)
    try:
        yield redis, marker
    finally:
        for key in ("emails:all", TASKS):
            ids = await redis._redis.hkeys(key)
            owned = [task_id for task_id in ids if task_id.startswith(marker)]
            if owned:
                await redis._redis.hdel(key, *owned)
        for key in (REMINDERS, "activity:log"):
            for record in await redis._redis.lrange(key, 0, -1):
                entry = json.loads(record)
                if any(str(entry.get(field, "")).startswith(marker) for field in ("task_id", "id", "event_id", "plan_id")):
                    await redis._redis.lrem(key, 1, record)
        await redis.close()


def draft_record(task_id, **fields):
    return {
        "id": task_id, "event_type": "email", "deadline": "Submit the project",
        "evidence": "Please submit by Friday at 5 PM.", "due": "Friday at 5 PM",
        "action": "track_deadline", "priority": "high", **fields,
    }


@pytest.mark.asyncio
async def test_email_to_confirmation_reminder_completion_and_reschedule(workflow_redis, monkeypatch):
    redis, marker = workflow_redis
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    cfg = Settings(_env_file=None, deadline_quiet_start=time(0), deadline_quiet_end=time(0), deadline_hourly_limit=1000)
    monkeypatch.setattr(deadlines, "get_settings", lambda: cfg)
    planner = PlannerAgent.__new__(PlannerAgent)
    planner._redis = redis
    planner._memory = None
    planner._model = "test-model"
    planner._openrouter = False
    excerpt = "Please submit by Friday at 5 PM."
    second_excerpt = "Complete the survey by Monday."
    proposals = {"tasks": [
        {"deadline": "Submit the project", "evidence": excerpt, "due": "Friday at 5 PM", "priority": "high"},
        {"deadline": "Complete the survey", "evidence": second_excerpt, "due": "Monday", "priority": "low"},
    ]}
    completion = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(proposals)))])
    planner._llm = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=completion))))
    event = {
        "event_id": marker, "type": "email", "synthetic": True,
        "payload": {"subject": "Upcoming work", "snippet": "Read below", "body": f"{excerpt} {second_excerpt}"},
    }
    await planner._process_email(event)
    await planner._process_email(event)
    worker = ReminderWorker(redis, cfg)
    history = DeadlineTasks(redis)

    async def recent():
        return [item for item in await history.history() if item["task_id"] == marker]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        cards = [item for item in (await client.get("/api/deadlines")).json() if item["id"].startswith(marker)]
        assert len(cards) == 2
        task = next(item for item in cards if item["id"] == marker)
        assert task["task_status"] == "needs_review"
        await worker.tick(now)
        assert await recent() == []
        path = "/api/deadlines/" + task["id"]
        bad = await client.patch(path, json={"due_at": "2027-10-30T17:00"})
        assert bad.status_code == 422
        confirmed = await client.patch(path, json={"due_at": (now + timedelta(hours=3, minutes=30)).isoformat()})
        assert confirmed.status_code == 200
        assert confirmed.json()["task_status"] == "upcoming"
        assert confirmed.json()["priority"] == "critical"

        await asyncio.gather(worker.tick(now), worker.tick(now))
        assert len(await recent()) == 1

        new_due = now + timedelta(days=2)
        assert (await client.patch(path, json={"due_at": new_due.isoformat()})).status_code == 200
        await worker.tick(now + timedelta(minutes=40))
        assert len(await recent()) == 1
        await worker.tick(new_due - timedelta(days=1))
        assert len(await recent()) == 2
        assert (await recent())[0]["due_at"] == new_due.isoformat()

        done = await client.patch(path, json={"completed": True})
        assert done.json()["task_status"] == "completed"
        await worker.tick(new_due - timedelta(hours=2))
        assert len(await recent()) == 2
        response = await client.get("/api/reminders")
        assert any(item["task_id"] == marker for item in response.json()["items"])


@pytest.mark.asyncio
async def test_critical_call_is_automatic_after_snooze_and_attempted_once(workflow_redis, monkeypatch):
    redis, marker = workflow_redis
    # Fixed future clock keeps the policy test deterministic.
    now = datetime(2077, 8, 1, 8, tzinfo=timezone.utc) + timedelta(days=int(marker[-8:], 16) % 10000)
    cfg = Settings(_env_file=None, deadline_quiet_start=time(22), deadline_quiet_end=time(7))
    worker = ReminderWorker(redis, cfg)
    notifier = AsyncMock()
    notifier.call_for_deadline.return_value = CallProgress("queued", "CA-test")
    notifier.get_call_status.return_value = "no-answer"
    monkeypatch.setattr(worker, "_notifier", notifier)
    store = DeadlineTasks(redis)
    plan = draft_record(marker)
    await redis.store_deadline_plan(plan)
    due_at = (now + timedelta(hours=4, minutes=30)).isoformat()
    await store.update(marker, {"due_at": due_at}, now)
    sample = draft_record(f"{marker}-sample", synthetic=True)
    await redis.store_deadline_plan(sample)
    await store.update(sample["id"], {"due_at": (now + timedelta(hours=1)).isoformat()}, now)
    await worker.tick(now)
    notifier.call_for_deadline.assert_not_awaited()

    await store.update(marker, {
        "snoozed_until": (now + timedelta(minutes=45)).isoformat(),
    }, now + timedelta(minutes=5))
    await worker.tick(now + timedelta(minutes=35))
    notifier.call_for_deadline.assert_not_awaited()
    await asyncio.gather(worker.tick(now + timedelta(minutes=45)), worker.tick(now + timedelta(minutes=45)))
    notifier.call_for_deadline.assert_awaited_once_with(marker)
    assert len([alert for alert in await store.history() if alert["task_id"] == marker]) == 2
    await worker.tick(now + timedelta(minutes=46))
    notifier.get_call_status.assert_awaited_with("CA-test")
    task = next(item for item in await store.get_tasks(now) if item["id"] == marker)
    assert task["phone_status"] == "no-answer"
    assert "phone_sid" not in task
    notifier.call_for_deadline.assert_awaited_once_with(marker)


@pytest.mark.asyncio
async def test_quiet_hours_and_rate_limit_prioritize_critical_tasks(workflow_redis):
    redis, marker = workflow_redis
    now = datetime(2107, 3, 1, 6, 40, tzinfo=timezone.utc) + timedelta(days=int(marker[-8:], 16) % 10000)
    cfg = Settings(_env_file=None, deadline_quiet_start=time(22), deadline_quiet_end=time(7), deadline_hourly_limit=1)
    worker = ReminderWorker(redis, cfg)
    store = DeadlineTasks(redis)
    for suffix, priority in (("low", "low"), ("critical", "high")):
        plan = draft_record(f"{marker}-{suffix}", priority=priority)
        await redis.store_deadline_plan(plan)
        await store.update(plan["id"], {"due_at": (now + timedelta(hours=1)).isoformat()}, now)

    await worker.tick(now)
    assert not any(item["task_id"].startswith(marker) for item in await store.history())
    await worker.tick(now + timedelta(minutes=20))
    alerts = [item for item in await store.history() if item["task_id"].startswith(marker)]
    assert [item["task_id"] for item in alerts] == [f"{marker}-critical"]


@pytest.mark.asyncio
async def test_legacy_reply_keeps_deadline_and_discards_reply_fields_from_api(workflow_redis):
    redis, marker = workflow_redis
    plan = draft_record(marker, action="send_email", user_response="queued", action_args={"body": "old draft"})
    await redis.store_deadline_plan(plan)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for path in ("/api/deadlines", "/api/emails"):
            data = (await client.get(path)).json()
            deadline = next(item for item in data if item["id"] == marker)
            assert deadline["action"] == "track_deadline"
            assert "action_args" not in deadline
            assert "user_response" not in deadline
