"""Wire shapes: user, group, settings and meeting records plus the pieces they embed.

Records live in the store in (almost) the exact JSON shape Zoom returns; ``_private`` keys carry
bookkeeping and are stripped by :func:`emulators.providers.zoom.common.clean` on the way out.
"""

from __future__ import annotations

import base64
import calendar
import json
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from emulators.core.clock import isoformat_z
from emulators.core.ids import b64url
from emulators.providers.zoom.common import (
    CLUSTER,
    WEB_HOST,
    b64_22,
    digest,
    digits_str,
    number,
    zoom_user_id,
)
from emulators.world.model import Person, World

ROLE_NAMES = {"0": "Owner", "1": "Admin", "2": "Member"}
CLIENT_VERSIONS = (
    "5.17.11.20258(win)",
    "5.17.11.31870(mac)",
    "5.17.10.28521(win)",
    "5.17.5.29964(ios)",
    "5.17.7.28831(android)",
    "6.0.2.30157(mac)",
)
DIAL_IN = (
    {
        "country_name": "US",
        "city": "Washington DC",
        "number": "+1 202 555 0100",
        "type": "toll",
        "country": "US",
    },
    {"country_name": "US", "city": "New York", "number": "+1 212 555 0101", "type": "toll", "country": "US"},
    {"country_name": "US", "city": "Chicago", "number": "+1 312 555 0102", "type": "toll", "country": "US"},
)
ZOOM_TZ_LABELS = {
    "America/New_York": "Eastern Time (US and Canada)",
    "America/Toronto": "Eastern Time (US and Canada)",
    "America/Chicago": "Central Time (US and Canada)",
    "America/Denver": "Mountain Time (US and Canada)",
    "America/Los_Angeles": "Pacific Time (US and Canada)",
    "Europe/London": "London",
    "UTC": "UTC",
}


# -- helpers ------------------------------------------------------------------------------


def phone_display(e164: str | None) -> str:
    if not e164:
        return ""
    digits = e164.lstrip("+")
    if e164.startswith("+1") and len(digits) == 11:
        return f"+1 {digits[1:]}"
    if e164.startswith("+44"):
        return f"+44 {digits[2:]}"
    return e164


def phone_country(e164: str | None) -> str:
    if not e164:
        return ""
    if e164.startswith("+44"):
        return "GB"
    return "CA" if e164[2:5] in ("416", "647", "905", "613") else "US"


def numeric_password(world: World, *parts: object) -> str:
    return digits_str(world, "password", *parts, digits=6)


def encrypted_password(world: World, *parts: object) -> str:
    """The opaque ``pwd=`` token of a join URL: base64 of a base64 string, 32 chars."""
    inner = base64.b64encode(digest(world, "pwd", *parts)[:18])
    return base64.b64encode(inner).decode()


def join_url(meeting_id: int, pwd: str) -> str:
    return f"https://{WEB_HOST}/j/{meeting_id}?pwd={pwd}"


def start_url(world: World, meeting_id: int, host_id: str) -> str:
    return f"https://{WEB_HOST}/s/{meeting_id}?zak={zak_token(world, meeting_id, host_id)}"


def zak_token(world: World, meeting_id: int, host_id: str) -> str:
    header = {"zm_sks": b64url(digest(world, "zak", meeting_id)[:12]), "alg": "HS256"}
    payload = {
        "iss": "web",
        "clt": 0,
        "mnum": str(meeting_id),
        "aud": "client",
        "uid": host_id,
        "exp": 4102444800,
    }
    sig = b64url(digest(world, "zaks", meeting_id, host_id))
    enc = lambda v: b64url(json.dumps(v, separators=(",", ":")).encode())  # noqa: E731
    return f"{enc(header)}.{enc(payload)}.{sig}"


def share_token(world: World, kind: str, *parts: object, length: int = 64) -> str:
    raw = b64url(digest(world, kind, *parts) + digest(world, kind, "b", *parts))
    return raw[:length]


