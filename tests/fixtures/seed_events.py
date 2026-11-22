"""Queue sample emails for the deadline tracker without connecting Gmail."""
import asyncio
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SAMPLES = [
    ("professor@example.com", "Final project deadline", "Please submit your final project by Friday at 5 PM."),
    ("campus@example.com", "Tuition reminder", "Your tuition payment is due on December 15."),
    ("news@example.com", "Weekly digest", "Here are the top articles this week."),
]


async def seed(limit: int | None = None) -> None:
    from agents.observer_agent import ObserverAgent
    from memory.redis_client import RedisClient

    redis = RedisClient.get_instance()
    samples = SAMPLES[:limit]
    try:
        for sender, subject, snippet in samples:
            event_id = f"inj-{uuid.uuid4().hex[:12]}"
            event = ObserverAgent._normalize_email({
                "id": event_id,
                "from": sender,
                "subject": subject,
                "snippet": snippet,
                "date": datetime.now(timezone.utc).isoformat(),
                "unread": True,
            })
            event.update(event_id=event_id, source="demo", synthetic=True)
            await redis.push_event(event)
    finally:
        await redis.close()
    print(f"Queued {len(samples)} sample emails for deadline detection.")


if __name__ == "__main__":
    asyncio.run(seed(limit=int(sys.argv[1]) if len(sys.argv) > 1 else None))
