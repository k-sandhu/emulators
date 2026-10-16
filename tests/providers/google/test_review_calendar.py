"""Calendar recurrence, invitation, ACL and sync regression contracts."""

from __future__ import annotations

import re
from typing import Any

import pytest

from emulators.testing.helpers import bearer, mint

BASE = "https://www.googleapis.com"
ROOT = BASE + "/calendar/v3"
EVENTS = ROOT + "/calendars/primary/events"
OWNER = "eleanor.whitfield@halcyonadvisory.example"
GUEST = "maya.chen@halcyonadvisory.example"


def event(**fields: Any) -> dict[str, Any]:
    return {
        "summary": "Review contracts",
        "description": "quarterly roadmap",
        "location": "Toronto office",
        "start": {"dateTime": "2026-10-05T09:00:00-04:00", "timeZone": "America/Toronto"},
        "end": {"dateTime": "2026-10-05T10:00:00-04:00", "timeZone": "America/Toronto"},
        **fields,
    }


@pytest.mark.parametrize(
    "recurrence,dates",
    [
        (["RRULE:FREQ=MONTHLY;COUNT=3;BYDAY=1MO;WKST=SU"], ["2026-10-05", "2026-11-02", "2026-12-07"]),
        (["RRULE:FREQ=YEARLY;COUNT=3;BYMONTH=10;BYMONTHDAY=5"], ["2026-10-05", "2027-10-05", "2028-10-05"]),
        (
            [
                "RRULE:FREQ=DAILY;INTERVAL=2;UNTIL=20261009",
                "EXDATE:20261007T130000Z",
                "RDATE:20261008T130000Z",
            ],
            ["2026-10-05", "2026-10-08", "2026-10-09"],
        ),
        (["RRULE:FREQ=MONTHLY;COUNT=2;BYMONTHDAY=-1"], ["2026-10-31", "2026-11-30"]),
    ],
)
async def test_full_recurrence(make_client: Any, recurrence: list[str], dates: list[str]) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, BASE, user="p001"))
        created = await client.post(EVENTS, headers=auth, json=event(recurrence=recurrence))
        assert created.status_code == 200, created.text
        rows = (
            await client.get(
                EVENTS + "/" + created.json()["id"] + "/instances",
                headers=auth,
                params={"timeMax": "2030-01-01T00:00:00Z"},
            )
        ).json()["items"]
        assert [r["start"]["dateTime"][:10] for r in rows] == dates
        if all("COUNT=" in r for r in recurrence):
            unbounded_request = await client.get(
                EVENTS + "/" + created.json()["id"] + "/instances", headers=auth
            )
            assert [r["start"]["dateTime"][:10] for r in unbounded_request.json()["items"]] == dates


async def test_attendee_defaults_permissions_rsvp_and_guest_edit(make_client: Any) -> None:
    async with make_client("google") as client:
        owner = bearer(await mint(client, BASE, user="p001"))
        guest = bearer(await mint(client, BASE, user="p018"))
        created = (
            await client.post(
                EVENTS, headers=owner, json=event(attendees=[{"email": OWNER}, {"email": GUEST}])
            )
        ).json()
        attendees = {a["email"]: a for a in created["attendees"]}
        assert attendees[GUEST]["responseStatus"] == "needsAction"
        assert (
            attendees[OWNER]["organizer"]
            and attendees[OWNER]["self"]
            and attendees[OWNER]["responseStatus"] == "accepted"
        )
        path = EVENTS + "/" + created["id"]
        denied = await client.patch(path, headers=guest, json={"summary": "Cannot change"})
        assert (
            denied.status_code == 403
            and denied.json()["error"]["errors"][0]["reason"] == "forbiddenForNonOrganizer"
        )
        rsvp = await client.patch(
            path, headers=guest, json={"attendees": [{"email": GUEST, "responseStatus": "accepted"}]}
        )
        assert rsvp.status_code == 200
        copy = (await client.get(path, headers=owner)).json()
        assert next(a for a in copy["attendees"] if a["email"] == GUEST)["responseStatus"] == "accepted"
        assert (
            await client.patch(path, headers=guest, json={"attendees": copy["attendees"]})
        ).status_code == 200
        await client.patch(path, headers=owner, json={"guestsCanModify": True})
        assert (await client.patch(path, headers=guest, json={"summary": "Guest allowed"})).status_code == 200
        assert (await client.get(path, headers=owner)).json()["summary"] == "Guest allowed"