# -- users --------------------------------------------------------------------------------


def user_record(
    world: World, person: Person, *, role_id: str, user_type: int, group_ids: list[str]
) -> dict[str, Any]:
    zid = zoom_user_id(world, person.id)
    office = world.office(person.office_id) if person.office_id else None
    dept = world.department(person.department_id).name if person.department_id else ""
    seed_n = number(world, "login", person.id, digits=9)
    if person.active:
        last_login = world.anchor - timedelta(hours=1 + seed_n % (6 * 24))
    else:
        last_login = world.anchor - timedelta(days=30 + seed_n % 40)
    created = (
        datetime(person.hire_date.year, person.hire_date.month, person.hire_date.day, 9, 0, 0, tzinfo=UTC)
        if person.hire_date
        else datetime(2020, 1, 6, 9, tzinfo=UTC)
    )
    pmi = number(world, "pmi", person.id, digits=10)
    pwd = encrypted_password(world, "pmi", person.id)
    manager = world.person(person.manager_id).email if person.manager_id else ""
    return {
        "id": zid,
        "first_name": person.first_name,
        "last_name": person.last_name,
        "display_name": person.display_name,
        "email": person.email,
        "type": user_type,
        "pmi": pmi,
        "timezone": person.timezone,
        "verified": 1,
        "dept": dept,
        "created_at": isoformat_z(created),
        "last_login_time": isoformat_z(last_login),
        "last_client_version": CLIENT_VERSIONS[seed_n % len(CLIENT_VERSIONS)],
        "pic_url": f"https://{WEB_HOST}/p/v2/{b64_22(world, 'pic', person.id)}",
        "language": "en-US",
        "phone_number": phone_display(person.phone_business),
        "status": "active" if person.active else "inactive",
        "role_id": role_id,
        "employee_unique_id": person.employee_id or "",
        "user_created_at": isoformat_z(created),
        "group_ids": group_ids,
        # full-record fields
        "role_name": ROLE_NAMES[role_id],
        "use_pmi": False,
        "personal_meeting_url": join_url(pmi, pwd),
        "cms_user_id": b64_22(world, "cms", person.id),
        "jid": f"{digest(world, 'jid', person.id)[:16].hex()}@xmpp.zoom.us",
        "account_id": world.org.zoom_account_id,
        "account_number": number(world, "acct", digits=8),
        "cluster": CLUSTER,
        "host_key": digits_str(world, "hostkey", person.id, digits=6),
        "job_title": person.job_title,
        "company": world.org.name,
        "location": office.address.city if office else "",
        "login_types": [100],
        "phone_country": phone_country(person.phone_business),
        "cost_center": world.department(person.department_id).cost_center if person.department_id else "",
        "manager": manager,
        "vanity_url": "",
        "_person_id": person.id,
    }


LIST_USER_KEYS = [
    "id",
    "first_name",
    "last_name",
    "display_name",
    "email",
    "type",
    "pmi",
    "timezone",
    "verified",
    "dept",
    "created_at",
    "last_login_time",
    "last_client_version",
    "pic_url",
    "language",
    "phone_number",
    "status",
    "role_id",
    "employee_unique_id",
    "user_created_at",
    "group_ids",
]
FULL_USER_KEYS = [
    "id",
    "first_name",
    "last_name",
    "display_name",
    "email",
    "type",
    "role_name",
    "pmi",
    "use_pmi",
    "personal_meeting_url",
    "timezone",
    "verified",
    "dept",
    "created_at",
    "last_login_time",
    "last_client_version",
    "pic_url",
    "cms_user_id",
    "jid",
    "group_ids",
    "language",
    "phone_country",
    "phone_number",
    "status",
    "job_title",
    "cost_center",
    "company",
    "location",
    "login_types",
    "role_id",
    "account_id",
    "account_number",
    "cluster",
    "user_created_at",
    "employee_unique_id",
]


