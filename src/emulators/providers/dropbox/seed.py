"""Project the shared Halcyon world into a Dropbox Business team.

* every employee is a team member with a home namespace (``team_member_folder``); departed
  employees are *suspended*; client contacts are non-team accounts;
* each world team space becomes a team folder whose access comes from groups (``Space.members``);
* documents become files with real bytes, ``Document.versions`` revisions, explicit file members
  from ``Document.shares`` and shared links from link shares;
* ``Document.restricted`` items get ``no_inherit``: inherited folder access is removed, only the
  owner and explicit members keep it (real Dropbox cuts inheritance on folders; we do it per file);
* a few vendor-specific extras (Paper docs, images) exercise non-downloadable files and thumbnails.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.dropbox import catalog
from emulators.providers.dropbox.common import (
    account_id_for,
    autorename,
    digest,
    group_id_for,
    member_id_for,
    numeric_id,
    revision_for,
    split_ext,
    ts,
)
from emulators.providers.dropbox.model import (
    ACCOUNTS,
    GROUPS,
    LINKS,
    NAMESPACES,
    Tree,
)
from emulators.providers.dropbox.ops import Ops
from emulators.world import documents as docs
from emulators.world.model import Document, Person, Share, World

ROLE_MAP = {"owner": "editor", "editor": "editor", "commenter": "viewer", "viewer": "viewer"}
FILE_ROLE_MAP = {"owner": "owner", "editor": "editor", "commenter": "viewer", "viewer": "viewer_no_comment"}
BAD = str.maketrans({c: "-" for c in '\\<>:"|?*'})


def clean_name(name: str) -> str:
    out = name.translate(BAD).strip()
    return out.rstrip(". ") or "untitled"


def day(d: Any, hour: int = 9) -> datetime:
    return datetime.combine(d, time(hour), tzinfo=UTC)


def link_slug(key: str, n: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    raw = digest("dbx-link", key)
    return "".join(alphabet[b % 36] for b in raw[:n])


def make_account(
    w: World, p: Person, ns_id: str, root_id: str, team_id: str, anchor: datetime
) -> dict[str, Any]:
    is_team = not p.is_external
    key = p.id
    office = w.office(p.office_id) if p.office_id else None
    company = None
    if p.company_id:
        try:
            company = w.client(p.company_id)
        except KeyError:
            company = w.vendor(p.company_id)  # vendor contacts carry a "v###" company id
    country = office.address.country_code if office else (company.address.country_code if company else "US")
    status = "active" if (p.active or not is_team) else "suspended"
    return {
        "id": account_id_for(key),
        "person": p.id,
        "uid": 10_000_000 + int.from_bytes(digest("dbx-uid", key)[:4], "big") % 89_999_999,
        "email": p.email,
        "email_verified": True,
        "given_name": p.first_name,
        "surname": p.last_name,
        "familiar_name": p.first_name,
        "display_name": p.display_name,
        "abbreviated_name": p.initials,
        "country": country,
        "locale": p.locale.split("-")[0],
        "disabled": False,
        "is_team": is_team,
        "team_member_id": member_id_for(key) if is_team else None,
        "status": status,
        "joined_on": ts(day(p.hire_date)) if p.hire_date else ts(anchor - timedelta(days=200)),
        "invited_on": ts(day(p.hire_date) - timedelta(days=2)) if p.hire_date else None,
        "suspended_on": ts(anchor - timedelta(days=30)) if status == "suspended" else None,
        "groups": [],
        "roles": [],
        "home_ns": ns_id,
        "root_ns": root_id if is_team else ns_id,
        "team_id": team_id if is_team else None,
        "external_id": p.employee_id,
        "referral_link": "https://www.dropbox.com/referrals/AAB" + link_slug("ref/" + key, 24),
        "account_type": "business" if is_team else "basic",
        "space_allocated": 2 * 1024**4 if is_team else 2 * 1024**3,
    }


def role_keys_for(p: Person) -> list[str]:
    if p.id == "p035":
        return ["team_admin"]
    if p.id == "p036":
        return ["user_management_admin"]
    return []


def seed(ctx: ProviderContext) -> None:
    w = ctx.world
    store = ctx.store
    anchor = w.anchor
    team_id = w.org.dropbox_team_id
    store.meta.update(
        {
            "seq": 0,
            "counters": {},
            "team": {
                "id": team_id,
                "name": w.org.name,
                "policies": dict(catalog.DEFAULT_SHARING_POLICIES),
                "emm_state": "disabled",
                "office_addin": "disabled",
                "suggest_members_policy": "disabled",
                "top_level_content_policy": "admin_only",
                "num_licensed_users": 60,
            },
        }
    )
    accounts = store.collection(ACCOUNTS)
    namespaces = store.collection(NAMESPACES)
    root_id = numeric_id("team-root")

    # -- accounts and their home namespaces -------------------------------------------------
    home_of: dict[str, str] = {}
    for p in w.people:
        ns_id = numeric_id("home/" + p.id)
        home_of[p.id] = ns_id
        acct = make_account(w, p, ns_id, root_id, team_id, anchor)
        acct["roles"] = [catalog.role_obj(k) for k in role_keys_for(p)]
        accounts.put(acct)
        namespaces.put(
            {
                "id": ns_id,
                "type": "team_member_folder" if acct["is_team"] else "personal",
                "name": p.display_name,
                "owner": acct["id"],
                "team_id": team_id if acct["is_team"] else None,
                "members": [],
                "status": "active",
                "created": acct["joined_on"],
                "n": 0,
                "seq": 0,
            }
        )
    namespaces.put(
        {
            "id": root_id,
            "type": "team_root",
            "name": w.org.name,
            "owner": None,
            "team_id": team_id,
            "members": [],
            "status": "active",
            "created": ts(day(w.org.founded)),
            "n": 0,
            "seq": 0,
        }
    )

    # -- groups -------------------------------------------------------------------------
    by_person = {a["person"]: a for a in accounts}
    for g in w.groups:
        gid = group_id_for(g.id)
        members = [
            by_person[m]["team_member_id"] for m in g.member_ids if m in by_person and by_person[m]["is_team"]
        ]
        store.collection(GROUPS).put(
            {
                "id": gid,
                "name": g.name,
                "external_id": g.id,
                "management_type": "company_managed",
                "member_ids": members,
                "created": ts(day(w.org.founded)),
                "email": g.email,
            }
        )
        for m in g.member_ids:
            if m in by_person and by_person[m]["is_team"]:
                by_person[m]["groups"].append(gid)

    # -- team folders ---------------------------------------------------------------------
    tree = Tree(ctx)
    ops = Ops(ctx, tree, log=False)
    space_ns: dict[str, str] = {}
    for space in w.spaces:
        if space.kind == "team":
            ns_id = numeric_id("team-folder/" + space.id)
            members: list[dict[str, Any]] = []
            for principal, role in space.members:
                access = ROLE_MAP.get(role, "viewer")
                if principal.startswith("g-"):
                    members.append(
                        {
                            "kind": "group",
                            "group_id": group_id_for(principal),
                            "access_type": access,
                            "inherited": False,
                        }
                    )
                elif principal in by_person:
                    members.append(
                        {
                            "kind": "user",
                            "account_id": by_person[principal]["id"],
                            "access_type": access,
                            "inherited": False,
                            "invited": ts(day(w.org.founded)),
                        }
                    )
            namespaces.put(
                {
                    "id": ns_id,
                    "type": "team_folder",
                    "name": space.name,
                    "owner": None,
                    "team_id": team_id,
                    "members": members,
                    "status": "active",
                    "sync": "default_sync",
                    "created": ts(day(w.org.founded)),
                    "policy": {
                        "acl_update_policy": "editors",
                        "member_policy": "team",
                        "shared_link_policy": "anyone",
                        "viewer_info_policy": "enabled",
                    },
                    "access_inheritance": "inherit",
                    "n": 0,
                    "seq": 0,
                }
            )
            space_ns[space.id] = ns_id
        else:
            space_ns[space.id] = home_of[space.owner_id]

    # -- team-space root: member folders and team folders mounted at top level ----------------
    taken: set[str] = set()

    def mount(name: str, target: str) -> None:
        name = clean_name(name)
        if name.lower() in taken:
            name = autorename(name, taken)
        taken.add(name.lower())
        ops.create_folder(root_id, "", name, when=anchor - timedelta(days=400), mount_ns=target)

    for space in w.spaces:
        if space.kind == "team":
            mount(space.name, space_ns[space.id])
    for p in w.employees:
        mount(p.display_name, home_of[p.id])

    # -- documents ------------------------------------------------------------------------
    for doc in w.documents:
        _seed_document(ctx, ops, w, doc, space_ns[doc.space_id], by_person)

    _seed_extras(ctx, ops, w, space_ns, by_person)


def _doc_text(doc: Document) -> str:
    parts = [doc.title, *doc.paragraphs, doc.description]
    if doc.table:
        parts.extend(" ".join(str(c) for c in row if c is not None) for row in doc.table)
    return "\n".join(p for p in parts if p)


def _seed_document(
    ctx: ProviderContext,
    ops: Ops,
    w: World,
    doc: Document,
    ns_id: str,
    by_person: dict[str, dict[str, Any]],
) -> None:
    owner = by_person[doc.owner_id]
    modifier = by_person.get(doc.modified_by_id, owner)
    table = [list(r) for r in doc.table] if doc.table else None
    data, _ = docs.render(
        doc.kind,
        doc.title,
        list(doc.paragraphs),
        author=owner["display_name"],
        created=doc.created,
        table=table,
    )
    parent = ""
    if doc.folder:
        parent = ops.ensure_folders(
            ns_id, "", [clean_name(s) for s in doc.folder.split("/")], when=doc.created, owner=owner["id"]
        )
    name = clean_name(doc.name)
    taken = {r["name"].lower() for r in ops.tree.children(ns_id, parent)}
    if name.lower() in taken:
        name = autorename(name, taken)
    n = max(1, doc.versions)
    span = doc.modified - doc.created
    old = []
    for i in range(n - 1):
        when = doc.created + (span * i) // max(n - 1, 1)
        old.append(
            ops.revision(
                data,
                when=min(when + timedelta(seconds=7), w.anchor),
                client_modified=when,
                modified_by=owner["id"],
                rev=revision_for(f"{doc.id}/{i}"),
            )
        )
    rec = ops.create_file(
        ns_id,
        parent,
        name,
        data,
        when=min(doc.modified + timedelta(seconds=7), w.anchor),
        client_modified=doc.modified,
        modified_by=modifier["id"],
        owner=owner["id"],
        revisions=old,
        extra={"doc": doc.id, "no_inherit": doc.restricted},
        text=_doc_text(doc),
    )
    rec["revisions"][-1]["rev"] = revision_for(f"{doc.id}/{n - 1}")
    for share in doc.shares:
        _seed_share(ctx, rec, doc, share, owner, by_person)


def _seed_share(
    ctx: ProviderContext,
    rec: dict[str, Any],
    doc: Document,
    share: Share,
    owner: dict[str, Any],
    by_person: dict[str, dict[str, Any]],
) -> None:
    created = share.created or doc.modified
    if share.link != "none" and share.principal_id is None:
        slug = link_slug("link/" + doc.id, 25)
        ctx.store.collection(LINKS).put(
            {
                "id": slug,
                "kind": "file",
                "entry": rec["id"],
                "creator": owner["id"],
                "audience": "public" if share.link == "anyone" else "team",
                "access": "editor" if share.role == "editor" else "viewer",
                "allow_download": True,
                "password": None,
                "expires": ts(share.expires) if share.expires else None,
                "created": ts(created),
                "rlkey": link_slug("rlkey/" + doc.id, 25),
                "legacy": doc.id.endswith("2"),  # one legacy /s/<id>/<name> link, one /scl/fi/ link
            }
        )
        return
    acct = by_person.get(share.principal_id or "")
    if acct is None and share.email:
        for candidate in by_person.values():
            if candidate["email"].lower() == share.email.lower():
                acct = candidate
    if acct is None:
        return
    rec["members"].append(
        {
            "account_id": acct["id"],
            "access_type": FILE_ROLE_MAP.get(share.role, "viewer"),
            "created": ts(created),
            "expires": ts(share.expires) if share.expires else None,
            "invited_by": owner["id"],
        }
    )


def _seed_extras(
    ctx: ProviderContext,
    ops: Ops,
    w: World,
    space_ns: dict[str, str],
    by_person: dict[str, dict[str, Any]],
) -> None:
    """Vendor-specific extras: Dropbox Paper docs (not downloadable, exportable) and PNG images."""
    anchor = w.anchor
    knowledge = space_ns.get("knowledge")
    if knowledge:
        writer = by_person[w.space("knowledge").owner_id]
        notes = ops.ensure_folders(
            knowledge, "", ["Paper"], when=anchor - timedelta(days=60), owner=writer["id"]
        )
        samples = [
            (
                "Onboarding Checklist",
                ["Set up laptop and accounts", "Meet your manager", "Complete security training"],
            ),
            (
                "Engagement Kickoff Template",
                ["Goals and scope", "Team roles and rates", "Key dates and deliverables"],
            ),
        ]
        for i, (title, bullets) in enumerate(samples):
            md = f"# {title}\n\n" + "\n".join(f"- {b}" for b in bullets) + "\n"
            rec = ops.create_file(
                knowledge,
                notes,
                f"{title}.paper",
                md.encode(),
                when=anchor - timedelta(days=20 + i),
                modified_by=writer["id"],
                owner=writer["id"],
                extra={
                    "downloadable": False,
                    "export": {"export_as": "markdown", "export_options": ["html", "markdown"]},
                    "paper": True,
                },
                text=md,
            )
            rec["revisions"][-1]["size"] = 0
    growth = space_ns.get("growth")
    if growth:
        owner = by_person[w.space("growth").owner_id]
        folder = ops.ensure_folders(
            growth, "", ["Brand Assets", "Photos"], when=anchor - timedelta(days=90), owner=owner["id"]
        )
        for i, (name, width, height) in enumerate(
            [
                ("Office Entrance.png", 160, 90),
                ("Team Offsite.png", 128, 128),
                ("Conference Booth.png", 96, 96),
            ]
        ):
            ops.create_file(
                growth,
                folder,
                name,
                docs.make_png(width, height, seed=700 + i),
                when=anchor - timedelta(days=30 + i),
                owner=owner["id"],
            )


def unique_name(name: str, taken: set[str]) -> str:
    if name.lower() not in taken:
        return name
    return autorename(name, taken)


__all__ = ["seed", "split_ext", "unique_name"]
