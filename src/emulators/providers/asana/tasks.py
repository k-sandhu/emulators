"""Task mutations validate complete candidate records before changing store state."""

from __future__ import annotations

import copy
import html
import xml.etree.ElementTree as ET
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from emulators.core.auth import Grant
from emulators.core.clock import parse_instant
from emulators.core.provider import ProviderContext
from emulators.providers.asana import common as c
from emulators.providers.asana.events import emit
from emulators.providers.asana.seed import task_record

WRITABLE = {
    "name",
    "notes",
    "html_notes",
    "completed",
    "assignee",
    "assignee_status",
    "due_on",
    "due_at",
    "start_on",
    "start_at",
    "workspace",
    "parent",
    "projects",
    "followers",
    "tags",
    "resource_subtype",
    "is_rendered_as_separator",
    "approval_status",
    "liked",
}


def validate_parent(ctx: ProviderContext, grant: Grant, row: dict[str, Any], parent_id: Any) -> None:
    if parent_id is None:
        row["parent"] = None
        return
    parent = c.require(ctx, grant, "tasks", parent_id)
    if parent["workspace"]["gid"] != row["workspace"]["gid"]:
        c.invalid("Parent must be in the same workspace")
    current = parent
    visited: set[str] = set()
    while current:
        if current["gid"] == row["gid"] or current["gid"] in visited:
            c.invalid("A task cannot be a subtask of itself or its descendants")
        visited.add(current["gid"])
        current = ctx.store["tasks"].get((current.get("parent") or {}).get("gid"))
    row["parent"] = c.compact(parent)


def validate(
    ctx: ProviderContext, grant: Grant, row: dict[str, Any], data: dict[str, Any], *, creating: bool = False
) -> None:
    if set(data) - WRITABLE:
        c.invalid("Unsupported task field: " + sorted(set(data) - WRITABLE)[0])
    if "notes" in data and "html_notes" in data:
        c.invalid("Specify only notes or html_notes")
    for key in ("name", "notes", "assignee_status", "resource_subtype", "approval_status"):
        if key in data:
            row[key] = c.string(data[key], key)
    if "notes" in data:
        row["html_notes"] = "<body>" + html.escape(data["notes"]) + "</body>"
    if "html_notes" in data:
        value = c.string(data["html_notes"], "html_notes")
        try:
            tree = ET.fromstring(value)
            if tree.tag != "body":
                raise ET.ParseError
        except ET.ParseError:
            c.invalid("html_notes must be valid rich text with a body root")
        row["html_notes"] = value
        row["notes"] = "".join(tree.itertext())
    if row["assignee_status"] not in ("inbox", "today", "upcoming", "later"):
        c.invalid("Invalid assignee_status")
    if row["resource_subtype"] not in ("default_task", "milestone", "approval"):
        c.invalid("Unsupported resource_subtype")
    if row.get("approval_status") not in (None, "pending", "approved", "rejected", "changes_requested"):
        c.invalid("Invalid approval_status")
    if row["resource_subtype"] == "approval":
        if "approval_status" in data:
            completion = data["approval_status"] != "pending"
            if "completed" in data and c.boolean(data["completed"], "completed") != completion:
                c.invalid("completed and approval_status must agree")
            data = {**data, "completed": completion}
        elif "completed" in data:
            row["approval_status"] = "approved" if c.boolean(data["completed"], "completed") else "pending"
        elif row.get("approval_status") is None:
            row["approval_status"] = "approved" if row["completed"] else "pending"
    for key in ("completed", "liked", "is_rendered_as_separator"):
        if key in data:
            value = c.boolean(data[key], key)
            if key == "completed" and value != row["completed"]:
                row["completed_at"] = c.stamp(ctx) if value else None
                row["completed_by"] = (
                    c.compact(ctx.store["users"].get(c.actor(ctx, grant))) if value else None
                )
            row[key] = value
    if "workspace" in data and data["workspace"] != row["workspace"]["gid"]:
        c.invalid("The task workspace cannot be changed")
    if "assignee" in data:
        if data["assignee"] is None:
            row["assignee"] = None
        else:
            row["assignee"] = c.compact(c.require(ctx, grant, "users", data["assignee"]))
    if "start_on" in data and not {"due_on", "due_at"}.intersection(data):
        c.invalid("Setting or unsetting start_on requires due_on or due_at in the same request")
    if "start_at" in data and "due_at" not in data:
        c.invalid("Setting or unsetting start_at requires due_at in the same request")
    for left, right in (("due_on", "due_at"), ("start_on", "start_at")):
        if data.get(left) is not None and data.get(right) is not None:
            c.invalid(f"{left} and {right} cannot both be set")
        for key in (left, right):
            if key in data:
                row[key] = c.date_value(data[key], key, instant=key.endswith("_at"))
                if data[key] is not None:
                    row[right if key == left else left] = None
    if row["resource_subtype"] == "milestone" and (row["start_on"] or row["start_at"]):
        c.invalid("Milestones cannot have start dates")
    for start, due in (("start_on", "due_on"), ("start_at", "due_at")):
        if row[start] and row[due] and parse_instant(row[start]) > parse_instant(row[due]):
            c.invalid("The start date must be on or before the due date")
    if "parent" in data:
        if not creating:
            c.invalid("Use setParent to change a task's parent")
        validate_parent(ctx, grant, row, data["parent"])
    if "projects" in data:
        if not creating:
            c.invalid("Use addProject or removeProject to change task projects")
        projects = [c.require(ctx, grant, "projects", value) for value in c.ids(data["projects"], "projects")]
        if any(project["workspace"]["gid"] != row["workspace"]["gid"] for project in projects):
            c.invalid("All projects must belong to the task workspace")
        row["projects"] = [c.compact(project) for project in projects]
        row["memberships"] = [{"project": c.compact(project), "section": None} for project in projects]
    for key, collection in (("followers", "users"), ("tags", "tags")):
        if key in data:
            row[key] = [
                c.compact(c.require(ctx, grant, collection, value)) for value in c.ids(data[key], key)
            ]


