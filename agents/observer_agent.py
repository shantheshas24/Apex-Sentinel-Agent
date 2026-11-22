"""
ObserverAgent — The Eyes of the system.
Synchronizes Gmail inbox changes via MCP.
Normalizes raw data into structured events and pushes new (deduplicated) events
to Redis events:queue for the PlannerAgent to process.
"""
import asyncio
import hashlib
from datetime import datetime, timezone
from typing import Any

from agents.base_agent import BaseAgent
from memory.redis_client import RedisClient

# Keywords that increase urgency scoring in the PlannerAgent
URGENCY_KEYWORDS = [
    "urgent", "asap", "deadline", "immediately", "critical",
    "overdue", "emergency", "important", "action required", "must",
]

# ── Manual poll trigger ────────────────────────────────────────────────────────
# The dashboard POST /api/poll/now calls trigger_immediate_poll() to wake the
# Observer early instead of waiting for the full poll interval.
_poll_trigger: asyncio.Event | None = None


def get_poll_trigger() -> asyncio.Event:
    """Return the module-level poll trigger, creating it lazily on first call."""
    global _poll_trigger
    if _poll_trigger is None:
        _poll_trigger = asyncio.Event()
    return _poll_trigger


async def trigger_immediate_poll() -> None:
    """Wake the Observer immediately — used by /api/poll/now."""
    get_poll_trigger().set()


class ObserverAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__("Observer")

    async def start(self) -> None:
        """Override BaseAgent.start() — Observer manages its own MCP connection per cycle."""
        while True:
            try:
                await self.run()
            except asyncio.CancelledError:
                await self.disconnect_mcp()
                raise
            except Exception as exc:
                self.logger.error("observer_crashed", error_type=type(exc).__name__)
                await asyncio.sleep(5)

    async def run(self) -> None:
        """
        Poll Gmail every OBSERVER_POLL_INTERVAL seconds.

        The MCP SSE post_writer is prone to dropping after the first few
        requests when the server runs in a daemon thread (httpx ReadError on
        an empty keep-alive body).  To work around this, we reconnect the MCP
        session before every poll cycle instead of reusing a long-lived session.
        """
        from config.settings import get_settings
        redis = RedisClient.get_instance()
        interval = get_settings().observer_poll_interval

        self.logger.info("observer_started", poll_interval=interval)

        while True:
            trigger = get_poll_trigger()
            trigger.clear()
            # Fresh MCP connection per cycle — avoids SSE post_writer drop
            try:
                await self.disconnect_mcp()
            except Exception:
                pass
            try:
                await self.connect_mcp()
            except Exception as exc:
                self.logger.error("observer_mcp_reconnect_failed", error_type=type(exc).__name__)
                await asyncio.sleep(5)
                continue

            try:
                new_count = await self._sync_inbox(redis)
                if new_count:
                    self.logger.info("gmail_messages_queued", count=new_count)

            except Exception as exc:
                self.logger.error("observer_poll_error", error_type=type(exc).__name__)

            # Wait for next cycle — but allow the dashboard to wake us early via
            # /api/poll/now (sets the module-level _poll_trigger Event).
            # Using asyncio.wait() instead of asyncio.wait_for() to avoid
            # cancellation edge-cases in Python 3.11 when the outer task
            # is cancelled during the timeout cleanup phase.
            try:
                sleep_task   = asyncio.create_task(asyncio.sleep(float(interval)))
                trigger_task = asyncio.create_task(trigger.wait())
                done, pending = await asyncio.wait(
                    {sleep_task, trigger_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
                if trigger_task in done:
                    self.logger.info("observer_manual_poll_triggered")
                    print("[Observer] Manual poll triggered — running cycle now")
            except asyncio.CancelledError:
                raise  # propagate to start() for graceful shutdown
            except Exception:
                pass  # never let the sleep phase crash the observer loop

    # ── Polling ───────────────────────────────────────────────────────────────

    async def _sync_inbox(self, redis: RedisClient) -> int:
        """Advance the cursor only after every message on the page is durably queued."""
        cursor = await redis.get_gmail_sync()
        count = 0
        for _ in range(5):
            page = await self.call_tool("sync_gmail", {"cursor": cursor})
            if not isinstance(page, dict) or not isinstance(page.get("messages"), list):
                raise RuntimeError("Invalid Gmail sync response")
            for email in page["messages"]:
                if await redis.enqueue_unseen_email(self._normalize_email(email)):
                    count += 1
            next_cursor = page["cursor"]
            if not await redis.save_gmail_sync(next_cursor, previous=cursor):
                break
            cursor = next_cursor
            if not cursor["page_token"]:
                break
        return count

    @staticmethod
    def _normalize_email(email: dict) -> dict[str, Any]:
        """Convert raw Gmail email dict to normalized agent event."""
        raw_text = f"{email.get('from', '')} {email.get('subject', '')} {email.get('snippet', '')}".lower()
        keywords = [kw for kw in URGENCY_KEYWORDS if kw in raw_text]
        event_id = hashlib.sha256(f"email:{email.get('id', '')}".encode()).hexdigest()[:16]
        return {
            "event_id": event_id,
            "type": "email",
            "source": "gmail",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": email,
            "urgency_keywords": keywords,
            "summary": f"Email from {email.get('from', 'unknown')}: {email.get('subject', '')}",
        }
