"""Pay-period math. 14-day Sun-Sat periods; payroll is the Monday two days
after the period ends (RULES §7). Anchor: PAYROLL_ANCHOR_MONDAY."""
from __future__ import annotations

from datetime import date, timedelta


def anchor_period_end(anchor_monday: str) -> date:
    """Saturday two days before the anchor payroll Monday."""
    return date.fromisoformat(anchor_monday) - timedelta(days=2)


def period_for(d: date, anchor_monday: str) -> tuple[date, date]:
    """(start_sunday, end_saturday) of the period containing d."""
    end0 = anchor_period_end(anchor_monday)
    delta = (d - end0).days
    n = (delta + 13) // 14   # smallest n with end0 + 14n >= d
    end = end0 + timedelta(days=14 * n)
    return end - timedelta(days=13), end


def latest_closed_period(today: date, anchor_monday: str) -> tuple[date, date]:
    """Most recent period whose Saturday end is strictly before today."""
    start, end = period_for(today, anchor_monday)
    if end >= today:
        end = end - timedelta(days=14)
        start = end - timedelta(days=13)
    return start, end


def payroll_monday(period_end: date) -> date:
    return period_end + timedelta(days=2)


def week_ranges(period_end: date | str) -> list[tuple[date, date]]:
    """The two Sun-Sat weeks of a 14-day period: [(w1_start, w1_end), (w2_start, w2_end)]."""
    end = period_end if isinstance(period_end, date) else date.fromisoformat(str(period_end)[:10])
    return [(end - timedelta(days=13), end - timedelta(days=7)),
            (end - timedelta(days=6), end)]


def week_of(event_date: str | None, period_end: date | str) -> int:
    """RULES §7 weekly split: which week of the period a ledger line belongs
    to, by the FieldRoutes date of the event it settles. 1 = the first
    Sun-Sat week (an event dated before the period counts here too: it was
    due from the period's first day), 2 = the second week (and any line
    whose event cannot be dated: it was booked at the close)."""
    if not event_date:
        return 2
    (_, w1_end), _ = week_ranges(period_end)
    return 1 if str(event_date)[:10] <= w1_end.isoformat() else 2


def local_from_utc(ts: str | None) -> str | None:
    """A SQLite datetime('now') UTC stamp as office-local time (OFFICE_TIMEZONE), in
    the same 'YYYY-MM-DD HH:MM:SS' shape FieldRoutes dates use."""
    if not ts:
        return None
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    try:
        d = datetime.fromisoformat(str(ts)[:19]).replace(tzinfo=timezone.utc)
    except ValueError:
        return str(ts)[:19]
    import os
    return d.astimezone(ZoneInfo(os.environ["OFFICE_TIMEZONE"])).strftime("%Y-%m-%d %H:%M:%S")
