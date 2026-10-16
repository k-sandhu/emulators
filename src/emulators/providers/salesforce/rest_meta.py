"""``/services/data``, resource map, ``/limits``, ``/recent`` and the org identity URL."""

from __future__ import annotations

from typing import Any

from emulators.providers.salesforce.common import DAILY_API_LIMIT, LOGIN_HOST, SfError, bad_request
from emulators.providers.salesforce.rest_core import Api, ApiRequest, ApiResponse, int_param, json_response
from emulators.providers.salesforce.rest_sobjects import recent_items
from emulators.providers.salesforce.schema import SCHEMA

RESOURCES = (
    "tooling",
    "metadata",
    "eclair",
    "folders",
    "prechatForms",
    "chatter",
    "tabs",
    "appMenu",
    "quickActions",
    "queryAll",
    "commerce",
    "wave",
    "iot",
    "analytics",
    "search",
    "identity",
    "composite",
    "parameterizedSearch",
    "theme",
    "nouns",
    "recent",
    "licensing",
    "limits",
    "async-queries",
    "emailConnect",
    "compactLayouts",
    "flexiPage",
    "knowledgeManagement",
    "sobjects",
    "actions",
    "support",
    "connect",
    "query",
    "process",
    "consent",
    "ui-api",
    "payments",
    "scheduling",
    "smartdatadiscovery",
    "jobs",
)


def api_limit(api: Api) -> int:
    value = api.ctx.setting("DAILY_API_LIMIT")
    override = api.org.meta.get("api_limit")
    if override is not None:
        return int(override)
    return int(value) if value and value.isdigit() else DAILY_API_LIMIT


def limits_body(api: Api) -> dict[str, dict[str, int]]:
    maximum = api_limit(api)
    used = int(api.org.meta.get("api_calls", 0))

    def fixed(maximum_: int, used_: int = 0) -> dict[str, int]:
        return {"Max": maximum_, "Remaining": maximum_ - used_}

    return {
        "ConcurrentAsyncGetReportInstances": fixed(200),
        "ConcurrentSyncReportRuns": fixed(20),
        "DailyApiRequests": {"Max": maximum, "Remaining": max(0, maximum - used)},
        "DailyAsyncApexExecutions": fixed(250000),
        "DailyBulkApiBatches": fixed(15000),
        "DailyBulkV2QueryFileStorageMB": fixed(976562),
        "DailyBulkV2QueryJobs": fixed(10000),
        "DailyDeliveredPlatformEvents": fixed(10000),
        "DailyDurableGenericStreamingApiEvents": fixed(200000),
        "DailyDurableStreamingApiEvents": fixed(200000),
        "DailyGenericStreamingApiEvents": fixed(10000),
        "DailyStandardVolumePlatformEvents": fixed(10000),
        "DailyStreamingApiEvents": fixed(200000),
        "DailyWorkflowEmails": fixed(390000),
        "DataStorageMB": fixed(1000, 14),
        "FileStorageMB": fixed(1024, 3),
        "HourlyAsyncReportRuns": fixed(1200),
        "HourlyDashboardRefreshes": fixed(200),
        "HourlyDashboardResults": fixed(5000),
        "HourlyDashboardStatuses": fixed(999999999),
        "HourlyLongTermIdMapping": fixed(100000),
        "HourlyODataCallout": fixed(10000),
        "HourlyPublishedPlatformEvents": fixed(50000),
        "HourlyPublishedStandardVolumePlatformEvents": fixed(1000),
        "HourlyShortTermIdMapping": fixed(100000),
        "HourlySyncReportRuns": fixed(500),
        "HourlyTimeBasedWorkflow": fixed(1000),
        "MassEmail": fixed(5000),
        "MonthlyPlatformEventsUsageEntitlement": fixed(0),
        "Package2VersionCreates": fixed(6),
        "PermissionSets": fixed(1500, 7),
        "SingleEmail": fixed(5000),
        "StreamingApiConcurrentClients": fixed(2000),
    }


def register(api: Api) -> None:
    r = api.router

    @r.route("GET", "")
    def resources(req: ApiRequest) -> ApiResponse:
        base = f"/services/data/v{req.version}"
        body: dict[str, Any] = {name: f"{base}/{name}" for name in RESOURCES}
        from emulators.core.provider import url_for_host

        user = req.session.user
        if req.request is not None:
            body["identity"] = url_for_host(
                req.request, api.ctx, LOGIN_HOST, f"/id/{api.org.org_id18}/{user['Id']}"
            )
        else:
            body["identity"] = f"https://{LOGIN_HOST}/id/{api.org.org_id18}/{user['Id']}"
        return json_response(body)

    @r.route("GET", "limits")
    def limits(req: ApiRequest) -> ApiResponse:
        return json_response(limits_body(api))

    @r.route("GET", "limits/recordCount")
    def record_count(req: ApiRequest) -> ApiResponse:
        wanted = [s.strip() for s in (req.q("sObjects") or "").split(",") if s.strip()]
        if not wanted:
            wanted = [o.name for o in SCHEMA.objects.values() if o.queryable and o.name != "Organization"]
        out = []
        for name in wanted:
            obj = SCHEMA.get(name)
            if obj is None:
                raise bad_request("INVALID_TYPE", f"sObject type '{name}' is not supported.")
            pred = req.access.readable(obj)
            n = sum(1 for rec in req.org.coll(obj.name) if not rec.get("IsDeleted") and pred(rec))
            out.append({"count": n, "name": obj.name})
        return json_response({"sObjects": out})

    @r.route("GET", "recent")
    def recent(req: ApiRequest) -> ApiResponse:
        limit = min(max(int_param(req.q("limit") or 200, "limit"), 1), 200)
        return json_response(recent_items(req, None, limit))

    @r.route("GET", "chatter/users/me", "chatter/users/{id}")
    def chatter_user(req: ApiRequest) -> ApiResponse:
        ident = req.params.get("id", "me")
        user = req.session.user if ident == "me" else req.org.user(ident)
        if user is None:
            raise SfError(404, "NOT_FOUND", "The requested resource does not exist")
        base = f"/services/data/v{req.version}/chatter/users/{user['Id']}"
        return json_response(
            {
                "companyName": user.get("CompanyName"),
                "displayName": user["Name"],
                "email": user["Email"],
                "firstName": user.get("FirstName"),
                "id": user["Id"],
                "isActive": bool(user.get("IsActive")),
                "lastName": user["LastName"],
                "mySubscription": None,
                "name": user["Name"],
                "photo": {
                    "largePhotoUrl": user.get("FullPhotoUrl"),
                    "photoVersionId": None,
                    "smallPhotoUrl": user.get("SmallPhotoUrl"),
                    "standardEmailPhotoUrl": None,
                    "url": user.get("FullPhotoUrl"),
                },
                "title": user.get("Title"),
                "type": "User",
                "url": base,
                "userType": "Internal",
                "username": user["Username"],
            }
        )

    @r.route("GET", "tooling/describe", "tooling/sobjects")
    def tooling(req: ApiRequest) -> ApiResponse:
        raise SfError(404, "NOT_FOUND", "The Tooling API is not available in this emulator")
