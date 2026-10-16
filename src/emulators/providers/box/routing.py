"""Route registration helpers shared by the ``routes_*`` modules."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.provider import ProviderContext
from emulators.providers.box import access
from emulators.providers.box.access import Principal
from emulators.providers.box.shapes import Shaper

API = "/2.0"
UPLOAD_API = "/api/2.0"  # upload.box.com


class Api:
    """Thin decorator facade over ``app.add_api_route`` that keeps handlers short."""

    def __init__(self, app: FastAPI, ctx: ProviderContext) -> None:
        self.app = app
        self.ctx = ctx

    def route(
        self, path: str, methods: list[str], *, prefix: str = API, status_code: int = 200
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.app.add_api_route(
                prefix + path, fn, methods=methods, status_code=status_code, response_model=None
            )
            return fn

        return decorator

    def get(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self.route(path, ["GET"], **kw)

    def post(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self.route(path, ["POST"], **kw)

    def put(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self.route(path, ["PUT"], **kw)

    def delete(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self.route(path, ["DELETE"], **kw)

    def options(self, path: str, **kw: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self.route(path, ["OPTIONS"], **kw)

    # -- request context -----------------------------------------------------------------
    def principal(self, request: Request, *, allow_anonymous: bool = False) -> Principal:
        return access.authenticate(request, self.ctx, allow_anonymous=allow_anonymous)

    def shaper(self, principal: Principal, request: Request) -> Shaper:
        return Shaper(principal, request)


def created(body: Any, headers: dict[str, str] | None = None) -> JSONResponse:
    return JSONResponse(body, status_code=201, headers=headers)


def no_content() -> Response:
    return Response(status_code=204)
