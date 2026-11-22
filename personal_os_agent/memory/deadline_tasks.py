"""Redis persistence for editable deadline tasks and their reminder history."""
import json
from datetime import datetime, timezone
from typing import Any

from redis.exceptions import WatchError

from personal_os_agent.core.deadlines import deadline_view, is_deadline_plan, is_sample_deadline, reminder_slots, task_priority, task_state
from personal_os_agent.memory.redis_client import RedisClient

TASKS = "deadline:tasks"
REMINDERS = "deadline:reminders"


class DeadlineTasks:
    def __init__(self, redis: RedisClient):
        self._redis = redis
        self._client = redis._redis

    async def entries(self) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        plans = await self._redis.get_deadlines()
        raw = await self._client.hgetall(TASKS) if plans else {}
        return [
            (plan, task_state(plan, json.loads(raw.get(plan["id"], "null"))))
            for plan in plans
        ]

    async def get_tasks(self, now: datetime) -> list[dict[str, Any]]:
        return [deadline_view(plan, state, now) for plan, state in await self.entries()]

    async def update(self, task_id: str, changes: dict[str, Any], now: datetime) -> dict[str, Any] | None:
        plan = await self._redis.get_email_record(task_id)
        if not is_deadline_plan(plan):
            return None
        async with self._client.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch(TASKS)
                    raw = await pipe.hget(TASKS, task_id)
                    state = task_state(plan, json.loads(raw) if raw else None)

                    if "due_at" in changes and changes["due_at"] != state["due_at"]:
                        state.update(delivered={}, snoozed_until=None)
                    if "snoozed_until" in changes and changes["snoozed_until"]:
                        snooze = datetime.fromisoformat(changes["snoozed_until"])
                        due_value = changes.get("due_at", state["due_at"])
                        completed = changes.get("completed_at", state["completed_at"])
                        if completed or not due_value or not now < snooze < datetime.fromisoformat(due_value):
                            raise ValueError("Snooze must end before a confirmed, incomplete deadline.")
                    state.update(changes)
                    if state["completed_at"]:
                        state["snoozed_until"] = None

                    pipe.multi()
                    pipe.hset(TASKS, task_id, json.dumps(state))
                    await pipe.execute()
                    return deadline_view(plan, state, now)
                except WatchError:
                    continue

    async def history(self) -> list[dict[str, Any]]:
        return [json.loads(raw) for raw in await self._client.lrange(REMINDERS, 0, 49)]

    async def claim(
        self, plan: dict[str, Any], now: datetime, *, phone_enabled: bool, hourly_limit: int, phone_daily_limit: int
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Record a reminder and reserve any call atomically across workers."""
        task_id = plan["id"]
        hour_counter = f"deadline:reminder-hour:{now.astimezone(timezone.utc):%Y%m%d%H}"
        day_counter = f"deadline:phone-day:{now.astimezone(timezone.utc):%Y%m%d}"
        async with self._client.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch(TASKS, hour_counter, day_counter)
                    raw = await pipe.hget(TASKS, task_id)
                    state = task_state(plan, json.loads(raw) if raw else None)
                    slots, phone = reminder_slots(state, now, phone_enabled and not is_sample_deadline(plan))
                    if slots and int(await pipe.get(hour_counter) or 0) >= hourly_limit:
                        slots = []
                    if phone and int(await pipe.get(day_counter) or 0) >= phone_daily_limit:
                        phone = False
                    if not slots and not phone:
                        return None, None

                    timestamp = now.isoformat()
                    alert = None
                    pipe.multi()
                    if slots:
                        state["delivered"].update(dict.fromkeys(slots, timestamp))
                        alert = {
                            "task_id": task_id,
                            "title": state["title"],
                            "due_at": state["due_at"],
                            "sent_at": timestamp,
                        }
                        pipe.lpush(REMINDERS, json.dumps(alert))
                        pipe.ltrim(REMINDERS, 0, 49)
                        pipe.incr(hour_counter)
                        pipe.expire(hour_counter, 7200)
                    if phone:
                        state["phone_attempted_at"] = timestamp
                        state["phone_status"] = "submitting"
                        pipe.incr(day_counter)
                        pipe.expire(day_counter, 172800)
                    pipe.hset(TASKS, task_id, json.dumps(state))
                    await pipe.execute()
                    return alert, state["due_at"] if phone else None
                except WatchError:
                    continue

    async def can_call(self, plan: dict, due_at: str, now: datetime) -> bool:
        raw = await self._client.hget(TASKS, plan["id"])
        state = task_state(plan, json.loads(raw) if raw else None)
        return bool(
            not is_sample_deadline(plan) and not state["completed_at"] and state["due_at"] == due_at
            and task_priority(state["importance"], datetime.fromisoformat(due_at), now) == "critical"
            and (not state["snoozed_until"] or now >= datetime.fromisoformat(state["snoozed_until"]))
        )

    async def record_call_status(
        self, task_id: str, previous: str, status: str, *, sid: str | None = None,
    ) -> bool:
        """Update a call without overwriting a concurrent task edit or newer phone status."""
        async with self._client.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch(TASKS)
                    raw = await pipe.hget(TASKS, task_id)
                    state = json.loads(raw) if raw else {}
                    if state.get("phone_status") != previous or status == previous:
                        return False
                    state["phone_status"] = status
                    if sid:
                        state["phone_sid"] = sid
                    pipe.multi()
                    pipe.hset(TASKS, task_id, json.dumps(state))
                    await pipe.execute()
                    return True
                except WatchError:
                    continue