async def test_cancel_master_cancels_modified_instances_and_second_delete(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, BASE, user="p001"))
        master = (
            await client.post(EVENTS, headers=auth, json=event(recurrence=["RRULE:FREQ=DAILY;COUNT=3"]))
        ).json()
        path = EVENTS + "/" + master["id"]
        instance = (await client.get(path + "/instances", headers=auth)).json()["items"][0]
        changed = await client.patch(
            EVENTS + "/" + instance["id"], headers=auth, json={"summary": "Modified"}
        )
        assert changed.status_code == 200
        assert (await client.delete(path, headers=auth)).status_code == 204
        assert (await client.get(path, headers=auth)).json()["status"] == "cancelled"
        assert (await client.get(EVENTS + "/" + instance["id"], headers=auth)).json()["status"] == "cancelled"
        assert not (await client.get(path + "/instances", headers=auth)).json()["items"]
        assert all(
            r["status"] == "cancelled"
            for r in (
                await client.get(path + "/instances", headers=auth, params={"showDeleted": "true"})
            ).json()["items"]
        )
        again = await client.delete(path, headers=auth)
        assert again.status_code == 410 and again.json()["error"]["errors"][0]["reason"] == "deleted"


async def test_event_extended_properties_q_fields_and_timestamps(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, BASE, user="p001"))
        created = (
            await client.post(
                EVENTS,
                headers=auth,
                json=event(
                    attendees=[{"email": GUEST, "displayName": "Maya Chen"}],
                    extendedProperties={
                        "private": {"project": "one", "owner": "me"},
                        "shared": {"team": "api"},
                    },
                ),
            )
        ).json()
        assert all(re.fullmatch(r".+\.\d{3}Z", created[f]) for f in ("created", "updated"))
        params = [
            ("privateExtendedProperty", "project=one"),
            ("privateExtendedProperty", "owner=me"),
            ("sharedExtendedProperty", "team=api"),
            ("fields", "items(id,summary),nextSyncToken"),
        ]
        listed = await client.get(EVENTS, headers=auth, params=params)
        assert listed.json()["items"] == [{"id": created["id"], "summary": "Review contracts"}]
        for term in ("Review", "roadmap", "Toronto", GUEST, "Maya Chen"):
            assert created["id"] in {
                r["id"] for r in (await client.get(EVENTS, headers=auth, params={"q": term})).json()["items"]
            }
        assert not (await client.get(EVENTS, headers=auth, params={"q": "displayName"})).json()["items"]


async def test_acl_crud_and_calendar_list_incremental_sync(make_client: Any) -> None:
    async with make_client("google") as client:
        owner = bearer(await mint(client, BASE, user="p001"))
        guest = bearer(await mint(client, BASE, user="p018"))
        calendar = (
            await client.post(ROOT + "/calendars", headers=owner, json={"summary": "ACL calendar"})
        ).json()["id"]
        path = ROOT + "/calendars/" + calendar
        assert (await client.get(path, headers=guest)).status_code == 404
        acl = (
            await client.post(
                path + "/acl",
                headers=owner,
                json={"scope": {"type": "user", "value": GUEST}, "role": "reader"},
            )
        ).json()
        assert (await client.get(path + "/acl/" + acl["id"], headers=owner)).json()["role"] == "reader"
        assert (await client.get(path, headers=guest)).status_code == 200
        assert (await client.post(path + "/events", headers=guest, json=event())).status_code == 403
        assert (
            await client.patch(path + "/acl/" + acl["id"], headers=guest, json={"role": "owner"})
        ).status_code == 403
        calendars = ROOT + "/users/me/calendarList"
        sync = (await client.get(calendars, headers=guest)).json()["nextSyncToken"]
        await client.post(calendars, headers=guest, json={"id": calendar})
        delta = (await client.get(calendars, headers=guest, params={"syncToken": sync})).json()
        assert [r["id"] for r in delta["items"]] == [calendar]
        await client.put(
            path + "/acl/" + acl["id"], headers=owner, json={"role": "writer", "scope": acl["scope"]}
        )
        assert (await client.post(path + "/events", headers=guest, json=event())).status_code == 200
        await client.delete(path + "/acl/" + acl["id"], headers=owner)
        assert (await client.get(path, headers=guest)).status_code == 404
        removed = (
            await client.get(calendars, headers=guest, params={"syncToken": delta["nextSyncToken"]})
        ).json()["items"]
        assert removed[0]["deleted"] and removed[0]["id"] == calendar
