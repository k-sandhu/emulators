"""Project the shared organization into two relational Airtable bases without rendering files."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from emulators.core.clock import isoformat_z
from emulators.core.provider import ProviderContext

from .common import OWNER_TOKEN, READER_TOKEN, SCOPES, identifier


def seed(ctx: ProviderContext) -> None:
    world = ctx.world
    workspace_id = identifier(ctx, "wsp", "workspace")
    for person in world.employees:
        ctx.store.collection("users").put(
            {
                "id": identifier(ctx, "usr", person.id),
                "email": person.email,
                "name": person.display_name,
                "_world": person.id,
                "_active": person.active,
            }
        )
    for key, name in (("operations", "Halcyon Client Operations"), ("finance", "Halcyon Finance")):
        base_id = identifier(ctx, "app", key)
        acl = {}
        for person in world.active_employees:
            if key == "finance" and not (
                person.is_partner or person.is_admin or person.department_id == "finance"
            ):
                continue
            acl[identifier(ctx, "usr", person.id)] = (
                "create"
                if person.is_partner or person.is_admin
                else "read"
                if person.id == "p005"
                else "edit"
            )
        ctx.store.collection("bases").put(
            {
                "id": base_id,
                "name": name,
                "_workspace": workspace_id,
                "_acl": acl,
                "_key": key,
            }
        )
        table_names = ["Employees", "Clients", "Engagements", "Documents"]
        if key == "finance":
            table_names.append("Invoices")
        tables = {name: identifier(ctx, "tbl", key, name) for name in table_names}

        def field(name: str, kind: str = "singleLineText", **options: Any) -> dict[str, Any]:
            return {"name": name, "type": kind, **({"options": options} if options else {})}

        def link(
            name: str, target: str, inverse: str | None = None, *, tables: dict[str, str] = tables
        ) -> dict[str, Any]:
            options: dict[str, Any] = {
                "linkedTableId": tables[target],
                "isReversed": False,
                "prefersSingleRecordLink": False,
            }
            if inverse:
                options["_inverse_name"] = inverse
            return field(name, "multipleRecordLinks", **options)

        def select(name: str, values: list[str], *, key: str = key) -> dict[str, Any]:
            return field(
                name,
                "singleSelect",
                choices=[
                    {"id": identifier(ctx, "sel", key, name, value), "name": value, "color": "blueLight2"}
                    for value in values
                ],
            )

        schemas = {
            "Employees": [
                field("Name"),
                field("Email", "email"),
                field("Title"),
                field("Department"),
                field("Active", "checkbox", icon="check", color="greenBright"),
                link("Manager", "Employees", "Direct reports"),
                link("Direct reports", "Employees", "Manager"),
                field("User", "singleCollaborator"),
            ],
            "Clients": [
                field("Name"),
                field("Domain"),
                select("Status", ["Active", "Former", "Prospect"]),
                link("Owner", "Employees"),
                field("Email", "email"),
                field("Industry"),
                link("Engagements", "Engagements", "Client"),
                field("Notes", "multilineText"),
            ],
            "Engagements": [
                field("Name"),
                link("Client", "Clients", "Engagements"),
                link("Lead", "Employees"),
                link("Team", "Employees"),
                select("Status", ["Planned", "In Progress", "Completed", "On Hold"]),
                field("Start", "date", dateFormat={"name": "iso", "format": "YYYY-MM-DD"}),
                field("End", "date", dateFormat={"name": "iso", "format": "YYYY-MM-DD"}),
                field("Budget hours", "number", precision=0),
            ],
            "Documents": [
                field("Name"),
                field("Folder"),
                link("Owner", "Employees"),
                link("Client", "Clients"),
                link("Engagement", "Engagements"),
                field("Files", "multipleAttachments", isReversed=False),
            ],
            "Invoices": [
                field("Number"),
                link("Client", "Clients"),
                link("Engagement", "Engagements"),
                select("Status", ["Draft", "Open", "Paid", "Overdue", "Void", "Partial"]),
                field("Date", "date", dateFormat={"name": "iso", "format": "YYYY-MM-DD"}),
                field("Due", "date", dateFormat={"name": "iso", "format": "YYYY-MM-DD"}),
                field("Total", "currency", precision=2, symbol="$"),
                field("Paid", "currency", precision=2, symbol="$"),
                field(
                    "Balance",
                    "formula",
                    formula="{Total} - {Paid}",
                    isValid=True,
                    result={"type": "number", "options": {"precision": 2}},
                ),
            ],
        }
        for table_name in table_names:
            fields = schemas[table_name]
            for f in fields:
                f["id"] = identifier(ctx, "fld", key, table_name, f["name"])
                inverse = f.get("options", {}).pop("_inverse_name", None)
                if inverse:
                    target = next(n for n, tid in tables.items() if tid == f["options"]["linkedTableId"])
                    f["options"]["inverseLinkFieldId"] = identifier(ctx, "fld", key, target, inverse)
            views = [
                {"id": identifier(ctx, "viw", key, table_name, "all"), "name": "Grid view", "type": "grid"}
            ]
            if table_name == "Clients":
                views.append(
                    {
                        "id": identifier(ctx, "viw", key, table_name, "active"),
                        "name": "Active clients",
                        "type": "grid",
                        "_formula": "{Status}='Active'",
                    }
                )
            ctx.store.collection("tables").put(
                {
                    "id": tables[table_name],
                    "name": table_name,
                    "description": f"{world.org.name} {table_name.lower()}",
                    "primaryFieldId": fields[0]["id"],
                    "fields": fields,
                    "views": views,
                    "_base": base_id,
                }
            )

        def rec(table: str, world_id: str, *, key: str = key) -> str:
            return identifier(ctx, "rec", key, table, world_id)

        def add(
            table: str,
            world_id: str,
            values: dict[str, Any],
            *,
            key: str = key,
            base_id: str = base_id,
            tables: dict[str, str] = tables,
        ) -> None:
            normalized = {identifier(ctx, "fld", key, table, name): value for name, value in values.items()}
            ctx.store.collection("records").put(
                {
                    "id": rec(table, world_id),
                    "createdTime": isoformat_z(world.anchor - timedelta(days=30), timespec="milliseconds"),
                    "fields": normalized,
                    "_base": base_id,
                    "_table": tables[table],
                    "_world": world_id,
                }
            )

        for person in world.employees:
            add(
                "Employees",
                person.id,
                {
                    "Name": person.display_name,
                    "Email": person.email,
                    "Title": person.job_title,
                    "Department": world.department(person.department_id).name if person.department_id else "",
                    "Active": person.active,
                    "Manager": [rec("Employees", person.manager_id)] if person.manager_id else [],
                    "Direct reports": [rec("Employees", p.id) for p in world.direct_reports(person.id)],
                    "User": {
                        "id": identifier(ctx, "usr", person.id),
                        "email": person.email,
                        "name": person.display_name,
                    },
                },
            )
        for client in world.clients:
            add(
                "Clients",
                client.id,
                {
                    "Name": client.name,
                    "Domain": client.domain,
                    "Status": client.status,
                    "Owner": [rec("Employees", client.owner_id)],
                    "Email": world.person(client.primary_contact_id).email,
                    "Industry": client.industry,
                    "Notes": client.description,
                    "Engagements": [
                        rec("Engagements", e.id) for e in world.engagements if e.client_id == client.id
                    ],
                },
            )
        for engagement in world.engagements:
            add(
                "Engagements",
                engagement.id,
                {
                    "Name": engagement.name,
                    "Client": [rec("Clients", engagement.client_id)],
                    "Lead": [rec("Employees", engagement.lead_id)],
                    "Team": [rec("Employees", p) for p in engagement.team_ids],
                    "Status": engagement.status,
                    "Start": engagement.start.isoformat(),
                    "End": engagement.end.isoformat(),
                    "Budget hours": engagement.budget_hours,
                },
            )
        # A base is a shared permission boundary: never project personal/restricted file areas.
        for document in world.documents:
            if document.trashed or document.restricted or world.space(document.space_id).kind == "personal":
                continue
            if (key == "finance") != (document.space_id == "finance"):
                continue
            if key == "operations" and document.space_id not in {"clients", "knowledge", "shared"}:
                continue
            attachment_id = identifier(ctx, "att", key, document.id)
            ctx.store.collection("attachments").put(
                {
                    "id": attachment_id,
                    "filename": document.name,
                    "_world": document.id,
                    "_base": base_id,
                }
            )
            add(
                "Documents",
                document.id,
                {
                    "Name": document.name,
                    "Folder": document.folder,
                    "Owner": [rec("Employees", document.owner_id)],
                    "Client": [rec("Clients", document.client_id)] if document.client_id else [],
                    "Engagement": [rec("Engagements", document.engagement_id)]
                    if document.engagement_id
                    else [],
                    "Files": [{"id": attachment_id}],
                },
            )
        if key == "finance":
            for invoice in world.invoices:
                add(
                    "Invoices",
                    invoice.id,
                    {
                        "Number": invoice.number,
                        "Client": [rec("Clients", invoice.client_id)],
                        "Engagement": [rec("Engagements", invoice.engagement_id)]
                        if invoice.engagement_id
                        else [],
                        "Status": invoice.status,
                        "Date": invoice.issue_date.isoformat(),
                        "Due": invoice.due_date.isoformat(),
                        "Total": invoice.total,
                        "Paid": invoice.paid_amount,
                    },
                )
    for token, user in ((OWNER_TOKEN, "p001"), (READER_TOKEN, "p005")):
        seeded_grant = ctx.tokens.issue_access(
            token=token,
            client_id="airtable-pat",
            user_id=identifier(ctx, "usr", user),
            scopes=SCOPES,
            ttl=None,
        )
        ctx.store.collection("_grants").get(seeded_grant.token)["issued_at"] = world.anchor
    ctx.tokens.register_client(
        "airtable-emulator", "airtable-emulator-secret", redirect_uris=["https://consumer.example/callback"]
    )
