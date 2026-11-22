"""Deadline validation, task status, and reminder policy."""
import re
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

IMPORTANCE_LEVELS = ("low", "medium", "high")
PRIORITIES = (*IMPORTANCE_LEVELS, "critical")
REMINDER_LEADS = {
    "low": (("near", timedelta(hours=1)),),
    "medium": (("early", timedelta(days=1)), ("near", timedelta(hours=2))),
    "high": (("early", timedelta(days=1)), ("near", timedelta(hours=3))),
}

_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)"
)
_DAY = r"\d{1,2}(?:st|nd|rd|th)?"
_DATE = (
    rf"(?:\d{{4}}-\d{{2}}-\d{{2}}(?:[T ]\d{{2}}:\d{{2}}(?::\d{{2}})?(?:Z|[+-]\d{{2}}:\d{{2}})?)?"
    rf"|{_DAY}\s+{_MONTH}(?:\s+\d{{4}})?"
    rf"|{_MONTH}\s+{_DAY}(?:,?\s+\d{{4}})?"
    r"|today|tomorrow|(?:next\s+)?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday))"
)
_TIME = r"(?:\d{1,2}(?::[0-5]\d)?\s*[ap]\.?m\.?|\d{1,2}:[0-5]\d|noon|midnight)"
_EXPLICIT_DEADLINE = re.compile(
    r"\b(?:deadline(?:\s+is\b|\s*:)|(?:is|are)\s+due(?:\s+(?:by|on))?"
    r"|(?:must|please)\s+[^.!?]{0,140}?\b(?:submit(?:ted)?|complet(?:e|ed)|"
    r"respond|repl(?:y|ied)|pay|paid|register(?:ed)?|deliver(?:ed)?)\b[^.!?]{0,60}?\s+by)"
    rf"\s*(?P<due>{_DATE}\b(?:,?\s+(?:at\s+)?{_TIME})?)(?!\w)",
    re.IGNORECASE,
)


def explicit_deadline_candidate(subject: str, message: str) -> dict[str, Any] | None:
    """Find a directly stated cutoff if the model cannot process the message."""
    for source in (message, subject):
        match = _EXPLICIT_DEADLINE.search(" ".join(source.split()))
        if match:
            return {
                "deadline": " ".join(subject.split()) or "Review the deadline in this email",
                "due": match.group("due").rstrip("."),
                "evidence": match.group().rstrip("."),
                "priority": "medium",
                "confidence": 50,
                "reason": "The email directly states a deadline. Review its due time.",
            }
    return None


def is_deadline_plan(plan: dict[str, Any] | None) -> bool:
    """Recognize current deadlines and stored deadlines from the old reply workflow."""
    return bool(
        plan
        and plan.get("event_type") == "email"
        and plan.get("deadline")
        and plan.get("evidence")
        and plan.get("action") in ("track_deadline", "send_email")
    )


def deadline_record(plan: dict[str, Any]) -> dict[str, Any]:
    """Expose the deadline fields and omit legacy reply data."""
    return {
        **{key: value for key, value in plan.items() if key not in (
            "action_args", "reply_approved", "requires_approval", "user_response",
        )},
        "action": "track_deadline",
    }


def is_sample_deadline(plan: dict[str, Any]) -> bool:
    event_id = plan.get("event_id") or plan["id"]
    return bool(plan.get("synthetic") or event_id.startswith(("inj-", "demo-")))


def explicit_due_time(text: str) -> datetime | None:
    """Only resolve a literal date and time with an explicit UTC offset."""
    matches = re.findall(
        r"(?<!\d)\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)(?!\w)",
        text,
    )
    if len(matches) != 1:
        return None
    try:
        return datetime.fromisoformat(matches[0]).astimezone(timezone.utc)
    except ValueError:
        return None


def task_state(plan: dict[str, Any], saved: dict[str, Any] | None = None) -> dict[str, Any]:
    due_text = plan.get("due") or ""
    due = explicit_due_time(due_text) if due_text in plan["evidence"] else None
    importance = plan.get("priority", "medium")
    return {
        "title": plan["deadline"],
        "importance": importance if importance in IMPORTANCE_LEVELS else "medium",
        "due_at": due.isoformat() if due else None,
        "completed_at": None,
        "snoozed_until": None,
        "phone_attempted_at": None,
        "phone_status": None,
        "phone_sid": None,
        "delivered": {},
        **(saved or {}),
    }


def task_priority(importance: str, due: datetime | None, now: datetime) -> str:
    if due is None or due <= now:
        return importance
    remaining = due - now
    if importance == "high" and remaining <= timedelta(hours=4):
        return "critical"
    if importance == "medium" and remaining <= timedelta(hours=6):
        return "high"
    return importance


def deadline_view(plan: dict[str, Any], state: dict[str, Any], now: datetime) -> dict[str, Any]:
    due = datetime.fromisoformat(state["due_at"]) if state["due_at"] else None
    if state["completed_at"]:
        status = "completed"
    elif due is None:
        status = "needs_review"
    elif due <= now:
        status = "overdue"
    elif due - now <= timedelta(days=7):
        status = "upcoming"
    else:
        status = "pending"
    return {
        **deadline_record(plan),
        "deadline": state["title"],
        "due_at": state["due_at"],
        "importance": state["importance"],
        "priority": task_priority(state["importance"], due, now),
        "task_status": status,
        "completed_at": state["completed_at"],
        "snoozed_until": state["snoozed_until"],
        "synthetic": is_sample_deadline(plan),
        "phone_status": state["phone_status"] or ("unconfirmed" if state["phone_attempted_at"] else None),
    }


def in_quiet_hours(now: datetime, zone: str, start: time, end: time) -> bool:
    local = now.astimezone(ZoneInfo(zone)).time()
    if start <= end:
        return start <= local < end
    return local >= start or local < end


def reminder_slots(state: dict[str, Any], now: datetime, phone_enabled: bool) -> tuple[list[str], bool]:
    """Return eligible dashboard slots and whether phone escalation is due."""
    if state["completed_at"] or not state["due_at"]:
        return [], False
    due = datetime.fromisoformat(state["due_at"])
    snoozed = state["snoozed_until"]
    if now >= due or (snoozed and now < datetime.fromisoformat(snoozed)):
        return [], False

    priority = task_priority(state["importance"], due, now)
    delivered = state["delivered"]
    slots = [
        slot for slot, lead in REMINDER_LEADS[state["importance"]]
        if now >= due - lead and slot not in delivered
    ]
    if priority == "critical" and "critical" not in delivered:
        slots.append("critical")
    if snoozed and f"snooze:{snoozed}" not in delivered:
        slots.append(f"snooze:{snoozed}")

    phone_due = (
        phone_enabled and priority == "critical" and state["phone_attempted_at"] is None
    )
    return slots, phone_due