def user_list_item(rec: dict[str, Any], include_fields: set[str]) -> dict[str, Any]:
    item = {k: rec[k] for k in LIST_USER_KEYS if k in rec}
    if "host_key" in include_fields:
        item["host_key"] = rec["host_key"]
    if "custom_attributes" in include_fields:
        item["custom_attributes"] = []
    return item


def user_detail(rec: dict[str, Any], include_fields: set[str]) -> dict[str, Any]:
    item = {k: rec[k] for k in FULL_USER_KEYS if k in rec}
    if rec.get("manager"):
        item["manager"] = rec["manager"]
    item["host_key"] = rec["host_key"]
    if "custom_attributes" in include_fields:
        item["custom_attributes"] = []
    return item


def user_settings(rec: dict[str, Any]) -> dict[str, Any]:
    """A representative subset of ``GET /users/{id}/settings``."""
    return {
        "schedule_meeting": {
            "host_video": False,
            "participants_video": False,
            "audio_type": "voip",
            "join_before_host": False,
            "force_pmi_jbh_password": False,
            "pstn_password_protected": False,
            "use_pmi_for_scheduled_meetings": False,
            "use_pmi_for_instant_meetings": False,
            "require_password_for_scheduling_new_meetings": True,
            "require_password_for_scheduled_meetings": False,
            "require_password_for_instant_meetings": True,
            "require_password_for_pmi_meetings": "jbh_only",
            "embed_password_in_join_link": True,
            "meeting_template_id": "",
        },
        "in_meeting": {
            "e2e_encryption": False,
            "chat": True,
            "private_chat": True,
            "auto_saving_chat": False,
            "entry_exit_chime": "none",
            "record_play_voice": False,
            "feedback": False,
            "co_host": True,
            "polling": True,
            "attendee_on_hold": False,
            "show_meeting_control_toolbar": True,
            "allow_show_zoom_windows": True,
            "annotation": True,
            "whiteboard": True,
            "remote_control": True,
            "non_verbal_feedback": True,
            "breakout_room": True,
            "closed_caption": True,
            "far_end_camera_control": False,
            "group_hd": False,
            "virtual_background": True,
            "waiting_room": True,
            "screen_sharing": True,
            "who_can_share_screen": "all",
            "workplace_by_facebook": False,
        },
        "email_notification": {
            "cloud_recording_available_reminder": True,
            "jbh_reminder": True,
            "cancel_meeting_reminder": True,
            "alternative_host_reminder": True,
            "schedule_for_reminder": True,
        },
        "recording": {
            "local_recording": True,
            "cloud_recording": True,
            "record_speaker_view": True,
            "record_gallery_view": False,
            "record_audio_file": True,
            "save_chat_text": True,
            "show_timestamp": False,
            "recording_audio_transcript": True,
            "auto_recording": "none",
            "host_pause_stop_recording": False,
            "auto_delete_cmr": False,
            "auto_delete_cmr_days": 30,
            "ip_address_access_control": {"enable": False},
        },
        "telephony": {
            "third_party_audio": False,
            "audio_conference_info": "",
            "show_international_numbers_link": True,
        },
        "feature": {"meeting_capacity": 100, "large_meeting": False, "webinar": False, "zoom_phone": False},
        "tsp": {"call_out": False, "call_out_countries": [], "show_international_numbers_link": True},
    }


# -- groups -------------------------------------------------------------------------------


def group_wire(rec: dict[str, Any]) -> dict[str, Any]:
    return {"id": rec["id"], "name": rec["name"], "total_members": len(rec["_member_ids"])}


# -- meetings -----------------------------------------------------------------------------


