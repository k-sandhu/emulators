"""Jira v3 projects, enhanced JQL search and mutable issues."""

from __future__ import annotations

import copy
import json
import re
from datetime import date
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from emulators.core.auth import Grant
from emulators.core.clock import isoformat_z
from emulators.core.provider import ProviderContext

from .auth import jira_auth
from .common import (
    Record,
    api_url,
    clean,
    cursor_offset,
    cursor_token,
    fail,
    integer,
    numeric_id,
    person_record,
    principal,
    query_values,
)
from .hooks import emit
from .query import QueryError, parse_query
from .seed import ISSUE_TYPES, PRIORITIES, jira_user, status_shape
from .shapes import adf_text, validate_adf

TRANSITIONS = {"11": "To Do", "21": "In Progress", "31": "Done"}
COMMENT_ROLES = {
    "Administrators": ("owner",),
    "Developers": ("owner", "editor"),
    "Users": ("owner", "editor", "viewer", "commenter"),
}


def project_role(ctx: ProviderContext, project: Record, grant: Grant) -> str | None:
    user = person_record(ctx, principal(ctx, grant))
    return project["_roles"].get(user["_world_id"]) if user else None


def visible(ctx: ProviderContext, issue: Record, grant: Grant) -> bool:
    project = ctx.store["projects"].get(issue["fields"]["project"]["id"])
    user = person_record(ctx, principal(ctx, grant))
    return bool(
        project
        and user
        and project_role(ctx, project, grant)
        and (issue.get("_read") is None or user["_world_id"] in issue["_read"])
    )


def get_issue(
    request: Request, ctx: ProviderContext, grant: Grant, key: str, *, edit: bool = False
) -> Record:
    issue = ctx.store["issues"].get(key) or ctx.store["issues"].first(key=key.upper())
    if not issue or not visible(ctx, issue, grant):
        fail(request, 404, "Issue does not exist or you do not have permission to see it.")
    project = ctx.store["projects"].get(issue["fields"]["project"]["id"])
    if edit and project_role(ctx, project, grant) not in ("editor", "owner"):
        fail(request, 403, "You do not have permission to edit this issue.")
    return issue


def user_wire(request: Request, user: Record | None) -> Record | None:
    if user is None:
        return None
    result = copy.deepcopy(user)
    result["self"] = api_url(request, "/rest/api/3/user?accountId=" + quote(user["accountId"]))
    result["avatarUrls"] = {
        str(size) + "x" + str(size): api_url(request, "/secure/useravatar?size=" + str(size))
        for size in (16, 24, 32, 48)
    }
    return result


def project_wire(request: Request, project: Record) -> Record:
    result = clean(project)
    result["self"] = api_url(request, "/rest/api/3/project/" + project["id"])
    result["avatarUrls"] = {
        str(size) + "x" + str(size): api_url(
            request, f"/secure/projectavatar?pid={project['id']}&size={size}"
        )
        for size in (16, 24, 32, 48)
    }
    return result


def comment_wire(request: Request, issue: Record, comment: Record) -> Record:
    result = clean(comment)
    result["self"] = api_url(request, f"/rest/api/3/issue/{issue['id']}/comment/{comment['id']}")
    result["author"] = user_wire(request, result["author"])
    result["updateAuthor"] = user_wire(request, result["updateAuthor"])
    return result


def can_read_comment(ctx: ProviderContext, issue: Record, comment: Record, grant: Grant) -> bool:
    restriction = comment.get("visibility")
    if not restriction:
        return True
    user = person_record(ctx, principal(ctx, grant))
    if restriction["type"] == "group":
        group = ctx.store["groups"].get(restriction["identifier"])
        return bool(group and user and user["_world_id"] in group["_members"])
    project = ctx.store["projects"].get(issue["fields"]["project"]["id"])
    return project_role(ctx, project, grant) in COMMENT_ROLES.get(restriction["value"], ())


def comment_visibility(request: Request, ctx: ProviderContext, value: Any) -> Record | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("type") not in ("group", "role"):
        fail(request, 400, "visibility requires a group or role restriction.")
    identifier = value.get("identifier", value.get("value"))
    if not isinstance(identifier, str) or not identifier:
        fail(request, 400, "visibility requires an identifier or value.")
    if value["type"] == "group":
        group = ctx.store["groups"].get(identifier) or ctx.store["groups"].first(name=identifier)
        if not group:
            fail(request, 400, "Unknown comment visibility group.")
        return {"type": "group", "identifier": group["id"], "value": group["name"]}
    if identifier not in COMMENT_ROLES:
        fail(request, 400, "Unknown comment visibility role.")
    return {"type": "role", "identifier": identifier, "value": identifier}


