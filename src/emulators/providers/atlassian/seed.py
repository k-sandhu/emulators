"""Project the shared firm into Confluence trees and Jira engagement work."""

from __future__ import annotations

import copy
import html
import json
import re
from datetime import datetime, timedelta

from emulators.core.clock import isoformat_z
from emulators.core.ids import stable_guid
from emulators.core.provider import ProviderContext
from emulators.world.model import Document, Space

from .common import Record, account_id, numeric_id

SPACE_KEYS = {
    "knowledge": "KB",
    "people": "PEOPLE",
    "it": "TECH",
    "growth": "GROWTH",
    "clients": "CLIENTS",
    "leadership": "PARTNERS",
    "finance": "FINANCE",
}


def adf(paragraphs: list[str]) -> Record:
    return {
        "version": 1,
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": p}]}
            if p
            else {"type": "paragraph", "content": []}
            for p in paragraphs
        ],
    }


def storage_body(title: str, paragraphs: list[str]) -> str:
    blocks = [f"<h2>{html.escape(title)}</h2>"]
    for paragraph in paragraphs:
        lines = paragraph.splitlines()
        if lines and all(line.startswith(("- ", "* ", "• ")) for line in lines):
            blocks.append("<ul>" + "".join(f"<li>{html.escape(line[2:])}</li>" for line in lines) + "</ul>")
        elif paragraph.startswith("## "):
            blocks.append(f"<h2>{html.escape(paragraph[3:])}</h2>")
        else:
            blocks.append("<p>" + html.escape(paragraph).replace("\n", "<br />") + "</p>")
    if "policy" in title.casefold() or "policies" in title.casefold():
        blocks.append(
            '<ac:structured-macro ac:name="info" ac:schema-version="1"><ac:rich-text-body>'
            "<p>Consult the policy owner before changing this guidance.</p>"
            "</ac:rich-text-body></ac:structured-macro>"
        )
    return "".join(blocks)


def bodies(title: str, paragraphs: list[str]) -> Record:
    return {
        "storage": {"value": storage_body(title, paragraphs), "representation": "storage"},
        "atlas_doc_format": {"value": json.dumps(adf(paragraphs)), "representation": "atlas_doc_format"},
    }


def space_roles(ctx: ProviderContext, space: Space) -> dict[str, str]:
    roles = {space.owner_id: "owner"}
    ranks = {"viewer": 0, "commenter": 1, "editor": 2, "owner": 3}
    for principal_id, role in space.members:
        for person_id in ctx.world.expand_principal(principal_id):
            if ranks.get(roles.get(person_id, "viewer"), 0) <= ranks[role]:
                roles[person_id] = role
    if space.id == "leadership":
        roles = {pid: role for pid, role in roles.items() if ctx.world.person(pid).is_partner}
    return roles


def document_restrictions(ctx: ProviderContext, doc: Document) -> tuple[list[str] | None, list[str] | None]:
    if not doc.restricted:
        return None, None
    readers = {doc.owner_id}
    editors = {doc.owner_id}
    for share in doc.shares:
        if share.principal_id:
            members = ctx.world.expand_principal(share.principal_id)
            readers.update(members)
            if share.role in ("owner", "editor"):
                editors.update(members)
        elif share.link in ("organization", "anyone"):
            readers.update(p.id for p in ctx.world.active_employees)
    return sorted(readers), sorted(editors)


