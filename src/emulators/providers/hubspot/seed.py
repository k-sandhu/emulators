"""Project the shared firm into CRM objects without generating binary content."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from emulators.core.provider import ProviderContext

from .associations import add, default_type, seed_types
from .common import OBJECT_IDS, PRIVATE_TOKEN, all_scopes, numeric_id, stamp
from .definitions import seed_definitions

STAGE_MAP = {
    "Qualification": "qualifiedtobuy",
    "Needs Analysis": "presentationscheduled",
    "Proposal/Price Quote": "decisionmakerboughtin",
    "Negotiation/Review": "contractsent",
    "Closed Won": "closedwon",
    "Closed Lost": "closedlost",
}


def date_stamp(value: date) -> str:
    return stamp(datetime.combine(value, datetime.min.time(), tzinfo=UTC))


def record(
    ctx: ProviderContext, kind: str, world_id: str, props: dict[str, Any], created: datetime | None = None
) -> dict[str, Any]:
    rid = numeric_id(kind, world_id)
    now = stamp(created or ctx.world.anchor)
    properties = {k: str(v) for k, v in props.items() if v is not None}
    properties.update(
        {
            "createdate": now,
            "hs_createdate": now,
            "hs_lastmodifieddate": now,
            "lastmodifieddate": now,
            "hs_object_id": rid,
        }
    )
    result = {
        "id": rid,
        "properties": properties,
        "createdAt": now,
        "updatedAt": now,
        "archived": False,
        "_world_id": world_id,
        "_history": {
            k: [{"value": v, "timestamp": now, "sourceType": "CRM_UI", "sourceId": "0"}]
            for k, v in properties.items()
        },
    }
    ctx.store[kind].put(result)
    return result


def link(ctx: ProviderContext, src: str, sid: str, dst: str, did: str, *, primary: bool = False) -> None:
    add(
        ctx,
        src,
        numeric_id(src, sid),
        dst,
        numeric_id(dst, did),
        [default_type(ctx, src, dst, primary=primary)],
    )


def seed(ctx: ProviderContext) -> None:
    world = ctx.world
    ctx.store.meta["object_ids"] = dict(OBJECT_IDS)
    seed_definitions(ctx)
    seed_types(ctx)
    now = stamp(world.anchor)
    seats = [
        p
        for p in world.employees
        if p.is_partner
        or p.is_admin
        or p.department_id == "growth"
        or "Manager" in p.job_title
        or any(d.owner_id == p.id for d in world.deals)
        or any(c.owner_id == p.id for c in world.cases)
    ]
    for person in seats:
        department = next((d for d in world.departments if d.id == person.department_id), None)
        ctx.store["owners"].put(
            {
                "id": numeric_id("owners", person.id),
                "email": person.email,
                "firstName": person.first_name,
                "lastName": person.last_name,
                "userId": int(numeric_id("users", person.id)) if person.active else None,
                "userIdIncludingInactive": int(numeric_id("users", person.id)),
                "type": "PERSON",
                "createdAt": now,
                "updatedAt": now,
                "archived": not person.active,
                "teams": [
                    {"id": numeric_id("teams", department.id), "name": department.name, "primary": True}
                ]
                if department
                else [],
                "_world_id": person.id,
                "_owned_only": person.department_id == "growth"
                and person.id != (department.head_id if department else None)
                and "Manager" not in person.job_title,
            }
        )
    ctx.tokens.issue_access(
        client_id="emulator-private-app",
        user_id=None,
        app_only=True,
        scopes=all_scopes(),
        token=PRIVATE_TOKEN,
        ttl=None,
    )
    for client in world.clients:
        props = {
            "name": client.name,
            "domain": client.domain,
            "industry": client.industry,
            "numberofemployees": client.employees,
            "annualrevenue": client.annual_revenue,
            "city": client.address.city,
            "state": client.address.state,
            "country": client.address.country,
            "address": client.address.street,
            "zip": client.address.postal_code,
            "phone": client.phone,
            "website": client.website,
            "description": client.description,
            "lifecyclestage": "opportunity" if client.status == "Prospect" else "customer",
            "hubspot_owner_id": numeric_id("owners", client.owner_id),
            "client_since": client.client_since.isoformat(),
            "preferred_meeting_platform": client.preferred_platform,
        }
        record(ctx, "companies", client.id, props)
        for contact_id in client.contact_ids:
            person = world.person(contact_id)
            record(
                ctx,
                "contacts",
                person.id,
                {
                    "email": person.email,
                    "firstname": person.first_name,
                    "lastname": person.last_name,
                    "phone": person.phone_business,
                    "mobilephone": person.phone_mobile,
                    "jobtitle": person.job_title,
                    "company": client.name,
                    **{
                        k: props[k]
                        for k in (
                            "city",
                            "state",
                            "country",
                            "lifecyclestage",
                            "hubspot_owner_id",
                            "client_since",
                            "preferred_meeting_platform",
                        )
                    },
                },
            )
            link(ctx, "contacts", person.id, "companies", client.id, primary=True)
    for lead in world.leads:
        company_key = "lead:" + lead.company.casefold()
        if not ctx.store["companies"].get(numeric_id("companies", company_key)):
            record(
                ctx,
                "companies",
                company_key,
                {
                    "name": lead.company,
                    "domain": lead.email.split("@")[1],
                    "industry": lead.industry,
                    "city": lead.city,
                    "state": lead.state,
                    "country": lead.country,
                    "annualrevenue": lead.annual_revenue,
                    "numberofemployees": lead.employees,
                    "lifecyclestage": "lead",
                    "hubspot_owner_id": numeric_id("owners", lead.owner_id),
                },
                lead.created,
            )
        record(
            ctx,
            "contacts",
            lead.id,
            {
                "email": lead.email,
                "firstname": lead.first_name,
                "lastname": lead.last_name,
                "phone": lead.phone,
                "jobtitle": lead.title,
                "company": lead.company,
                "city": lead.city,
                "state": lead.state,
                "country": lead.country,
                "lifecyclestage": "lead",
                "hubspot_owner_id": numeric_id("owners", lead.owner_id),
            },
            lead.created,
        )
        link(ctx, "contacts", lead.id, "companies", company_key, primary=True)
    for deal in world.deals:
        record(
            ctx,
            "deals",
            deal.id,
            {
                "dealname": deal.name,
                "amount": format(deal.amount, ".2f"),
                "closedate": date_stamp(deal.close_date),
                "pipeline": "default",
                "dealstage": STAGE_MAP[deal.stage],
                "hubspot_owner_id": numeric_id("owners", deal.owner_id),
                "hs_next_step": deal.next_step,
                "hs_deal_stage_probability": str(deal.probability / 100),
                "dealtype": "newbusiness" if deal.type == "New Business" else "existingbusiness",
            },
            deal.created,
        )
        link(ctx, "deals", deal.id, "companies", deal.client_id, primary=True)
        for contact in world.client(deal.client_id).contact_ids:
            link(ctx, "deals", deal.id, "contacts", contact)
    for case in world.cases:
        record(
            ctx,
            "tickets",
            case.id,
            {
                "subject": case.subject,
                "content": case.description,
                "hs_pipeline": "0",
                "hs_pipeline_stage": {"New": "1", "Working": "3", "Escalated": "3", "Closed": "4"}[
                    case.status
                ],
                "hs_ticket_priority": case.priority.upper(),
                "source_type": case.origin.upper(),
                "hubspot_owner_id": numeric_id("owners", case.owner_id),
                "closed_date": stamp(case.closed) if case.closed else None,
            },
            case.created,
        )
        link(ctx, "tickets", case.id, "contacts", case.contact_id)
        link(ctx, "tickets", case.id, "companies", case.client_id)
    for item in world.items:
        record(
            ctx,
            "products",
            item.id,
            {
                "name": item.name,
                "description": item.description,
                "price": item.rate,
                "hs_sku": item.sku,
                "hs_product_type": "service",
            },
        )
    for deal in world.deals:
        if not deal.is_won:
            continue
        engagement = world.engagement(deal.engagement_id) if deal.engagement_id else None
        item = next(
            (i for i in world.items if engagement and i.name == engagement.service_item), world.items[0]
        )
        key = deal.id + ":" + item.id
        record(
            ctx,
            "line_items",
            key,
            {
                "name": item.name,
                "description": item.description,
                "quantity": 1,
                "price": deal.amount,
                "amount": deal.amount,
                "hs_product_id": numeric_id("products", item.id),
                "hs_sku": item.sku,
                "hubspot_owner_id": numeric_id("owners", deal.owner_id),
            },
        )
        link(ctx, "deals", deal.id, "line_items", key)
    for meeting in world.meetings:
        if not meeting.client_id:
            continue
        record(
            ctx,
            "meetings",
            meeting.id,
            {
                "hs_meeting_title": meeting.title,
                "hs_meeting_body": meeting.description,
                "hs_timestamp": stamp(meeting.start),
                "hs_meeting_start_time": stamp(meeting.start),
                "hs_meeting_end_time": stamp(meeting.end),
                "hs_meeting_outcome": "CANCELED"
                if meeting.cancelled
                else "COMPLETED"
                if meeting.happened
                else "SCHEDULED",
                "hubspot_owner_id": numeric_id("owners", world.client(meeting.client_id).owner_id),
            },
            meeting.start,
        )
        activity_links(ctx, "meetings", meeting.id, meeting.client_id)
    for thread in world.email_threads:
        if not thread.client_id:
            continue
        for message in thread.messages:
            sender = world.person(message.from_id)
            record(
                ctx,
                "emails",
                message.id,
                {
                    "hs_email_subject": message.subject,
                    "hs_email_text": message.body,
                    "hs_email_direction": "INCOMING_EMAIL" if sender.is_external else "EMAIL",
                    "hs_email_status": "SENT",
                    "hs_timestamp": stamp(message.sent),
                    "hubspot_owner_id": numeric_id("owners", world.client(thread.client_id).owner_id),
                },
                message.sent,
            )
            activity_links(ctx, "emails", message.id, thread.client_id)
    for client in world.clients:
        owner = numeric_id("owners", client.owner_id)
        record(
            ctx,
            "notes",
            client.id,
            {"hs_note_body": client.description, "hs_timestamp": now, "hubspot_owner_id": owner},
        )
        activity_links(ctx, "notes", client.id, client.id)
        deal = next((d for d in world.deals if d.client_id == client.id and not d.is_closed), None)
        if deal:
            record(
                ctx,
                "tasks",
                deal.id,
                {
                    "hs_task_subject": deal.next_step or deal.name,
                    "hs_task_body": deal.name,
                    "hs_task_status": "NOT_STARTED",
                    "hs_task_type": "TODO",
                    "hs_task_priority": "MEDIUM",
                    "hs_timestamp": date_stamp(deal.close_date),
                    "hubspot_owner_id": numeric_id("owners", deal.owner_id),
                },
            )
            activity_links(ctx, "tasks", deal.id, client.id)
    for case in world.cases:
        if case.origin == "Phone":
            record(
                ctx,
                "calls",
                case.id,
                {
                    "hs_call_title": case.subject,
                    "hs_call_body": case.description,
                    "hs_call_status": "COMPLETED",
                    "hs_call_direction": "INBOUND",
                    "hs_timestamp": stamp(case.created),
                    "hs_call_from_number": world.person(case.contact_id).phone_business,
                    "hs_call_to_number": world.org.phone,
                    "hs_call_duration": "300000",
                    "hubspot_owner_id": numeric_id("owners", case.owner_id),
                },
                case.created,
            )
            activity_links(ctx, "calls", case.id, case.client_id)
    for kind in OBJECT_IDS:
        ctx.store[kind]


def activity_links(ctx: ProviderContext, kind: str, world_id: str, client_id: str) -> None:
    link(ctx, kind, world_id, "companies", client_id)
    for contact_id in ctx.world.client(client_id).contact_ids:
        link(ctx, kind, world_id, "contacts", contact_id)
    for deal in ctx.world.deals:
        if deal.client_id == client_id:
            link(ctx, kind, world_id, "deals", deal.id)