def issue_wire(
    request: Request,
    ctx: ProviderContext,
    issue: Record,
    fields: list[str] | None = None,
    *,
    grant: Grant | None = None,
) -> Record:
    result: Record = {
        "id": issue["id"],
        "key": issue["key"],
        "self": api_url(request, "/rest/api/3/issue/" + issue["id"]),
    }
    values = copy.deepcopy(issue["fields"])
    for name in ("assignee", "reporter"):
        values[name] = user_wire(request, values.get(name))
    values["project"] = project_wire(request, values["project"])
    if "parent" in values:
        parent = ctx.store["issues"].get(values["parent"]["id"])
        if grant is not None and (not parent or not visible(ctx, parent, grant)):
            values.pop("parent")
        else:
            values["parent"]["self"] = api_url(request, "/rest/api/3/issue/" + values["parent"]["id"])
    comments = ctx.store["jira_comments"].where(_issue=issue["id"])
    if grant is not None:
        comments = [c for c in comments if can_read_comment(ctx, issue, c, grant)]
    values["comment"] = {
        "comments": [comment_wire(request, issue, c) for c in comments[:50]],
        "self": api_url(request, "/rest/api/3/issue/" + issue["id"] + "/comment"),
        "startAt": 0,
        "maxResults": 50,
        "total": len(comments),
    }
    if fields is not None:
        include_all = any(f in ("*all", "*navigable") or f.startswith("-") for f in fields)
        values = {k: v for k, v in values.items() if (include_all or k in fields) and "-" + k not in fields}
    if values or fields is None:
        result["fields"] = values
    return result


def search_shape(issue: Record) -> Record:
    f = issue["fields"]

    def alternatives(name: str) -> list[str]:
        value = f.get(name)
        return [
            str(value[k])
            for k in ("id", "key", "name", "accountId", "emailAddress")
            if isinstance(value, dict) and k in value
        ]

    return {
        "id": issue["id"],
        "key": issue["key"],
        "issuekey": issue["key"],
        "project": alternatives("project"),
        "status": alternatives("status"),
        "statuscategory": [f["status"]["statusCategory"]["key"], f["status"]["statusCategory"]["name"]],
        "assignee": alternatives("assignee") or None,
        "reporter": alternatives("reporter") or None,
        "priority": alternatives("priority"),
        "issuetype": alternatives("issuetype"),
        "type": alternatives("issuetype"),
        "labels": f["labels"],
        "created": f["created"],
        "updated": f["updated"],
        "due": f.get("duedate"),
        "duedate": f.get("duedate"),
        "parent": alternatives("parent") or None,
        "summary": f["summary"],
        "description": " ".join(adf_text(f.get("description"))),
        "text": f["summary"] + " " + " ".join(adf_text(f.get("description"))),
        "_issue": issue,
    }


def valid_adf(request: Request, value: Any) -> None:
    if value is not None:
        validate_adf(request, value)


def edit_operations(request: Request, payload: Record, original: Record) -> Record:
    """Validate intermediate label sets before processing later operations."""
    fields, update = payload.get("fields", {}), payload.get("update", {})
    if not isinstance(fields, dict) or not isinstance(update, dict) or set(fields) & set(update):
        fail(request, 400, "fields and update must be objects with disjoint keys.")
    fields = copy.deepcopy(fields)
    for field, actions in update.items():
        if not isinstance(actions, list):
            fail(request, 400, "update operations must be arrays.")
        value = copy.deepcopy(original.get(field))
        for action in actions:
            if not isinstance(action, dict) or len(action) != 1:
                fail(request, 400, "Invalid update operation.")
            op, item = next(iter(action.items()))
            if op == "set":
                value = item
            elif field == "labels" and op in ("add", "remove") and isinstance(item, str):
                if not isinstance(value, list):
                    fail(request, 400, "labels must be an array before add/remove operations.")
                if op == "add" and item not in value:
                    value.append(item)
                elif op == "remove" and item in value:
                    value.remove(item)
            else:
                fail(request, 400, "Unsupported update operation.")
        fields[field] = value
    return fields


