"""
Async Redis client — singleton wrapper around redis.asyncio.
Provides typed helper methods for all queue operations used by the agents.

Key schema:
  events:queue       LIST  — ready scan jobs
  events:in-flight   HASH  — claimed jobs, indexed by lease token
  events:retry       ZSET  — jobs scheduled for another attempt
  gmail:sync         JSON  — mailbox history and pagination checkpoint
  emails:all         HASH  — field=plan_id, ALL email plans (every confidence level)
  seen:event_ids     SET   — deduplication, TTL=86400s
  activity:log       LIST  — LTRIM to 500 entries
"""
import json
from dataclasses import dataclass
from time import time
from typing import Any
from uuid import uuid4

import redis.asyncio as aioredis
from redis.exceptions import WatchError

from personal_os_agent.core.deadlines import is_deadline_plan

READY = "events:queue"
ACTIVE = "events:active"
DONE = "events:classified"
IN_FLIGHT = "events:in-flight"
LEASES = "events:leases"
RETRIES = "events:retry"
GMAIL_SYNC = "gmail:sync"

_RESERVE_EVENT = """
local now = tonumber(ARGV[1])
local expired = redis.call('ZRANGEBYSCORE', KEYS[3], '-inf', now, 'LIMIT', 0, 32)
for _, token in ipairs(expired) do
    local raw = redis.call('HGET', KEYS[2], token)
    if raw then redis.call('RPUSH', KEYS[1], raw) end
    redis.call('HDEL', KEYS[2], token)
    redis.call('ZREM', KEYS[3], token)
end
local due = redis.call('ZRANGEBYSCORE', KEYS[4], '-inf', now, 'LIMIT', 0, 32)
for _, raw in ipairs(due) do
    redis.call('RPUSH', KEYS[1], raw)
    redis.call('ZREM', KEYS[4], raw)
end
local raw = redis.call('LPOP', KEYS[1])
if not raw then return nil end
redis.call('HSET', KEYS[2], ARGV[2], raw)
redis.call('ZADD', KEYS[3], ARGV[3], ARGV[2])
return raw
"""

_FINISH_EVENT = """
if redis.call('HDEL', KEYS[1], ARGV[1]) == 0 then return 0 end
redis.call('ZREM', KEYS[2], ARGV[1])
if ARGV[3] == '' then
    redis.call('SREM', KEYS[3], ARGV[2])
    redis.call('SADD', KEYS[4], ARGV[2])
else
    redis.call('ZADD', KEYS[5], ARGV[4], ARGV[3])
end
return 1
"""


@dataclass(frozen=True)
class EventJob:
    token: str
    event: dict[str, Any]


