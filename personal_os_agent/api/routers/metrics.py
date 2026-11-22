"""
Metrics endpoint — aggregate stats over all processed plans.
  GET /api/metrics  → task status, priority, confidence and scan queue size
"""
from datetime import datetime, timezone

from fastapi import APIRouter

from personal_os_agent.memory.deadline_tasks import DeadlineTasks

router = APIRouter()


@router.get("/api/metrics")
async def get_metrics() -> dict:
    """Aggregate stats over all plans in emails:all."""
    from personal_os_agent.memory.redis_client import RedisClient
    redis = RedisClient.get_instance()
    emails = await DeadlineTasks(redis).get_tasks(datetime.now(timezone.utc))

    total = len(emails)
    by_priority: dict[str, int] = {"critical": 0, "high": 0, "medium": 0, "low": 0}
    by_status = dict.fromkeys(("needs_review", "pending", "upcoming", "overdue", "completed"), 0)
    conf_dist: dict[str, int] = {"0-30": 0, "31-60": 0, "61-80": 0, "81-100": 0}
    conf_sum = 0

    for e in emails:
        pr = e.get("priority", "low")
        if pr in by_priority:
            by_priority[pr] += 1
        by_status[e["task_status"]] += 1
        c = int(e.get("confidence") or 0)
        conf_sum += c
        if c <= 30:
            conf_dist["0-30"] += 1
        elif c <= 60:
            conf_dist["31-60"] += 1
        elif c <= 80:
            conf_dist["61-80"] += 1
        else:
            conf_dist["81-100"] += 1

    avg_conf = round(conf_sum / total, 1) if total else 0.0

    return {
        "total": total,
        "by_priority": by_priority,
        "by_status": by_status,
        "avg_confidence": avg_conf,
        "confidence_distribution": conf_dist,
        "queue_depths": await redis.scan_queue_depths(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
