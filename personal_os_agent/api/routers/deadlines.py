"""Deadline task lifecycle and reminder endpoints."""
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import AwareDatetime, BaseModel, Field

from personal_os_agent.api.ws import manager
from personal_os_agent.config.settings import get_settings
from personal_os_agent.memory.deadline_tasks import DeadlineTasks
from personal_os_agent.memory.redis_client import RedisClient

router = APIRouter()


class DeadlineUpdate(BaseModel):
    title: str = Field(default="", max_length=160)
    importance: Literal["low", "medium", "high"] = "medium"
    due_at: AwareDatetime | None = None
    completed: bool = False
    snoozed_until: AwareDatetime | None = None

    model_config = {"extra": "forbid"}


@router.get("/api/deadlines")
async def get_tasks() -> list[dict]:
    return await DeadlineTasks(RedisClient.get_instance()).get_tasks(datetime.now(timezone.utc))


@router.patch("/api/deadlines/{task_id}")
async def update_task(task_id: str, body: DeadlineUpdate) -> dict:
    now = datetime.now(timezone.utc)
    changes = body.model_dump(exclude_unset=True)
    if "title" in changes:
        changes["title"] = changes["title"].strip()
        if not changes["title"]:
            raise HTTPException(status_code=422, detail="Enter a task title.")
    for field in ("due_at", "snoozed_until"):
        if changes.get(field):
            changes[field] = changes[field].astimezone(timezone.utc).isoformat()
    if "completed" in changes:
        changes["completed_at"] = now.isoformat() if changes.pop("completed") else None
    try:
        task = await DeadlineTasks(RedisClient.get_instance()).update(task_id, changes, now)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if task is None:
        raise HTTPException(status_code=404, detail="Deadline task not found.")
    await manager.broadcast({"type": "refresh"})
    return task


@router.get("/api/reminders")
async def list_reminders() -> dict:
    cfg = get_settings()
    return {
        "items": await DeadlineTasks(RedisClient.get_instance()).history(),
        "phone_available": cfg.deadline_phone_available,
        "quiet_start": cfg.deadline_quiet_start.isoformat(timespec="minutes"),
        "quiet_end": cfg.deadline_quiet_end.isoformat(timespec="minutes"),
        "timezone": cfg.deadline_timezone,
    }
