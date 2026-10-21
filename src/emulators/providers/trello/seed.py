"""Project Halcyon's staff, collaboration teams, engagements and documents into Trello."""

from __future__ import annotations

from datetime import timedelta

from emulators.core.clock import isoformat_z
from emulators.core.ids import stable_hash
from emulators.core.provider import ProviderContext

from .common import KEY, SECRET, TOKEN, Record, oid


def board_record(
    ctx: ProviderContext,
    rid: str,
    name: str,
    org_id: str | None,
    members: dict[str, str],
    desc: str = "",
    visibility: str = "private",
) -> Record:
    return {
        "id": rid,
        "name": name,
        "desc": desc,
        "descData": {"emoji": {}},
        "closed": False,
        "idOrganization": org_id,
        "idEnterprise": None,
        "pinned": False,
        "starred": False,
        "shortLink": stable_hash(rid, length=8),
        "dateLastActivity": isoformat_z(ctx.world.anchor),
        "dateLastView": None,
        "prefs": {
            "permissionLevel": visibility,
            "voting": "disabled",
            "comments": "members",
            "invitations": "members",
            "selfJoin": False,
            "cardCovers": True,
            "background": "blue",
            "backgroundColor": "#0079BF",
            "isTemplate": False,
        },
        "labelNames": {
            c: ""
            for c in ("green", "yellow", "orange", "red", "purple", "blue", "sky", "lime", "pink", "black")
        },
        "subscribed": False,
        "_members": members,
        "_created": isoformat_z(ctx.world.anchor),
        "_nextShort": 0,
    }


def card_record(
    ctx: ProviderContext,
    rid: str,
    board: Record,
    list_id: str,
    name: str,
    desc: str,
    *,
    members: list[str] | None = None,
    pos: float = 16384.0,
) -> Record:
    board["_nextShort"] += 1
    return {
        "id": rid,
        "name": name,
        "desc": desc,
        "descData": {"emoji": {}},
        "closed": False,
        "idBoard": board["id"],
        "idList": list_id,
        "idShort": board["_nextShort"],
        "shortLink": stable_hash(rid, length=8),
        "pos": pos,
        "idMembers": members or [],
        "idMembersVoted": [],
        "idLabels": [],
        "idChecklists": [],
        "idAttachmentCover": None,
        "manualCoverAttachment": False,
        "due": None,
        "start": None,
        "dueComplete": False,
        "dateLastActivity": isoformat_z(ctx.world.anchor),
        "subscribed": False,
        "cover": {
            "idAttachment": None,
            "color": None,
            "idUploadedBackground": None,
            "size": "normal",
            "brightness": "dark",
            "idPlugin": None,
        },
        "_created": isoformat_z(ctx.world.anchor),
    }


