"""Two-hour cloud schedule, bounded by the user's German-time window."""

from datetime import datetime, timedelta

from .schedule import BERLIN, due_slot


def day_slots(day: datetime) -> list[datetime]:
    weekday = day.weekday()
    if weekday >= 5:
        return []
    first = 9 if weekday == 0 else 1
    last = 21 if weekday == 4 else 23
    slots = [day.replace(hour=hour, minute=0, second=0, microsecond=0) for hour in range(first, last + 1, 2)]
    if weekday == 4:
        slots.append(day.replace(hour=23, minute=30, second=0, microsecond=0))
    return slots


def cloud_due_slot(now: datetime) -> datetime | None:
    if due_slot(now) is None:
        return None
    now = now.astimezone(BERLIN)
    available = [slot for slot in day_slots(now) if slot <= now]
    return available[-1] if available else None


def next_cloud_slot(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("A timezone-aware timestamp is required.")
    now = now.astimezone(BERLIN)
    for offset in range(8):
        for slot in day_slots(now + timedelta(days=offset)):
            if slot > now:
                return slot
    raise RuntimeError("No future cloud slot found.")

