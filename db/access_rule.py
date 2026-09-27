"""The access rule, reproduced from the server's access_decision() SQL function (see the dashboard
repo's db/PI_ACCESS_CHECK.md). Pure functions: no database or clock access, so they can be tested
against the server's reference cases.

Rule order, first match wins: unknown_user, user_disabled, group_disabled, no_permission, lab_hours, level, outside_hours.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

DEFAULT_TZ = "America/Denver"
ALL_DAYS = frozenset(range(1, 8))


def parse_time(value):
    """'08:00' or '08:00:00' -> datetime.time, or None if unset/unparseable."""
    if not value:
        return None
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(str(value).strip(), fmt).time()
        except ValueError:
            continue
    return None


def parse_days(value):
    """'1,2,3' -> {1,2,3}. Blank/invalid -> all days (the server's default)."""
    try:
        days = {int(d) for d in str(value).split(",") if d.strip()}
    except ValueError:
        return ALL_DAYS
    return days or ALL_DAYS


def in_window(days, start, end, local):
    """Is naive local datetime `local` inside the window? `days` are ISO weekdays of the START day.

    start == end means all day; start > end runs past midnight and belongs to the start day.
    """
    dow = local.isoweekday()
    t = local.time()
    if start == end:
        return dow in days
    if start < end:
        return dow in days and start <= t < end
    prev_dow = (dow + 5) % 7 + 1
    return (dow in days and t >= start) or (prev_dow in days and t < end)


def local_now(tz_name, at=None):
    """`at` (aware datetime, default now) as naive wall-clock time in the lab timezone."""
    at = at or datetime.now(timezone.utc)
    try:
        zone = ZoneInfo(tz_name or DEFAULT_TZ)
    except Exception:
        zone = ZoneInfo(DEFAULT_TZ)
    return at.astimezone(zone).replace(tzinfo=None)


def decide(*, user_exists, user_disabled_message=None, in_disabled_group, has_permission, lab_open, lab_close, lab_days,
           level_windows, local):
    """Return (allowed, reason, via).

    user_disabled_message: None if the user is enabled, else the "line1\nline2" text shown on the LCD.

    level_windows: iterable of (level_name, days_set, start_time, end_time) for the user's ENABLED
    levels only. A level with no windows simply contributes nothing.
    """
    if not user_exists:
        return False, "unknown_user", None
    if user_disabled_message is not None:
        return False, "user_disabled", user_disabled_message
    if in_disabled_group:
        return False, "group_disabled", None
    if not has_permission:
        return False, "no_permission", None
    if lab_open is not None and lab_close is not None and in_window(lab_days, lab_open, lab_close, local):
        return True, "lab_hours", None
    for level_name, days, start, end in sorted(level_windows, key=lambda w: w[0]):
        if in_window(days, start, end, local):
            return True, "level", level_name
    return False, "outside_hours", None
