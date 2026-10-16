"""Past meetings, the report API and the dashboard (metrics) API.

A *past meeting* is one instance (UUID) of a meeting that has ended. UUIDs are addressed in the
path; those starting with ``/`` or containing ``//`` must be double URL-encoded by clients, and
:func:`~emulators.providers.zoom.common.decode_path_key` accepts both encodings.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import FastAPI, Request, Response

from emulators.core.provider import ProviderContext
from emulators.providers.zoom import lookup
from emulators.providers.zoom.common import (
    LiveCollection,
    Need,
    Principal,
    authenticate,
    decode_path_key,
    invalid_field,
    meeting_missing,
    no_permission,
    paginate,
    respond,
)
from emulators.providers.zoom.timeutil import day_range, in_days, parse_instant

PAST_MEETING = Need("meeting:read", "meeting:read:past_meeting")
PAST_INSTANCES = Need("meeting:read", "meeting:read:list_past_instances")
PAST_PARTICIPANTS = Need("meeting:read", "meeting:read:list_past_participants")
REPORT_MEETING = Need("report:read", "report:read:meeting", admin_only=True)
REPORT_PARTICIPANTS = Need("report:read", "report:read:list_meeting_participants", admin_only=True)
REPORT_USER_MEETINGS = Need("report:read", "report:read:user", admin_only=True)
REPORT_DAILY = Need("report:read", "report:read:list_daily_usage", admin_only=True)
REPORT_USERS = Need("report:read", "report:read:list_users", admin_only=True)
DASHBOARD = Need(
    "dashboard_meetings:read",
    "dashboard:read:list_meetings",
    admin_only=True,
    classic_aliases=("dashboard:read",),
)
DASHBOARD_DETAIL = Need(
    "dashboard_meetings:read", "dashboard:read:meeting", admin_only=True, classic_aliases=("dashboard:read",)
)
DASHBOARD_PARTICIPANTS = Need(
    "dashboard_meetings:read",
    "dashboard:read:list_meeting_participants",
    admin_only=True,
    classic_aliases=("dashboard:read",),
)

PAST_FIELDS = [
    "uuid",
    "id",
    "host_id",
    "type",
    "topic",
    "user_name",
    "user_email",
    "start_time",
    "end_time",
    "duration",
    "total_minutes",
    "participants_count",
    "dept",
    "source",
]
PAGE_KEYS = ("page_count", "page_size", "total_records", "next_page_token")


def instance_wire(inst: dict[str, Any]) -> dict[str, Any]:
    item = {"uuid": inst["id"], "id": inst["meeting_id"]}
    item.update({k: inst[k] for k in PAST_FIELDS if k not in item and k in inst})
    return item


def ended_instance(ctx: ProviderContext, key: str) -> dict[str, Any]:
    inst = lookup.find_instance(ctx, key, ended_only=True)
    if inst.get("state") != "ended":
        raise meeting_missing(decode_path_key(key))
    return inst


def ensure_host_or_admin(
    ctx: ProviderContext, principal: Principal, admin: bool, inst: dict[str, Any]
) -> None:
    mine = principal.person is not None and inst["host_id"] == lookup.my_user_id(ctx, principal)
    if not (admin or mine):
        raise no_permission()


def participant_rows(inst: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(inst["participants"], key=lambda r: (r["join_time"], r["id"]))


def report_participant(row: dict[str, Any], *, registrant: bool) -> dict[str, Any]:
    item = {
        "id": row["id"],
        "user_id": row["user_id"],
        "name": row["name"],
        "user_email": row["user_email"],
        "join_time": row["join_time"],
        "leave_time": row["leave_time"],
        "duration": row["duration"],
        "attentiveness_score": "",
        "failover": row["failover"],
        "status": row["status"],
        "customer_key": "",
        "participant_user_id": row["user_id"],
    }
    if registrant:
        item["registrant_id"] = row["registrant_id"]
    return item


def media_flags(inst: dict[str, Any]) -> dict[str, Any]:
    return {
        "has_pstn": False,
        "has_voip": True,
        "has_3rd_party_audio": False,
        "has_video": True,
        "has_screen_share": True,
        "has_recording": bool(inst.get("recorded")),
        "has_sip": False,
        "has_archiving": False,
    }


def register(app: FastAPI, ctx: ProviderContext) -> None:
    instances = LiveCollection(ctx, "instances")

    def today() -> date:
        return ctx.clock.now().date()

    def hosted_by(user_id: str) -> list[dict[str, Any]]:
        rows = [i for i in instances if i["host_id"] == user_id and i.get("state") == "ended"]
        return sorted(rows, key=lambda i: i["start_time"])

    def required_range(request: Request) -> tuple[date, date]:
        q = request.query_params
        for name in ("from", "to"):
            if not q.get(name):
                raise invalid_field(name, "Required field.")
        return day_range(q.get("from"), q.get("to"), default_from=today(), default_to=today())

    # -- past meetings ----------------------------------------------------------------------

    @app.get("/v2/past_meetings/{rest:path}")
    async def past_meetings(request: Request, rest: str) -> Response:
        principal = authenticate(ctx, request)
        if rest.endswith("/participants"):
            admin = principal.authorize(PAST_PARTICIPANTS)
            inst = ended_instance(ctx, rest[: -len("/participants")])
            ensure_host_or_admin(ctx, principal, admin, inst)
            page, meta = paginate(request, participant_rows(inst))
            return respond(request, ctx, {**{k: meta[k] for k in PAGE_KEYS}, "participants": page})
        if rest.endswith("/instances"):
            admin = principal.authorize(PAST_INSTANCES)
            key = decode_path_key(rest[: -len("/instances")])
            meeting = ctx.store.collection("meetings").get(key.replace(" ", "")) if key.isdigit() else None
            found = [
                i
                for i in instances
                if str(i["meeting_id"]) == key.replace(" ", "") and i.get("state") == "ended"
            ]
            if meeting is None and not found:
                raise meeting_missing(key)
            host_id = (meeting or found[0])["host_id"]
            if not admin and host_id != lookup.my_user_id(ctx, principal):
                raise no_permission()
            found.sort(key=lambda i: i["start_time"])
            body = {"meetings": [{"start_time": i["start_time"], "uuid": i["id"]} for i in found]}
            return respond(request, ctx, body)
        admin = principal.authorize(PAST_MEETING)
        inst = ended_instance(ctx, rest)
        ensure_host_or_admin(ctx, principal, admin, inst)
        return respond(request, ctx, instance_wire(inst))

    # -- report API -------------------------------------------------------------------------

    @app.get("/v2/report/meetings/{rest:path}")
    async def report_meetings(request: Request, rest: str) -> Response:
        principal = authenticate(ctx, request)
        if rest.endswith("/participants"):
            if not principal.authorize(REPORT_PARTICIPANTS):
                raise no_permission()
            inst = ended_instance(ctx, rest[: -len("/participants")])
            registrant = "registrant_id" in (request.query_params.get("include_fields") or "")
            rows = [report_participant(r, registrant=registrant) for r in participant_rows(inst)]
            page, meta = paginate(request, rows)
            return respond(request, ctx, {**{k: meta[k] for k in PAGE_KEYS}, "participants": page})
        if not principal.authorize(REPORT_MEETING):
            raise no_permission()
        inst = ended_instance(ctx, rest)
        host = ctx.store.collection("users").get(inst["host_id"]) or {}
        item = {
            **instance_wire(inst),
            "user_type": "Licensed" if host.get("type") == 2 else "Basic",
            **media_flags(inst),
            "custom_keys": [],
            "tracking_fields": [],
        }
        return respond(request, ctx, item)

    @app.get("/v2/report/users/{user_id}/meetings")
    async def report_user_meetings(request: Request, user_id: str) -> Response:
        principal = authenticate(ctx, request)
        if not principal.authorize(REPORT_USER_MEETINGS):
            raise no_permission()
        host = lookup.acting_user(ctx, principal, user_id, admin_level=True)
        lo, hi = required_range(request)
        rows = [i for i in hosted_by(host["id"]) if in_days(i["start_time"], lo, hi)]
        page, meta = paginate(request, rows)
        body = {
            "from": lo.isoformat(),
            "to": hi.isoformat(),
            **{k: meta[k] for k in PAGE_KEYS},
            "meetings": [instance_wire(i) for i in page],
        }
        return respond(request, ctx, body)

    @app.get("/v2/report/daily")
    async def report_daily(request: Request) -> Response:
        principal = authenticate(ctx, request)
        if not principal.authorize(REPORT_DAILY):
            raise no_permission()
        now = ctx.clock.now()
        try:
            year = int(request.query_params.get("year") or now.year)
            month = int(request.query_params.get("month") or now.month)
            first = date(year, month, 1)
        except ValueError:
            raise invalid_field("month") from None
        try:
            following = date(year + month // 12, month % 12 + 1, 1)
        except ValueError:
            raise invalid_field("year") from None
        dates = []
        for offset in range((following - first).days):
            day = first + timedelta(days=offset)
            held = [i for i in instances if i.get("state") == "ended" and in_days(i["start_time"], day, day)]
            dates.append(
                {
                    "date": day.isoformat(),
                    "new_users": sum(1 for u in lookup.users(ctx) if u["created_at"][:10] == day.isoformat()),
                    "meetings": len(held),
                    "participants": sum(i["participants_count"] for i in held),
                    "meeting_minutes": sum(i["total_minutes"] for i in held),
                }
            )
        return respond(request, ctx, {"year": year, "month": month, "dates": dates})

    @app.get("/v2/report/users")
    async def report_users(request: Request) -> Response:
        principal = authenticate(ctx, request)
        if not principal.authorize(REPORT_USERS):
            raise no_permission()
        lo, hi = required_range(request)
        kind = request.query_params.get("type", "active")
        if kind not in ("active", "inactive"):
            raise invalid_field("type")
        blank = {"meetings": 0, "participants": 0, "meeting_minutes": 0}
        stats: dict[str, dict[str, int]] = {}
        for inst in instances:
            if inst.get("state") == "ended" and in_days(inst["start_time"], lo, hi):
                s = stats.setdefault(inst["host_id"], dict(blank))
                s["meetings"] += 1
                s["participants"] += inst["participants_count"]
                s["meeting_minutes"] += inst["total_minutes"]
        rows = []
        for user in lookup.users(ctx):
            if (user["id"] in stats) != (kind == "active") or user["status"] != "active":
                continue
            rows.append(
                {
                    "id": user["id"],
                    "email": user["email"],
                    "user_name": user["display_name"],
                    "type": user["type"],
                    "dept": user["dept"],
                    **stats.get(user["id"], blank),
                    "last_client_version": user["last_client_version"],
                    "last_login_time": user["last_login_time"],
                    "create_time": user["created_at"],
                    "custom_attributes": [],
                }
            )
        page, meta = paginate(request, rows)
        body = {
            "from": lo.isoformat(),
            "to": hi.isoformat(),
            **{k: meta[k] for k in PAGE_KEYS},
            "users": page,
        }
        return respond(request, ctx, body)

    # -- dashboard / metrics ----------------------------------------------------------------

    def metrics_item(inst: dict[str, Any]) -> dict[str, Any]:
        host = ctx.store.collection("users").get(inst["host_id"]) or {}
        seconds = int(
            (
                (parse_instant(inst["end_time"]) if inst["end_time"] else ctx.clock.now())
                - parse_instant(inst["start_time"])
            ).total_seconds()
        )
        seconds = max(0, seconds)
        return {
            "uuid": inst["id"],
            "id": inst["meeting_id"],
            "topic": inst["topic"],
            "host": inst["user_name"],
            "email": inst["user_email"],
            "user_type": "Licensed" if host.get("type") == 2 else "Basic",
            "start_time": inst["start_time"],
            "end_time": inst["end_time"] or None,
            "duration": f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}",
            "participants": inst["participants_count"],
            **media_flags(inst),
            "in_room_participants": 0,
            "custom_keys": [],
            "dept": inst["dept"],
            "tracking_fields": [],
        }

    def metrics_type(request: Request) -> str:
        kind = request.query_params.get("type", "live")
        if kind not in ("past", "pastOne", "live"):
            raise invalid_field("type")
        return kind

    def metrics_instance(request: Request, key: str) -> dict[str, Any]:
        kind = metrics_type(request)
        inst = lookup.find_instance(ctx, key, ended_only=kind != "live")
        if ((kind == "live") != (inst.get("state") == "live")) or (
            kind == "pastOne" and inst["participants_count"] != 1
        ):
            raise meeting_missing(decode_path_key(key))
        return inst

    @app.get("/v2/metrics/meetings")
    async def metrics_meetings(request: Request) -> Response:
        principal = authenticate(ctx, request)
        if not principal.authorize(DASHBOARD):
            raise no_permission()
        q = request.query_params
        kind = metrics_type(request)
        from_raw = q.get("from") or (today() - timedelta(days=1)).isoformat()
        to_raw = q.get("to") or today().isoformat()
        try:
            lo, hi = parse_instant(from_raw), parse_instant(to_raw)
            if len(to_raw) == 10:
                hi += timedelta(days=1)
        except (ValueError, OverflowError):
            raise invalid_field("from") from None
        if hi < lo or (hi - lo).days > 31:
            raise invalid_field("to")
        date_type = q.get("query_date_type", "start_time")
        if date_type not in ("start_time", "end_time"):
            raise invalid_field("query_date_type")
        if kind == "live":
            rows = [i for i in instances if i.get("state") == "live"]
        else:
            rows = [
                i
                for i in instances
                if i.get("state") == "ended"
                and lo <= parse_instant(i[date_type]) <= hi
                and (kind != "pastOne" or i["participants_count"] == 1)
            ]
        rows.sort(key=lambda i: i["start_time"])
        page, meta = paginate(request, rows)
        body = {
            "from": from_raw,
            "to": to_raw,
            **{k: meta[k] for k in PAGE_KEYS},
            "meetings": [metrics_item(i) for i in page],
        }
        return respond(request, ctx, body)

    @app.get("/v2/metrics/meetings/{rest:path}")
    async def metrics_meeting(request: Request, rest: str) -> Response:
        principal = authenticate(ctx, request)
        if rest.endswith("/participants"):
            if not principal.authorize(DASHBOARD_PARTICIPANTS):
                raise no_permission()
            inst = metrics_instance(request, rest[: -len("/participants")])
            rows = [
                {
                    "id": r["id"],
                    "user_id": r["user_id"],
                    "user_name": r["name"],
                    "device": "Windows" if r.get("_device") == "desktop" else str(r.get("_device") or "Web"),
                    "ip_address": "198.51.100.23",
                    "location": "Toronto (CA)",
                    "network_type": "Wired",
                    "data_center": "SJC",
                    "connection_type": "UDP",
                    "join_time": r["join_time"],
                    "leave_time": r["leave_time"],
                    "share_application": False,
                    "share_desktop": False,
                    "share_whiteboard": False,
                    "recording": bool(inst.get("recorded")),
                    "status": "in_meeting" if r.get("_open") else "leave",
                    "email": r["user_email"],
                    "participant_user_id": r["user_id"],
                    "registrant_id": r["registrant_id"],
                    "customer_key": "",
                }
                for r in participant_rows(inst)
            ]
            page, meta = paginate(request, rows)
            return respond(request, ctx, {**{k: meta[k] for k in PAGE_KEYS}, "participants": page})
        if not principal.authorize(DASHBOARD_DETAIL):
            raise no_permission()
        return respond(request, ctx, metrics_item(metrics_instance(request, rest)))
