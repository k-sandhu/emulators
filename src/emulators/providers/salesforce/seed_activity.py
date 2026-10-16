"""Seed activities: events for client meetings, tasks for client emails and open follow-ups."""

from __future__ import annotations

from datetime import date, datetime, timedelta

from emulators.providers.salesforce.common import fmt_dt, parse_dt
from emulators.providers.salesforce.org import Record
from emulators.providers.salesforce.seedlib import SeedState, stamp
from emulators.world.model import Meeting, World

PLATFORM_NAMES = {"teams": "Microsoft Teams", "zoom": "Zoom", "meet": "Google Meet"}


def seed(state: SeedState, world: World) -> None:
    seed_events(state, world)
    seed_email_tasks(state, world)
    seed_follow_ups(state, world)
    rollup_last_activity(state, world)


def _employee_for(state: SeedState, world: World, ids: list[str]) -> str:
    for pid in ids:
        if pid in state.user_by_person:
            return state.user_by_person[pid]
    return state.admin_id


def _account_of(state: SeedState, what_id: str | None) -> str | None:
    if not what_id:
        return None
    found = state.org.find(what_id)
    if not found:
        return None
    obj, rec = found
    if obj.name == "Account":
        return rec["Id"]
    return rec.get("AccountId") or rec.get("Account__c")


# -- events ------------------------------------------------------------------------------


def seed_events(state: SeedState, world: World) -> None:
    anchor = world.anchor
    deal_by_engagement = {d.engagement_id: d for d in world.deals if d.engagement_id}
    for m in world.meetings:
        if not m.client_id or m.cancelled or m.client_id not in state.account_by_client:
            continue
        _event(state, world, m, deal_by_engagement, anchor)


def _event(state: SeedState, world: World, m: Meeting, deals: dict, anchor: datetime) -> None:
    attendees = [m.organizer_id, *m.attendee_ids, *m.optional_ids]
    owner = _employee_for(state, world, [a for a in attendees if a in state.user_by_person])
    who = next((state.contact_by_person[a] for a in attendees if a in state.contact_by_person), None)
    account_id = state.account_by_client[m.client_id]
    deal = deals.get(m.engagement_id) if m.engagement_id else None
    what = state.opp_by_deal.get(deal.id) if deal else None
    created = min(m.start - timedelta(days=7), anchor - timedelta(days=1))
    rec = state.new(
        "Event",
        WhoId=who,
        WhatId=what or account_id,
        Subject=m.title,
        Location=m.location or PLATFORM_NAMES.get(m.platform),
        IsAllDayEvent=False,
        ActivityDateTime=fmt_dt(m.start),
        ActivityDate=m.start.date().isoformat(),
        DurationInMinutes=m.duration_minutes,
        StartDateTime=fmt_dt(m.start),
        EndDateTime=fmt_dt(m.end),
        EndDate=m.end.date().isoformat(),
        Description=m.description or None,
        AccountId=account_id,
        OwnerId=owner,
        Type="Meeting",
        IsPrivate=False,
        ShowAs="Busy",
        EventSubtype="Event",
        IsReminderSet=False,
    )
    stamp(rec, created, modified=created, created_by=owner)
    state.save("Event", rec)


# -- tasks -------------------------------------------------------------------------------


def seed_email_tasks(state: SeedState, world: World) -> None:
    for thread in world.email_threads:
        if thread.category != "client" or thread.client_id not in state.account_by_client:
            continue
        account_id = state.account_by_client[thread.client_id]
        for msg in thread.messages:
            participants = [msg.from_id, *msg.to_ids, *msg.cc_ids]
            owner = _employee_for(state, world, participants)
            who = next(
                (state.contact_by_person[p] for p in participants if p in state.contact_by_person), None
            )
            sent = min(msg.sent, world.anchor)
            rec = state.new(
                "Task",
                WhoId=who,
                WhatId=account_id,
                Subject=f"Email: {thread.subject}",
                ActivityDate=sent.date().isoformat(),
                Status="Completed",
                Priority="Normal",
                OwnerId=owner,
                Description=msg.body,
                AccountId=account_id,
                IsClosed=True,
                TaskSubtype="Email",
                CompletedDateTime=fmt_dt(sent),
            )
            stamp(rec, sent, created_by=owner)
            state.save("Task", rec)