def default_settings(**overrides: Any) -> dict[str, Any]:
    """The full ``settings`` object Zoom echoes on create/get, with caller overrides applied."""
    base: dict[str, Any] = {
        "host_video": False,
        "participant_video": False,
        "cn_meeting": False,
        "in_meeting": False,
        "join_before_host": False,
        "jbh_time": 0,
        "mute_upon_entry": False,
        "watermark": False,
        "use_pmi": False,
        "approval_type": 2,
        "audio": "voip",
        "auto_recording": "none",
        "enforce_login": False,
        "enforce_login_domains": "",
        "alternative_hosts": "",
        "alternative_host_update_polls": False,
        "close_registration": False,
        "show_share_button": False,
        "allow_multiple_devices": False,
        "registrants_confirmation_email": True,
        "waiting_room": False,
        "request_permission_to_unmute_participants": False,
        "registrants_email_notification": True,
        "meeting_authentication": False,
        "encryption_type": "enhanced_encryption",
        "approved_or_denied_countries_or_regions": {"enable": False},
        "breakout_room": {"enable": False},
        "internal_meeting": False,
        "continuous_meeting_chat": {"enable": False, "auto_add_invited_external_users": False},
        "participant_focused_meeting": False,
        "push_change_to_calendar": False,
        "resources": [],
        "alternative_hosts_email_notification": True,
        "show_join_info": False,
        "device_testing": False,
        "focus_mode": False,
        "enable_dedicated_group_chat": False,
        "private_meeting": False,
        "email_notification": True,
        "host_save_video_order": False,
        "sign_language_interpretation": {"enable": False},
        "email_in_attendee_report": False,
        "global_dial_in_countries": ["US"],
        "global_dial_in_numbers": [dict(n) for n in DIAL_IN],
    }
    base.update(overrides)
    return base


VALID_AUTO_RECORDING = ("local", "cloud", "none")
SETTINGS_BOOL_KEYS = [
    "host_video",
    "participant_video",
    "cn_meeting",
    "in_meeting",
    "join_before_host",
    "mute_upon_entry",
    "watermark",
    "use_pmi",
    "enforce_login",
    "waiting_room",
    "meeting_authentication",
    "registrants_confirmation_email",
    "private_meeting",
    "allow_multiple_devices",
    "show_share_button",
    "email_notification",
    "internal_meeting",
    "focus_mode",
]


def meeting_listing(rec: dict[str, Any], *, start_time: str | None = None) -> dict[str, Any]:
    """The compact entry of ``GET /users/{id}/meetings``."""
    item: dict[str, Any] = {
        "uuid": rec["uuid"],
        "id": rec["id"],
        "host_id": rec["host_id"],
        "topic": rec["topic"],
        "type": rec["type"],
    }
    start = start_time or rec.get("start_time")
    if start:
        item["start_time"] = start
    if rec.get("duration") is not None:
        item["duration"] = rec["duration"]
    item["timezone"] = rec["timezone"]
    item["agenda"] = rec.get("agenda", "")
    item["created_at"] = rec["created_at"]
    item["join_url"] = rec["join_url"]
    if rec.get("_pmi"):
        item["pmi"] = str(rec["_pmi"])
    return item


def occurrence_wire(occ: dict[str, Any]) -> dict[str, Any]:
    return {
        "occurrence_id": occ["occurrence_id"],
        "start_time": occ["start_time"],
        "duration": occ["duration"],
        "status": occ["status"],
    }


def visible_occurrences(rec: dict[str, Any], *, show_previous: bool) -> list[dict[str, Any]]:
    occs = rec.get("occurrences") or []
    return [occurrence_wire(o) for o in occs if show_previous or not o.get("_happened")]


