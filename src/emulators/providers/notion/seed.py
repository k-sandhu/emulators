"""Project the world into a deterministic workspace without rendering binaries."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from emulators.core.clock import parse_instant
from emulators.core.ids import stable_guid, stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.notion.common import (
    BOT_ID,
    INTERNAL_TOKEN,
    LEGACY_TOKEN,
    envelope,
    partial_user,
    person_id,
    policy,
    rich_text,
)
from emulators.providers.notion.properties import schema, values


def sid(kind: str, world_id: str) -> str:
    return stable_guid("notion", kind, world_id)


def page(
    ctx: ProviderContext,
    record_id: str,
    title: str,
    parent: dict[str, Any],
    access: dict[str, Any],
    *,
    at: Any = None,
) -> dict[str, Any]:
    record = {
        **envelope(record_id, "page", parent, at or ctx.world.anchor),
        "properties": {"title": {"id": "title", "type": "title", "title": rich_text(title)}},
        "icon": None,
        "cover": None,
        "url": "",
        "public_url": None,
        "_children": [],
        **policy(access),
    }
    ctx.store["pages"].put(record)
    if parent.get("type") == "page_id":
        ctx.store["pages"].get(parent["page_id"])["_children"].append(record_id)
    return record


def block(
    ctx: ProviderContext, parent: dict[str, Any], kind: str, data: dict[str, Any], suffix: str
) -> dict[str, Any]:
    record_id = sid("block", parent["id"] + "/" + suffix)
    record = {
        **envelope(
            record_id,
            "block",
            {
                "type": "page_id" if parent["object"] == "page" else "block_id",
                "page_id" if parent["object"] == "page" else "block_id": parent["id"],
            },
            parse_instant(parent["created_time"]),
        ),
        "type": kind,
        kind: data,
        "has_children": False,
        "_children": [],
        **policy(parent),
    }
    record.update(
        last_edited_time=parent["last_edited_time"],
        created_by=parent["created_by"],
        last_edited_by=parent["last_edited_by"],
    )
    ctx.store["blocks"].put(record)
    parent["_children"].append(record_id)
    parent["has_children"] = True
    return record


def database(
    ctx: ProviderContext,
    record_id: str,
    title: str,
    parent: dict[str, Any],
    specs: dict[str, Any],
    *,
    source_id: str | None = None,
) -> dict[str, Any]:
    source_id = source_id or sid("source", record_id)
    created = parse_instant(parent["created_time"])
    db = {
        **envelope(record_id, "database", {"type": "page_id", "page_id": parent["id"]}, created),
        "title": rich_text(title),
        "description": [],
        "icon": None,
        "cover": None,
        "is_inline": False,
        "url": "",
        "public_url": None,
        "_sources": [source_id],
        **policy(parent),
    }
    ctx.store["databases"].put(db)
    parent["_children"].append(record_id)
    ctx.store["data_sources"].put(
        {
            **envelope(source_id, "data_source", {"type": "database_id", "database_id": record_id}, created),
            "title": rich_text(title),
            "description": [],
            "icon": None,
            "database_parent": db["parent"],
            "properties": schema(specs, source_id),
            **policy(parent),
        }
    )
    return db


def row(
    ctx: ProviderContext, db: dict[str, Any], record_id: str, properties: dict[str, Any]
) -> dict[str, Any]:
    source = ctx.store["data_sources"].get(db["_sources"][0])
    record = page(
        ctx,
        record_id,
        "",
        {"type": "data_source_id", "data_source_id": source["id"], "database_id": db["id"]},
        source,
    )
    record["properties"] = values(ctx, source["properties"], properties)
    return record


def seed(ctx: ProviderContext) -> None:
    ctx.store.meta.update(
        workspace_id=sid("workspace", ctx.world.org.domain),
        file_secret=stable_hash(ctx.world.seed, "notion-files", length=64),
    )
    for person in ctx.world.active_employees:
        ctx.store["users"].put(
            {
                "object": "user",
                "id": person_id(person.id),
                "name": person.display_name,
                "avatar_url": None,
                "type": "person",
                "person": {"email": person.email},
                "_world_id": person.id,
            }
        )
    ctx.store["users"].put(
        {
            "object": "user",
            "id": BOT_ID,
            "name": "Halcyon Knowledge Integration",
            "avatar_url": None,
            "type": "bot",
            "bot": {"owner": {"type": "workspace", "workspace": True}, "workspace_name": ctx.world.org.name},
        }
    )
    ctx.tokens.register_client("notion-client", "notion-secret", name="Halcyon Knowledge Integration")
    for token in (INTERNAL_TOKEN, LEGACY_TOKEN):
        ctx.tokens.issue_access(
            client_id="notion-client",
            user_id=None,
            token=token,
            ttl=None,
            app_only=True,
            scopes=[
                "read_content",
                "update_content",
                "insert_content",
                "read_comments",
                "insert_comments",
                "read_user_with_email",
            ],
        )
    roots = {}
    workspace_created = min(doc.created for doc in ctx.world.documents) - timedelta(days=1)
    for space in ctx.world.spaces:
        acl = {space.owner_id: "owner"}
        for principal, access in space.members:
            for pid in ctx.world.expand_principal(principal):
                if acl.get(pid) != "owner" and not (acl.get(pid) == "editor" and access == "viewer"):
                    acl[pid] = access
        if space.id == "people":
            acl = {
                p.id: "editor" if p.department_id == "people" else "viewer"
                for p in ctx.world.active_employees
                if p.department_id in ("people", "exec") or p.is_partner
            }
        access = {"_acl": acl, "_internal": space.id in ("knowledge", "clients")}
        roots[space.id] = page(
            ctx,
            sid("space", space.id),
            space.name,
            {"type": "workspace", "workspace": True},
            access,
            at=workspace_created,
        )
        roots[space.id]["_world_space"] = space.id
    folders = {}
    for space in ctx.world.spaces:
        for folder in ctx.world.folders_in(space.id):
            parent_path, _, name = folder.rpartition("/")
            parent = folders.get((space.id, parent_path), roots[space.id])
            folders[space.id, folder] = page(
                ctx,
                sid("folder", space.id + "/" + folder),
                name,
                {"type": "page_id", "page_id": parent["id"]},
                parent,
                at=workspace_created,
            )
    for doc in ctx.world.documents:
        parent = folders.get((doc.space_id, doc.folder), roots[doc.space_id])
        access = policy(parent)
        if doc.restricted:
            access["_acl"] = {doc.owner_id: "owner"}
            access["_internal"] = False
        for share in doc.shares:
            if share.principal_id:
                for pid in ctx.world.expand_principal(share.principal_id):
                    access["_acl"][pid] = share.role
            elif share.link in ("organization", "anyone"):
                for person in ctx.world.active_employees:
                    access["_acl"].setdefault(person.id, share.role)
        access["_acl"][doc.owner_id] = "owner"
        # Notion's People & Culture teamspace is intentionally narrower than the drive.
        if doc.space_id == "people":
            access["_acl"] = {
                pid: value for pid, value in access["_acl"].items() if pid in roots["people"]["_acl"]
            }
        item = page(
            ctx,
            sid("document", doc.id),
            doc.title,
            {"type": "page_id", "page_id": parent["id"]},
            access,
            at=doc.created,
        )
        item.update(
            _world_document=doc.id,
            archived=doc.trashed,
            in_trash=doc.trashed,
            last_edited_time=doc.modified.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            created_by=partial_user(person_id(doc.owner_id)),
            last_edited_by=partial_user(person_id(doc.modified_by_id)),
        )
        if doc.kind in ("docx", "md", "txt"):
            block(
                ctx,
                item,
                "heading_1",
                {"rich_text": rich_text(doc.title), "color": "default", "is_toggleable": False},
                "title",
            )
            for i, paragraph in enumerate(doc.paragraphs):
                heading = paragraph.startswith("#")
                kind = (
                    f"heading_{min(3, len(paragraph) - len(paragraph.lstrip('#')))}"
                    if heading
                    else "paragraph"
                )
                data = {
                    "rich_text": rich_text(paragraph.lstrip("# ") if heading else paragraph),
                    "color": "default",
                }
                if heading:
                    data["is_toggleable"] = False
                block(ctx, item, kind, data, str(i))
        elif doc.kind in ("xlsx", "csv"):
            rows = doc.table or (("Content",), *((p,) for p in doc.paragraphs))
            width = max(len(r) for r in rows)
            table = block(
                ctx,
                item,
                "table",
                {"table_width": width, "has_column_header": True, "has_row_header": False},
                "table",
            )
            for i, cells in enumerate(rows):
                block(
                    ctx,
                    table,
                    "table_row",
                    {"cells": [rich_text(str(cells[j])) if j < len(cells) else [] for j in range(width)]},
                    str(i),
                )
        elif doc.kind == "pptx":
            for i in range(0, len(doc.paragraphs), 4):
                toggle = block(
                    ctx,
                    item,
                    "toggle",
                    {
                        "rich_text": rich_text(doc.title if i == 0 else f"Details {i // 4}"),
                        "color": "default",
                    },
                    str(i),
                )
                for j, paragraph in enumerate(doc.paragraphs[i : i + 4]):
                    block(
                        ctx,
                        toggle,
                        "paragraph",
                        {"rich_text": rich_text(paragraph), "color": "default"},
                        str(j),
                    )
        else:
            fid = sid("file", doc.id)
            ctx.store["files"].put({"id": fid, "filename": doc.name, "_world_document": doc.id})
            block(
                ctx,
                item,
                "pdf" if doc.kind == "pdf" else "image" if doc.kind == "png" else "file",
                {"type": "file", "_file_id": fid, "caption": []},
                "file",
            )

    all_access = {p.id: "editor" for p in ctx.world.active_employees}
    db_parent = {**roots["clients"], "_internal": True, "_acl": all_access}
    db_clients = database(
        ctx,
        sid("database", "clients"),
        "Clients",
        db_parent,
        {
            "Name": {"title": {}},
            "Industry": {
                "select": {"options": [{"name": x} for x in sorted({c.industry for c in ctx.world.clients})]}
            },
            "Status": {"status": {"options": [{"name": x} for x in ("Active", "Former", "Prospect")]}},
            "Owner": {"people": {}},
            "Client Since": {"date": {}},
            "Annual Revenue": {"number": {"format": "dollar"}},
            "Website": {"url": {}},
            "Engagements": {"relation": {"data_source_id": sid("source", sid("database", "engagements"))}},
        },
    )
    db_engagements = database(
        ctx,
        sid("database", "engagements"),
        "Engagements",
        db_parent,
        {
            "Name": {"title": {}},
            "Client": {"relation": {"data_source_id": db_clients["_sources"][0]}},
            "Service": {"select": {}},
            "Lead": {"people": {}},
            "Start": {"date": {}},
            "End": {"date": {}},
            "Budget Hours": {"number": {}},
            "Status": {
                "status": {
                    "options": [{"name": s} for s in ("Planned", "In Progress", "Completed", "On Hold")]
                }
            },
        },
    )
    notes = database(
        ctx,
        sid("database", "meetings"),
        "Meeting Notes",
        db_parent,
        {
            "Name": {"title": {}},
            "Attendees": {"people": {}},
            "Date": {"date": {}},
            "Client": {"relation": {"data_source_id": db_clients["_sources"][0]}},
        },
    )
    runbooks = database(
        ctx,
        sid("database", "runbooks"),
        "IT Runbooks",
        {**roots["it"], "_internal": True},
        {
            "Name": {"title": {}},
            "Owner": {"people": {}},
            "Last Reviewed": {"date": {}},
            "Status": {"select": {"options": [{"name": "Published"}]}},
        },
    )
    for client in ctx.world.clients:
        row(
            ctx,
            db_clients,
            sid("client", client.id),
            {
                "Name": {"title": rich_text(client.name)},
                "Industry": {"select": {"name": client.industry}},
                "Status": {"status": {"name": client.status}},
                "Owner": {"people": [partial_user(person_id(client.owner_id))]},
                "Client Since": {"date": {"start": client.client_since.isoformat()}},
                "Annual Revenue": {"number": client.annual_revenue},
                "Website": {"url": client.website},
                "Engagements": {
                    "relation": [
                        {"id": sid("engagement", e.id)}
                        for e in ctx.world.engagements
                        if e.client_id == client.id
                    ]
                },
            },
        )
    for engagement in ctx.world.engagements:
        row(
            ctx,
            db_engagements,
            sid("engagement", engagement.id),
            {
                "Name": {"title": rich_text(engagement.name)},
                "Client": {"relation": [{"id": sid("client", engagement.client_id)}]},
                "Service": {"select": {"name": engagement.service}},
                "Lead": {"people": [partial_user(person_id(engagement.lead_id))]},
                "Start": {"date": {"start": engagement.start.isoformat()}},
                "End": {"date": {"start": engagement.end.isoformat()}},
                "Budget Hours": {"number": engagement.budget_hours},
                "Status": {"status": {"name": engagement.status}},
            },
        )
    for meeting in ctx.world.meetings:
        if not meeting.happened or meeting.kind != "client" or not meeting.client_id:
            continue
        item = row(
            ctx,
            notes,
            sid("meeting", meeting.id),
            {
                "Name": {"title": rich_text(meeting.title)},
                "Attendees": {
                    "people": [
                        partial_user(person_id(pid))
                        for pid in meeting.all_attendee_ids()
                        if ctx.store["users"].get(person_id(pid))
                    ]
                },
                "Date": {"date": {"start": meeting.start.isoformat(), "end": meeting.end.isoformat()}},
                "Client": {"relation": [{"id": sid("client", meeting.client_id)}]},
            },
        )
        for i, paragraph in enumerate(
            (
                "Agenda",
                meeting.description,
                "Summary",
                f"Client engagement discussion: {meeting.title}.",
                "Transcript excerpt",
                *(f"{ctx.world.person(t.speaker_id).display_name}: {t.text}" for t in meeting.transcript[:8]),
            )
        ):
            block(
                ctx,
                item,
                "heading_2" if paragraph in ("Agenda", "Summary", "Transcript excerpt") else "paragraph",
                {
                    "rich_text": rich_text(paragraph),
                    "color": "default",
                    **(
                        {"is_toggleable": False}
                        if paragraph in ("Agenda", "Summary", "Transcript excerpt")
                        else {}
                    ),
                },
                str(i),
            )
    for doc in ctx.world.documents_in("it"):
        item = row(
            ctx,
            runbooks,
            sid("runbook", doc.id),
            {
                "Name": {"title": rich_text(doc.title)},
                "Owner": {"people": [partial_user(person_id(doc.owner_id))]},
                "Last Reviewed": {
                    "date": {"start": (ctx.world.anchor - timedelta(days=7)).date().isoformat()}
                },
                "Status": {"select": {"name": "Published"}},
            },
        )
        for i, paragraph in enumerate(doc.paragraphs):
            block(ctx, item, "paragraph", {"rich_text": rich_text(paragraph), "color": "default"}, str(i))