def seed_follow_ups(state: SeedState, world: World) -> None:
    anchor = world.anchor
    today = anchor.date()
    open_deals = [d for d in world.deals if not d.is_closed and d.id in state.opp_by_deal]
    for i, d in enumerate(open_deals):
        opp_id = state.opp_by_deal[d.id]
        client = world.client(d.client_id)
        owner = state.user_by_person.get(d.owner_id, state.admin_id)
        due = today - timedelta(days=2) if i % 5 == 0 else today + timedelta(days=2 + i % 9)
        _task(
            state,
            anchor,
            who=state.contact_by_person.get(client.primary_contact_id),
            what=opp_id,
            subject=f"Follow up: {d.next_step or d.name}",
            due=due,
            status="In Progress" if i % 3 == 1 else "Not Started",
            priority="High" if d.amount >= 100000 else "Normal",
            owner=owner,
            description=f"Open opportunity {d.name} ({d.stage}).",
            account=state.account_by_client.get(d.client_id),
        )
    for lead in world.leads:
        if lead.status != "Open - Not Contacted" or lead.id not in state.lead_by_world:
            continue
        _task(
            state,
            anchor,
            who=state.lead_by_world[lead.id],
            what=None,
            subject=f"Call new lead: {lead.first_name} {lead.last_name} ({lead.company})",
            due=today + timedelta(days=1),
            status="Not Started",
            priority="High",
            owner=state.user_by_person.get(lead.owner_id, state.admin_id),
            description=lead.description,
            account=None,
            call=True,
        )
    for case in state.org.coll("Case"):
        if case["IsClosed"]:
            continue
        _task(
            state,
            anchor,
            who=case["ContactId"],
            what=case["Id"],
            subject=f"Respond to case {case['CaseNumber']}: {case['Subject']}",
            due=today + timedelta(days=1 if case["Priority"] == "High" else 3),
            status="Not Started",
            priority="High" if case["Priority"] == "High" else "Normal",
            owner=case["OwnerId"],
            description=case["Description"],
            account=case["AccountId"],
        )


def _task(
    state: SeedState,
    anchor: datetime,
    *,
    who: str | None,
    what: str | None,
    subject: str,
    due: date,
    status: str,
    priority: str,
    owner: str,
    description: str | None,
    account: str | None,
    call: bool = False,
) -> Record:
    created = anchor - timedelta(days=1)
    rec = state.new(
        "Task",
        WhoId=who,
        WhatId=what,
        Subject=subject,
        ActivityDate=due.isoformat(),
        Status=status,
        Priority=priority,
        IsHighPriority=priority == "High",
        OwnerId=owner,
        Description=description,
        AccountId=account or _account_of(state, what),
        IsClosed=False,
        TaskSubtype="Call" if call else "Task",
        CallType="Outbound" if call else None,
        IsReminderSet=False,
    )
    stamp(rec, created, created_by=owner)
    state.save("Task", rec)
    return rec


# -- LastActivityDate rollups ------------------------------------------------------------


def rollup_last_activity(state: SeedState, world: World) -> None:
    """Most recent completed task / past event per related record, like Salesforce's rollup."""
    org = state.org
    today = world.anchor.date().isoformat()
    latest: dict[str, str] = {}

    def note(record_id: str | None, when: str) -> None:
        if record_id and when > latest.get(record_id, ""):
            latest[record_id] = when

    for task in org.coll("Task"):
        if task["IsClosed"] and task["ActivityDate"]:
            for rid in (task["WhoId"], task["WhatId"], task["AccountId"]):
                note(rid, task["ActivityDate"])
    for event in org.coll("Event"):
        when = event["ActivityDate"]
        if when and when <= today:
            for rid in (event["WhoId"], event["WhatId"], event["AccountId"]):
                note(rid, when)
    for name in ("Account", "Contact", "Opportunity", "Lead", "Campaign", "Engagement__c"):
        for rec in org.coll(name):
            rec["LastActivityDate"] = latest.get(rec["Id"])
    _ = parse_dt