def new_page(
    ctx: ProviderContext,
    *,
    page_id: str,
    space_id: str,
    title: str,
    owner_id: str,
    created: datetime,
    modified: datetime,
    paragraphs: list[str],
    parent_id: str | None = None,
    versions: int = 1,
    modified_by: str | None = None,
    content_type: str = "page",
) -> Record:
    author = account_id(owner_id)
    body = bodies(title, paragraphs)
    page: Record = {
        "id": page_id,
        "status": "current",
        "title": title,
        "spaceId": space_id,
        "position": len(ctx.store["pages"]),
        "authorId": author,
        "ownerId": author,
        "lastOwnerId": None,
        "createdAt": isoformat_z(created, timespec="milliseconds"),
        "body": body,
        "_type": content_type,
        "_read": None,
        "_update": None,
        "_labels": [],
        "_history": [],
    }
    if content_type == "page":
        page["subtype"] = "page"
    if parent_id:
        page.update({"parentId": parent_id, "parentType": "page"})
    for number in range(1, versions + 1):
        when = created + (modified - created) * ((number - 1) / (versions - 1)) if versions > 1 else modified
        version = {
            "number": number,
            "message": "Initial publication" if number == 1 else "Content reviewed",
            "minorEdit": False,
            "authorId": account_id(modified_by or owner_id),
            "createdAt": isoformat_z(when, timespec="milliseconds"),
        }
        page["_history"].append({"version": version, "title": title, "body": copy.deepcopy(body)})
    page["version"] = copy.deepcopy(page["_history"][-1]["version"])
    ctx.store["pages"].put(page)
    return page


