"""Relative date literals (TODAY, LAST_N_DAYS:n, THIS_QUARTER ...) and SOQL date functions.

Literals resolve to a half-open range ``[start, end)`` of calendar dates in the calling
user's time zone (the week starts on Sunday, as for en_US users). Datetime fields convert
the local midnight boundaries to UTC.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from emulators.providers.salesforce.common import fmt_dt, parse_dt


def _week_start(d: date) -> date:
    return d - timedelta(days=(d.weekday() + 1) % 7)


def _add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    index = year * 12 + (month - 1) + delta
    return index // 12, index % 12 + 1


def _month_start(d: date, delta: int = 0) -> date:
    y, m = _add_months(d.year, d.month, delta)
    return date(y, m, 1)


def _quarter_start(d: date, delta: int = 0, first_month: int = 1) -> date:
    """Start of the (fiscal) quarter containing ``d`` shifted by ``delta`` quarters."""
    offset = (d.month - first_month) % 12
    q_first = d.month - offset % 3
    y, m = _add_months(d.year, q_first, 3 * delta)
    return date(y, m, 1)


def _year_start(d: date, delta: int = 0, first_month: int = 1) -> date:
    year = d.year if d.month >= first_month else d.year - 1
    return date(year + delta, first_month, 1)


def _months(start: date, n: int) -> date:
    y, m = _add_months(start.year, start.month, n)
    return date(y, m, 1)


def date_range(name: str, n: int | None, today: date, fiscal_start_month: int = 1) -> tuple[date, date]:
    """Half-open ``[start, end)`` date range for a SOQL date literal."""
    name = name.upper()
    fm = fiscal_start_month
    ws = _week_start(today)
    if name == "TODAY":
        return today, today + timedelta(days=1)
    if name == "YESTERDAY":
        return today - timedelta(days=1), today
    if name == "TOMORROW":
        return today + timedelta(days=1), today + timedelta(days=2)
    if name == "THIS_WEEK":
        return ws, ws + timedelta(days=7)
    if name == "LAST_WEEK":
        return ws - timedelta(days=7), ws
    if name == "NEXT_WEEK":
        return ws + timedelta(days=7), ws + timedelta(days=14)
    if name == "THIS_MONTH":
        return _month_start(today), _month_start(today, 1)
    if name == "LAST_MONTH":
        return _month_start(today, -1), _month_start(today)
    if name == "NEXT_MONTH":
        return _month_start(today, 1), _month_start(today, 2)
    if name == "LAST_90_DAYS":
        return today - timedelta(days=90), today + timedelta(days=1)
    if name == "NEXT_90_DAYS":
        return today + timedelta(days=1), today + timedelta(days=91)
    if name in ("THIS_QUARTER", "THIS_FISCAL_QUARTER"):
        first = fm if name.startswith("THIS_FISCAL") else 1
        s = _quarter_start(today, 0, first)
        return s, _months(s, 3)
    if name in ("LAST_QUARTER", "LAST_FISCAL_QUARTER"):
        first = fm if "FISCAL" in name else 1
        s = _quarter_start(today, -1, first)
        return s, _months(s, 3)
    if name in ("NEXT_QUARTER", "NEXT_FISCAL_QUARTER"):
        first = fm if "FISCAL" in name else 1
        s = _quarter_start(today, 1, first)
        return s, _months(s, 3)
    if name in ("THIS_YEAR", "THIS_FISCAL_YEAR"):
        first = fm if "FISCAL" in name else 1
        s = _year_start(today, 0, first)
        return s, _months(s, 12)
    if name in ("LAST_YEAR", "LAST_FISCAL_YEAR"):
        first = fm if "FISCAL" in name else 1
        s = _year_start(today, -1, first)
        return s, _months(s, 12)
    if name in ("NEXT_YEAR", "NEXT_FISCAL_YEAR"):
        first = fm if "FISCAL" in name else 1
        s = _year_start(today, 1, first)
        return s, _months(s, 12)
    k = n or 0
    if name == "LAST_N_DAYS":
        return today - timedelta(days=k), today + timedelta(days=1)
    if name == "NEXT_N_DAYS":
        return today + timedelta(days=1), today + timedelta(days=k + 1)
    if name == "N_DAYS_AGO":
        d = today - timedelta(days=k)
        return d, d + timedelta(days=1)
    if name == "LAST_N_WEEKS":
        return ws - timedelta(days=7 * k), ws
    if name == "NEXT_N_WEEKS":
        return ws + timedelta(days=7), ws + timedelta(days=7 + 7 * k)
    if name == "N_WEEKS_AGO":
        s = ws - timedelta(days=7 * k)
        return s, s + timedelta(days=7)
    if name == "LAST_N_MONTHS":
        return _month_start(today, -k), _month_start(today)
    if name == "NEXT_N_MONTHS":
        return _month_start(today, 1), _month_start(today, 1 + k)
    if name == "N_MONTHS_AGO":
        s = _month_start(today, -k)
        return s, _months(s, 1)
    fiscal = "FISCAL" in name
    first = fm if fiscal else 1
    if name in ("LAST_N_QUARTERS", "LAST_N_FISCAL_QUARTERS"):
        return _quarter_start(today, -k, first), _quarter_start(today, 0, first)
    if name in ("NEXT_N_QUARTERS", "NEXT_N_FISCAL_QUARTERS"):
        return _quarter_start(today, 1, first), _quarter_start(today, 1 + k, first)
    if name in ("N_QUARTERS_AGO", "N_FISCAL_QUARTERS_AGO"):
        s = _quarter_start(today, -k, first)
        return s, _months(s, 3)
    if name in ("LAST_N_YEARS", "LAST_N_FISCAL_YEARS"):
        return _year_start(today, -k, first), _year_start(today, 0, first)
    if name in ("NEXT_N_YEARS", "NEXT_N_FISCAL_YEARS"):
        return _year_start(today, 1, first), _year_start(today, 1 + k, first)
    if name in ("N_YEARS_AGO", "N_FISCAL_YEARS_AGO"):
        s = _year_start(today, -k, first)
        return s, _months(s, 12)
    raise ValueError(name)


def zone(name: str | None) -> tzinfo:
    try:
        return ZoneInfo(name) if name else UTC
    except (ZoneInfoNotFoundError, ValueError):
        return UTC


def local_today(now: datetime, tz_name: str | None) -> date:
    return now.astimezone(zone(tz_name)).date()


def datetime_bounds(start: date, end: date, tz_name: str | None) -> tuple[str, str]:
    """UTC wire strings for the local-midnight boundaries of a date range."""
    tz = zone(tz_name)

    def utc_of(d: date) -> str:
        return fmt_dt(datetime(d.year, d.month, d.day, tzinfo=tz).astimezone(UTC))

    return utc_of(start), utc_of(end)


# -- date functions --------------------------------------------------------------------


def _as_dt(value: str) -> datetime | None:
    return parse_dt(value) if value else None


def date_function(
    name: str, value: str | None, tz_name: str | None, fiscal_start_month: int = 1
) -> int | str | None:
    """Evaluate CALENDAR_YEAR(x), DAY_ONLY(x), HOUR_IN_DAY(x)... on a stored date/datetime string."""
    if value is None:
        return None
    dt = _as_dt(value)
    if dt is None:
        return None
    is_datetime = "T" in value
    local = dt.astimezone(zone(tz_name)) if is_datetime else dt
    d = local.date()
    if name == "CALENDAR_YEAR":
        return d.year
    if name == "CALENDAR_MONTH":
        return d.month
    if name == "CALENDAR_QUARTER":
        return (d.month - 1) // 3 + 1
    if name == "DAY_IN_MONTH":
        return d.day
    if name == "DAY_IN_WEEK":
        return (d.weekday() + 1) % 7 + 1
    if name == "DAY_IN_YEAR":
        return d.timetuple().tm_yday
    if name == "DAY_ONLY":
        return d.isoformat()
    if name == "HOUR_IN_DAY":
        return local.hour if is_datetime else 0
    if name == "WEEK_IN_MONTH":
        return (d.day - 1) // 7 + 1
    if name == "WEEK_IN_YEAR":
        return (d.timetuple().tm_yday - 1) // 7 + 1
    if name == "FISCAL_MONTH":
        return (d.month - fiscal_start_month) % 12 + 1
    if name == "FISCAL_QUARTER":
        return ((d.month - fiscal_start_month) % 12) // 3 + 1
    if name == "FISCAL_YEAR":
        return d.year if fiscal_start_month == 1 or d.month < fiscal_start_month else d.year + 1
    raise ValueError(name)
