"""Inbox scanning, tracked email records, and the activity feed."""
from fastapi import APIRouter, Request

from personal_os_agent.agents.observer_agent import trigger_immediate_poll
from personal_os_agent.core.deadlines import deadline_record
from personal_os_agent.memory.redis_client import RedisClient

router = APIRouter()


@router.get("/api/emails")
async def get_emails() -> list[dict]:
    """Return the stored deadline extractions from Gmail and test emails."""
    return [deadline_record(plan) for plan in await RedisClient.get_instance().get_deadlines()]


@router.get("/api/feed")
async def activity_feed(limit: int = 50) -> list[dict]:
    return await RedisClient.get_instance().get_activity_log(limit=limit, workflow="deadline")


@router.post("/api/poll/now")
async def poll_now(request: Request) -> dict:
    """Wake the Gmail Observer when the user requests an inbox scan."""
    if not getattr(request.app.state, "gmail_polling", True) or getattr(request.app.state, "dashboard_only", False):
        return {"status": "paused", "message": "Gmail polling is disabled for this run."}
    await trigger_immediate_poll()
    return {"status": "triggered", "message": "Gmail scan requested."}
