"""Deliver due reminders from the persisted deadline task state."""
import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from personal_os_agent.api.ws import manager
from personal_os_agent.config.settings import Settings
from personal_os_agent.core.deadlines import PRIORITIES, in_quiet_hours, is_sample_deadline, reminder_slots, task_priority
from personal_os_agent.memory.deadline_tasks import DeadlineTasks
from personal_os_agent.memory.redis_client import RedisClient
from personal_os_agent.utils.logger import get_logger
from personal_os_agent.utils.notifier import ACTIVE_CALL_STATUSES, CallProgress, Notifier


class ReminderWorker:
    def __init__(self, redis: RedisClient, settings: Settings):
        self._redis = redis
        self._tasks = DeadlineTasks(redis)
        self._settings = settings
        self._notifier = Notifier(settings) if settings.deadline_phone_available else None
        self._logger = get_logger("Reminders")

    async def tick(self, now: datetime, *, recheck_clock: Callable[[], datetime] | None = None) -> None:
        cfg = self._settings
        entries = await self._tasks.entries()
        await self._refresh_call_statuses(entries, now)
        if in_quiet_hours(now, cfg.deadline_timezone, cfg.deadline_quiet_start, cfg.deadline_quiet_end):
            return
        candidates = []
        for plan, state in entries:
            phone_enabled = self._notifier is not None and not is_sample_deadline(plan)
            slots, phone = reminder_slots(state, now, phone_enabled)
            if slots or phone:
                due = datetime.fromisoformat(state["due_at"])
                priority = task_priority(state["importance"], due, now)
                candidates.append((-PRIORITIES.index(priority), due, plan))
        alerts = []
        calls = []
        for _, _, plan in sorted(candidates, key=lambda item: item[:2]):
            alert, phone_due_at = await self._tasks.claim(
                plan, now, phone_enabled=self._notifier is not None,
                hourly_limit=cfg.deadline_hourly_limit, phone_daily_limit=cfg.deadline_phone_daily_limit,
            )
            if alert:
                alerts.append(alert)
            if phone_due_at:
                calls.append((plan, phone_due_at))
        if alerts:
            await manager.broadcast({"type": "reminder", "items": alerts})

        if self._notifier:
            for plan, due_at in calls:
                call_now = recheck_clock() if recheck_clock else now
                if (
                    not in_quiet_hours(call_now, cfg.deadline_timezone, cfg.deadline_quiet_start, cfg.deadline_quiet_end)
                    and await self._tasks.can_call(plan, due_at, call_now)
                ):
                    progress = await self._notifier.call_for_deadline(plan["id"])
                else:
                    progress = CallProgress("skipped")
                await self._tasks.record_call_status(
                    plan["id"], "submitting", progress.status, sid=progress.sid,
                )
                await self._redis.append_activity_log({
                    "timestamp": call_now.isoformat(),
                    "agent": "Reminders",
                    "workflow": "deadline",
                    "task_id": plan["id"],
                    "action": f"Critical deadline call status: {progress.status}.",
                })
            if calls:
                await manager.broadcast({"type": "refresh"})

    async def _refresh_call_statuses(self, entries: list[tuple[dict, dict]], now: datetime) -> None:
        changed = False
        for plan, state in entries:
            previous = state["phone_status"]
            status = None
            if previous == "submitting" and now - datetime.fromisoformat(state["phone_attempted_at"]) >= timedelta(minutes=2):
                status = "unconfirmed"
            elif self._notifier and previous in ACTIVE_CALL_STATUSES and state["phone_sid"]:
                status = await self._notifier.get_call_status(state["phone_sid"])
            if status and await self._tasks.record_call_status(plan["id"], previous, status):
                changed = True
        if changed:
            await manager.broadcast({"type": "refresh"})

    async def run(self) -> None:
        def utc_now() -> datetime:
            return datetime.now(timezone.utc)

        while True:
            try:
                await self.tick(utc_now(), recheck_clock=utc_now)
            except Exception as exc:
                self._logger.error("reminder_cycle_failed", error_type=type(exc).__name__)
            await asyncio.sleep(self._settings.deadline_reminder_interval)
