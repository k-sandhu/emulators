"""Builds the FastAPI app for one provider: vendor routes + the ``/_emulator`` control plane.

Control plane (same on every provider, also reachable through the gateway at
``/_emulator/<provider>/...``):

=========================================  ==================================================
``GET  /_emulator/health``                 liveness
``GET  /_emulator/info``                   hosts, test credentials, actions, collection sizes
``POST /_emulator/reset``                  restore the seeded state (fast, snapshot based)
``GET  /_emulator/state``                  collection names and sizes
``GET  /_emulator/state/{collection}``     dump records (``?id=`` for one)
``PUT  /_emulator/state/{collection}``     upsert a record (test setup)
``DELETE /_emulator/state/{collection}/{id}``
``POST /_emulator/snapshots``              save state under a name; ``POST .../{name}/restore``
``GET|DELETE /_emulator/requests``         request journal (``?method=&path=&since=``)
``GET|POST|DELETE /_emulator/faults``      fault injection rules
``POST /_emulator/tokens``                 mint an access token for a user or the app
``GET  /_emulator/webhooks``               delivery journal; ``POST .../flush`` in manual mode
``POST /_emulator/actions/{name}``         provider-specific simulations
``GET  /_emulator/docs``                   OpenAPI UI of everything the emulator implements
=========================================  ==================================================
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Body, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from emulators.core.errors import EmulatorError
from emulators.core.middleware import EmulatorMiddleware, FaultRule
from emulators.core.provider import ProviderContext, remember_request


def _render_error(exc: EmulatorError) -> Response:
    if isinstance(exc.body, (bytes, str)):
        return Response(exc.body, status_code=exc.status, headers=exc.headers, media_type=exc.media_type)
    return JSONResponse(exc.body, status_code=exc.status, headers=exc.headers, media_type=exc.media_type)


def build_provider_app(ctx: ProviderContext) -> Any:
    provider = ctx.provider
    app = FastAPI(
        title=f"{provider.title} emulator",
        description=provider.description,
        docs_url="/_emulator/docs",
        openapi_url="/_emulator/openapi.json",
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
    )
    app.state.ctx = ctx

    @app.exception_handler(EmulatorError)
    async def _emulator_error(_: Request, exc: EmulatorError) -> Response:
        return _render_error(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
        errors = exc.errors()
        first = errors[0] if errors else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p not in ("body", "query"))
        message = f"Invalid request: {where} {first.get('msg', '')}".strip()
        return JSONResponse(
            provider.error_body_for(request.scope, 400, "BadRequest", message),
            status_code=400,
            headers=provider.error_headers_for(request.scope, 400),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
        scope = request.scope
        if exc.status_code == 404:
            body = provider.error_body_for(
                scope, 404, "NotFound", "Resource not found for the segment requested."
            )
        elif exc.status_code == 405:
            body = provider.error_body_for(
                scope, 405, "MethodNotAllowed", "The HTTP method is not allowed on this resource."
            )
        else:
            body = provider.error_body_for(scope, exc.status_code, "Error", str(exc.detail))
        headers = {
            **provider.error_headers_for(scope, exc.status_code),
            **(getattr(exc, "headers", None) or {}),
        }
        return JSONResponse(body, status_code=exc.status_code, headers=headers)

    @app.middleware("http")
    async def _remember(request: Request, call_next: Any) -> Response:
        remember_request(ctx, request)
        return await call_next(request)

    app.include_router(admin_router(ctx))
    provider.build(app, ctx)
    app.add_middleware(
        EmulatorMiddleware,
        provider=ctx.name,
        journal=ctx.journal,
        faults=ctx.faults,
        clock=ctx.clock,
        fault_response=provider.fault_response,
    )
    return app


def admin_router(ctx: ProviderContext) -> APIRouter:
    router = APIRouter(prefix="/_emulator", tags=["emulator control plane"])

    @router.get("/health")
    async def health() -> dict[str, Any]:
        return {"status": "ok", "provider": ctx.name, "time": ctx.clock.now().isoformat()}

    @router.get("/info")
    async def info() -> dict[str, Any]:
        return ctx.provider.info(ctx)

    @router.post("/reset")
    async def reset() -> dict[str, Any]:
        ctx.hub.reset(ctx.name)
        return {"status": "reset", "provider": ctx.name}

    @router.get("/state")
    async def state() -> dict[str, Any]:
        return {name: len(ctx.store.collection(name)) for name in ctx.store.names()}

    @router.get("/state/{collection}")
    async def dump(collection: str, id: str | None = None, limit: int = 1000) -> Any:
        coll = ctx.store.collection(collection)
        if id is not None:
            record = coll.get(id)
            if record is None:
                raise HTTPException(404)
            return record
        return {"collection": collection, "count": len(coll), "records": coll.values()[:limit]}

    @router.put("/state/{collection}")
    async def upsert(collection: str, record: dict[str, Any] = Body(...)) -> Any:
        if "id" not in record:
            raise EmulatorError(400, {"error": "record needs an 'id'"})
        return ctx.store.collection(collection).put(record)

    @router.delete("/state/{collection}/{record_id}")
    async def delete(collection: str, record_id: str) -> dict[str, Any]:
        return {"deleted": ctx.store.collection(collection).delete(record_id) is not None}

    @router.post("/snapshots")
    async def save_snapshot(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        name = payload.get("name") or f"snap-{len(ctx.hub.snapshots.get(ctx.name, {})) + 1}"
        ctx.hub.snapshots.setdefault(ctx.name, {})[name] = ctx.store.snapshot()
        return {"name": name}

    @router.post("/snapshots/{name}/restore")
    async def restore_snapshot(name: str) -> dict[str, Any]:
        snap = ctx.hub.snapshots.get(ctx.name, {}).get(name)
        if snap is None:
            raise HTTPException(404)
        ctx.store.restore(snap)
        return {"restored": name}

    @router.get("/requests")
    async def requests(
        method: str | None = None, path: str | None = None, since: int = 0, limit: int = 500
    ) -> dict[str, Any]:
        entries = ctx.journal.entries(method=method, path=path, since=since, limit=limit)
        return {"count": len(entries), "requests": entries}

    @router.delete("/requests")
    async def clear_requests() -> dict[str, Any]:
        ctx.journal.clear()
        return {"cleared": True}

    @router.get("/faults")
    async def faults() -> dict[str, Any]:
        return {"faults": ctx.faults.list()}

    @router.post("/faults")
    async def add_fault(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
        allowed = set(inspect.signature(FaultRule).parameters) - {"id", "hits"}
        rule = ctx.faults.add(FaultRule(**{k: v for k, v in payload.items() if k in allowed}))
        return {"id": rule.id}

    @router.delete("/faults")
    async def clear_faults() -> dict[str, Any]:
        ctx.faults.clear()
        return {"cleared": True}

    @router.delete("/faults/{fault_id}")
    async def remove_fault(fault_id: str) -> dict[str, Any]:
        return {"removed": ctx.faults.remove(fault_id)}

    @router.post("/tokens")
    async def mint_token(payload: dict[str, Any] = Body(default={})) -> dict[str, Any]:
        """Mint an access token without an OAuth dance.

        ``{"user": "<id|email>", "scopes": [...], "client_id": "...", "app_only": false,
        "expires_in": 3600}`` — providers may override via a ``mint_token`` action.
        """
        handler = ctx.provider.actions.get("mint_token")
        if handler is not None:
            result = handler(ctx, payload)
            return await result if inspect.isawaitable(result) else result
        user = payload.get("user")
        grant = ctx.tokens.issue_access(
            client_id=payload.get("client_id", "emulator-client"),
            user_id=user,
            scopes=payload.get("scopes", []),
            ttl=timedelta(seconds=int(payload.get("expires_in", 3600))),
            app_only=bool(payload.get("app_only", user is None)),
        )
        return {
            "access_token": grant.token,
            "token_type": "Bearer",
            "expires_in": int(payload.get("expires_in", 3600)),
        }

    @router.get("/webhooks")
    async def webhooks(event_type: str | None = None, status: str | None = None) -> dict[str, Any]:
        records = [
            r
            for r in ctx.webhooks.deliveries
            if (event_type is None or r["event_type"] == event_type)
            and (status is None or r["status"] == status)
        ]
        return {"count": len(records), "deliveries": records}

    @router.post("/webhooks/flush")
    async def flush() -> dict[str, Any]:
        flushed = await ctx.webhooks.flush()
        await ctx.webhooks.drain()
        return {"flushed": len(flushed)}

    @router.delete("/webhooks")
    async def clear_webhooks() -> dict[str, Any]:
        ctx.webhooks.deliveries.clear()
        return {"cleared": True}

    @router.get("/actions")
    async def list_actions() -> dict[str, Any]:
        return {
            name: (inspect.getdoc(fn) or "").split("\n\n")[0]
            for name, fn in sorted(ctx.provider.actions.items())
        }

    @router.post("/actions/{name}")
    async def run_action(name: str, payload: dict[str, Any] = Body(default={})) -> Any:
        handler = ctx.provider.actions.get(name)
        if handler is None:
            raise EmulatorError(
                404, {"error": f"unknown action '{name}'", "actions": sorted(ctx.provider.actions)}
            )
        result = handler(ctx, payload)
        if inspect.isawaitable(result):
            result = await result
        return result

    return router