def meeting_detail(
    rec: dict[str, Any], *, show_previous: bool = False, occurrence_id: str | None = None
) -> dict[str, Any]:
    """``GET /meetings/{id}``: everything, including start_url/passwords (host and admin view)."""
    item: dict[str, Any] = {
        "uuid": rec["uuid"],
        "id": rec["id"],
        "host_id": rec["host_id"],
        "host_email": rec["host_email"],
        "topic": rec["topic"],
        "type": rec["type"],
        "status": rec.get("status", "waiting"),
    }
    occ = None
    if occurrence_id:
        occ = next((o for o in rec.get("occurrences", []) if o["occurrence_id"] == occurrence_id), None)
    if rec.get("start_time"):
        item["start_time"] = occ["start_time"] if occ else rec["start_time"]
    if rec.get("duration") is not None:
        item["duration"] = occ["duration"] if occ else rec["duration"]
    item["timezone"] = rec["timezone"]
    item["agenda"] = occ.get("_agenda", rec.get("agenda", "")) if occ else rec.get("agenda", "")
    item["created_at"] = rec["created_at"]
    item["start_url"] = rec["start_url"]
    item["join_url"] = rec["join_url"]
    item["password"] = rec["password"]
    item["h323_password"] = rec["h323_password"]
    item["pstn_password"] = rec["pstn_password"]
    item["encrypted_password"] = rec["encrypted_password"]
    item["settings"] = occ.get("_settings", rec["settings"]) if occ else rec["settings"]
    item["pre_schedule"] = rec.get("pre_schedule", False)
    if rec.get("recurrence"):
        item["recurrence"] = rec["recurrence"]
    if rec["type"] in (3, 8):
        item["occurrences"] = (
            [occurrence_wire(occ)] if occ else visible_occurrences(rec, show_previous=show_previous)
        )
    if rec.get("tracking_fields"):
        item["tracking_fields"] = rec["tracking_fields"]
    return item


def meeting_payload(rec: dict[str, Any], *, string_id: bool = False) -> dict[str, Any]:
    """The ``object`` of meeting.* webhook events."""
    item: dict[str, Any] = {
        "uuid": rec["uuid"],
        "id": str(rec["id"]) if string_id else rec["id"],
        "host_id": rec["host_id"],
        "topic": rec["topic"],
        "type": rec["type"],
    }
    if rec.get("start_time"):
        item["start_time"] = rec["start_time"]
    if rec.get("duration") is not None:
        item["duration"] = rec["duration"]
    item["timezone"] = rec["timezone"]
    return item


# -- recurrence ---------------------------------------------------------------------------

MAX_OCCURRENCES = 60


def zoom_weekday(d: date) -> int:
    """Zoom numbers weekdays 1 (Sunday) to 7 (Saturday)."""
    return (d.weekday() + 1) % 7 + 1


def _nth_weekday(year: int, month: int, week: int, zdow: int) -> date | None:
    py_dow = (zdow - 2) % 7  # Monday=0
    days = [
        date(year, month, d)
        for d in range(1, calendar.monthrange(year, month)[1] + 1)
        if date(year, month, d).weekday() == py_dow
    ]
    if week == -1:
        return days[-1]
    return days[week - 1] if 0 < week <= len(days) else None