async def create_task(ctx: ProviderContext, grant: Grant, data: dict[str, Any]) -> dict[str, Any]:
    workspace_id = data.get("workspace")
    if "projects" in data:
        project_ids = c.ids(data["projects"], "projects")
        if project_ids:
            workspace_id = (
                workspace_id or c.require(ctx, grant, "projects", project_ids[0])["workspace"]["gid"]
            )
    if data.get("parent"):
        workspace_id = workspace_id or c.require(ctx, grant, "tasks", data["parent"])["workspace"]["gid"]
    workspace = c.require(ctx, grant, "workspaces", workspace_id)
    creator = ctx.store["users"].get(c.actor(ctx, grant))
    row = task_record(ctx, c.new_gid(ctx), "", workspace, creator)
    validate(ctx, grant, row, data, creating=True)
    c.put(ctx, "tasks", row)
    for ref in row["projects"]:
        ctx.store["projects"].get(ref["gid"])["_task_order"].append(row["gid"])
    await emit(ctx, grant, row, "added")
    return row


def ordered(rows: list[dict[str, Any]], project: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if project is None:
        return rows
    order = {value: i for i, value in enumerate(project["_task_order"])}
    return sorted(rows, key=lambda row: order.get(row["gid"], len(order)))


def add_project(
    ctx: ProviderContext, grant: Grant, row: dict[str, Any], data: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    project = c.require(ctx, grant, "projects", data.get("project"))
    if project["workspace"]["gid"] != row["workspace"]["gid"]:
        c.invalid("Project must belong to the task workspace")
    section = c.require(ctx, grant, "sections", data["section"]) if data.get("section") else None
    if section and section["project"]["gid"] != project["gid"]:
        c.invalid("Section does not belong to this project")
    before, after = data.get("insert_before"), data.get("insert_after")
    if before is not None and after is not None:
        c.invalid("Specify only insert_before or insert_after")
    pivot = before or after
    order = [value for value in project["_task_order"] if value != row["gid"]]
    if pivot and pivot not in order:
        c.invalid("Insertion task must belong to the project and cannot be this task")
    if pivot:
        c.require(ctx, grant, "tasks", pivot)
    if project["gid"] not in {p["gid"] for p in row["projects"]}:
        row["projects"].append(c.compact(project))
        row["memberships"].append(
            {"project": c.compact(project), "section": c.compact(section) if section else None}
        )
    elif section:
        for membership in row["memberships"]:
            if membership["project"]["gid"] == project["gid"]:
                membership["section"] = c.compact(section)
    position = order.index(pivot) + (1 if after else 0) if pivot else len(order)
    order.insert(position, row["gid"])
    return project, order


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get(c.BASE + "/tasks")
    @app.get(c.BASE + "/projects/{project_gid}/tasks")
    @app.get(c.BASE + "/sections/{section_gid}/tasks")
    @app.get(c.BASE + "/tags/{tag_gid}/tasks")
    async def listing(
        request: Request,
        project_gid: str | None = None,
        section_gid: str | None = None,
        tag_gid: str | None = None,
    ) -> dict[str, Any]:
        grant = c.principal(request, ctx, "tasks:read")
        query = request.query_params
        project_id, section_id, tag_id = (
            project_gid or query.get("project"),
            section_gid or query.get("section"),
            tag_gid or query.get("tag"),
        )
        if not (project_id or tag_id or section_gid) and not (
            query.get("assignee") and query.get("workspace")
        ):
            c.invalid("You must specify a project or tag, or both assignee and workspace")
        project = c.require(ctx, grant, "projects", project_id) if project_id else None
        section = c.require(ctx, grant, "sections", section_id) if section_id else None
        if section and project and section["project"]["gid"] != project["gid"]:
            c.invalid("Section does not belong to this project")
        if tag_id:
            c.require(ctx, grant, "tags", tag_id)
        if query.get("workspace"):
            c.require(ctx, grant, "workspaces", query["workspace"])
        assignee = c.require(ctx, grant, "users", query["assignee"])["gid"] if query.get("assignee") else None
        times = {}
        for key in ("completed_since", "modified_since"):
            if key in query:
                c.date_value(query[key], key, instant=True)
                times[key] = parse_instant(query[key])
        rows = [
            task
            for task in ctx.store["tasks"]
            if c.allowed(ctx, grant, task)
            and (not project_id or any(ref["gid"] == project_id for ref in task["projects"]))
            and (not tag_id or any(ref["gid"] == tag_id for ref in task["tags"]))
            and (
                not section_id
                or any((m.get("section") or {}).get("gid") == section_id for m in task["memberships"])
            )
            and (not assignee or (task.get("assignee") or {}).get("gid") == assignee)
            and (
                "completed_since" not in times
                or not task["completed"]
                or (task["completed_at"] and parse_instant(task["completed_at"]) >= times["completed_since"])
            )
            and (
                "modified_since" not in times or parse_instant(task["modified_at"]) >= times["modified_since"]
            )
        ]
        return c.page(request, ctx, grant, ordered(rows, project))

    @app.post(c.BASE + "/tasks")
    async def create(request: Request) -> Any:
        grant = c.principal(request, ctx, "tasks:write")
        row = await create_task(ctx, grant, await c.payload(request))
        return JSONResponse({"data": c.render(request, ctx, grant, row)}, status_code=201)

    @app.get(c.BASE + "/tasks/{task_gid}")
    async def retrieve(task_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "tasks:read")
        return {"data": c.render(request, ctx, grant, c.require(ctx, grant, "tasks", task_gid))}

    @app.put(c.BASE + "/tasks/{task_gid}")
    async def update(task_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "tasks:write")
        current = c.require(ctx, grant, "tasks", task_gid)
        row = copy.deepcopy(current)
        data = await c.payload(request)
        validate(ctx, grant, row, data)
        row["modified_at"] = c.stamp(ctx)
        c.put(ctx, "tasks", row)
        for key in data:
            if row.get(key) != current.get(key):
                await emit(ctx, grant, row, "changed", field=key, value=row.get(key))
        return {"data": c.render(request, ctx, grant, row)}

    @app.delete(c.BASE + "/tasks/{task_gid}")
    async def delete(task_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "tasks:delete")
        row = c.require(ctx, grant, "tasks", task_gid)
        await emit(ctx, grant, row, "deleted")
        row.update(_deleted=True, _deleted_at=c.stamp(ctx), _deleted_by=c.actor(ctx, grant))
        for task in ctx.store["tasks"]:
            task["dependencies"] = [ref for ref in task["dependencies"] if ref["gid"] != task_gid]
        return {"data": {}}

    @app.get(c.BASE + "/tasks/{task_gid}/subtasks")
    async def subtasks(task_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "tasks:read")
        c.require(ctx, grant, "tasks", task_gid)
        return c.page(
            request,
            ctx,
            grant,
            [
                task
                for task in ctx.store["tasks"]
                if (task.get("parent") or {}).get("gid") == task_gid and c.allowed(ctx, grant, task)
            ],
        )

    @app.post(c.BASE + "/tasks/{task_gid}/subtasks")
    async def create_subtask(task_gid: str, request: Request) -> Any:
        grant = c.principal(request, ctx, "tasks:write")
        c.require(ctx, grant, "tasks", task_gid)
        data = await c.payload(request)
        if "parent" in data and data["parent"] != task_gid:
            c.invalid("parent does not match the request path")
        data["parent"] = task_gid
        row = await create_task(ctx, grant, data)
        return JSONResponse({"data": c.render(request, ctx, grant, row)}, status_code=201)

    @app.get(c.BASE + "/tasks/{task_gid}/dependencies")
    @app.get(c.BASE + "/tasks/{task_gid}/dependents")
    async def dependencies(task_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "tasks:read")
        row = c.require(ctx, grant, "tasks", task_gid)
        if request.url.path.endswith("/dependencies"):
            rows = [
                task
                for ref in row["dependencies"]
                if (task := ctx.store["tasks"].get(ref["gid"])) and c.allowed(ctx, grant, task)
            ]
        else:
            rows = [
                task
                for task in ctx.store["tasks"]
                if any(ref["gid"] == task_gid for ref in task["dependencies"]) and c.allowed(ctx, grant, task)
            ]
        return c.page(request, ctx, grant, rows)

    @app.post(c.BASE + "/tasks/{task_gid}/addProject")
    @app.post(c.BASE + "/tasks/{task_gid}/removeProject")
    @app.post(c.BASE + "/tasks/{task_gid}/setParent")
    @app.post(c.BASE + "/tasks/{task_gid}/addFollowers")
    @app.post(c.BASE + "/tasks/{task_gid}/removeFollowers")
    @app.post(c.BASE + "/tasks/{task_gid}/addDependencies")
    @app.post(c.BASE + "/tasks/{task_gid}/removeDependencies")
    @app.post(c.BASE + "/tasks/{task_gid}/addDependents")
    @app.post(c.BASE + "/tasks/{task_gid}/removeDependents")
    @app.post(c.BASE + "/tasks/{task_gid}/addTag")
    @app.post(c.BASE + "/tasks/{task_gid}/removeTag")
    async def action(task_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "tasks:write")
        current = c.require(ctx, grant, "tasks", task_gid)
        row = copy.deepcopy(current)
        data = await c.payload(request)
        operation = request.url.path.rsplit("/", 1)[-1]
        affected = [ref["gid"] for ref in row["projects"]]
        project_update = None
        other_updates: list[dict[str, Any]] = []
        if operation == "addProject":
            project_update = add_project(ctx, grant, row, data)
        elif operation == "removeProject":
            project = c.require(ctx, grant, "projects", data.get("project"))
            row["projects"] = [ref for ref in row["projects"] if ref["gid"] != project["gid"]]
            row["memberships"] = [m for m in row["memberships"] if m["project"]["gid"] != project["gid"]]
            project_update = (project, [rid for rid in project["_task_order"] if rid != task_gid])
        elif operation == "setParent":
            if "parent" not in data:
                c.invalid("parent: Missing required parameter")
            validate_parent(ctx, grant, row, data["parent"])
        elif operation.endswith("Followers"):
            users = [c.require(ctx, grant, "users", uid) for uid in c.ids(data.get("followers"), "followers")]
            user_ids = {user["gid"] for user in users}
            if operation.startswith("add"):
                existing = {user["gid"] for user in row["followers"]}
                row["followers"].extend(c.compact(user) for user in users if user["gid"] not in existing)
            else:
                row["followers"] = [user for user in row["followers"] if user["gid"] not in user_ids]
        elif operation.endswith(("Dependencies", "Dependents")):
            key = "dependencies" if operation.endswith("Dependencies") else "dependents"
            others = [c.require(ctx, grant, "tasks", rid) for rid in c.ids(data.get(key), key)]
            if any(
                other["gid"] == task_gid or other["workspace"]["gid"] != row["workspace"]["gid"]
                for other in others
            ):
                c.invalid("Dependencies must be other tasks in the same workspace")
            for other in others:
                target, dependency = (row, other) if key == "dependencies" else (copy.deepcopy(other), row)
                if operation.startswith("add"):
                    pending, seen = [dependency], set()
                    while pending:
                        node = pending.pop()
                        if node["gid"] == target["gid"]:
                            c.invalid("Dependencies cannot form a cycle")
                        if node["gid"] not in seen:
                            seen.add(node["gid"])
                            pending.extend(
                                ctx.store["tasks"].get(ref["gid"])
                                for ref in node["dependencies"]
                                if ctx.store["tasks"].get(ref["gid"])
                            )
                    if dependency["gid"] not in {ref["gid"] for ref in target["dependencies"]}:
                        target["dependencies"].append(c.compact(dependency))
                else:
                    target["dependencies"] = [
                        ref for ref in target["dependencies"] if ref["gid"] != dependency["gid"]
                    ]
                if key == "dependents":
                    other_updates.append(target)
            candidates = {task["gid"]: task for task in ctx.store["tasks"] if not task.get("_deleted")}
            candidates.update({task["gid"]: task for task in [row, *other_updates]})
            counts = {rid: len(task["dependencies"]) for rid, task in candidates.items()}
            for candidate in candidates.values():
                for ref in candidate["dependencies"]:
                    counts[ref["gid"]] = counts.get(ref["gid"], 0) + 1
            if any(count > 30 for count in counts.values()):
                c.invalid("A task can have at most 30 dependencies and dependents combined")
        else:
            tag = c.require(ctx, grant, "tags", data.get("tag"))
            if operation == "addTag" and tag["gid"] not in {ref["gid"] for ref in row["tags"]}:
                row["tags"].append(c.compact(tag))
            elif operation == "removeTag":
                row["tags"] = [ref for ref in row["tags"] if ref["gid"] != tag["gid"]]
        row["modified_at"] = c.stamp(ctx)
        c.put(ctx, "tasks", row)
        for other in other_updates:
            other["modified_at"] = c.stamp(ctx)
            c.put(ctx, "tasks", other)
        if project_update:
            project_update[0]["_task_order"] = project_update[1]
        field = {
            "addProject": "memberships",
            "removeProject": "memberships",
            "setParent": "parent",
            "addFollowers": "followers",
            "removeFollowers": "followers",
            "addDependencies": "dependencies",
            "removeDependencies": "dependencies",
            "addDependents": "dependents",
            "removeDependents": "dependents",
            "addTag": "tags",
            "removeTag": "tags",
        }[operation]
        await emit(ctx, grant, row, "changed", field=field, value=row.get(field), affected=affected)
        return (
            {"data": c.render(request, ctx, grant, row)}
            if operation.endswith("Followers") or operation == "setParent"
            else {"data": {}}
        )

    @app.post(c.BASE + "/sections/{section_gid}/addTask")
    async def section_task(section_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "tasks:write")
        section = c.require(ctx, grant, "sections", section_gid)
        data = await c.payload(request)
        current = c.require(ctx, grant, "tasks", data.get("task"))
        row = copy.deepcopy(current)
        project, order = add_project(
            ctx, grant, row, {**data, "project": section["project"]["gid"], "section": section_gid}
        )
        row["modified_at"] = c.stamp(ctx)
        c.put(ctx, "tasks", row)
        project["_task_order"] = order
        await emit(ctx, grant, row, "changed", field="memberships")
        return {"data": {}}
