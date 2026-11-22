"""Add sample deadline records to Redis for an offline dashboard demo."""
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

SAMPLES = [
    ("demo-final-project", "Submit the final project", "Friday at 5 PM", "high", "professor@example.com"),
    ("demo-tuition", "Pay tuition", "December 15", "medium", "campus@example.com"),
    ("demo-survey", "Complete the student survey", "next Monday", "low", "survey@example.com"),
]


async def seed() -> None:
    from memory.redis_client import RedisClient

    redis = RedisClient.get_instance()
    try:
        for event_id, task, due, priority, sender in SAMPLES:
            evidence = f"{task} by {due}."
            await redis.store_deadline_plan({
                "id": event_id,
                "event_id": event_id,
                "event_type": "email",
                "action": "track_deadline",
                "synthetic": True,
                "deadline": task,
                "due": due,
                "evidence": evidence,
                "subject": task,
                "from_addr": sender,
                "snippet": evidence,
                "priority": priority,
                "confidence": 95,
                "reason": "The email specifies a due time.",
                "created_at": datetime.now(timezone.utc).isoformat(),
            })
    finally:
        await redis.close()
    print(f"Added {len(SAMPLES)} sample deadlines to the dashboard.")


if __name__ == "__main__":
    asyncio.run(seed())