def seed_confluence(ctx: ProviderContext) -> None:
    anchor = ctx.world.anchor
    for person in ctx.world.employees:
        uid = account_id(person.id)
        ctx.store["users"].put(
            {
                "id": uid,
                "accountId": uid,
                "displayName": person.display_name,
                "publicName": person.display_name,
                "email": person.email,
                "timeZone": person.timezone,
                "accountStatus": "active" if person.active else "inactive",
                "accountType": "atlassian",
                "isExternalCollaborator": False,
                "personalSpaceId": numeric_id("space", f"personal-{person.id}") if person.active else None,
                "_world_id": person.id,
            }
        )
    group_names = {"g-all": "confluence-users"}
    for group in ctx.world.groups:
        ctx.store["groups"].put(
            {
                "id": stable_guid("atlassian", "group", group.id),
                "name": group_names.get(group.id, group.name.lower().replace(" ", "-")),
                "_world_id": group.id,
                "_members": list(group.member_ids),
            }
        )
    for space in ctx.world.spaces:
        sid = numeric_id("space", space.id)
        key = SPACE_KEYS.get(space.id, "~" + account_id(space.owner_id).replace(":", ""))
        home_id = numeric_id("home", space.id)
        permissions = []
        for principal_id, role in space.members:
            if space.id == "leadership" and principal_id != "g-partners":
                continue
            group = ctx.store["groups"].first(_world_id=principal_id)
            who = (
                {"type": "group", "id": group["id"]}
                if group
                else {"type": "user", "id": account_id(principal_id)}
            )
            ops = [("read", "space")]
            if role in ("owner", "editor"):
                ops += [
                    (op, target)
                    for op in ("create", "update", "delete")
                    for target in ("page", "blogpost", "comment", "attachment")
                ]
            if role == "owner":
                ops.append(("administer", "space"))
            for op, target in ops:
                permissions.append(
                    {
                        "id": numeric_id("permission", space.id, principal_id, op, target),
                        "principal": who,
                        "operation": {"key": op, "targetType": target},
                    }
                )
        ctx.store["spaces"].put(
            {
                "id": sid,
                "key": key,
                "name": space.name,
                "type": "personal" if space.kind == "personal" else "global",
                "status": "current",
                "authorId": account_id(space.owner_id),
                "spaceOwnerId": account_id(space.owner_id),
                "createdAt": isoformat_z(anchor - timedelta(days=700), timespec="milliseconds"),
                "homepageId": home_id,
                "description": {"plain": {"value": space.description, "representation": "plain"}},
                "_world_id": space.id,
                "_roles": space_roles(ctx, space),
                "_permissions": permissions,
            }
        )
        new_page(
            ctx,
            page_id=home_id,
            space_id=sid,
            title=space.name,
            owner_id=space.owner_id,
            created=anchor - timedelta(days=700),
            modified=anchor - timedelta(days=12),
            paragraphs=[space.description],
        )
        for folder in ctx.world.folders_in(space.id):
            parent_folder = folder.rpartition("/")[0]
            new_page(
                ctx,
                page_id=numeric_id("folder", space.id, folder),
                space_id=sid,
                title=folder.rsplit("/", 1)[-1],
                owner_id=space.owner_id,
                created=anchor - timedelta(days=600),
                modified=anchor - timedelta(days=15),
                paragraphs=[f"{folder} — {space.description}"],
                parent_id=numeric_id("folder", space.id, parent_folder) if parent_folder else home_id,
            )
        prop_id = numeric_id("property", sid)
        ctx.store["properties"].put(
            {
                "id": prop_id,
                "key": "halcyon.area",
                "value": space.id,
                "createdAt": isoformat_z(anchor),
                "createdBy": account_id(space.owner_id),
                "version": {
                    "number": 1,
                    "createdAt": isoformat_z(anchor),
                    "createdBy": account_id(space.owner_id),
                    "message": "",
                },
                "_resource": "spaces",
                "_parent": sid,
            }
        )
    for doc in ctx.world.documents:
        sid = numeric_id("space", doc.space_id)
        page = new_page(
            ctx,
            page_id=numeric_id("document", doc.id),
            space_id=sid,
            title=doc.title,
            owner_id=doc.owner_id,
            # Some world edits are later on the anchor day, after this seed's instant.
            created=min(doc.created, anchor),
            modified=min(doc.modified, anchor),
            modified_by=doc.modified_by_id,
            paragraphs=list(doc.paragraphs),
            versions=doc.versions,
            parent_id=numeric_id("folder", doc.space_id, doc.folder)
            if doc.folder
            else numeric_id("home", doc.space_id),
        )
        page["_world_id"] = doc.id
        page["_read"], page["_update"] = document_restrictions(ctx, doc)
        page["status"] = "trashed" if doc.trashed else "current"
        area = re.sub(r"[^a-z0-9]+", "-", doc.space_id.lower()).strip("-")
        names = [area, "client-engagement" if doc.client_id else "internal"]
        page["_labels"] = [
            {"id": numeric_id("label", name), "name": name, "prefix": "global"} for name in names
        ]
        if doc.kind in ("xlsx", "pptx", "pdf", "csv", "png"):
            ctx.store["attachments"].put(
                {
                    "id": "att" + numeric_id("attachment", doc.id),
                    "status": "current",
                    "title": doc.name,
                    "createdAt": page["createdAt"],
                    "pageId": page["id"],
                    "fileId": stable_guid("attachment-file", doc.id),
                    "comment": doc.description,
                    "version": copy.deepcopy(page["version"]),
                    "_world_id": doc.id,
                    "_blob": None,
                }
            )
        peers = [
            pid
            for pid in ctx.store["spaces"].get(sid)["_roles"]
            if pid != doc.owner_id and (page["_read"] is None or pid in page["_read"])
        ]
        if peers:
            ctx.store["comments"].put(
                {
                    "id": numeric_id("comment", doc.id),
                    "status": "current",
                    "pageId": page["id"],
                    "title": "Re: " + doc.title,
                    "body": bodies("", [f"Reviewed {doc.title}. Please use the current version."]),
                    "version": {
                        "number": 1,
                        "message": "",
                        "minorEdit": False,
                        "createdAt": page["version"]["createdAt"],
                        "authorId": account_id(peers[0]),
                    },
                }
            )
    clients_space = numeric_id("space", "clients")
    for meeting in ctx.world.meetings:
        if meeting.kind != "client" or not meeting.client_id or not meeting.happened or meeting.cancelled:
            continue
        client = ctx.world.client(meeting.client_id)
        folder = client.name + "/Meeting Notes"
        client_page = numeric_id("folder", "clients", client.name)
        if not ctx.store["pages"].get(client_page):
            new_page(
                ctx,
                page_id=client_page,
                space_id=clients_space,
                title=client.name,
                owner_id=client.owner_id,
                created=anchor - timedelta(days=600),
                modified=meeting.start,
                paragraphs=[client.description],
                parent_id=numeric_id("home", "clients"),
            )
        folder_id = numeric_id("folder", "clients", folder)
        if not ctx.store["pages"].get(folder_id):
            new_page(
                ctx,
                page_id=folder_id,
                space_id=clients_space,
                title="Meeting Notes",
                owner_id=client.owner_id,
                created=anchor - timedelta(days=600),
                modified=meeting.start,
                paragraphs=[client.name],
                parent_id=client_page,
            )
        paragraphs = [
            meeting.description,
            "Attendees: "
            + ", ".join(ctx.world.person(pid).display_name for pid in meeting.all_attendee_ids()),
        ]
        paragraphs += [
            ctx.world.person(turn.speaker_id).display_name + ": " + turn.text for turn in meeting.transcript
        ]
        page = new_page(
            ctx,
            page_id=numeric_id("meeting", meeting.id),
            space_id=clients_space,
            title=f"{meeting.start.date()} — {meeting.title}",
            owner_id=meeting.organizer_id,
            created=meeting.start,
            modified=meeting.end,
            paragraphs=paragraphs,
            parent_id=folder_id,
        )
        page["_labels"] = [
            {"id": numeric_id("label", "meeting-notes"), "name": "meeting-notes", "prefix": "global"}
        ]
    for index, space_id in enumerate(("knowledge", "people", "it")):
        space = ctx.world.space(space_id)
        new_page(
            ctx,
            page_id=numeric_id("blog", space_id),
            space_id=numeric_id("space", space_id),
            title=f"{space.name} — October update",
            owner_id=space.owner_id,
            created=anchor - timedelta(days=index + 1),
            modified=anchor - timedelta(days=index + 1),
            paragraphs=[space.description],
            content_type="blogpost",
        )


