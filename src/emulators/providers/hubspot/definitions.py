"""Property and pipeline definitions shared by validation, seeding, and schema routes."""

from __future__ import annotations

from typing import Any

from emulators.core.provider import ProviderContext

from .common import ACTIVITIES, DEFAULTS, OBJECT_IDS, stamp

STAGES = [
    ("appointmentscheduled", "Appointment scheduled", "0.2"),
    ("qualifiedtobuy", "Qualified to buy", "0.4"),
    ("presentationscheduled", "Presentation scheduled", "0.6"),
    ("decisionmakerboughtin", "Decision maker bought-in", "0.8"),
    ("contractsent", "Contract sent", "0.9"),
    ("closedwon", "Closed won", "1.0"),
    ("closedlost", "Closed lost", "0.0"),
]
GROUPS = {
    "contacts": "contactinformation",
    "companies": "companyinformation",
    "deals": "dealinformation",
    "tickets": "ticketinformation",
    "products": "productinformation",
    "line_items": "line_item_information",
}
FIELDS: dict[str, dict[str, list[str]]] = {
    "contacts": {
        "string": [
            "email",
            "firstname",
            "lastname",
            "phone",
            "mobilephone",
            "jobtitle",
            "company",
            "website",
            "address",
            "city",
            "state",
            "zip",
            "country",
        ],
        "enumeration": ["lifecyclestage"],
    },
    "companies": {
        "string": [
            "name",
            "domain",
            "phone",
            "website",
            "industry",
            "description",
            "address",
            "city",
            "state",
            "zip",
            "country",
        ],
        "number": ["numberofemployees", "annualrevenue"],
        "enumeration": ["lifecyclestage"],
    },
    "deals": {
        "string": ["dealname", "description", "dealtype", "hs_next_step"],
        "number": ["amount", "hs_deal_stage_probability"],
        "datetime": ["closedate"],
        "enumeration": ["pipeline", "dealstage"],
    },
    "tickets": {
        "string": ["subject", "content", "source_type"],
        "enumeration": ["hs_pipeline", "hs_pipeline_stage", "hs_ticket_priority"],
        "datetime": ["closed_date"],
    },
    "products": {"string": ["name", "description", "hs_sku", "hs_product_type"], "number": ["price"]},
    "line_items": {
        "string": ["name", "description", "hs_product_id", "hs_sku"],
        "number": ["quantity", "price", "amount"],
    },
    "meetings": {
        "string": ["hs_meeting_title", "hs_meeting_body", "hs_meeting_external_url"],
        "datetime": ["hs_meeting_start_time", "hs_meeting_end_time"],
        "enumeration": ["hs_meeting_outcome"],
    },
    "emails": {
        "string": ["hs_email_subject", "hs_email_text", "hs_email_html", "hs_email_headers"],
        "enumeration": ["hs_email_direction", "hs_email_status"],
    },
    "notes": {"string": ["hs_note_body", "hs_attachment_ids"]},
    "tasks": {
        "string": ["hs_task_subject", "hs_task_body"],
        "enumeration": ["hs_task_status", "hs_task_priority", "hs_task_type"],
    },
    "calls": {
        "string": [
            "hs_call_title",
            "hs_call_body",
            "hs_call_from_number",
            "hs_call_to_number",
            "hs_call_recording_url",
        ],
        "number": ["hs_call_duration"],
        "enumeration": ["hs_call_status", "hs_call_direction"],
    },
}
OPTIONS = {
    "lifecyclestage": [
        "subscriber",
        "lead",
        "marketingqualifiedlead",
        "salesqualifiedlead",
        "opportunity",
        "customer",
        "evangelist",
        "other",
    ],
    "hs_ticket_priority": ["LOW", "MEDIUM", "HIGH"],
    "hs_meeting_outcome": ["SCHEDULED", "COMPLETED", "RESCHEDULED", "NO_SHOW", "CANCELED"],
    "hs_email_direction": ["EMAIL", "INCOMING_EMAIL", "FORWARDED_EMAIL"],
    "hs_email_status": ["BOUNCED", "FAILED", "SCHEDULED", "SENDING", "SENT"],
    "hs_task_status": ["NOT_STARTED", "IN_PROGRESS", "WAITING", "COMPLETED", "DEFERRED"],
    "hs_task_priority": ["LOW", "MEDIUM", "HIGH"],
    "hs_task_type": ["EMAIL", "CALL", "TODO"],
    "hs_call_status": [
        "BUSY",
        "CALLING_CRM_USER",
        "CANCELED",
        "COMPLETED",
        "CONNECTING",
        "FAILED",
        "IN_PROGRESS",
        "NO_ANSWER",
        "QUEUED",
        "RINGING",
    ],
    "hs_call_direction": ["INBOUND", "OUTBOUND"],
}