class RedisClient:
    _instance: "RedisClient | None" = None

    def __init__(self, url: str) -> None:
        self._redis: aioredis.Redis = aioredis.from_url(
            url, encoding="utf-8", decode_responses=True
        )

    # ── Singleton ─────────────────────────────────────────────────────────────

    @classmethod
    def get_instance(cls) -> "RedisClient":
        if cls._instance is None:
            from personal_os_agent.config.settings import get_settings
            cls._instance = cls(get_settings().redis_url)
        return cls._instance

    # ── Event queue (Observer → Planner) ─────────────────────────────────────

    async def push_event(self, event: dict[str, Any]) -> None:
        """Queue a normalized event using the same deduplication as Gmail."""
        await self.enqueue_unseen_email(event)

    async def enqueue_unseen_email(self, event: dict[str, Any]) -> bool:
        """Queue an unclassified message once, including across Gmail sync replays."""
        event_id = event["event_id"]
        return bool(await self._redis.eval(
            """
            if redis.call('SISMEMBER', KEYS[1], ARGV[1]) == 1
                or redis.call('SISMEMBER', KEYS[2], ARGV[1]) == 1
                or redis.call('HEXISTS', KEYS[3], ARGV[1]) == 1 then return 0 end
            redis.call('SADD', KEYS[1], ARGV[1])
            redis.call('RPUSH', KEYS[4], ARGV[2])
            return 1
            """,
            4, ACTIVE, DONE, "emails:all", READY, event_id, json.dumps(event),
        ))

    async def reserve_event(self, *, lease_seconds: float = 180) -> EventJob | None:
        """Lease one job and recover abandoned or due retry jobs atomically."""
        now, token = time(), uuid4().hex
        raw = await self._redis.eval(
            _RESERVE_EVENT, 4, READY, IN_FLIGHT, LEASES, RETRIES, now, token, now + lease_seconds,
        )
        return EventJob(token, json.loads(raw)) if raw else None

    async def event_classified(self, event_id: str) -> bool:
        return bool(await self._redis.sismember(DONE, event_id))

    async def finish_event(self, job: EventJob, *, retry_in: float | None = None) -> bool:
        """Acknowledge a completed job or store a durable, delayed retry."""
        event = job.event
        retry = "" if retry_in is None else json.dumps({
            **event, "scan_attempt": event.get("scan_attempt", 0) + 1,
        })
        return bool(await self._redis.eval(
            _FINISH_EVENT, 5, IN_FLIGHT, LEASES, ACTIVE, DONE, RETRIES,
            job.token, event["event_id"], retry, time() + (retry_in or 0),
        ))

    async def scan_queue_depths(self) -> dict[str, int]:
        async with self._redis.pipeline(transaction=False) as pipe:
            pipe.llen(READY)
            pipe.hlen(IN_FLIGHT)
            pipe.zcard(RETRIES)
            events, processing, retrying = await pipe.execute()
        return {"events": events, "processing": processing, "retrying": retrying}

    async def get_gmail_sync(self) -> dict[str, Any]:
        raw = await self._redis.get(GMAIL_SYNC)
        return json.loads(raw) if raw else {}

    async def save_gmail_sync(self, state: dict[str, Any], *, previous: dict[str, Any]) -> bool:
        async with self._redis.pipeline() as pipe:
            try:
                await pipe.watch(GMAIL_SYNC)
                raw = await pipe.get(GMAIL_SYNC)
                if (json.loads(raw) if raw else {}) != previous:
                    return False
                pipe.multi()
                pipe.set(GMAIL_SYNC, json.dumps(state))
                await pipe.execute()
                return True
            except WatchError:
                return False

    # ── Email records — all emails regardless of confidence (Dashboard view) ──

    async def push_email_record(self, plan: dict[str, Any]) -> None:
        """HSET a plan into emails:all hash (field = plan id). Stores every email."""
        await self._redis.hset("emails:all", plan["id"], json.dumps(plan))

    async def get_email_record(self, plan_id: str) -> dict[str, Any] | None:
        raw = await self._redis.hget("emails:all", plan_id)
        return json.loads(raw) if raw else None

    async def store_deadline_plan(self, plan: dict[str, Any]) -> None:
        await self.store_deadline_plans([plan])

    async def store_deadline_plans(self, plans: list[dict[str, Any]]) -> None:
        """Persist the tasks from one email atomically without duplicating them."""
        if not plans:
            return
        async with self._redis.pipeline() as pipe:
            while True:
                try:
                    await pipe.watch("emails:all")
                    if await pipe.hexists("emails:all", plans[0]["id"]):
                        return
                    pipe.multi()
                    for plan in plans:
                        pipe.hset("emails:all", plan["id"], json.dumps(plan))
                    await pipe.execute()
                    return
                except WatchError:
                    continue

    async def get_all_emails(self) -> list[dict[str, Any]]:
        """HGETALL emails:all → list sorted newest-first by created_at."""
        raw = await self._redis.hgetall("emails:all")
        records = [json.loads(v) for v in raw.values()]
        records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return records

    async def get_deadlines(self) -> list[dict[str, Any]]:
        return [plan for plan in await self.get_all_emails() if is_deadline_plan(plan)]

    # ── Deduplication (Observer) ──────────────────────────────────────────────

    async def mark_event_seen(self, event_id: str) -> None:
        """
        Record event_id as seen with a 24-hour individual TTL.

        Uses per-event SETEX keys (seen_event:<id>) instead of a shared Set.
        The shared-Set approach called EXPIRE on the entire key every time any
        event was added, resetting the 24-hour clock for *all* existing events
        on each new one — events would never expire as long as activity continued.
        """
        await self._redis.setex(f"seen_event:{event_id}", 86400, "1")

    async def is_event_seen(self, event_id: str) -> bool:
        """Check if event_id has been seen within the last 24 hours."""
        return bool(await self._redis.exists(f"seen_event:{event_id}"))

    async def clear_event_seen(self, event_id: str) -> None:
        """Remove a single event from the dedup cache (used by demo seed scripts)."""
        await self._redis.delete(f"seen_event:{event_id}")

    # ── Activity log (all agents write, dashboard reads) ─────────────────────

    async def append_activity_log(self, entry: dict[str, Any]) -> None:
        """
        RPUSH an activity log entry, then LTRIM to keep only the last 500.
        """
        pipe = self._redis.pipeline()
        pipe.rpush("activity:log", json.dumps(entry))
        pipe.ltrim("activity:log", -500, -1)
        await pipe.execute()

    async def get_activity_log(self, limit: int = 50, *, workflow: str | None = None) -> list[dict[str, Any]]:
        """LRANGE activity:log, most recent N entries."""
        raw = await self._redis.lrange("activity:log", -500 if workflow else -limit, -1)
        entries = [json.loads(r) for r in reversed(raw)]
        if workflow:
            entries = [entry for entry in entries if entry.get("workflow") == workflow]
        return entries[:limit]

    # ── Session state (general K/V) ───────────────────────────────────────────

    async def set_session_state(
        self, key: str, value: dict[str, Any], ttl: int = 3600
    ) -> None:
        await self._redis.setex(f"session:{key}", ttl, json.dumps(value))

    async def get_session_state(self, key: str) -> dict[str, Any] | None:
        raw = await self._redis.get(f"session:{key}")
        return json.loads(raw) if raw else None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def close(self) -> None:
        await self._redis.aclose()

    async def ping(self) -> bool:
        """Health check — returns True if Redis is reachable."""
        try:
            return await self._redis.ping()
        except Exception:
            return False