def resolve_project(
    request: Request, ctx: ProviderContext, grant: Grant, value: Any, *, edit: bool = True
) -> Record:
    if not isinstance(value, dict):
        fail(request, 400, "project is required.")
    project = ctx.store["projects"].get(str(value.get("id", ""))) or ctx.store["projects"].first(
        key=value.get("key")
    )
    if not project or not project_role(ctx, project, grant):
        fail(request, 404, "Project does not exist or you do not have permission to see it.")
    if edit and project_role(ctx, project, grant) not in ("editor", "owner"):
        fail(request, 403, "You do not have permission to create issues in this project.")
    return project


def validate_fields(
    request: Request,
    ctx: ProviderContext,
    grant: Grant,
    fields: Record,
    original: Record,
    *,
    creating: bool = False,
) -> Record:
    result = copy.deepcopy(original)
    allowed = {
        "summary",
        "description",
        "assignee",
        "reporter",
        "priority",
        "labels",
        "duedate",
        "issuetype",
        "parent",
    }
    if creating:
        allowed.add("project")
    if set(fields) - allowed:
        fail(request, 400, "Unsupported or non-editable field: " + ", ".join(sorted(set(fields) - allowed)))
    for name, value in fields.items():
        if name == "summary":
            if not isinstance(value, str) or not value.strip() or len(value) > 255:
                fail(request, 400, "summary is required and cannot exceed 255 characters.")
            result[name] = value
        elif name == "description":
            valid_adf(request, value)
            result[name] = value
        elif name in ("assignee", "reporter"):
            if value is None and name == "assignee":
                result[name] = None
                continue
            if not isinstance(value, dict):
                fail(request, 400, f"{name} requires accountId.")
            user = ctx.store["users"].get(value.get("accountId"))
            if not user or user["accountStatus"] != "active":
                fail(request, 400, f"Invalid {name} accountId.")
            project = ctx.store["projects"].get(result["project"]["id"])
            if user["_world_id"] not in project["_roles"]:
                fail(request, 400, f"{name} is not a member of this project.")
            result[name] = jira_user(ctx, user["id"])
        elif name in ("priority", "issuetype"):
            options = PRIORITIES if name == "priority" else ISSUE_TYPES
            if not isinstance(value, dict):
                fail(request, 400, f"Invalid {name}.")
            selected = next(
                (key for key, oid in options.items() if value.get("id") == oid or value.get("name") == key),
                None,
            )
            if not selected:
                fail(request, 400, f"Invalid {name}.")
            if name == "issuetype" and selected == "Sub-task":
                fail(request, 400, "Sub-task creation is not supported.")
            result[name] = {"id": options[selected], "name": selected}
            if name == "issuetype":
                result[name]["subtask"] = False
        elif name == "labels":
            if not isinstance(value, list) or any(
                not isinstance(v, str) or not v or any(c.isspace() for c in v) for v in value
            ):
                fail(request, 400, "labels must be an array of strings without spaces.")
            result[name] = list(dict.fromkeys(value))
        elif name == "duedate":
            if value is not None:
                try:
                    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                        raise ValueError
                    date.fromisoformat(value)
                except (ValueError, TypeError):
                    fail(request, 400, "duedate must use yyyy-MM-dd.")
            result[name] = value
        elif name == "parent":
            if value is None:
                result.pop("parent", None)
                continue
            if not isinstance(value, dict):
                fail(request, 400, "Invalid parent.")
            parent = get_issue(request, ctx, grant, str(value.get("key", value.get("id", ""))))
            if (
                parent["fields"]["issuetype"]["name"] != "Epic"
                or parent["fields"]["project"]["id"] != result["project"]["id"]
            ):
                fail(request, 400, "Parent must be an epic in this project.")
            result[name] = {
                "id": parent["id"],
                "key": parent["key"],
                "fields": {
                    "summary": parent["fields"]["summary"],
                    "issuetype": parent["fields"]["issuetype"],
                },
            }
    return result


def now_string(ctx: ProviderContext) -> str:
    return isoformat_z(ctx.clock.now(), timespec="milliseconds").replace("Z", "+0000")


