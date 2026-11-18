"""All scheduling decisions use German time, including while travelling."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BERLIN = ZoneInfo("Europe/Berlin")


def local_now() -> datetime:
    return datetime.now(BERLIN)


def due_slot(now: datetime) -> datetime | None:
    """Latest hourly slot in the active window, or None outside it.

    A check can catch up after waking, once per slot. Friday's final check
    has its own 23:30 slot. That entire minute is allowed for scheduler jitter.
    """
    if now.tzinfo is None:
        raise ValueError("Scheduling needs a timezone-aware timestamp.")
    now = now.astimezone(BERLIN)
    weekday = now.weekday()
    minute = now.hour * 60 + now.minute
    if weekday >= 5 or (weekday == 0 and minute < 9 * 60):
        return None
    if weekday == 4:
        if minute > 23 * 60 + 30:
            return None
        if minute == 23 * 60 + 30:
            return now.replace(second=0, microsecond=0)
    return now.replace(minute=0, second=0, microsecond=0)


def next_slot(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("Scheduling needs a timezone-aware timestamp.")
    now = now.astimezone(BERLIN)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for offset in range(8):
        day = midnight + timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        hours = range(9 if day.weekday() == 0 else 0, 24)
        candidates = [day.replace(hour=hour) for hour in hours]
        if day.weekday() == 4:
            candidates.append(day.replace(hour=23, minute=30))
        for candidate in candidates:
            if candidate > now:
                return candidate
    raise RuntimeError("Could not find the next scheduled check.")