def seed(ctx: ProviderContext) -> None:
    when = ctx.world.anchor - timedelta(days=90)
    ctx.tokens.register_client(KEY, SECRET, name="Halcyon Trello Integration")
    for person in ctx.world.people:
        if not person.active:
            continue
        rid = oid("member", person.id, when)
        ctx.store["members"].put(
            {
                "id": rid,
                "fullName": person.display_name,
                "username": person.username.replace(".", ""),
                "initials": person.initials,
                "avatarHash": None,
                "avatarUrl": None,
                "bio": person.job_title,
                "bioData": {"emoji": {}},
                "confirmed": True,
                "memberType": "normal",
                "status": "disconnected",
                "activityBlocked": False,
                "_person": person.id,
                "_email": person.email,
                "_employee": not person.is_external,
            }
        )
    mids = {m["_person"]: m["id"] for m in ctx.store["members"]}
    admins = {mids[p.id] for p in ctx.world.active_employees if p.is_admin or p.id == "p001"}
    employees = {
        mids[p.id]: "admin" if mids[p.id] in admins else "normal" for p in ctx.world.active_employees
    }
    org_id = oid("organization", ctx.world.org.slug, when)
    ctx.store["organizations"].put(
        {
            "id": org_id,
            "name": ctx.world.org.slug,
            "displayName": ctx.world.org.name,
            "desc": "Client engagements and Halcyon operations",
            "descData": {"emoji": {}},
            "website": ctx.world.org.website,
            "logoHash": None,
            "logoUrl": None,
            "products": [],
            "premiumFeatures": [],
            "prefs": {
                "permissionLevel": "private",
                "boardVisibilityRestrict": {"private": "org", "org": "org", "public": "admin"},
            },
            "_members": employees,
        }
    )
    ctx.store.meta.update(key=KEY, secret=SECRET, organization_id=org_id, sequence=0)
    ctx.tokens.issue_access(
        client_id=KEY, user_id=mids["p001"], scopes=["read", "write", "account"], token=TOKEN, ttl=None
    )

    def add_board(
        key: str, name: str, people: dict[str, str], description: str, workspace: str = org_id
    ) -> Record:
        board = board_record(ctx, oid("board", key, when), name, workspace, people, description)
        ctx.store["boards"].put(board)
        for i, label in enumerate(("To Do", "In Progress", "Done")):
            ctx.store["lists"].put(
                {
                    "id": oid("list", key + str(i), when),
                    "name": label,
                    "closed": False,
                    "idBoard": board["id"],
                    "pos": float((i + 1) * 16384),
                    "softLimit": None,
                    "subscribed": False,
                }
            )
        for color, label in (("blue", "Client work"), ("green", "Ready"), ("red", "Needs attention")):
            ctx.store["labels"].put(
                {
                    "id": oid("label", key + color, when),
                    "idBoard": board["id"],
                    "name": label,
                    "color": color,
                    "uses": 0,
                }
            )
        return board

    ops = add_board(
        "operations",
        ctx.world.org.name + " Operations",
        employees,
        "Client onboarding and internal operations",
    )
    ctx.store.meta["operations_board_id"] = ops["id"]
    ops_list = ctx.store["lists"].first(idBoard=ops["id"])["id"]
    for client in ctx.world.clients:
        card = card_record(
            ctx,
            oid("card", client.id, when + timedelta(days=1)),
            ops,
            ops_list,
            client.name,
            client.description,
            members=[mids[client.owner_id]],
            pos=float((len(ctx.store["cards"]) + 1) * 16384),
        )
        card["_client"] = client.id
        ctx.store["cards"].put(card)
    for team in ctx.world.teams:
        people = {mids[p]: "admin" if p in team.owner_ids else "normal" for p in team.member_ids if p in mids}
        people.update({mids[p]: "admin" for p in team.owner_ids if p in mids})
        team_org = oid("organization", team.id, when)
        ctx.store["organizations"].put(
            {
                "id": team_org,
                "name": team.mail_nickname.lower(),
                "displayName": team.name,
                "desc": team.description,
                "descData": {"emoji": {}},
                "website": "",
                "logoHash": None,
                "logoUrl": None,
                "products": [],
                "premiumFeatures": [],
                "prefs": {"permissionLevel": "private"},
                "_members": people,
            }
        )
        add_board("team:" + team.id, team.name, people, team.description, team_org)
    for engagement in ctx.world.engagements:
        people = {mids[p]: "normal" for p in engagement.team_ids if p in mids}
        people[mids[engagement.lead_id]] = "admin"
        board = add_board(
            engagement.id,
            ctx.world.client(engagement.client_id).name + " — " + engagement.name,
            people,
            engagement.service,
        )
        lists = ctx.store["lists"].where(idBoard=board["id"])
        index = {"Planned": 0, "In Progress": 1, "Completed": 2, "On Hold": 0}[engagement.status]
        card = card_record(
            ctx,
            oid("card", engagement.id, when + timedelta(days=2)),
            board,
            lists[index]["id"],
            engagement.name,
            f"{engagement.service}: {engagement.status}. Budget {engagement.budget_hours} hours.",
            members=[mids[engagement.lead_id]],
        )
        card.update(
            due=engagement.end.isoformat() + "T17:00:00.000Z",
            start=engagement.start.isoformat() + "T09:00:00.000Z",
            dueComplete=engagement.status == "Completed",
        )
        card["_engagement"] = engagement.id
        ctx.store["cards"].put(card)
        checklist_id = oid("checklist", engagement.id, when)
        card["idChecklists"].append(checklist_id)
        ctx.store["checklists"].put(
            {
                "id": checklist_id,
                "idCard": card["id"],
                "idBoard": board["id"],
                "name": "Delivery checklist",
                "pos": 16384.0,
                "checkItems": [
                    {
                        "id": oid("checkitem", engagement.id + str(i), when),
                        "name": name,
                        "nameData": {"emoji": {}},
                        "pos": float((i + 1) * 16384),
                        "state": "complete" if engagement.status == "Completed" else "incomplete",
                        "idChecklist": checklist_id,
                        "due": None,
                        "idMember": None,
                    }
                    for i, name in enumerate(
                        ("Confirm scope", "Review deliverables", "Obtain client approval")
                    )
                ],
            }
        )
        docs = [
            d
            for d in ctx.world.documents
            if d.engagement_id == engagement.id and not d.trashed and not d.restricted
        ]
        for doc in docs[:2]:
            attachment_id = oid("attachment", doc.id, doc.created)
            ctx.store["attachments"].put(
                {
                    "id": attachment_id,
                    "idMember": mids.get(doc.owner_id),
                    "bytes": None,
                    "date": isoformat_z(doc.created),
                    "edgeColor": None,
                    "idCard": card["id"],
                    "idBoard": board["id"],
                    "isUpload": True,
                    "mimeType": "",
                    "name": doc.name,
                    "previews": [],
                    "pos": 16384.0,
                    "_document": doc.id,
                    "_blob": None,
                }
            )
        aid = oid("action", engagement.id, when + timedelta(days=3))
        ctx.store["actions"].put(
            {
                "id": aid,
                "idMemberCreator": mids[engagement.lead_id],
                "type": "commentCard",
                "date": isoformat_z(when + timedelta(days=3)),
                "data": {
                    "text": f"{engagement.service} work is {engagement.status.lower()}.",
                    "card": {
                        "id": card["id"],
                        "name": card["name"],
                        "idShort": card["idShort"],
                        "shortLink": card["shortLink"],
                    },
                    "board": {"id": board["id"], "name": board["name"], "shortLink": board["shortLink"]},
                },
                "_board": board["id"],
                "_card": card["id"],
            }
        )
