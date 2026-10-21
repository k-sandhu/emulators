"""Project the same Halcyon people, engagements and documents into Asana work."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from emulators.core.clock import isoformat_z
from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext
from emulators.providers.asana import common as c


def task_record(
    ctx: ProviderContext,
    identifier: str,
    name: str,
    workspace: dict[str, Any],
    creator: dict[str, Any],
    *,
    created: str | None = None,
) -> dict[str, Any]:
    at = created or c.stamp(ctx)
    return {
        "gid": identifier,
        "resource_type": "task",
        "resource_subtype": "default_task",
        "name": name,
        "notes": "",
        "html_notes": "<body></body>",
        "created_at": at,
        "modified_at": at,
        "completed": False,
        "completed_at": None,
        "completed_by": None,
        "assignee": None,
        "assignee_status": "inbox",
        "due_on": None,
        "due_at": None,
        "start_on": None,
        "start_at": None,
        "workspace": c.compact(workspace),
        "projects": [],
        "memberships": [],
        "parent": None,
        "followers": [c.compact(creator)],
        "tags": [],
        "dependencies": [],
        "custom_fields": [],
        "liked": False,
        "likes": [],
        "num_likes": 0,
        "is_rendered_as_separator": False,
        "created_by": c.compact(creator),
        "_creator": creator["gid"],
        "_deleted": False,
    }


def seed(ctx: ProviderContext) -> None:
    world = ctx.world
    workspace = c.put(
        ctx,
        "workspaces",
        {
            "gid": c.gid("workspace", world.org.domain),
            "resource_type": "workspace",
            "name": world.org.name,
            "is_organization": True,
            "email_domains": [world.org.domain],
        },
    )
    ctx.store.meta.update(
        workspace_gid=workspace["gid"], file_secret=stable_hash("asana", world.seed, "files", length=64)
    )
    for person in world.active_employees:
        user = c.put(
            ctx,
            "users",
            {
                "gid": c.gid("user", person.id),
                "resource_type": "user",
                "name": person.display_name,
                "email": person.email,
                "photo": None,
                "_world": person.id,
            },
        )
        ctx.tokens.issue_access(
            client_id="asana-pat",
            user_id=user["gid"],
            scopes=["default"],
            ttl=None,
            token=f"asana-dev-{person.id}",
        )
    for team in world.teams:
        c.put(
            ctx,
            "teams",
            {
                "gid": c.gid("team", team.id),
                "resource_type": "team",
                "name": team.name,
                "description": team.description,
                "html_description": "<body>" + team.description + "</body>",
                "organization": c.compact(workspace),
                "visibility": "public" if team.visibility == "Public" else "secret",
                "_members": [
                    c.gid("user", pid)
                    for pid in team.member_ids
                    if ctx.store["users"].get(c.gid("user", pid))
                ],
                "_owners": [c.gid("user", pid) for pid in team.owner_ids],
                "_world": team.id,
            },
        )
    for department in world.departments:
        members = [
            c.gid("user", person.id)
            for person in world.active_employees
            if person.department_id == department.id
        ]
        c.put(
            ctx,
            "teams",
            {
                "gid": c.gid("department", department.id),
                "resource_type": "team",
                "name": department.name,
                "description": department.description,
                "html_description": "<body>" + department.description + "</body>",
                "organization": c.compact(workspace),
                "visibility": "secret",
                "_members": members,
                "_owners": [c.gid("user", department.head_id)],
                "_world": department.id,
            },
        )
    fallback_team = ctx.store["teams"].values()[0]
    for engagement in world.engagements:
        lead = ctx.store["users"].get(c.gid("user", engagement.lead_id))
        team = ctx.store["teams"].get(c.gid("team", engagement.team_id or ""))
        if team is None:
            person = world.person(engagement.lead_id)
            team = ctx.store["teams"].get(c.gid("department", person.department_id or "")) or fallback_team
        project = c.put(
            ctx,
            "projects",
            {
                "gid": c.gid("project", engagement.id),
                "resource_type": "project",
                "name": world.client(engagement.client_id).name + " — " + engagement.name,
                "notes": f"{engagement.service} engagement for {world.client(engagement.client_id).name}.",
                "html_notes": "<body>" + engagement.name + "</body>",
                "workspace": c.compact(workspace),
                "team": c.compact(team),
                "owner": c.compact(lead),
                "members": [],
                "followers": [c.compact(lead)],
                "archived": engagement.status == "Completed",
                "public": False,
                "privacy_setting": "private_to_team",
                "color": "light-blue",
                "default_view": "list",
                "due_on": engagement.end.isoformat(),
                "due_date": engagement.end.isoformat(),
                "start_on": engagement.start.isoformat(),
                "created_at": isoformat_z(world.anchor - timedelta(days=180)),
                "modified_at": isoformat_z(world.anchor),
                "completed": engagement.status == "Completed",
                "completed_at": None,
                "current_status": None,
                "current_status_update": None,
                "custom_fields": [],
                "_world": engagement.id,
                "_members": list(
                    dict.fromkeys([lead["gid"], *[c.gid("user", pid) for pid in engagement.team_ids]])
                ),
                "_sections": [],
                "_task_order": [],
            },
        )
        project["members"] = [
            c.compact(u) for uid in project["_members"] if (u := ctx.store["users"].get(uid))
        ]
        for label in ("Planning", "In Progress", "Completed"):
            section = c.put(
                ctx,
                "sections",
                {
                    "gid": c.gid("section", engagement.id + ":" + label),
                    "resource_type": "section",
                    "name": label,
                    "created_at": project["created_at"],
                    "project": c.compact(project),
                },
            )
            project["_sections"].append(section["gid"])
    for index, document in enumerate(world.documents):
        owner = ctx.store["users"].get(c.gid("user", document.owner_id))
        if owner is None or document.trashed:
            continue
        task = task_record(
            ctx,
            c.gid("task", document.id),
            "Review " + document.title,
            workspace,
            owner,
            created=isoformat_z(document.created, timespec="milliseconds"),
        )
        task.update(
            notes="\n".join(document.paragraphs[:4]),
            assignee=c.compact(owner),
            modified_at=isoformat_z(document.modified, timespec="milliseconds"),
            _world=document.id,
        )
        task["followers"] = [c.compact(owner)]
        if document.restricted:
            for share in document.shares:
                for person_id in world.expand_principal(share.principal_id):
                    user = ctx.store["users"].get(c.gid("user", person_id))
                    if user and user["gid"] not in {u["gid"] for u in task["followers"]}:
                        task["followers"].append(c.compact(user))
        else:
            project = ctx.store["projects"].get(c.gid("project", document.engagement_id or ""))
            if project:
                task["projects"] = [c.compact(project)]
                section_id = project["_sections"][index % 3]
                task["memberships"] = [
                    {
                        "project": c.compact(project),
                        "section": c.compact(ctx.store["sections"].get(section_id)),
                    }
                ]
                task["due_on"] = project["due_on"]
                task["completed"] = section_id == project["_sections"][2]
                task["completed_at"] = task["modified_at"] if task["completed"] else None
                project["_task_order"].append(task["gid"])
        c.put(ctx, "tasks", task)
        c.put(
            ctx,
            "attachments",
            {
                "gid": c.gid("attachment", document.id),
                "resource_type": "attachment",
                "resource_subtype": "asana",
                "name": document.name,
                "created_at": task["created_at"],
                "host": "asana",
                "parent": c.compact(task),
                "size": 0,
                "_document": document.id,
                "_blob": None,
            },
        )
    for client in world.clients:
        c.put(
            ctx,
            "tags",
            {
                "gid": c.gid("tag", client.id),
                "resource_type": "tag",
                "name": client.name,
                "workspace": c.compact(workspace),
                "color": "light-blue",
                "notes": client.description,
            },
        )
        for task in ctx.store["tasks"]:
            document = world.document(task["_world"])
            if document.client_id == client.id:
                task["tags"].append(c.compact(ctx.store["tags"].get(c.gid("tag", client.id))))
