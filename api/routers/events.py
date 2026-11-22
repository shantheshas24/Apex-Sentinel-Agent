"""Inject a synthetic deadline email for testing without Gmail access."""
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from agents.observer_agent import ObserverAgent
from api.ws import manager

router = APIRouter()


class InjectEventRequest(BaseModel):
    event_type: Literal["email"] = "email"
    from_addr: str = Field(default="demo@example.com", alias="from", max_length=320)
    subject: str = Field(default="Assignment deadline", max_length=500)
    snippet: str = Field(default="", max_length=12000)
    urgent: bool = False

    model_config = {"populate_by_name": True}


@router.post("/api/events/inject")
async def inject_event(body: InjectEventRequest, request: Request) -> dict:
    from memory.redis_client import RedisClient

    if getattr(request.app.state, "dashboard_only", False):
        raise HTTPException(status_code=409, detail="Start with --skip-poll to classify sample emails.")
    redis = RedisClient.get_instance()
    event_id = f"inj-{uuid.uuid4().hex[:12]}"
    timestamp = datetime.now(timezone.utc).isoformat()
    event = ObserverAgent._normalize_email({
        "id": event_id,
        "from": body.from_addr,
        "subject": body.subject,
        "snippet": body.snippet,
        "date": timestamp,
        "unread": True,
    })
    event.update(event_id=event_id, source="dashboard", synthetic=True)
    if body.urgent and "urgent" not in event["urgency_keywords"]:
        event["urgency_keywords"].append("urgent")

    await redis.push_event(event)
    await redis.append_activity_log({
        "timestamp": timestamp,
        "agent": "Dashboard",
        "workflow": "deadline",
        "action": "Queued a test email for deadline detection",
        "event_id": event_id,
    })
    await manager.broadcast({"type": "new_plan", "message": "Test email queued for deadline detection"})
    return {"status": "injected", "event_id": event_id, "type": "email"}
