"""Time helpers. Persisted timestamps are UTC ISO-8601 strings ending in 'Z'
so they sort lexically; schedules and month boundaries use the market tz."""
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc


def utcnow():
    return datetime.now(UTC)


def to_iso(dt):
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_iso(text):
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def local_today(tz_name, now=None):
    return (now or utcnow()).astimezone(ZoneInfo(tz_name)).date()


def month_start(day):
    return day.replace(day=1)


def local_midnight_utc(day, tz_name):
    """UTC instant of 00:00 local time on `day`."""
    return datetime(day.year, day.month, day.day, tzinfo=ZoneInfo(tz_name)).astimezone(UTC)


def price_window_start(today, lookback_days):
    """First trade_date kept/imported for prices: month start minus a
    baseline lookback (MTD change needs the last pre-month close)."""
    return month_start(today) - timedelta(days=lookback_days)


__all__ = [
    "UTC", "date", "utcnow", "to_iso", "from_iso", "local_today",
    "month_start", "local_midnight_utc", "price_window_start",
]
