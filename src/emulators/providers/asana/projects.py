"""Projects, section ordering and explicit project memberships."""

from __future__ import annotations

import copy
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from emulators.core.auth import Grant
from emulators.core.provider import ProviderContext
from emulators.providers.asana import common as c
from emulators.providers.asana.events import emit


def validate(ctx: ProviderContext, grant: Grant, row: dict[str, Any], data: dict[str, Any]) -> None:
    permitted = {
        "name",
        "notes",
        "html_notes",
        "archived",
        "public",
        "privacy_setting",
        "color",
        "owner",
        "due_on",
        "due_date",
        "start_on",
        "default_view",
        "team",
        "workspace",
    }
    if set(data) - permitted:
        c.invalid("Unsupported project field: " + sorted(set(data) - permitted)[0])
    for key in ("name", "notes", "html_notes", "color", "default_view"):
        if key in data:
            row[key] = c.string(data[key], key, nullable=key == "color", empty=key != "name")
    for key in ("archived", "public"):
        if key in data:
            row[key] = c.boolean(data[key], key)
    privacy = data.get(
        "privacy_setting",
        ("public_to_workspace" if data["public"] else "private")
        if "public" in data
        else row.get("privacy_setting", "private"),
    )
    if not isinstance(privacy, str) or privacy not in ("private", "private_to_team", "public_to_workspace"):
        c.invalid("Invalid privacy_setting")
    row["privacy_setting"], row["public"] = privacy, privacy == "public_to_workspace"
    if "owner" in data:
        row["owner"] = (
            c.compact(c.require(ctx, grant, "users", data["owner"])) if data["owner"] is not None else None
        )
    if "workspace" in data and data["workspace"] != row["workspace"]["gid"]:
        c.invalid("The project workspace cannot be changed")
    if "team" in data:
        row["team"] = c.compact(c.require(ctx, grant, "teams", data["team"]))
    if "start_on" in data and not {"due_on", "due_date"}.intersection(data):
        c.invalid("Setting or unsetting start_on requires a due date in the same request")
    for key in ("due_on", "start_on"):
        if key in data:
            row[key] = c.date_value(data[key], key)
    if "due_date" in data:
        row["due_on"] = c.date_value(data["due_date"], "due_date")
    row["due_date"] = row.get("due_on")
    if row.get("due_on") and row.get("start_on") and row["start_on"] >= row["due_on"]:
        c.invalid("Project start_on must be before due_on")
    if row.get("default_view") not in (None, "list", "board", "calendar", "timeline"):
        c.invalid("Invalid default_view")


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get(c.BASE + "/projects")
    @app.get(c.BASE + "/workspaces/{workspace_gid}/projects")
    @app.get(c.BASE + "/teams/{team_gid}/projects")
    async def listing(
        request: Request, workspace_gid: str | None = None, team_gid: str | None = None
    ) -> dict[str, Any]:
        grant = c.principal(request, ctx, "projects:read")
        workspace_id = workspace_gid or request.query_params.get("workspace")
        team_id = team_gid or request.query_params.get("team")
        if workspace_id:
            c.require(ctx, grant, "workspaces", workspace_id)
        if team_id:
            c.require(ctx, grant, "teams", team_id)
        archived = c.query_bool(request, "archived")
        rows = [
            p
            for p in ctx.store["projects"]
            if c.allowed(ctx, grant, p)
            and (not team_id or (p.get("team") or {}).get("gid") == team_id)
            and (archived is None or p["archived"] == archived)
        ]
        return c.page(request, ctx, grant, rows)

    @app.post(c.BASE + "/projects")
    @app.post(c.BASE + "/workspaces/{workspace_gid}/projects")
    @app.post(c.BASE + "/teams/{team_gid}/projects")
    async def create(request: Request, workspace_gid: str | None = None, team_gid: str | None = None) -> Any:
        grant = c.principal(request, ctx, "projects:write")
        data = await c.payload(request)
        if workspace_gid:
            data.setdefault("workspace", workspace_gid)
        if team_gid:
            data.setdefault("team", team_gid)
        team = c.require(ctx, grant, "teams", data["team"]) if "team" in data else None
        workspace = c.require(
            ctx, grant, "workspaces", data.get("workspace") or (team or {}).get("organization", {}).get("gid")
        )
        uid = c.actor(ctx, grant)
        owner = c.require(ctx, grant, "users", uid)
        row = {
            "gid": c.new_gid(ctx),
            "resource_type": "project",
            "name": "",
            "notes": "",
            "html_notes": "<body></body>",
            "workspace": c.compact(workspace),
            "team": c.compact(team) if team else None,
            "owner": c.compact(owner),
            "followers": [c.compact(owner)],
            "members": [c.compact(owner)],
            "archived": False,
            "privacy_setting": "private_to_team" if team else "private",
            "public": False,
            "color": None,
            "default_view": "list",
            "due_on": None,
            "due_date": None,
            "start_on": None,
            "created_at": c.stamp(ctx),
            "modified_at": c.stamp(ctx),
            "completed": False,
            "completed_at": None,
            "current_status": None,
            "current_status_update": None,
            "custom_fields": [],
            "_members": [uid],
            "_sections": [],
            "_task_order": [],
        }
        if "name" not in data:
            c.invalid("name: Missing required parameter")
        validate(ctx, grant, row, data)
        if row["owner"] and row["owner"]["gid"] not in row["_members"]:
            row["_members"].append(row["owner"]["gid"])
            row["members"].append(row["owner"])
        c.put(ctx, "projects", row)
        await emit(ctx, grant, row, "added")
        return JSONResponse({"data": c.render(request, ctx, grant, row)}, status_code=201)

    @app.get(c.BASE + "/projects/{project_gid}")
    async def retrieve(project_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "projects:read")
        return {"data": c.render(request, ctx, grant, c.require(ctx, grant, "projects", project_gid))}

    @app.put(c.BASE + "/projects/{project_gid}")
    async def update(project_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "projects:write")
        current = c.require(ctx, grant, "projects", project_gid)
        row = copy.deepcopy(current)
        data = await c.payload(request)
        validate(ctx, grant, row, data)
        row["modified_at"] = c.stamp(ctx)
        if row["owner"] and row["owner"]["gid"] not in row["_members"]:
            row["_members"].append(row["owner"]["gid"])
            row["members"].append(row["owner"])
        c.put(ctx, "projects", row)
        for key in data:
            if current.get(key) != row.get(key):
                await emit(ctx, grant, row, "changed", field=key, value=row.get(key))
        return {"data": c.render(request, ctx, grant, row)}

    @app.delete(c.BASE + "/projects/{project_gid}")
    async def delete(project_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "projects:delete")
        row = c.require(ctx, grant, "projects", project_gid)
        await emit(ctx, grant, row, "deleted")
        row["_deleted"] = True
        for section in ctx.store["sections"]:
            if section["project"]["gid"] == project_gid:
                section["_deleted"] = True
        for task in ctx.store["tasks"]:
            if any(p["gid"] == project_gid for p in task["projects"]):
                task["projects"] = [p for p in task["projects"] if p["gid"] != project_gid]
                task["memberships"] = [m for m in task["memberships"] if m["project"]["gid"] != project_gid]
                task["modified_at"] = c.stamp(ctx)
        return {"data": {}}

    @app.post(c.BASE + "/projects/{project_gid}/addMembers")
    @app.post(c.BASE + "/projects/{project_gid}/removeMembers")
    @app.post(c.BASE + "/projects/{project_gid}/addFollowers")
    @app.post(c.BASE + "/projects/{project_gid}/removeFollowers")
    async def memberships(project_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "projects:write")
        operation = request.url.path.rsplit("/", 1)[-1]
        if operation not in ("addMembers", "removeMembers", "addFollowers", "removeFollowers"):
            raise c.error(404, "Not Found")
        row = c.require(ctx, grant, "projects", project_gid)
        data = await c.payload(request)
        key = "members" if operation.endswith("Members") else "followers"
        users = [c.require(ctx, grant, "users", uid) for uid in c.ids(data.get(key), key)]
        values = {u["gid"] for u in users}
        if (
            key == "members"
            and operation == "removeMembers"
            and (row.get("owner") or {}).get("gid") in values
        ):
            c.invalid("The project owner cannot be removed")
        if operation.startswith("add"):
            existing = {u["gid"] for u in row[key]}
            row[key].extend(c.compact(u) for u in users if u["gid"] not in existing)
        else:
            row[key] = [u for u in row[key] if u["gid"] not in values]
        if key == "members":
            row["_members"] = [u["gid"] for u in row[key]]
        row["modified_at"] = c.stamp(ctx)
        await emit(ctx, grant, row, "changed", field=key, value=row[key])
        return {"data": c.render(request, ctx, grant, row)}

    @app.get(c.BASE + "/projects/{project_gid}/project_memberships")
    async def project_memberships(project_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "projects:read")
        row = c.require(ctx, grant, "projects", project_gid)
        records = [
            {
                "gid": c.gid("project_membership", project_gid + ":" + uid),
                "resource_type": "project_membership",
                "project": c.compact(row),
                "user": c.compact(ctx.store["users"].get(uid)),
                "access_level": "admin" if (row.get("owner") or {}).get("gid") == uid else "editor",
            }
            for uid in row["_members"]
        ]
        return c.page(request, ctx, grant, records)

    @app.get(c.BASE + "/projects/{project_gid}/sections")
    async def sections(project_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "project_sections:read")
        project = c.require(ctx, grant, "projects", project_gid)
        return c.page(
            request,
            ctx,
            grant,
            [
                s
                for sid in project["_sections"]
                if (s := ctx.store["sections"].get(sid)) and not s.get("_deleted")
            ],
        )

    @app.post(c.BASE + "/projects/{project_gid}/sections")
    async def create_section(project_gid: str, request: Request) -> Any:
        grant = c.principal(request, ctx, "project_sections:write")
        project = c.require(ctx, grant, "projects", project_gid)
        data = await c.payload(request)
        name = c.string(data.get("name"), "name", empty=False)
        before, after = data.get("insert_before"), data.get("insert_after")
        if before and after:
            c.invalid("Specify only insert_before or insert_after")
        pivot = before or after
        if pivot and pivot not in project["_sections"]:
            c.invalid("The insertion section does not belong to this project")
        row = {
            "gid": c.new_gid(ctx),
            "resource_type": "section",
            "name": name,
            "created_at": c.stamp(ctx),
            "project": c.compact(project),
        }
        position = (
            project["_sections"].index(pivot) + (1 if after else 0) if pivot else len(project["_sections"])
        )
        c.put(ctx, "sections", row)
        project["_sections"].insert(position, row["gid"])
        return JSONResponse({"data": c.render(request, ctx, grant, row)}, status_code=201)

    @app.get(c.BASE + "/sections/{section_gid}")
    async def section(section_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "project_sections:read")
        return {"data": c.render(request, ctx, grant, c.require(ctx, grant, "sections", section_gid))}

    @app.put(c.BASE + "/sections/{section_gid}")
    async def update_section(section_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "project_sections:write")
        row = c.require(ctx, grant, "sections", section_gid)
        data = await c.payload(request)
        name = c.string(data.get("name"), "name", empty=False)
        row["name"] = name
        return {"data": c.render(request, ctx, grant, row)}

    @app.delete(c.BASE + "/sections/{section_gid}")
    async def delete_section(section_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "project_sections:delete")
        row = c.require(ctx, grant, "sections", section_gid)
        row["_deleted"] = True
        for task in ctx.store["tasks"]:
            for membership in task["memberships"]:
                if (membership.get("section") or {}).get("gid") == section_gid:
                    membership["section"] = None
        return {"data": {}}
