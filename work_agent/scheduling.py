"""Calendar calculations for a weekly time in Pacific."""

import re
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


PACIFIC = ZoneInfo("America/Los_Angeles")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_schedule_time(value: str) -> time:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{2}:[0-9]{2}", value):
        raise ValueError("Enter a Pacific time in HH:MM format.")
    try:
        return time.fromisoformat(value)
    except ValueError:
        raise ValueError("Enter a valid Pacific time.") from None


def _local_occurrence(day: date, local_time: time) -> int:
    # Resolve ambiguous local times to their first occurrence. A skipped hour
    # advances by the DST gap when converted to an instant.
    return int(datetime.combine(day, local_time, tzinfo=PACIFIC).timestamp())


def next_occurrence(weekday: int, local_time: time, after: datetime) -> int:
    """Return the first scheduled instant strictly after the supplied time."""
    local = after.astimezone(PACIFIC)
    day = local.date() + timedelta(days=(weekday - local.weekday()) % 7)
    candidate = _local_occurrence(day, local_time)
    if candidate <= after.timestamp():
        return _local_occurrence(day + timedelta(days=7), local_time)
    return candidate


def latest_occurrence(weekday: int, local_time: time, now: datetime) -> int:
    """Return the most recent scheduled instant at or before the supplied time."""
    local = now.astimezone(PACIFIC)
    day = local.date() - timedelta(days=(local.weekday() - weekday) % 7)
    candidate = _local_occurrence(day, local_time)
    if candidate > now.timestamp():
        return _local_occurrence(day - timedelta(days=7), local_time)
    return candidate


def format_pacific(epoch: int) -> str:
    local = datetime.fromtimestamp(epoch, PACIFIC)
    hour = local.hour % 12 or 12
    return f"{local:%A, %B} {local.day}, {local.year} at {hour}:{local:%M %p %Z}"


def scheduled_request(request: str, due_at: int, now: datetime, *, manual: bool = False) -> str:
    run_date = now.astimezone(PACIFIC).date().isoformat()
    occurrence = (
        "This weekly responsibility was run on demand."
        if manual else f"This is the occurrence scheduled for {format_pacific(due_at)}."
    )
    return (
        f"{request}\n\n"
        f"{occurrence} "
        f"Use {run_date} Pacific as the run date for relative dates such as the past week. "
        "Include that date in any saved brief."
    )