STATUSES = {"To Do": "10000", "In Progress": "3", "Done": "10001"}
ISSUE_TYPES = {"Task": "10001", "Epic": "10000", "Bug": "10004", "Sub-task": "10003"}
PRIORITIES = {"Highest": "1", "High": "2", "Medium": "3", "Low": "4", "Lowest": "5"}


def status_shape(name: str) -> Record:
    category = {
        "To Do": (2, "new", "blue-gray"),
        "In Progress": (4, "indeterminate", "yellow"),
        "Done": (3, "done", "green"),
    }[name]
    return {
        "id": STATUSES[name],
        "name": name,
        "description": "",
        "statusCategory": {"id": category[0], "key": category[1], "colorName": category[2], "name": name},
    }


def jira_user(ctx: ProviderContext, user_id: str | None) -> Record | None:
    user = ctx.store["users"].get(user_id)
    if not user:
        return None
    return {
        "accountId": user["id"],
        "accountType": "atlassian",
        "displayName": user["displayName"],
        "emailAddress": user["email"],
        "active": user["accountStatus"] == "active",
        "timeZone": user["timeZone"],
    }


def seed_jira(ctx: ProviderContext) -> None:
    for key, name in (("OPS", "Internal Operations"), ("ENG", "Client Engagements")):
        roles = (
            {p.id: "editor" for p in ctx.world.active_employees}
            if key == "OPS"
            else space_roles(ctx, ctx.world.space("clients"))
        )
        if key == "OPS":
            roles[ctx.world.space("it").owner_id] = "owner"
        ctx.store["projects"].put(
            {
                "id": numeric_id("project", key),
                "key": key,
                "name": name,
                "projectTypeKey": "software",
                "simplified": False,
                "style": "classic",
                "isPrivate": False,
                "_roles": roles,
            }
        )

    def add(
        project: str,
        summary: str,
        paragraphs: list[str],
        owner: str,
        reporter: str,
        status: str,
        created: datetime,
        *,
        issue_type: str = "Task",
        parent: Record | None = None,
        labels: list[str] | None = None,
        due: str | None = None,
        priority: str = "Medium",
        readers: list[str] | None = None,
    ) -> Record:
        count = ctx.store.meta.get("jira:" + project, 0) + 1
        ctx.store.meta["jira:" + project] = count
        key = f"{project}-{count}"
        fields: Record = {
            "summary": summary,
            "description": adf(paragraphs),
            "status": status_shape(status),
            "assignee": jira_user(ctx, account_id(owner)),
            "reporter": jira_user(ctx, account_id(reporter)),
            "priority": {"id": PRIORITIES[priority], "name": priority},
            "labels": labels or [],
            "created": isoformat_z(created, timespec="milliseconds").replace("Z", "+0000"),
            "updated": isoformat_z(created + timedelta(days=1), timespec="milliseconds").replace(
                "Z", "+0000"
            ),
            "duedate": due,
            "issuetype": {"id": ISSUE_TYPES[issue_type], "name": issue_type, "subtask": False},
            "project": {
                k: v for k, v in ctx.store["projects"].first(key=project).items() if not k.startswith("_")
            },
        }
        if parent:
            fields["parent"] = {
                "id": parent["id"],
                "key": parent["key"],
                "fields": {
                    "summary": parent["fields"]["summary"],
                    "issuetype": parent["fields"]["issuetype"],
                },
            }
        issue = {"id": numeric_id("issue", key), "key": key, "fields": fields, "_read": readers}
        ctx.store["issues"].put(issue)
        return issue

    for eng in ctx.world.engagements[:15]:
        created = datetime.combine(eng.start, datetime.min.time(), tzinfo=ctx.world.anchor.tzinfo)
        status = (
            "Done" if eng.status == "Completed" else "In Progress" if eng.status == "In Progress" else "To Do"
        )
        epic = add(
            "ENG",
            eng.name,
            [ctx.world.client(eng.client_id).description, eng.name],
            eng.lead_id,
            eng.lead_id,
            status,
            created,
            issue_type="Epic",
            labels=[eng.service.lower().replace(" ", "-")],
            due=eng.end.isoformat(),
        )
        readers = sorted(
            {eng.lead_id, *eng.team_ids, *(p.id for p in ctx.world.active_employees if p.is_partner)}
        )
        for index, task in enumerate(("PBC request list", "Fieldwork review")):
            add(
                "ENG",
                f"{task} — {ctx.world.client(eng.client_id).name}",
                [eng.name, task],
                eng.team_ids[index % len(eng.team_ids)] if eng.team_ids else eng.lead_id,
                eng.lead_id,
                status if index else "To Do",
                created + timedelta(days=index + 2),
                parent=epic,
                labels=["pbc" if index == 0 else "fieldwork"],
                due=eng.end.isoformat(),
                readers=readers if index == 1 else None,
            )
    for doc in [d for d in ctx.world.documents if d.space_id == "it"][:5]:
        add(
            "OPS",
            "Review runbook: " + doc.title,
            list(doc.paragraphs),
            doc.owner_id,
            doc.modified_by_id,
            "In Progress",
            doc.modified,
            labels=["runbook", "technology"],
        )
    for case in ctx.world.cases:
        add(
            "OPS",
            case.subject,
            [case.description],
            case.owner_id,
            case.owner_id,
            "Done" if case.status == "Closed" else "In Progress" if case.status == "Working" else "To Do",
            case.created,
            labels=["support"],
            priority=case.priority,
        )
    for issue in ctx.store["issues"]:
        author = issue["fields"]["reporter"]
        ctx.store["jira_comments"].put(
            {
                "id": numeric_id("jira-comment", issue["key"]),
                "_issue": issue["id"],
                "author": author,
                "updateAuthor": author,
                "body": adf(["Please track progress and blockers here."]),
                "created": issue["fields"]["created"],
                "updated": issue["fields"]["updated"],
            }
        )


def seed(ctx: ProviderContext) -> None:
    ctx.tokens.register_client("emulator-client", "emulator-secret")
    seed_confluence(ctx)
    seed_jira(ctx)
