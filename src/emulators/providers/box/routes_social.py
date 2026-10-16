"""Comments, tasks, task assignments and web links."""

from __future__ import annotations

import re
from typing import Any

from fastapi import FastAPI, Request

from emulators.core.provider import ProviderContext
from emulators.providers.box import access, db, errors, events, ops
from emulators.providers.box.access import Principal
from emulators.providers.box.routing import Api, created, no_content
from emulators.providers.box.shapes import Shaper
from emulators.providers.box.util import (
    box_ts,
    input_ts,
    json_body,
    next_id,
    now_iso,
    opt_str,
    page_window,
    paginate,
    parse_fields,
    ref_id,
    require_str,
    validate_name,
)

Record = dict[str, Any]
MENTION = re.compile(r"@\[(\d+):([^\]]+)\]")


def pick_fields(body: Record, fields: list[str] | None) -> Record:
    if fields is None:
        return body
    keep = {"type", "id", *[f.split(".")[0] for f in fields]}
    return {k: v for k, v in body.items() if k in keep}


def register(app: FastAPI, ctx: ProviderContext) -> None:
    api = Api(app, ctx)
    comments = db.Live(ctx, db.COMMENTS)
    tasks = db.Live(ctx, db.TASKS)
    assignments = db.Live(ctx, db.ASSIGNMENTS)

    # -- comments ------------------------------------------------------------------------
    def comment_body(shaper: Shaper, comment: Record, fields: list[str] | None = None) -> Record:
        body: Record = {
            "type": "comment",
            "id": comment["id"],
            "is_reply_comment": bool(comment.get("is_reply_comment", False)),
            "message": comment["message"],
            "created_by": shaper.user_mini(comment["created_by"]),
            "created_at": box_ts(comment["created_at"]),
            "item": {"id": comment["item_id"], "type": comment["item_type"]},
            "modified_at": box_ts(comment["modified_at"]),
        }
        if comment.get("tagged_message"):
            body["tagged_message"] = comment["tagged_message"]
        return pick_fields(body, fields)

    def comment_file(p: Principal, comment: Record, need: str | None = None) -> Record:
        item_id = comment["item_id"]
        if comment["item_type"] == "comment":
            parent = comments.get(item_id)
            item_id = parent["item_id"] if parent else item_id
        return p.get_item("file", item_id, need)

    @api.get("/files/{file_id}/comments")
    async def file_comments(request: Request, file_id: str) -> Any:
        p = api.principal(request)
        record = p.get_item("file", file_id, access.PREVIEW)
        rows = sorted(
            (c for c in comments if (c["item_type"] == "file" and c["item_id"] == record["id"])
             or (c["item_type"] == "comment" and (comments.get(c["item_id"]) or {}).get("item_id") == record["id"])),
            key=lambda c: c["created_at"],
        )  # fmt: skip
        offset, limit, use_marker = page_window(request, default_limit=100, max_limit=1000)
        body = paginate(rows, offset, limit, use_marker)
        shaper = Shaper(p, request)
        fields = parse_fields(request)
        body["entries"] = [comment_body(shaper, c, fields) for c in body["entries"]]
        return body

    @api.post("/comments")
    async def create_comment(request: Request) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        target = body.get("item")
        if (
            not isinstance(target, dict)
            or target.get("type") not in ("file", "comment")
            or not target.get("id")
        ):
            raise errors.missing_parameter("item")
        tagged = opt_str(body, "tagged_message", "", max_len=10_000) or None
        message = opt_str(body, "message", "", max_len=10_000) or None
        if not message and not tagged:
            raise errors.missing_parameter("message")
        if tagged:
            message = MENTION.sub(lambda m: f"@{m.group(2)}", tagged)
        reply = target["type"] == "comment"
        parent_comment = comments.get(str(target["id"])) if reply else None
        if reply and parent_comment is None:
            raise errors.not_found()
        record = (
            comment_file(p, parent_comment, access.COMMENT)
            if parent_comment
            else p.get_item("file", str(target["id"]), access.COMMENT)
        )
        now = now_iso(ctx)
        cid = next_id(ctx, "comment", comments, start=db.SMALL_ID_START * 1000)
        comment = {
            "type": "comment",
            "id": cid,
            "item_type": "comment" if reply else "file",
            "item_id": str(target["id"]),
            "message": message,
            "tagged_message": tagged,
            "created_by": p.uid,
            "created_at": now,
            "modified_at": now,
            "is_reply_comment": reply,
        }
        comments.put(comment)
        shaper = Shaper(p, request)
        await events.emit(
            p,
            "COMMENT_CREATE",
            None,
            source=comment_body(shaper, comment),
            kind="comment",
            webhook_item=record,
        )
        return created(comment_body(shaper, comment, parse_fields(request)))

    def comment_or_404(p: Principal, comment_id: str) -> tuple[Record, Record]:
        comment = comments.get(comment_id)
        if comment is None:
            raise errors.not_found()
        return comment, comment_file(p, comment, access.PREVIEW)

    @api.get("/comments/{comment_id}")
    async def get_comment(request: Request, comment_id: str) -> Any:
        p = api.principal(request)
        comment, _file = comment_or_404(p, comment_id)
        return comment_body(Shaper(p, request), comment, parse_fields(request))

    @api.put("/comments/{comment_id}")
    async def update_comment(request: Request, comment_id: str) -> Any:
        p = api.principal(request)
        comment, record = comment_or_404(p, comment_id)
        if comment["created_by"] != p.uid:
            raise errors.forbidden()
        body = await json_body(request)
        message = require_str(body, "message")
        comment["message"] = message
        comment["modified_at"] = now_iso(ctx)
        shaper = Shaper(p, request)
        await events.emit(
            p,
            "COMMENT_UPDATE",
            None,
            source=comment_body(shaper, comment),
            webhook_item=record,
            trigger="COMMENT.UPDATED",
            log=False,  # Box has no COMMENT_UPDATE event, only the webhook trigger
        )
        return comment_body(shaper, comment, parse_fields(request))

    @api.delete("/comments/{comment_id}")
    async def delete_comment(request: Request, comment_id: str) -> Any:
        p = api.principal(request)
        comment, record = comment_or_404(p, comment_id)
        if comment["created_by"] != p.uid and access.DELETE not in p.caps(record):
            raise errors.forbidden()
        comments.delete(comment["id"])
        for reply in [c for c in comments if c["item_type"] == "comment" and c["item_id"] == comment["id"]]:
            comments.delete(reply["id"])
        await events.emit(
            p,
            "COMMENT_DELETE",
            None,
            source=comment_body(Shaper(p, request), comment),
            kind="comment",
            webhook_item=record,
        )
        return no_content()

    # -- tasks ---------------------------------------------------------------------------
    def assignment_body(shaper: Shaper, assignment: Record, fields: list[str] | None = None) -> Record:
        record = db.files(ctx).get(assignment["item_id"])
        body: Record = {
            "type": "task_assignment",
            "id": assignment["id"],
            "item": shaper.file_mini(record) if record else None,
            "assigned_to": shaper.user_mini(assignment["assigned_to"]),
            "message": assignment.get("message", ""),
            "completed_at": box_ts(assignment.get("completed_at")),
            "assigned_at": box_ts(assignment["assigned_at"]),
            "reminded_at": box_ts(assignment.get("reminded_at")),
            "resolution_state": assignment["resolution_state"],
            "assigned_by": shaper.user_mini(assignment["assigned_by"]),
        }
        return pick_fields(body, fields)

    def task_body(shaper: Shaper, task: Record, fields: list[str] | None = None) -> Record:
        record = db.files(ctx).get(task["item_id"])
        rows = [a for a in assignments if a["task_id"] == task["id"]]
        body: Record = {
            "type": "task",
            "id": task["id"],
            "item": shaper.file_mini(record) if record else None,
            "due_at": box_ts(task.get("due_at")),
            "action": task["action"],
            "message": task.get("message", ""),
            "task_assignment_collection": {
                "total_count": len(rows),
                "entries": [assignment_body(shaper, a) for a in rows],
            },
            "is_completed": bool(task.get("is_completed", False)),
            "created_by": shaper.user_mini(task["created_by"]),
            "created_at": box_ts(task["created_at"]),
            "completion_rule": task.get("completion_rule", "all_assignees"),
        }
        return pick_fields(body, fields)

    def task_or_404(p: Principal, task_id: str, need: str | None = None) -> tuple[Record, Record]:
        task = tasks.get(task_id)
        if task is None:
            raise errors.not_found()
        return task, p.get_item("file", task["item_id"], need)

    @api.get("/files/{file_id}/tasks")
    async def file_tasks(request: Request, file_id: str) -> Any:
        p = api.principal(request)
        record = p.get_item("file", file_id, access.PREVIEW)
        rows = [t for t in tasks if t["item_id"] == record["id"]]
        shaper = Shaper(p, request)
        fields = parse_fields(request)
        return {"total_count": len(rows), "entries": [task_body(shaper, t, fields) for t in rows]}

    @api.post("/tasks")
    async def create_task(request: Request) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        target = body.get("item")
        if not isinstance(target, dict) or target.get("type") != "file" or not target.get("id"):
            raise errors.missing_parameter("item")
        record = p.get_item("file", str(target["id"]), access.COMMENT)
        action = body.get("action", "review")
        if action not in ("review", "complete"):
            raise errors.invalid_parameter("action", "'action' must be review or complete")
        rule = body.get("completion_rule", "all_assignees")
        if rule not in ("all_assignees", "any_assignee"):
            raise errors.invalid_parameter("completion_rule")
        task = {
            "type": "task",
            "id": next_id(ctx, "task", tasks, start=db.SMALL_ID_START * 100),
            "item_id": record["id"],
            "action": action,
            "message": opt_str(body, "message", "", max_len=10_000),
            "due_at": input_ts(body["due_at"], "due_at") if body.get("due_at") else None,
            "completion_rule": rule,
            "created_by": p.uid,
            "created_at": now_iso(ctx),
            "is_completed": False,
        }
        tasks.put(task)
        shaper = Shaper(p, request)
        await events.emit(
            p, "TASK_CREATE", None, source=task_body(shaper, task), kind="task", webhook_item=record
        )
        return created(task_body(shaper, task, parse_fields(request)))

    @api.get("/tasks/{task_id}")
    async def get_task(request: Request, task_id: str) -> Any:
        p = api.principal(request)
        task, _record = task_or_404(p, task_id, access.PREVIEW)
        return task_body(Shaper(p, request), task, parse_fields(request))

    @api.put("/tasks/{task_id}")
    async def update_task(request: Request, task_id: str) -> Any:
        p = api.principal(request)
        task, _record = task_or_404(p, task_id, access.PREVIEW)
        if task["created_by"] != p.uid:
            raise errors.forbidden()
        body = await json_body(request)
        if "action" in body:
            if body["action"] not in ("review", "complete"):
                raise errors.invalid_parameter("action")
            task["action"] = body["action"]
        if "message" in body:
            task["message"] = opt_str(body, "message", "", max_len=10_000)
        if "due_at" in body:
            task["due_at"] = input_ts(body["due_at"], "due_at") if body["due_at"] else None
        if "completion_rule" in body:
            if body["completion_rule"] not in ("all_assignees", "any_assignee"):
                raise errors.invalid_parameter("completion_rule")
            task["completion_rule"] = body["completion_rule"]
        shaper = Shaper(p, request)
        await events.emit(
            p, "TASK_UPDATE", None, source=task_body(shaper, task), kind="task", webhook_item=_record
        )
        return task_body(shaper, task, parse_fields(request))

    @api.delete("/tasks/{task_id}")
    async def delete_task(request: Request, task_id: str) -> Any:
        p = api.principal(request)
        task, _record = task_or_404(p, task_id, access.PREVIEW)
        if task["created_by"] != p.uid:
            raise errors.forbidden()
        for assignment in [a for a in assignments if a["task_id"] == task["id"]]:
            assignments.delete(assignment["id"])
        tasks.delete(task["id"])
        return no_content()

    @api.get("/tasks/{task_id}/assignments")
    async def task_assignments(request: Request, task_id: str) -> Any:
        p = api.principal(request)
        task, _record = task_or_404(p, task_id, access.PREVIEW)
        rows = [a for a in assignments if a["task_id"] == task["id"]]
        shaper = Shaper(p, request)
        return {"total_count": len(rows), "entries": [assignment_body(shaper, a) for a in rows]}

    @api.post("/task_assignments")
    async def create_assignment(request: Request) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        task, record = task_or_404(p, ref_id(body, "task"), access.COMMENT)
        target = body.get("assign_to")
        if not isinstance(target, dict):
            raise errors.missing_parameter("assign_to")
        login = target.get("login")
        if not target.get("id") and not isinstance(login, str):
            raise errors.missing_parameter("assign_to.id")
        user = (
            db.users(ctx).get(str(target["id"])) if target.get("id") else db.user_by_login(ctx, login or "")
        )
        if user is None:
            raise errors.not_found("User not found")
        if any(a["task_id"] == task["id"] and a["assigned_to"] == user["id"] for a in assignments):
            raise errors.box_error(409, "conflict", "The user is already assigned to this task")
        assignment = {
            "type": "task_assignment",
            "id": next_id(ctx, "assignment", assignments, start=db.SMALL_ID_START * 100),
            "task_id": task["id"],
            "item_id": record["id"],
            "assigned_to": user["id"],
            "assigned_by": p.uid,
            "assigned_at": now_iso(ctx),
            "message": "",
            "completed_at": None,
            "reminded_at": None,
            "resolution_state": "incomplete",
        }
        assignments.put(assignment)
        shaper = Shaper(p, request)
        await events.emit(
            p,
            "TASK_ASSIGNMENT_CREATE",
            None,
            source=assignment_body(shaper, assignment),
            kind="task_assignment",
            webhook_item=record,
        )
        return created(assignment_body(shaper, assignment, parse_fields(request)))

    def assignment_or_404(p: Principal, assignment_id: str) -> tuple[Record, Record]:
        assignment = assignments.get(assignment_id)
        if assignment is None:
            raise errors.not_found()
        return assignment, p.get_item("file", assignment["item_id"], access.PREVIEW)

    @api.get("/task_assignments/{assignment_id}")
    async def get_assignment(request: Request, assignment_id: str) -> Any:
        p = api.principal(request)
        assignment, _record = assignment_or_404(p, assignment_id)
        return assignment_body(Shaper(p, request), assignment, parse_fields(request))

    @api.put("/task_assignments/{assignment_id}")
    async def update_assignment(request: Request, assignment_id: str) -> Any:
        p = api.principal(request)
        assignment, record = assignment_or_404(p, assignment_id)
        if assignment["assigned_to"] != p.uid:
            raise errors.forbidden()
        body = await json_body(request)
        if "message" in body:
            assignment["message"] = opt_str(body, "message", "", max_len=10_000)
        if "resolution_state" in body:
            if body["resolution_state"] not in ("completed", "incomplete", "approved", "rejected"):
                raise errors.invalid_parameter("resolution_state")
            parent_task = tasks.get(assignment["task_id"])
            allowed = (
                ("incomplete", "completed")
                if parent_task and parent_task["action"] == "complete"
                else ("incomplete", "approved", "rejected")
            )
            if body["resolution_state"] not in allowed:
                raise errors.invalid_parameter(
                    "resolution_state",
                    f"A '{(parent_task or {}).get('action', 'review')}' task takes: {', '.join(allowed)}",
                )
            assignment["resolution_state"] = body["resolution_state"]
            assignment["completed_at"] = now_iso(ctx) if body["resolution_state"] != "incomplete" else None
            task = tasks.get(assignment["task_id"])
            if task is not None:
                rows = [a for a in assignments if a["task_id"] == task["id"]]
                done = [a for a in rows if a["resolution_state"] != "incomplete"]
                task["is_completed"] = (
                    bool(done) if task["completion_rule"] == "any_assignee" else len(done) == len(rows)
                )
        shaper = Shaper(p, request)
        await events.emit(
            p,
            "TASK_ASSIGNMENT_UPDATE",
            None,
            source=assignment_body(shaper, assignment),
            kind="task_assignment",
            webhook_item=record,
        )
        return assignment_body(shaper, assignment, parse_fields(request))

    @api.delete("/task_assignments/{assignment_id}")
    async def delete_assignment(request: Request, assignment_id: str) -> Any:
        p = api.principal(request)
        assignment, record = assignment_or_404(p, assignment_id)
        if assignment["assigned_by"] != p.uid and assignment["assigned_to"] != p.uid:
            raise errors.forbidden()
        assignments.delete(assignment["id"])
        await events.emit(
            p,
            "TASK_ASSIGNMENT_DELETE",
            None,
            source=assignment_body(Shaper(p, request), assignment),
            kind="task_assignment",
            webhook_item=record,
        )
        return no_content()

    # -- web links -----------------------------------------------------------------------
    links = db.Live(ctx, db.WEB_LINKS)

    def link_or_404(p: Principal, link_id: str, need: str | None = None, **kw: Any) -> Record:
        return p.get_item("web_link", link_id, need, **kw)

    @api.post("/web_links")
    async def create_web_link(request: Request) -> Any:
        p = api.principal(request)
        body = await json_body(request)
        url = require_str(body, "url")
        if not re.match(r"^https?://", url):
            raise errors.invalid_parameter("url", "'url' must be an http(s) URL")
        parent = p.get_item("folder", ref_id(body, "parent"), access.UPLOAD)
        name = validate_name(body["name"]) if body.get("name") else url
        ops.ensure_name_free(p, parent["id"], name)
        now = now_iso(ctx)
        record = {
            "type": "web_link",
            "id": next_id(ctx, "web_link", links, start=db.FILE_ID_START * 2),
            "name": name,
            "url": url,
            "description": opt_str(body, "description", ""),
            "_parent": parent["id"],
            "_owner": parent.get("_owner") or p.uid,
            "created_by": p.uid,
            "modified_by": p.uid,
            "created_at": now,
            "modified_at": now,
            "trashed_at": None,
            "purged_at": None,
            "_trash": False,
            "_seq": 0,
            "shared_link": None,
        }
        links.put(record)
        p.invalidate()
        return created(Shaper(p, request).web_link(record, parse_fields(request)))

    @api.get("/web_links/{link_id}")
    async def get_web_link(request: Request, link_id: str) -> Any:
        p = api.principal(request)
        return Shaper(p, request).web_link(link_or_404(p, link_id), parse_fields(request))

    @api.put("/web_links/{link_id}")
    async def update_web_link(request: Request, link_id: str) -> Any:
        p = api.principal(request)
        record = link_or_404(p, link_id, access.RENAME)
        body = await json_body(request)
        now = now_iso(ctx)
        if "name" in body:
            name = validate_name(body["name"])
            ops.ensure_name_free(p, record["_parent"], name, ignore=record["id"])
            record["name"] = name
        if "url" in body:
            url = require_str(body, "url")
            if not re.match(r"^https?://", url):
                raise errors.invalid_parameter("url", "'url' must be an http(s) URL")
            record["url"] = url
        if "description" in body:
            record["description"] = opt_str(body, "description", "")
        if isinstance(body.get("parent"), dict) and "id" in body["parent"]:
            ops.move(p, record, p.get_item("folder", str(body["parent"]["id"]), access.UPLOAD), now)
        ops.touch(record, p, now)
        return Shaper(p, request).web_link(record, parse_fields(request))

    @api.delete("/web_links/{link_id}")
    async def delete_web_link(request: Request, link_id: str) -> Any:
        p = api.principal(request)
        record = link_or_404(p, link_id, access.DELETE)
        ops.trash(p, record, now_iso(ctx))
        return no_content()

    @api.get("/web_links/{link_id}/trash")
    async def get_trashed_web_link(request: Request, link_id: str) -> Any:
        p = api.principal(request)
        record = link_or_404(p, link_id, allow_trashed=True, only_trashed=True)
        return Shaper(p, request).web_link(record, parse_fields(request))

    @api.post("/web_links/{link_id}")
    async def restore_web_link(request: Request, link_id: str) -> Any:
        p = api.principal(request)
        record = link_or_404(p, link_id, access.DELETE, allow_trashed=True, only_trashed=True)
        ops.restore(p, record, now_iso(ctx))
        return created(Shaper(p, request).web_link(record, parse_fields(request)))

    @api.delete("/web_links/{link_id}/trash")
    async def purge_web_link(request: Request, link_id: str) -> Any:
        p = api.principal(request)
        record = link_or_404(p, link_id, access.DELETE, allow_trashed=True, only_trashed=True)
        ops.purge(p, record)
        return no_content()
