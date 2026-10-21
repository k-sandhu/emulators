"""Workspace, people and team membership endpoints."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from emulators.core.provider import ProviderContext
from emulators.providers.asana import common as c


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get(c.BASE + "/users")
    async def users(request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "users:read")
        if workspace := request.query_params.get("workspace"):
            c.require(ctx, grant, "workspaces", workspace)
        team = (
            c.require(ctx, grant, "teams", request.query_params["team"])
            if "team" in request.query_params
            else None
        )
        rows = [u for u in ctx.store["users"] if team is None or u["gid"] in team["_members"]]
        return c.page(request, ctx, grant, sorted(rows, key=lambda u: u["gid"]))

    @app.get(c.BASE + "/users/{user_gid}")
    async def user(user_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "users:read")
        return {"data": c.render(request, ctx, grant, c.require(ctx, grant, "users", user_gid))}

    @app.get(c.BASE + "/workspaces")
    @app.get(c.BASE + "/users/{user_gid}/workspaces")
    async def workspaces(request: Request, user_gid: str = "me") -> dict[str, Any]:
        grant = c.principal(request, ctx, "workspaces:read")
        c.require(ctx, grant, "users", user_gid)
        return c.page(request, ctx, grant, ctx.store["workspaces"].values())

    @app.get(c.BASE + "/workspaces/{workspace_gid}")
    async def workspace(workspace_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "workspaces:read")
        return {"data": c.render(request, ctx, grant, c.require(ctx, grant, "workspaces", workspace_gid))}

    @app.get(c.BASE + "/workspaces/{workspace_gid}/users")
    async def workspace_users(workspace_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "users:read")
        c.require(ctx, grant, "workspaces", workspace_gid)
        return c.page(request, ctx, grant, sorted(ctx.store["users"].values(), key=lambda u: u["gid"]))

    @app.get(c.BASE + "/organizations/{workspace_gid}/teams")
    async def organization_teams(workspace_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "teams:read")
        c.require(ctx, grant, "workspaces", workspace_gid)
        return c.page(request, ctx, grant, [t for t in ctx.store["teams"] if c.allowed(ctx, grant, t)])

    @app.get(c.BASE + "/users/{user_gid}/teams")
    async def user_teams(user_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "teams:read")
        user = c.require(ctx, grant, "users", user_gid)
        c.require(ctx, grant, "workspaces", request.query_params.get("organization"))
        return c.page(
            request,
            ctx,
            grant,
            [t for t in ctx.store["teams"] if user["gid"] in t["_members"] and c.allowed(ctx, grant, t)],
        )

    @app.get(c.BASE + "/teams/{team_gid}")
    async def team(team_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "teams:read")
        return {"data": c.render(request, ctx, grant, c.require(ctx, grant, "teams", team_gid))}

    @app.get(c.BASE + "/teams/{team_gid}/users")
    async def team_users(team_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "users:read")
        row = c.require(ctx, grant, "teams", team_gid)
        return c.page(request, ctx, grant, [u for u in ctx.store["users"] if u["gid"] in row["_members"]])

    @app.post(c.BASE + "/teams")
    async def create_team(request: Request) -> Any:
        grant = c.principal(request, ctx, "teams:write")
        data = await c.payload(request)
        org = c.require(ctx, grant, "workspaces", data.get("organization"))
        name = c.string(data.get("name"), "name", empty=False)
        description = c.string(data.get("description", ""), "description")
        visibility = data.get("visibility", "secret")
        if visibility not in ("secret", "public", "request_to_join"):
            c.invalid("Invalid team visibility")
        uid = c.actor(ctx, grant)
        row = c.put(
            ctx,
            "teams",
            {
                "gid": c.new_gid(ctx),
                "resource_type": "team",
                "name": name,
                "description": description,
                "html_description": "<body></body>",
                "visibility": visibility,
                "organization": c.compact(org),
                "_members": [uid],
                "_owners": [uid],
            },
        )
        return JSONResponse({"data": c.render(request, ctx, grant, row)}, status_code=201)

    @app.put(c.BASE + "/teams/{team_gid}")
    async def update_team(team_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "teams:write")
        row = c.require(ctx, grant, "teams", team_gid)
        if c.actor(ctx, grant) not in row["_owners"]:
            raise c.error(403, "Only a team administrator can modify the team")
        data = await c.payload(request)
        updates = {}
        for key in ("name", "description", "html_description"):
            if key in data:
                updates[key] = c.string(data[key], key, empty=key != "name")
        if "visibility" in data:
            if data["visibility"] not in ("secret", "public", "request_to_join"):
                c.invalid("Invalid team visibility")
            updates["visibility"] = data["visibility"]
        row.update(updates)
        return {"data": c.render(request, ctx, grant, row)}

    @app.post(c.BASE + "/teams/{team_gid}/{operation}")
    async def team_members(team_gid: str, operation: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "teams:write")
        if operation not in ("addUser", "removeUser"):
            raise c.error(404, "Not Found")
        row = c.require(ctx, grant, "teams", team_gid)
        data = await c.payload(request)
        user = c.require(ctx, grant, "users", data.get("user"))
        uid = c.actor(ctx, grant)
        if uid not in row["_members"] or (
            operation == "removeUser" and uid not in row["_owners"] and user["gid"] != uid
        ):
            raise c.error(403, "Forbidden")
        if operation == "addUser":
            row["_members"] = list(dict.fromkeys([*row["_members"], user["gid"]]))
            return {"data": c.compact(user)}
        row["_members"] = [v for v in row["_members"] if v != user["gid"]]
        return {"data": {}}
