"""FastAPI wiring: the ``/services/data`` entry point plus the OAuth and SOAP endpoints."""

from __future__ import annotations

import json
from urllib.parse import parse_qs

from fastapi import FastAPI, Request, Response

from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext
from emulators.providers.salesforce import (
    bulk,
    composite,
    oauth,
    rest_meta,
    rest_query,
    rest_sobjects,
    soap,
)
from emulators.providers.salesforce.auth import authenticate, count_call, limit_info
from emulators.providers.salesforce.common import (
    SfError,
    bad_request,
    not_found,
    parse_version,
    versions_list,
)
from emulators.providers.salesforce.dml import Txn
from emulators.providers.salesforce.rest_core import JSON, Api, ApiRequest, ApiResponse


def api_for(ctx: ProviderContext) -> Api:
    """The per-context REST state (shared by the routes and the control-plane actions)."""
    api = getattr(ctx, "_sf_api", None)
    if api is None:
        api = Api(ctx)
        ctx._sf_api = api  # type: ignore[attr-defined]
    return api


def render(resp: ApiResponse) -> Response:
    headers = dict(resp.headers)
    if resp.status in (204, 304) or resp.body is None:
        return Response(status_code=resp.status, headers=headers)
    if isinstance(resp.body, bytes):
        return Response(resp.body, status_code=resp.status, headers=headers, media_type=resp.media_type)
    if isinstance(resp.body, str):
        return Response(resp.body, status_code=resp.status, headers=headers, media_type=resp.media_type)
    payload = json.dumps(resp.body, ensure_ascii=True, separators=(",", ":"), default=str).encode("utf-8")
    return Response(payload, status_code=resp.status, headers=headers, media_type=JSON)


def register(app: FastAPI, ctx: ProviderContext) -> None:
    api = api_for(ctx)
    org = api.org
    notifier = api.notifier
    for module in (rest_meta, rest_sobjects, rest_query, composite, bulk):
        module.register(api)
    oauth.register(app, ctx, org)
    soap.register(app, ctx, org)

    @app.api_route(
        "/services/apexrest/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def apexrest(request: Request, path: str) -> Response:
        authenticate(ctx, org, request)
        message = f"Could not find a match for URL /{path}"
        return render(ApiResponse(404, [{"errorCode": "NOT_FOUND", "message": message}]))

    @app.api_route("/services/data", methods=["GET", "HEAD"], include_in_schema=False)
    @app.api_route("/services/data/", methods=["GET", "HEAD"], include_in_schema=False)
    async def versions() -> Response:
        return render(ApiResponse(200, versions_list()))

    @app.api_route(
        "/services/data/{rest:path}",
        methods=["GET", "POST", "PATCH", "PUT", "DELETE", "HEAD"],
        include_in_schema=False,
    )
    async def data(request: Request, rest: str) -> Response:
        path = rest.strip("/")
        first, _, tail = path.partition("/")
        number = parse_version(first)
        if number is None:
            raise not_found()
        session = authenticate(ctx, org, request)
        session.version = f"{int(number)}.0"
        limit = rest_meta.api_limit(api)
        txn = Txn(org=org)
        try:
            count_call(ctx, org, limit)
            query = {k: v for k, v in parse_qs(request.url.query, keep_blank_values=True).items()}
            method = "GET" if request.method == "HEAD" else request.method
            if (
                method == "POST" and "_HttpMethod" in query
            ):  # clients that cannot send PATCH tunnel it via POST
                override = (query.pop("_HttpMethod")[0] or "").upper()
                if override not in ("PATCH", "PUT", "DELETE", "GET"):
                    raise bad_request("BAD_REQUEST", f"Invalid _HttpMethod: {override or '(empty)'}")
                method = override
            req = ApiRequest(
                method=method,
                path=tail.strip("/"),
                session=session,
                api=api,
                txn=txn,
                query=query,
                headers={k.lower(): v for k, v in request.headers.items()},
                body=await request.body(),
                request=request,
            )
            resp = await api.dispatch(req)
        except SfError as exc:
            resp = ApiResponse(exc.status, exc.body, dict(exc.headers))
        except EmulatorError as exc:
            resp = ApiResponse(exc.status, exc.body, dict(exc.headers))
        except Exception as exc:  # emulator bug: surface it in the Salesforce envelope instead of a bare 500
            txn.rollback()
            resp = ApiResponse(
                500,
                [
                    {
                        "message": f"An unexpected error occurred: {type(exc).__name__}: {exc}",
                        "errorCode": "UNKNOWN_EXCEPTION",
                    }
                ],
            )
        resp.headers["Sforce-Limit-Info"] = limit_info(org, limit)
        if resp.status < 400 and txn.events:
            await notifier.flush(txn.events)
        out = render(resp)
        if request.method == "HEAD":
            out.body = b""
        return out