def expand_occurrences(
    start_utc: datetime, tz_name: str, recurrence: dict[str, Any], duration: int
) -> list[dict[str, Any]]:
    """Generate the occurrences of a type-8 meeting (at most 60, DST-aware)."""
    zone = _zone(tz_name)
    local = start_utc.astimezone(zone)
    kind = int(recurrence.get("type", 1))
    interval = max(1, int(recurrence.get("repeat_interval", 1) or 1))
    end_times = recurrence.get("end_times")
    end_dt = recurrence.get("end_date_time")
    limit_n = MAX_OCCURRENCES if end_times == 0 else min(int(end_times or 1), MAX_OCCURRENCES)
    if end_dt and end_times is None:
        limit_n = MAX_OCCURRENCES
    stop = datetime.fromisoformat(str(end_dt).replace("Z", "+00:00")).astimezone(UTC) if end_dt else None
    found: list[datetime] = []

    def accept(day: date) -> bool:
        moment = datetime.combine(day, local.timetz().replace(tzinfo=None), tzinfo=zone).astimezone(UTC)
        if moment < start_utc:
            return True
        if stop and moment > stop:
            return False
        found.append(moment)
        return len(found) < limit_n

    start_day = local.date()
    if kind == 1:
        day = start_day
        while accept(day):
            day += timedelta(days=interval)
    elif kind == 2:
        wanted = {
            int(x) for x in str(recurrence.get("weekly_days") or zoom_weekday(start_day)).split(",") if x
        }
        week0 = start_day - timedelta(days=(start_day.weekday() + 1) % 7)  # Sunday of the start week
        day = start_day
        for _ in range(2000):
            if ((day - week0).days // 7) % interval == 0 and zoom_weekday(day) in wanted and not accept(day):
                break
            day += timedelta(days=1)
    else:
        year, month = start_day.year, start_day.month
        for _ in range(240):
            if recurrence.get("monthly_week"):
                picked = _nth_weekday(
                    year, month, int(recurrence["monthly_week"]), int(recurrence.get("monthly_week_day", 1))
                )
            else:
                dom = int(recurrence.get("monthly_day") or start_day.day)
                picked = date(year, month, min(dom, calendar.monthrange(year, month)[1]))
            if picked is not None and not accept(picked):
                break
            month += interval
            year += (month - 1) // 12
            month = (month - 1) % 12 + 1
    return [
        {
            "occurrence_id": str(int(moment.timestamp() * 1000)),
            "start_time": isoformat_z(moment),
            "duration": duration,
            "status": "available",
        }
        for moment in found
    ]


# -- invitation ---------------------------------------------------------------------------


def invitation_text(rec: dict[str, Any], host_name: str) -> str:
    zone = _zone(rec["timezone"])
    meeting_id = str(rec["id"])
    spaced = " ".join(
        [meeting_id[:3], meeting_id[3:7], meeting_id[7:]]
        if len(meeting_id) == 11
        else [meeting_id[:3], meeting_id[3:6], meeting_id[6:]]
    )
    lines = [f"{host_name} is inviting you to a scheduled Zoom meeting.", "", f"Topic: {rec['topic']}"]
    if rec.get("start_time"):
        start = datetime.fromisoformat(rec["start_time"].replace("Z", "+00:00")).astimezone(zone)
        label = ZOOM_TZ_LABELS.get(rec["timezone"], rec["timezone"])
        lines.append(f"Time: {start.strftime('%b %d, %Y %I:%M %p')} {label}")
        recurrence = rec.get("recurrence")
        if recurrence:
            lines.append(_recurrence_text(recurrence, start))
    else:
        lines.append("Time: This is a recurring meeting Meet anytime")
    lines += [
        "",
        "Join Zoom Meeting",
        rec["join_url"],
        "",
        f"Meeting ID: {spaced}",
        f"Passcode: {rec['password']}",
        "---",
    ]
    lines += ["One tap mobile"]
    for n in DIAL_IN:
        digits = n["number"].replace(" ", "").replace("+", "")
        lines.append(f"+{digits},,{meeting_id}#,,,,*{rec['password']}# US ({n['city']})")
    lines += ["", "---", "Dial by your location"]
    lines += [f"• {n['number']} US ({n['city']})" for n in DIAL_IN]
    lines += [f"Meeting ID: {spaced}", f"Passcode: {rec['password']}"]
    return "\n".join(lines) + "\n"


def _recurrence_text(recurrence: dict[str, Any], start: datetime) -> str:
    interval = int(recurrence.get("repeat_interval", 1) or 1)
    kind = int(recurrence.get("type", 1))
    names = {1: "Sun", 2: "Mon", 3: "Tue", 4: "Wed", 5: "Thu", 6: "Fri", 7: "Sat"}
    if kind == 1:
        return "Every day" if interval == 1 else f"Every {interval} days"
    if kind == 2:
        days = ",".join(
            names[int(d)] for d in str(recurrence.get("weekly_days") or zoom_weekday(start.date())).split(",")
        )
        return f"Every week on {days}" if interval == 1 else f"Every {interval} weeks on {days}"
    return f"Every month on day {recurrence.get('monthly_day') or start.day}"


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")
