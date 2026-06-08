from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class DayWindow:
    collected_date: date
    start_utc: datetime
    end_utc: datetime
    timezone_name: str


@dataclass(frozen=True)
class RollingWindow:
    collected_date: date
    start_utc: datetime
    end_utc: datetime
    timezone_name: str


@dataclass(frozen=True)
class IncrementalRollingWindow:
    collected_date: date
    start_utc: datetime
    end_utc: datetime
    timezone_name: str
    previous_ingested_until: datetime | None
    start_exclusive: bool


def previous_day_window(timezone_name: str, now: datetime | None = None) -> DayWindow:
    tz = ZoneInfo(timezone_name)
    current = now or datetime.now(tz)
    local_now = current.astimezone(tz)
    target_date = local_now.date() - timedelta(days=1)
    return day_window_for_date(timezone_name, target_date)


def day_window_for_date(timezone_name: str, target_date: date) -> DayWindow:
    tz = ZoneInfo(timezone_name)
    start_local = datetime.combine(target_date, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return DayWindow(
        collected_date=target_date,
        start_utc=start_local.astimezone(timezone.utc),
        end_utc=end_local.astimezone(timezone.utc),
        timezone_name=timezone_name,
    )


def rolling_utc_window(timezone_name: str, hours: int = 24, now: datetime | None = None) -> RollingWindow:
    end_utc = now or datetime.now(timezone.utc)
    if end_utc.tzinfo is None:
        end_utc = end_utc.replace(tzinfo=timezone.utc)
    end_utc = end_utc.astimezone(timezone.utc)
    return RollingWindow(
        collected_date=end_utc.date(),
        start_utc=end_utc - timedelta(hours=max(1, hours)),
        end_utc=end_utc,
        timezone_name=timezone_name,
    )


def incremental_utc_window(
    timezone_name: str,
    previous_ingested_until: datetime | None,
    hours: int = 24,
    now: datetime | None = None,
) -> IncrementalRollingWindow:
    base = rolling_utc_window(timezone_name, hours=hours, now=now)
    previous = _ensure_utc(previous_ingested_until) if previous_ingested_until else None
    if previous is None or previous < base.start_utc:
        return IncrementalRollingWindow(
            collected_date=base.collected_date,
            start_utc=base.start_utc,
            end_utc=base.end_utc,
            timezone_name=base.timezone_name,
            previous_ingested_until=previous,
            start_exclusive=False,
        )
    return IncrementalRollingWindow(
        collected_date=base.collected_date,
        start_utc=previous,
        end_utc=base.end_utc,
        timezone_name=base.timezone_name,
        previous_ingested_until=previous,
        start_exclusive=True,
    )


def is_inside_window(value: datetime, window: DayWindow) -> bool:
    published_utc = value.astimezone(timezone.utc)
    return window.start_utc <= published_utc < window.end_utc


def is_inside_rolling_window(value: datetime, window: RollingWindow) -> bool:
    published_utc = value.astimezone(timezone.utc)
    return window.start_utc <= published_utc <= window.end_utc


def is_inside_incremental_window(value: datetime, window: IncrementalRollingWindow) -> bool:
    published_utc = _ensure_utc(value)
    lower_ok = published_utc > window.start_utc if window.start_exclusive else published_utc >= window.start_utc
    return lower_ok and published_utc <= window.end_utc


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