def property_shape(
    name: str,
    kind: str,
    type_: str = "string",
    *,
    custom: bool = False,
    options: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    values = options if options is not None else OPTIONS.get(name, [])
    return {
        "name": name,
        "label": name.replace("_", " ").title(),
        "type": type_,
        "fieldType": {
            "enumeration": "select",
            "number": "number",
            "bool": "booleancheckbox",
            "date": "date",
            "datetime": "date",
        }.get(type_, "text"),
        "groupName": GROUPS.get(kind, f"{kind}information"),
        "description": "",
        "options": [
            {"label": v.replace("_", " ").title(), "value": v, "displayOrder": i, "hidden": False}
            for i, v in enumerate(values)
        ],
        "displayOrder": -1,
        "calculated": False,
        "hasUniqueValue": name in ("hs_object_id", "email"),
        "hubspotDefined": not custom,
        "hidden": False,
        "formField": True,
        "archived": False,
        "modificationMetadata": {
            "archivable": custom,
            "readOnlyDefinition": not custom,
            "readOnlyValue": name
            in ("hs_object_id", "createdate", "hs_createdate", "lastmodifieddate", "hs_lastmodifieddate"),
        },
        **extra,
    }


def seed_definitions(ctx: ProviderContext) -> None:
    created = stamp(ctx.world.anchor)
    for kind in ("deals", "tickets"):
        stages = (
            STAGES
            if kind == "deals"
            else [
                ("1", "New", "0"),
                ("2", "Waiting on contact", "0"),
                ("3", "Waiting on us", "0"),
                ("4", "Closed", "1"),
            ]
        )
        for pipe_id, label in (
            [("default", "Sales Pipeline"), ("1000001", "Advisory Engagements")]
            if kind == "deals"
            else [("0", "Support Pipeline")]
        ):
            ctx.store[f"pipelines:{kind}"].put(
                {
                    "id": pipe_id,
                    "label": label,
                    "displayOrder": 0 if pipe_id in ("default", "0") else 1,
                    "createdAt": created,
                    "updatedAt": created,
                    "archived": False,
                    "stages": [
                        {
                            "id": str(10000001 + i) if pipe_id == "1000001" else stage,
                            "label": stage_label,
                            "displayOrder": i,
                            "createdAt": created,
                            "updatedAt": created,
                            "archived": False,
                            "metadata": {
                                "probability": probability,
                                "isClosed": str(stage in ("closedwon", "closedlost", "4")).lower(),
                            }
                            if kind == "deals"
                            else {"ticketState": "CLOSED" if stage == "4" else "OPEN"},
                        }
                        for i, (stage, stage_label, probability) in enumerate(stages)
                    ],
                }
            )
    for kind in OBJECT_IDS:
        group = GROUPS.get(kind, f"{kind}information")
        ctx.store[f"groups:{kind}"].put(
            {
                "name": group,
                "label": kind.replace("_", " ").title() + " information",
                "displayOrder": 0,
                "archived": False,
            },
            key="name",
        )
        fields = {**FIELDS[kind]}
        fields["datetime"] = [
            *fields.get("datetime", []),
            "createdate",
            "hs_createdate",
            "hs_lastmodifieddate",
            "lastmodifieddate",
        ]
        if kind in ACTIVITIES:
            fields["datetime"].append("hs_timestamp")
        fields["number"] = [*fields.get("number", []), "hs_object_id"]
        fields["string"] = [*fields.get("string", []), "hubspot_owner_id"]
        for type_, props in fields.items():
            for name in props:
                options = None
                if name in ("pipeline", "hs_pipeline"):
                    options = ctx.store[f"pipelines:{kind}"].ids()
                elif name in ("dealstage", "hs_pipeline_stage"):
                    options = [s["id"] for p in ctx.store[f"pipelines:{kind}"] for s in p["stages"]]
                ctx.store[f"properties:{kind}"].put(
                    property_shape(name, kind, type_, options=options), key="name"
                )
        if kind in ("contacts", "companies"):
            ctx.store[f"properties:{kind}"].put(
                property_shape("client_since", kind, "date", custom=True), key="name"
            )
            ctx.store[f"properties:{kind}"].put(
                property_shape(
                    "preferred_meeting_platform",
                    kind,
                    "enumeration",
                    custom=True,
                    options=["teams", "zoom", "meet"],
                ),
                key="name",
            )
        assert all(ctx.store[f"properties:{kind}"].get(p) for p in DEFAULTS[kind])
