"""Expand runtime recurring series in their original local time zone, including DST."""

from __future__ import annotations

import calendar
import copy
from datetime import UTC, date, datetime, timedelta
from typing import Any

from emulators.core.clock import isoformat_z
from emulators.core.ids import stable_hash
from emulators.providers.microsoft import calendar_helpers as h
from emulators.providers.microsoft import common as c


def validate(recurrence: dict[str, Any]) -> None:
    try:
        pattern, span = recurrence["pattern"], recurrence["range"]
        if pattern["type"] not in {
            "daily",
            "weekly",
            "absoluteMonthly",
            "relativeMonthly",
            "absoluteYearly",
            "relativeYearly",
        }:
            raise ValueError
        if not isinstance(pattern.get("interval"), int) or pattern["interval"] < 1:
            raise ValueError
        start = date.fromisoformat(span["startDate"])
        if span["type"] == "endDate":
            if date.fromisoformat(span["endDate"]) < start:
                raise ValueError
        elif span["type"] == "numbered":
            if not isinstance(span.get("numberOfOccurrences"), int) or span["numberOfOccurrences"] < 1:
                raise ValueError
        elif span["type"] != "noEnd":
            raise ValueError
        if pattern["type"] in {"weekly", "relativeMonthly", "relativeYearly"} and (
            not pattern["daysOfWeek"] or not set(pattern["daysOfWeek"]).issubset(h.DAYS)
        ):
            raise ValueError
        if pattern["type"].startswith("relative") and pattern.get("index") not in {
            "first",
            "second",
            "third",
            "fourth",
            "last",
        }:
            raise ValueError
        if pattern["type"].startswith("absolute") and not 1 <= pattern["dayOfMonth"] <= 31:
            raise ValueError
        if pattern["type"].endswith("Yearly") and not 1 <= pattern["month"] <= 12:
            raise ValueError
    except (KeyError, ValueError, TypeError):
        raise c.graph_error(
            400, "ErrorInvalidRecurrence", "The recurrence pattern or range is invalid."
        ) from None


def instances(master: dict[str, Any], low: datetime, high: datetime) -> list[dict[str, Any]]:
    recurrence = master["recurrence"]
    validate(recurrence)
    pattern, span = recurrence["pattern"], recurrence["range"]
    timezone = span.get("recurrenceTimeZone") or master["originalStartTimeZone"]
    tz = h.zone(timezone)
    original = h.parse(master["start"]["dateTime"], master["start"]["timeZone"]).astimezone(tz)
    duration = h.parse(master["end"]["dateTime"], master["end"]["timeZone"]) - original.astimezone(UTC)
    beginning = date.fromisoformat(span["startDate"])
    finish = high.astimezone(tz).date()
    if span["type"] == "endDate":
        finish = min(finish, date.fromisoformat(span["endDate"]))
    interval, kind = pattern["interval"], pattern["type"]
    first_weekday = h.DAYS.index(pattern.get("firstDayOfWeek", "sunday"))
    first_week = beginning - timedelta(days=(beginning.weekday() - first_weekday) % 7)
    day, count, rows = beginning, 0, []
    while day <= finish:
        month_offset = (day.year - beginning.year) * 12 + day.month - beginning.month
        selected = False
        if kind == "daily":
            selected = (day - beginning).days % interval == 0
        elif kind == "weekly":
            selected = ((day - first_week).days // 7) % interval == 0 and h.DAYS[day.weekday()] in pattern[
                "daysOfWeek"
            ]
        else:
            period = (
                month_offset % interval == 0
                if kind.endswith("Monthly")
                else ((day.year - beginning.year) % interval == 0 and day.month == pattern["month"])
            )
            if period:
                if kind.startswith("absolute"):
                    selected = day.day == min(
                        pattern["dayOfMonth"], calendar.monthrange(day.year, day.month)[1]
                    )
                else:
                    matching = [
                        n
                        for n in range(1, calendar.monthrange(day.year, day.month)[1] + 1)
                        if h.DAYS[date(day.year, day.month, n).weekday()] in pattern["daysOfWeek"]
                    ]
                    index = {"first": 0, "second": 1, "third": 2, "fourth": 3, "last": -1}[pattern["index"]]
                    selected = day.day == matching[index]
        if selected:
            count += 1
            if span["type"] == "numbered" and count > span["numberOfOccurrences"]:
                break
            start = datetime.combine(day, original.timetz().replace(tzinfo=None), tzinfo=tz)
            end = start + duration
            if start < high and end > low:
                record = copy.deepcopy(master)
                record.update(
                    {
                        "id": "AAMkA" + stable_hash(master["id"], day.isoformat(), length=64),
                        "type": "occurrence",
                        "seriesMasterId": master["id"],
                        "recurrence": None,
                        "originalStart": isoformat_z(start),
                        "start": h.datetime_zone(isoformat_z(start)),
                        "end": h.datetime_zone(isoformat_z(end)),
                        "iCalUId": stable_hash(master["iCalUId"], day, length=64),
                    }
                )
                rows.append(record)
        day += timedelta(days=1)
    return rows
