"""Task comments and client tags use the same task permissions as ordinary reads."""

from __future__ import annotations

import html
import xml.etree.ElementTree as ET
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from emulators.core.provider import ProviderContext
from emulators.providers.asana import common as c
from emulators.providers.asana.events import emit


def text_data(data: dict[str, Any]) -> tuple[str, str]:
    if ("text" in data) == ("html_text" in data):
        c.invalid("Specify exactly one of text or html_text")
    if "text" in data:
        text = c.string(data["text"], "text", empty=False)
        return text, "<body>" + html.escape(text) + "</body>"
    value = c.string(data["html_text"], "html_text", empty=False)
    try:
        root = ET.fromstring(value)
        if root.tag != "body":
            raise ET.ParseError
    except ET.ParseError:
        c.invalid("html_text must be valid rich text with a body root")
    text = "".join(root.itertext())
    if not text:
        c.invalid("Comment text must not be empty")
    return text, value


def register(app: FastAPI, ctx: ProviderContext) -> None:
    @app.get(c.BASE + "/tasks/{task_gid}/stories")
    async def stories(task_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "stories:read")
        c.require(ctx, grant, "tasks", task_gid)
        return c.page(
            request,
            ctx,
            grant,
            [s for s in ctx.store["stories"] if s["target"]["gid"] == task_gid and c.allowed(ctx, grant, s)],
        )

    @app.post(c.BASE + "/tasks/{task_gid}/stories")
    async def create_story(task_gid: str, request: Request) -> Any:
        grant = c.principal(request, ctx, "stories:write")
        task = c.require(ctx, grant, "tasks", task_gid)
        data = await c.payload(request)
        text, rich = text_data(data)
        pinned = c.boolean(data.get("is_pinned", False), "is_pinned")
        row = c.put(
            ctx,
            "stories",
            {
                "gid": c.new_gid(ctx),
                "resource_type": "story",
                "resource_subtype": "comment_added",
                "text": text,
                "html_text": rich,
                "created_at": c.stamp(ctx),
                "created_by": c.compact(ctx.store["users"].get(c.actor(ctx, grant))),
                "target": c.compact(task),
                "type": "comment",
                "source": "api",
                "is_pinned": pinned,
            },
        )
        task["modified_at"] = c.stamp(ctx)
        await emit(ctx, grant, row, "added")
        return JSONResponse({"data": c.render(request, ctx, grant, row)}, status_code=201)

    @app.get(c.BASE + "/stories/{story_gid}")
    async def story(story_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "stories:read")
        return {"data": c.render(request, ctx, grant, c.require(ctx, grant, "stories", story_gid))}

    @app.put(c.BASE + "/stories/{story_gid}")
    async def update_story(story_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "stories:write")
        row = c.require(ctx, grant, "stories", story_gid)
        if row["created_by"]["gid"] != c.actor(ctx, grant):
            raise c.error(403, "Only the comment author can edit this story")
        data = await c.payload(request)
        text, rich = (
            text_data(data) if "text" in data or "html_text" in data else (row["text"], row["html_text"])
        )
        pinned = c.boolean(data.get("is_pinned", row["is_pinned"]), "is_pinned")
        row.update(text=text, html_text=rich, is_pinned=pinned)
        ctx.store["tasks"].get(row["target"]["gid"])["modified_at"] = c.stamp(ctx)
        await emit(ctx, grant, row, "changed", field="text", value=text)
        return {"data": c.render(request, ctx, grant, row)}

    @app.delete(c.BASE + "/stories/{story_gid}")
    async def delete_story(story_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "stories:delete")
        row = c.require(ctx, grant, "stories", story_gid)
        if row["created_by"]["gid"] != c.actor(ctx, grant):
            raise c.error(403, "Only the comment author can delete this story")
        await emit(ctx, grant, row, "deleted")
        row["_deleted"] = True
        ctx.store["tasks"].get(row["target"]["gid"])["modified_at"] = c.stamp(ctx)
        return {"data": {}}

    @app.get(c.BASE + "/tags")
    @app.get(c.BASE + "/workspaces/{workspace_gid}/tags")
    async def tags(request: Request, workspace_gid: str | None = None) -> dict[str, Any]:
        grant = c.principal(request, ctx, "tags:read")
        if workspace := workspace_gid or request.query_params.get("workspace"):
            c.require(ctx, grant, "workspaces", workspace)
        return c.page(request, ctx, grant, ctx.store["tags"].values())

    @app.post(c.BASE + "/tags")
    @app.post(c.BASE + "/workspaces/{workspace_gid}/tags")
    async def create_tag(request: Request, workspace_gid: str | None = None) -> Any:
        grant = c.principal(request, ctx, "tags:write")
        data = await c.payload(request)
        workspace = c.require(ctx, grant, "workspaces", workspace_gid or data.get("workspace"))
        name = c.string(data.get("name"), "name", empty=False)
        notes = c.string(data.get("notes", ""), "notes")
        color = c.string(data.get("color"), "color", nullable=True)
        row = c.put(
            ctx,
            "tags",
            {
                "gid": c.new_gid(ctx),
                "resource_type": "tag",
                "name": name,
                "notes": notes,
                "color": color,
                "workspace": c.compact(workspace),
            },
        )
        return JSONResponse({"data": c.render(request, ctx, grant, row)}, status_code=201)

    @app.get(c.BASE + "/tags/{tag_gid}")
    async def tag(tag_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "tags:read")
        return {"data": c.render(request, ctx, grant, c.require(ctx, grant, "tags", tag_gid))}

    @app.put(c.BASE + "/tags/{tag_gid}")
    async def update_tag(tag_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "tags:write")
        row = c.require(ctx, grant, "tags", tag_gid)
        data = await c.payload(request)
        updates = {
            key: c.string(data[key], key, nullable=key == "color", empty=key != "name")
            for key in ("name", "notes", "color")
            if key in data
        }
        row.update(updates)
        return {"data": c.render(request, ctx, grant, row)}

    @app.delete(c.BASE + "/tags/{tag_gid}")
    async def delete_tag(tag_gid: str, request: Request) -> dict[str, Any]:
        grant = c.principal(request, ctx, "tags:delete")
        row = c.require(ctx, grant, "tags", tag_gid)
        row["_deleted"] = True
        for task in ctx.store["tasks"]:
            task["tags"] = [ref for ref in task["tags"] if ref["gid"] != tag_gid]
        return {"data": {}}
