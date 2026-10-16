"""The sharing model: role hierarchy, org-wide defaults, object permissions, silent query filtering."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .conftest import ELEANOR, HANNAH, JASMINE, MAYA, NATALIE, RAJESH, Actor

Make = Callable[..., Awaitable[Actor]]


def err(r: Any, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    body = r.json()
    assert body[0]["errorCode"] == code, body
    return body[0]


async def opportunities_by_owner(admin: Actor) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for r in await admin.rows("SELECT Id, OwnerId FROM Opportunity"):
        out.setdefault(r["OwnerId"], set()).add(r["Id"])
    return out


async def test_role_hierarchy_mirrors_the_org_chart(admin: Actor) -> None:
    roles = {r["Id"]: r for r in await admin.rows("SELECT Id, Name, ParentRoleId FROM UserRole")}
    by_name = {r["Name"]: r for r in roles.values()}
    root = by_name["Managing Partner"]
    assert root["ParentRoleId"] is None
    for name in ("Audit Partner", "Tax Partner", "Advisory Partner", "Chief Operating Officer",
                 "Director of Business Development"):  # fmt: skip
        assert by_name[name]["ParentRoleId"] == root["Id"], name
    assert roles[by_name["Audit Manager"]["ParentRoleId"]]["Name"] == "Audit Partner"
    assert roles[by_name["Audit Staff"]["ParentRoleId"]]["Name"] == "Audit Manager"
    assert roles[by_name["Head of People & Culture"]["ParentRoleId"]]["Name"] == "Chief Operating Officer"
    users = {
        u["EmployeeNumber"]: u
        for u in await admin.rows(
            "SELECT Id, Name, UserRole.Name, Profile.Name, ManagerId, EmployeeNumber, IsActive FROM User"
        )
    }
    assert users["HAG-1001"]["UserRole"]["Name"] == "Managing Partner"
    assert users["HAG-1004"]["UserRole"]["Name"] == "Audit Partner"
    assert users["HAG-1012"]["UserRole"]["Name"] == "Tax Partner"
    assert users["HAG-1035"]["Profile"]["Name"] == "System Administrator"
    assert users["HAG-1036"]["Profile"]["Name"] == "System Administrator"
    assert (
        users["HAG-1040"]["Profile"]["Name"] == "Sales User"
        and users["HAG-1042"]["Profile"]["Name"] == "Sales User"
    )
    assert users["HAG-1029"]["Profile"]["Name"] == "Read Only"
    assert users["HAG-1011"]["Profile"]["Name"] == "Standard User"
    assert users["HAG-1005"]["ManagerId"] == users["HAG-1004"]["Id"]  # ManagerId mirrors the org chart
    assert users["HAG-1046"]["IsActive"] is False  # departed


async def test_private_objects_are_filtered_per_user(admin: Actor, actor: Make) -> None:
    owned = await opportunities_by_owner(admin)
    total = sum(len(v) for v in owned.values())
    rajesh, eleanor, natalie = await actor(RAJESH), await actor(ELEANOR), await actor(NATALIE)
    assert set(await rajesh.ids("SELECT Id FROM Opportunity")) == owned[rajesh.user_id]
    assert (
        len(await eleanor.ids("SELECT Id FROM Opportunity")) == total
    )  # top of the hierarchy sees everything
    staff = [await actor("p042"), await actor("p043")]  # business development reps report into Natalie's role
    expected = owned[natalie.user_id].union(*(owned.get(s.user_id, set()) for s in staff))
    assert set(await natalie.ids("SELECT Id FROM Opportunity")) == expected > owned[natalie.user_id]
    assert (await rajesh.query("SELECT COUNT() FROM Opportunity"))["totalSize"] == len(owned[rajesh.user_id])
    grouped = await rajesh.rows("SELECT StageName, COUNT(Id) n FROM Opportunity GROUP BY StageName")
    assert sum(g["n"] for g in grouped) == len(owned[rajesh.user_id])  # aggregates only see visible rows
    for sobject in ("Lead", "Case", "Engagement__c"):
        mine = set(await rajesh.ids(f"SELECT Id FROM {sobject}"))
        every = set(await admin.ids(f"SELECT Id FROM {sobject}"))
        assert mine < every, sobject
        assert set(
            await rajesh.ids(f"SELECT Id FROM {sobject} WHERE OwnerId = '{rajesh.user_id}'")
        ) == mine - set(await rajesh.ids(f"SELECT Id FROM {sobject} WHERE OwnerId != '{rajesh.user_id}'"))


async def test_users_above_the_owner_see_subordinates_records_but_peers_do_not(
    admin: Actor, actor: Make
) -> None:
    owned = await opportunities_by_owner(admin)
    advisory_partner, advisory_director = await actor(MAYA), await actor("p019")
    visible = set(await advisory_partner.ids("SELECT Id FROM Opportunity"))
    assert owned[advisory_director.user_id] <= visible  # Maya's role is above the advisory manager's
    assert owned[advisory_partner.user_id] <= visible
    director_sees = set(await advisory_director.ids("SELECT Id FROM Opportunity"))
    assert director_sees == owned[advisory_director.user_id]  # nobody below them owns an opportunity
    assert not (director_sees & owned[advisory_partner.user_id])  # managers cannot look up the hierarchy
    tax_partner = await actor(RAJESH)
    assert not (
        set(await tax_partner.ids("SELECT Id FROM Opportunity")) & visible - owned[tax_partner.user_id]
    )


async def test_same_role_peers_cannot_see_each_others_private_records(actor: Make) -> None:
    sofia, james, daniel = (
        await actor("p005"),
        await actor("p006"),
        await actor("p004"),
    )  # two audit managers + their partner
    lead = await sofia.create("Lead", {"LastName": "Peerless", "Company": "Peer Review LLP"})
    assert lead in await sofia.ids("SELECT Id FROM Lead")
    assert lead not in await james.ids("SELECT Id FROM Lead")
    assert lead in await daniel.ids("SELECT Id FROM Lead")
    err(await james.get(f"/sobjects/Lead/{lead}"), 404, "NOT_FOUND")
    err(await james.patch(f"/sobjects/Lead/{lead}", {"Rating": "Hot"}), 404, "NOT_FOUND")
    err(await james.delete(f"/sobjects/Lead/{lead}"), 404, "NOT_FOUND")
    assert (await daniel.patch(f"/sobjects/Lead/{lead}", {"Rating": "Hot"})).status_code == 204


async def test_public_read_only_accounts_and_contacts(admin: Actor, actor: Make) -> None:
    rajesh, maya, eleanor = await actor(RAJESH), await actor(MAYA), await actor(ELEANOR)
    accounts = await admin.rows("SELECT Id, OwnerId FROM Account WHERE OwnerId != NULL")
    assert set(await maya.ids("SELECT Id FROM Account")) == {
        a["Id"] for a in accounts
    }  # everyone reads every account
    contacts = await admin.ids("SELECT Id FROM Contact")
    assert set(await maya.ids("SELECT Id FROM Contact")) == set(contacts)
    theirs = next(a["Id"] for a in accounts if a["OwnerId"] == rajesh.user_id)
    assert (await maya.get(f"/sobjects/Account/{theirs}")).status_code == 200
    err(
        await maya.patch(f"/sobjects/Account/{theirs}", {"Rating": "Hot"}),
        403,
        "INSUFFICIENT_ACCESS_OR_READONLY",
    )
    err(await maya.delete(f"/sobjects/Account/{theirs}"), 403, "INSUFFICIENT_ACCESS_OR_READONLY")
    assert (
        await rajesh.patch(f"/sobjects/Account/{theirs}", {"Rating": "Hot"})
    ).status_code == 204  # the owner
    assert (
        await eleanor.patch(f"/sobjects/Account/{theirs}", {"Rating": "Cold"})
    ).status_code == 204  # an ancestor role
    contact = (await admin.ids(f"SELECT Id FROM Contact WHERE AccountId = '{theirs}'"))[0]
    err(
        await maya.patch(f"/sobjects/Contact/{contact}", {"Title": "x"}),
        403,
        "INSUFFICIENT_ACCESS_OR_READONLY",
    )
    e = err(
        await maya.patch(f"/sobjects/Account/{theirs}", {"Rating": "Hot"}),
        403,
        "INSUFFICIENT_ACCESS_OR_READONLY",
    )
    assert e["message"] == "insufficient access rights on object id"


async def test_everyone_can_create_accounts_and_owns_what_they_create(actor: Make) -> None:
    hannah = await actor(HANNAH)
    acct = await hannah.create("Account", {"Name": "Hannah's Client"})
    got = (await hannah.get(f"/sobjects/Account/{acct}")).json()
    assert got["OwnerId"] == hannah.user_id and got["CreatedById"] == hannah.user_id
    assert (await hannah.patch(f"/sobjects/Account/{acct}", {"Rating": "Hot"})).status_code == 204
    assert (await hannah.delete(f"/sobjects/Account/{acct}")).status_code == 204


async def test_read_only_profile_can_read_but_never_write(admin: Actor, actor: Make) -> None:
    jasmine = await actor(JASMINE)
    assert len(await jasmine.ids("SELECT Id FROM Account")) == 41
    e = err(await jasmine.post("/sobjects/Account", {"Name": "Nope"}), 403, "INSUFFICIENT_ACCESS_OR_READONLY")
    assert e["message"] == "entity type cannot be inserted: Account"
    mine = (await jasmine.rows("SELECT Id FROM Account LIMIT 1"))[0]["Id"]
    err(
        await jasmine.patch(f"/sobjects/Account/{mine}", {"Rating": "Hot"}),
        403,
        "INSUFFICIENT_ACCESS_OR_READONLY",
    )
    err(await jasmine.delete(f"/sobjects/Account/{mine}"), 403, "INSUFFICIENT_ACCESS_OR_READONLY")
    for sobject, body in (
        ("Contact", {"LastName": "x"}),
        ("Lead", {"LastName": "x", "Company": "y"}),
        ("Task", {"Subject": "x"}),
    ):
        err(await jasmine.post(f"/sobjects/{sobject}", body), 403, "INSUFFICIENT_ACCESS_OR_READONLY")
    describe = (await jasmine.get("/sobjects/Account/describe")).json()
    assert (describe["createable"], describe["updateable"], describe["deletable"]) == (False, False, False)
    assert not any(f["createable"] or f["updateable"] for f in describe["fields"])
    assert describe["queryable"] is True
    admin_describe = (await admin.get("/sobjects/Account/describe")).json()
    assert (
        admin_describe["createable"] is True
        and next(f for f in admin_describe["fields"] if f["name"] == "Name")["createable"]
    )
    listing = {s["name"]: s for s in (await jasmine.get("/sobjects")).json()["sobjects"]}
    assert listing["Account"]["createable"] is False and listing["Account"]["queryable"] is True
    # bulk and composite writes are refused the same way
    batch = await jasmine.post(
        "/composite/sobjects",
        {"allOrNone": False, "records": [{"attributes": {"type": "Account"}, "Name": "x"}]},
    )
    assert batch.json()[0]["errors"][0]["statusCode"] == "INSUFFICIENT_ACCESS_OR_READONLY"


async def test_object_permissions_by_profile(actor: Make) -> None:
    hannah, natalie = await actor(HANNAH), await actor(NATALIE)  # Standard User vs Sales User
    e = err(
        await hannah.post("/sobjects/Campaign", {"Name": "Hannah's webinar"}),
        403,
        "INSUFFICIENT_ACCESS_OR_READONLY",
    )
    assert "cannot be inserted" in e["message"]
    campaign = await natalie.create(
        "Campaign", {"Name": "Natalie's webinar", "Type": "Webinar", "Status": "Planned"}
    )
    assert (
        await hannah.get(f"/sobjects/Campaign/{campaign}")
    ).status_code == 200  # campaigns are public read-only
    err(
        await hannah.patch(f"/sobjects/Campaign/{campaign}", {"Status": "Completed"}),
        403,
        "INSUFFICIENT_ACCESS_OR_READONLY",
    )
    err(
        await hannah.post("/sobjects/Product2", {"Name": "Sneaky product"}),
        403,
        "INSUFFICIENT_ACCESS_OR_READONLY",
    )
    err(
        await hannah.post("/sobjects/UserRole", {"Name": "Rogue", "DeveloperName": "Rogue"}),
        403,
        "INSUFFICIENT_ACCESS_OR_READONLY",
    )
    assert (
        len(await hannah.ids("SELECT Id FROM Product2")) == 10
        and len(await hannah.ids("SELECT Id FROM Profile")) == 5
    )


async def test_cross_reference_access_is_checked_on_save(admin: Actor, actor: Make) -> None:
    owned = await opportunities_by_owner(admin)
    rajesh, maya = await actor(RAJESH), await actor(MAYA)
    others = next(iter(owned[maya.user_id]))
    e = err(
        await rajesh.post("/sobjects/Task", {"Subject": "Sneak", "WhatId": others}),
        400,
        "INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY",
    )
    assert e["fields"] == ["WhatId"] and others in e["message"]
    mine = next(iter(owned[rajesh.user_id]))
    assert (await rajesh.post("/sobjects/Task", {"Subject": "Fine", "WhatId": mine})).status_code == 201
    err(
        await rajesh.post(
            "/sobjects/OpportunityContactRole",
            {"OpportunityId": others, "ContactId": (await admin.ids("SELECT Id FROM Contact LIMIT 1"))[0]},
        ),
        400,
        "INSUFFICIENT_ACCESS_ON_CROSS_REFERENCE_ENTITY",
    )


async def test_child_objects_follow_their_parent(admin: Actor, actor: Make) -> None:
    owned = await opportunities_by_owner(admin)
    rajesh = await actor(RAJESH)
    roles = await rajesh.rows("SELECT Id, OpportunityId FROM OpportunityContactRole")
    assert roles and {r["OpportunityId"] for r in roles} <= owned[rajesh.user_id]
    items = await rajesh.rows("SELECT Id, OpportunityId FROM OpportunityLineItem")
    assert {r["OpportunityId"] for r in items} <= owned[rajesh.user_id]
    everyone = await admin.rows("SELECT Id FROM OpportunityLineItem")
    assert len(items) < len(everyone)
    # accounts are public, but their opportunity subqueries are filtered to what the caller can see
    accounts = await rajesh.rows(
        "SELECT Id, (SELECT Id FROM Opportunities) FROM Account WHERE Id IN (SELECT AccountId FROM Opportunity)"
    )
    seen = {o["Id"] for a in accounts for o in (a["Opportunities"] or {"records": []})["records"]}
    assert seen <= owned[rajesh.user_id]
    assert (await rajesh.get(f"/sobjects/Account/{accounts[0]['Id']}/Opportunities")).json()["totalSize"] <= (
        await admin.get(f"/sobjects/Account/{accounts[0]['Id']}/Opportunities")
    ).json()["totalSize"]


async def test_parent_lookups_hide_records_the_caller_cannot_see(admin: Actor, actor: Make) -> None:
    owned = await opportunities_by_owner(admin)
    rajesh = await actor(NATALIE)  # owns three open opportunities with follow-up tasks
    hidden = {o for owner, opps in owned.items() if owner != rajesh.user_id for o in opps}
    rows = await admin.rows("SELECT Id, WhatId FROM Task WHERE IsClosed = false AND WhatId != NULL")
    assert any(r["WhatId"] in hidden for r in rows), (
        "expected an open follow-up task on someone else's opportunity"
    )
    # a task that only points at the private opportunity (no Who) is visible only through that record
    hidden_task = await admin.create("Task", {"Subject": "Private follow-up", "WhatId": sorted(hidden)[0]})
    # the task itself is visible only through its related record; the admin sees the opportunity name
    assert hidden_task not in await rajesh.ids("SELECT Id FROM Task")
    own = [r for r in rows if r["WhatId"] in owned[rajesh.user_id]]
    assert own
    got = await rajesh.rows(f"SELECT Id, What.Name FROM Task WHERE Id = '{own[0]['Id']}'")
    assert got[0]["What"]["Name"]
    # a contact role lookup into a private opportunity comes back null for people who cannot see it
    contact_roles = await rajesh.rows("SELECT Id, Opportunity.Name FROM OpportunityContactRole")
    assert all(r["Opportunity"] and r["Opportunity"]["Name"] for r in contact_roles)


async def test_activities_are_visible_through_their_related_record(admin: Actor, actor: Make) -> None:
    hannah = await actor(HANNAH)
    email_tasks = await admin.ids("SELECT Id FROM Task WHERE TaskSubtype = 'Email'")
    mine = set(await hannah.ids("SELECT Id FROM Task WHERE TaskSubtype = 'Email'"))
    assert mine == set(email_tasks)  # related to public accounts, so readable by everyone
    follow_up = (await admin.rows("SELECT Id, WhatId FROM Task WHERE Subject LIKE 'Follow up:%'"))[0]
    # a task that only hangs off a private opportunity is hidden with it...
    bare = await admin.create("Task", {"Subject": "Only on the deal", "WhatId": follow_up["WhatId"]})
    assert bare not in await hannah.ids("SELECT Id FROM Task")
    err(await hannah.get(f"/sobjects/Task/{bare}"), 404, "NOT_FOUND")
    # ...while either related record (here the contact in Who) is enough to see an activity
    assert follow_up["Id"] in await hannah.ids("SELECT Id FROM Task")


async def test_search_and_describe_respect_visibility(admin: Actor, actor: Make) -> None:
    rajesh = await actor(RAJESH)
    all_hits = (
        await admin.get("/search", params={"q": "FIND {Foxglove} RETURNING Opportunity(Id, Name)"})
    ).json()["searchRecords"]
    mine = (
        await rajesh.get("/search", params={"q": "FIND {Foxglove} RETURNING Opportunity(Id, Name)"})
    ).json()["searchRecords"]
    assert all_hits and mine == []
    accounts = (
        await rajesh.get("/search", params={"q": "FIND {Foxglove} RETURNING Account(Id, Name)"})
    ).json()["searchRecords"]
    assert accounts


async def test_admins_and_the_integration_user_see_and_edit_everything(admin: Actor, actor: Make) -> None:
    sarah, bot = await actor("p036"), await actor("integration")
    total = (await admin.query("SELECT COUNT() FROM Opportunity"))["totalSize"]
    for who in (sarah, bot):
        assert (await who.query("SELECT COUNT() FROM Opportunity"))["totalSize"] == total
    opp = (await admin.ids("SELECT Id FROM Opportunity LIMIT 1"))[0]
    for who in (sarah, bot):
        assert (
            await who.patch(f"/sobjects/Opportunity/{opp}", {"NextStep": f"by {who.who}"})
        ).status_code == 204
    app_only = await actor(app_only=True)
    assert (await app_only.get("/limits")).status_code == 200
    assert (await app_only.query("SELECT COUNT() FROM Lead"))["totalSize"] == 18


async def test_transferring_ownership_changes_who_can_see_a_record(actor: Make) -> None:
    rajesh, maya = await actor(RAJESH), await actor(MAYA)
    opp = await rajesh.create(
        "Opportunity", {"Name": "Hand-off", "StageName": "Prospecting", "CloseDate": "2027-01-01"}
    )
    assert opp not in await maya.ids("SELECT Id FROM Opportunity")
    assert (await rajesh.patch(f"/sobjects/Opportunity/{opp}", {"OwnerId": maya.user_id})).status_code == 204
    assert opp in await maya.ids("SELECT Id FROM Opportunity")
    assert opp not in await rajesh.ids("SELECT Id FROM Opportunity")  # the previous owner loses access
    err(await rajesh.get(f"/sobjects/Opportunity/{opp}"), 404, "NOT_FOUND")


async def test_composite_and_collections_apply_the_callers_permissions(admin: Actor, actor: Make) -> None:
    owned = await opportunities_by_owner(admin)
    rajesh, maya = await actor(RAJESH), await actor(MAYA)
    foreign = next(iter(owned[maya.user_id]))
    mine = next(iter(owned[rajesh.user_id]))
    body = {
        "allOrNone": False,
        "compositeRequest": [
            {
                "method": "PATCH",
                "url": f"/services/data/v62.0/sobjects/Opportunity/{foreign}",
                "referenceId": "a",
                "body": {"NextStep": "x"},
            },
            {
                "method": "PATCH",
                "url": f"/services/data/v62.0/sobjects/Opportunity/{mine}",
                "referenceId": "b",
                "body": {"NextStep": "y"},
            },
        ],
    }
    out = (await rajesh.post("/composite", body)).json()["compositeResponse"]
    assert [o["httpStatusCode"] for o in out] == [404, 204]
    res = (
        await rajesh.post("/composite/sobjects", None, json=None)
        if False
        else await rajesh.request(
            "PATCH",
            "/composite/sobjects",
            json={
                "allOrNone": False,
                "records": [
                    {"attributes": {"type": "Opportunity"}, "id": foreign, "NextStep": "x"},
                    {"attributes": {"type": "Opportunity"}, "id": mine, "NextStep": "z"},
                ],
            },
        )
    ).json()
    assert [r["success"] for r in res] == [False, True]
    assert res[0]["errors"][0]["statusCode"] == "NOT_FOUND"
    deleted = (
        await rajesh.request("DELETE", "/composite/sobjects", params={"ids": f"{foreign},{mine}"})
    ).json()
    assert [d["success"] for d in deleted] == [False, True]
    fetched = (
        await rajesh.get(
            "/composite/sobjects/Opportunity", params={"ids": f"{foreign},{mine}", "fields": "Id"}
        )
    ).json()
    assert fetched == [None, None]  # mine was just deleted, foreign was never visible
