from datetime import datetime, timezone

from datetime import timedelta

from news_app.time_window import (
    incremental_utc_window,
    is_inside_incremental_window,
    is_inside_rolling_window,
    is_inside_window,
    previous_day_window,
    rolling_utc_window,
)


def test_previous_day_window_for_ist():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    window = previous_day_window("Asia/Kolkata", now=now)

    assert window.collected_date.isoformat() == "2026-05-31"
    assert window.start_utc.isoformat() == "2026-05-30T18:30:00+00:00"
    assert window.end_utc.isoformat() == "2026-05-31T18:30:00+00:00"


def test_is_inside_window_is_end_exclusive():
    now = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    window = previous_day_window("Asia/Kolkata", now=now)

    assert is_inside_window(datetime(2026, 5, 30, 18, 30, tzinfo=timezone.utc), window)
    assert not is_inside_window(datetime(2026, 5, 31, 18, 30, tzinfo=timezone.utc), window)


def test_rolling_utc_window_uses_last_24_hours():
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    window = rolling_utc_window("Asia/Kolkata", now=now)

    assert window.collected_date.isoformat() == "2026-06-05"
    assert window.start_utc.isoformat() == "2026-06-04T12:00:00+00:00"
    assert window.end_utc.isoformat() == "2026-06-05T12:00:00+00:00"
    assert is_inside_rolling_window(datetime(2026, 6, 4, 12, 0, tzinfo=timezone.utc), window)
    assert is_inside_rolling_window(datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc), window)
    assert not is_inside_rolling_window(datetime(2026, 6, 4, 11, 59, 59, tzinfo=timezone.utc), window)


def test_incremental_window_without_cursor_uses_latest_24_hours():
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    window = incremental_utc_window("Asia/Kolkata", None, now=now)

    assert window.start_utc == now - timedelta(hours=24)
    assert window.end_utc == now
    assert window.start_exclusive is False
    assert window.previous_ingested_until is None
    assert is_inside_incremental_window(now - timedelta(hours=24), window)


def test_incremental_window_with_fresh_cursor_uses_exclusive_cursor_start():
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    cursor = now - timedelta(hours=3)
    window = incremental_utc_window("Asia/Kolkata", cursor, now=now)

    assert window.start_utc == cursor
    assert window.start_exclusive is True
    assert not is_inside_incremental_window(cursor, window)
    assert is_inside_incremental_window(cursor + timedelta(seconds=1), window)


def test_incremental_window_with_stale_or_exact_cutoff_cursor_falls_back_to_24_hours():
    now = datetime(2026, 6, 5, 12, 0, tzinfo=timezone.utc)
    stale = incremental_utc_window("Asia/Kolkata", now - timedelta(hours=25), now=now)
    exact = incremental_utc_window("Asia/Kolkata", now - timedelta(hours=24), now=now)

    assert stale.start_utc == now - timedelta(hours=24)
    assert stale.start_exclusive is False
    assert exact.start_utc == now - timedelta(hours=24)
    assert exact.start_exclusive is True