def build_jira(ctx: ProviderContext) -> APIRouter:
    router = APIRouter()

    @router.get("/myself")
    async def myself(request: Request) -> Record:
        grant = jira_auth(request, ctx, users=True)
        user = person_record(ctx, grant.user_id)
        if not user or grant.app_only:
            fail(request, 403, "This resource requires a user grant.")
        return user_wire(request, jira_user(ctx, user["id"]))

    @router.get("/project/search")
    async def projects(request: Request) -> Record:
        grant = jira_auth(request, ctx)
        start = integer(request, request.query_params.get("startAt"), "startAt", 0, 2147483647, 0)
        limit = integer(request, request.query_params.get("maxResults"), "maxResults", 50, 100)
        query = request.query_params.get("query", "").casefold()
        projects = [
            p
            for p in ctx.store["projects"]
            if project_role(ctx, p, grant) and (query in p["key"].casefold() or query in p["name"].casefold())
        ]
        result = {
            "self": api_url(request, "/rest/api/3/project/search") + f"?startAt={start}&maxResults={limit}",
            "maxResults": limit,
            "startAt": start,
            "total": len(projects),
            "isLast": start + limit >= len(projects),
            "values": [project_wire(request, p) for p in projects[start : start + limit]],
        }
        if not result["isLast"]:
            result["nextPage"] = (
                api_url(request, "/rest/api/3/project/search")
                + f"?startAt={start + limit}&maxResults={limit}&query={quote(query)}"
            )
        return result

    @router.get("/project/{project_id}")
    async def project(request: Request, project_id: str) -> Record:
        grant = jira_auth(request, ctx)
        record = ctx.store["projects"].get(project_id) or ctx.store["projects"].first(key=project_id)
        if not record or not project_role(ctx, record, grant):
            fail(request, 404, "Project does not exist or you do not have permission to see it.")
        return project_wire(request, record)

    async def search_result(request: Request, payload: Record) -> Record:
        grant = jira_auth(request, ctx)
        jql = payload.get("jql", "")
        if not isinstance(jql, str):
            fail(request, 400, "jql must be a string.")
        user = person_record(ctx, principal(ctx, grant))
        try:
            query = parse_query(jql, "jql", now=ctx.clock.now(), user=user["id"] if user else None)
            if query.node is None or len(query.order) > 7:
                fail(request, 400, "A bounded JQL query with at most 7 order fields is required.")
            matches = query.apply([search_shape(i) for i in ctx.store["issues"] if visible(ctx, i, grant)])
        except QueryError as exc:
            fail(request, 400, str(exc))
        fields = payload.get("fields", ["id"])
        if isinstance(fields, str):
            fields = fields.split(",")
        if not isinstance(fields, list) or any(not isinstance(f, str) for f in fields):
            fail(request, 400, "fields must be an array of strings.")
        limit = integer(request, payload.get("maxResults"), "maxResults", 50, 5000)
        binding = json.dumps([jql, fields, user["id"] if user else None, limit], sort_keys=True)
        token = payload.get("nextPageToken")
        if token is not None and not isinstance(token, str):
            fail(request, 400, "Invalid nextPageToken.")
        start = cursor_offset(request, token, binding)
        result = {
            "issues": [
                issue_wire(request, ctx, r["_issue"], fields, grant=grant)
                for r in matches[start : start + limit]
            ],
            "isLast": start + limit >= len(matches),
        }
        if not result["isLast"]:
            result["nextPageToken"] = cursor_token(start + limit, binding)
        return result

    @router.get("/search/jql")
    async def get_search(request: Request) -> Record:
        payload = dict(request.query_params)
        if "fields" in payload:
            payload["fields"] = query_values(request, "fields")
        return await search_result(request, payload)

    @router.post("/search/jql")
    async def post_search(request: Request, payload: dict[str, Any]) -> Record:
        return await search_result(request, payload)

    @router.get("/issue/{issue_key}")
    async def issue(request: Request, issue_key: str) -> Record:
        grant = jira_auth(request, ctx)
        record = get_issue(request, ctx, grant, issue_key)
        fields = query_values(request, "fields") if "fields" in request.query_params else None
        return issue_wire(request, ctx, record, fields, grant=grant)

    @router.post("/issue")
    async def create_issue(request: Request, payload: dict[str, Any]) -> JSONResponse:
        grant = jira_auth(request, ctx)
        user = person_record(ctx, principal(ctx, grant))
        fields = edit_operations(request, payload, {"labels": []})
        if not isinstance(fields, dict) or not fields.get("summary") or not fields.get("issuetype"):
            fail(request, 400, "fields.summary, fields.project and fields.issuetype are required.")
        project = resolve_project(request, ctx, grant, fields.get("project"))
        when = now_string(ctx)
        defaults = {
            "summary": "",
            "description": None,
            "project": clean(project),
            "status": status_shape("To Do"),
            "assignee": None,
            "reporter": jira_user(ctx, user["id"]),
            "priority": {"id": "3", "name": "Medium"},
            "labels": [],
            "created": when,
            "updated": when,
            "duedate": None,
        }
        values = validate_fields(request, ctx, grant, fields, defaults, creating=True)
        with ctx.store.lock:
            number = ctx.store.meta.get("jira:" + project["key"], 0) + 1
            while ctx.store["issues"].first(key=f"{project['key']}-{number}"):
                number += 1
            ctx.store.meta["jira:" + project["key"]] = number
            key = f"{project['key']}-{number}"
            issue = {"id": numeric_id("issue", key), "key": key, "fields": values, "_read": None}
            ctx.store["issues"].put(issue)
        await emit(ctx, "jira:issue_created", issue_wire(request, ctx, issue), user["id"], request=request)
        return JSONResponse(
            {"id": issue["id"], "key": key, "self": api_url(request, "/rest/api/3/issue/" + issue["id"])},
            status_code=201,
        )

    @router.put("/issue/{issue_key}")
    async def update_issue(request: Request, issue_key: str, payload: dict[str, Any]) -> Response:
        grant = jira_auth(request, ctx)
        issue = get_issue(request, ctx, grant, issue_key, edit=True)
        fields = edit_operations(request, payload, issue["fields"])
        values = validate_fields(request, ctx, grant, fields, issue["fields"])
        if values.get("parent", {}).get("id") == issue["id"]:
            fail(request, 400, "An issue cannot be its own parent.")
        changes = [
            {
                "field": key,
                "fieldtype": "jira",
                "fromString": str(issue["fields"].get(key)),
                "toString": str(value),
            }
            for key, value in fields.items()
            if issue["fields"].get(key) != value
        ]
        values["updated"] = now_string(ctx)
        issue["fields"] = values
        user = person_record(ctx, principal(ctx, grant))
        await emit(
            ctx,
            "jira:issue_updated",
            issue_wire(request, ctx, issue),
            user["id"],
            request=request,
            changelog=changes,
        )
        if request.query_params.get("returnIssue") == "true":
            return JSONResponse(issue_wire(request, ctx, issue, grant=grant))
        return Response(status_code=204)

    @router.get("/issue/{issue_key}/transitions")
    async def transitions(request: Request, issue_key: str) -> Record:
        grant = jira_auth(request, ctx)
        issue = get_issue(request, ctx, grant, issue_key)
        project = ctx.store["projects"].get(issue["fields"]["project"]["id"])
        permitted = project_role(ctx, project, grant) in ("editor", "owner")
        return {
            "expand": "transitions",
            "transitions": [
                {
                    "id": tid,
                    "name": name,
                    "to": status_shape(name),
                    "hasScreen": False,
                    "isGlobal": True,
                    "isInitial": False,
                    "isAvailable": True,
                    "isConditional": False,
                }
                for tid, name in TRANSITIONS.items()
                if permitted and name != issue["fields"]["status"]["name"]
            ],
        }

    @router.post("/issue/{issue_key}/transitions")
    async def transition(request: Request, issue_key: str, payload: dict[str, Any]) -> Response:
        grant = jira_auth(request, ctx)
        issue = get_issue(request, ctx, grant, issue_key, edit=True)
        transition = payload.get("transition")
        tid = transition.get("id") if isinstance(transition, dict) else None
        if (
            not isinstance(tid, str)
            or tid not in TRANSITIONS
            or TRANSITIONS[tid] == issue["fields"]["status"]["name"]
        ):
            fail(request, 400, "The requested transition is not available.")
        fields = payload.get("fields", {})
        if not isinstance(fields, dict) or payload.get("update"):
            fail(request, 400, "Unsupported transition fields or update.")
        values = validate_fields(request, ctx, grant, fields, issue["fields"])
        before = values["status"]
        values.update({"status": status_shape(TRANSITIONS[tid]), "updated": now_string(ctx)})
        issue["fields"] = values
        user = person_record(ctx, principal(ctx, grant))
        await emit(
            ctx,
            "jira:issue_updated",
            issue_wire(request, ctx, issue),
            user["id"],
            request=request,
            changelog=[
                {
                    "field": "status",
                    "fieldtype": "jira",
                    "from": before["id"],
                    "fromString": before["name"],
                    "to": values["status"]["id"],
                    "toString": values["status"]["name"],
                }
            ],
        )
        return Response(status_code=204)

    @router.get("/issue/{issue_key}/comment")
    async def comments(request: Request, issue_key: str) -> Record:
        grant = jira_auth(request, ctx)
        issue = get_issue(request, ctx, grant, issue_key)
        start = integer(request, request.query_params.get("startAt"), "startAt", 0, 2147483647, 0)
        limit = integer(request, request.query_params.get("maxResults"), "maxResults", 50, 100)
        comments = ctx.store["jira_comments"].where(_issue=issue["id"])
        comments = [c for c in comments if can_read_comment(ctx, issue, c, grant)]
        order = request.query_params.get("orderBy", "created")
        if order not in ("created", "+created", "-created"):
            fail(request, 400, "Invalid comment orderBy.")
        comments.sort(key=lambda c: c["created"], reverse=order.startswith("-"))
        return {
            "startAt": start,
            "maxResults": limit,
            "total": len(comments),
            "comments": [comment_wire(request, issue, c) for c in comments[start : start + limit]],
        }

    @router.post("/issue/{issue_key}/comment")
    async def add_comment(request: Request, issue_key: str, payload: dict[str, Any]) -> JSONResponse:
        grant = jira_auth(request, ctx)
        issue = get_issue(request, ctx, grant, issue_key, edit=True)
        body = payload.get("body")
        if body is None:
            fail(request, 400, "body is required.")
        valid_adf(request, body)
        visibility = comment_visibility(request, ctx, payload.get("visibility"))
        user = person_record(ctx, principal(ctx, grant))
        sequence = ctx.store.meta.get("jira_comment_sequence", 0) + 1
        ctx.store.meta["jira_comment_sequence"] = sequence
        when = now_string(ctx)
        comment = {
            "id": numeric_id("created-jira-comment", sequence),
            "_issue": issue["id"],
            "body": body,
            "author": jira_user(ctx, user["id"]),
            "updateAuthor": jira_user(ctx, user["id"]),
            "created": when,
            "updated": when,
        }
        if visibility is not None:
            comment["visibility"] = visibility
        ctx.store["jira_comments"].put(comment)
        issue["fields"]["updated"] = when
        await emit(ctx, "jira:issue_updated", issue_wire(request, ctx, issue), user["id"], request=request)
        return JSONResponse(comment_wire(request, issue, comment), status_code=201)

    @router.get("/issue/{issue_key}/comment/{comment_id}")
    async def comment(request: Request, issue_key: str, comment_id: str) -> Record:
        grant = jira_auth(request, ctx)
        issue = get_issue(request, ctx, grant, issue_key)
        record = ctx.store["jira_comments"].get(comment_id)
        if not record or record["_issue"] != issue["id"] or not can_read_comment(ctx, issue, record, grant):
            fail(request, 404, "Comment not found.")
        return comment_wire(request, issue, record)

    @router.get("/user")
    async def user(request: Request) -> Record:
        jira_auth(request, ctx, users=True)
        record = ctx.store["users"].get(request.query_params.get("accountId"))
        if not record:
            fail(request, 404, "User not found.")
        return user_wire(request, jira_user(ctx, record["id"]))

    @router.get("/user/search")
    async def users(request: Request) -> list[Record]:
        jira_auth(request, ctx, users=True)
        query = request.query_params.get("query", "").casefold()
        account = request.query_params.get("accountId")
        start = integer(request, request.query_params.get("startAt"), "startAt", 0, 2147483647, 0)
        limit = integer(request, request.query_params.get("maxResults"), "maxResults", 50, 1000)
        users = [
            u
            for u in ctx.store["users"]
            if u["accountStatus"] == "active"
            and (not account or u["id"] == account)
            and (query in u["displayName"].casefold() or query in u["email"].casefold())
        ]
        return [user_wire(request, jira_user(ctx, u["id"])) for u in users[start : start + limit]]

    return router
