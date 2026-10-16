"""Seeding: the Halcyon Advisory org projected into Salesforce (setup objects first).

``seed(ctx)`` builds, in order: Organization, Profiles, the role hierarchy (mirroring the org
chart by department), Users (every employee plus an integration user), then hands over to
:mod:`seed_crm` (accounts, contacts, opportunities, pricebooks, leads, cases, campaigns,
engagements) and :mod:`seed_activity` (events and tasks).
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.salesforce import seed_activity, seed_crm
from emulators.providers.salesforce.common import fmt_dt, to_18
from emulators.providers.salesforce.org import Org
from emulators.providers.salesforce.schema import SCHEMA
from emulators.providers.salesforce.seedlib import SeedState, at, blank, hnum, modified_after, stamp
from emulators.world.model import Department, Person, World

PROFILE_FLAGS: dict[str, dict[str, bool]] = {
    "System Administrator": {
        "PermissionsEmailMass": True, "PermissionsExportReport": True, "PermissionsDataExport": True,
        "PermissionsManageUsers": True, "PermissionsViewSetup": True, "PermissionsModifyAllData": True,
        "PermissionsViewAllData": True, "PermissionsConvertLeads": True,
        "PermissionsEditPublicDocuments": True,
    },  # fmt: skip
    "Standard User": {"PermissionsConvertLeads": True, "PermissionsExportReport": True},
    "Sales User": {
        "PermissionsConvertLeads": True, "PermissionsExportReport": True, "PermissionsEmailMass": True,
        "PermissionsImportPersonal": True,
    },  # fmt: skip
    "Read Only": {"PermissionsEditTask": False, "PermissionsEditEvent": False},
    "Salesforce API Only System Integrations": {
        "PermissionsApiUserOnly": True, "PermissionsModifyAllData": True, "PermissionsViewAllData": True,
        "PermissionsViewSetup": True,
    },  # fmt: skip
}
READ_ONLY_TITLES = ("Office Manager", "Learning & Development Coordinator", "IT Support Specialist")
HEAD_ROLE_NAMES = {
    "audit": "Audit Partner",
    "tax": "Tax Partner",
    "advisory": "Advisory Partner",
    "finance": "Chief Financial Officer",
    "people": "Head of People & Culture",
    "it": "Director of Technology",
    "growth": "Director of Business Development",
}
DEPT_SHORT = {
    "audit": "Audit", "tax": "Tax", "advisory": "Advisory", "finance": "Finance", "people": "People & Culture",
    "it": "Technology", "growth": "Business Development",
}  # fmt: skip


def seed(ctx: ProviderContext) -> None:
    org = Org(ctx)
    state = SeedState(org)
    world = ctx.world
    seed_organization(org, world)
    profiles = seed_profiles(org, world)
    roles = seed_roles(org, world)
    seed_users(org, world, state, profiles, roles)
    seed_crm.seed(state, world)
    seed_activity.seed(state, world)


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_")


# -- organization --------------------------------------------------------------------------


def seed_organization(org: Org, world: World) -> None:
    o = world.org
    obj = SCHEMA.objects["Organization"]
    rec = blank(obj)
    created = at(o.founded, 9)
    rec.update(
        Id=org.org_id18,
        Name=o.name,
        Street=o.address.street,
        City=o.address.city,
        State=o.address.state,
        PostalCode=o.address.postal_code,
        Country=o.address.country,
        Phone=o.phone,
        PrimaryContact=world.person("p001").display_name if world.people else None,
        DefaultLocaleSidKey="en_CA",
        TimeZoneSidKey="America/Toronto",
        LanguageLocaleKey="en_US",
        ReceivesInfoEmails=True,
        ReceivesAdminInfoEmails=True,
        FiscalYearStartMonth=o.fiscal_year_start_month,
        DefaultAccountAccess="Read",
        DefaultContactAccess="Read",
        DefaultOpportunityAccess="None",
        DefaultLeadAccess="None",
        DefaultCaseAccess="None",
        DefaultCalendarAccess="None",
        DefaultPricebookAccess="Read",
        DefaultCampaignAccess="Read",
        UiSkin="Theme3",
        OrganizationType="Enterprise Edition",
        InstanceName="NA224",
        IsSandbox=False,
        WebToCaseDefaultOrigin="Web",
        MonthlyPageViewsUsed=0,
        MonthlyPageViewsEntitlement=0,
        IsReadOnly=False,
    )
    stamp(rec, created, modified=world.anchor - timedelta(days=30))
    org.put("Organization", rec)


# -- profiles & roles ----------------------------------------------------------------------


def seed_profiles(org: Org, world: World) -> dict[str, str]:
    obj = SCHEMA.objects["Profile"]
    ids: dict[str, str] = {}
    created = at(world.org.founded, 9)
    for name, flags in PROFILE_FLAGS.items():
        rec = blank(obj)
        rec["Id"] = org.new_id("Profile")
        rec.update(Name=name, UserType="Standard", UserLicenseId=to_18("1005g0000001" + f"{len(ids):03d}"))
        rec["PermissionsEmailSingle"] = True
        rec["PermissionsEditTask"] = True
        rec["PermissionsEditEvent"] = True
        rec["PermissionsApiEnabled"] = True
        rec.update(flags)
        rec["Description"] = f"{name} profile"
        stamp(rec, created, created_by=None)
        ids[name] = rec["Id"]
        org.put("Profile", rec)
    return ids


def _dept_roles(world: World) -> list[tuple[str, str, str | None]]:
    """(key, name, parent key) for every role; parents always precede children."""
    roles: list[tuple[str, str, str | None]] = [("root", "Managing Partner", None)]
    exec_head = next((d.head_id for d in world.departments if d.parent_id is None), "p001")
    for p in world.employees:
        if p.department_id == "exec" and p.id != exec_head:
            roles.append((f"person:{p.id}", _shorten_title(p), "root"))
    ordered = sorted(
        (d for d in world.departments if d.parent_id is not None),
        key=lambda d: _depth_of_manager(world, d),
    )
    for dept in ordered:
        head = world.person(dept.head_id)
        parent = _role_key_of(world, head.manager_id) if head.manager_id else "root"
        short = DEPT_SHORT.get(dept.id, dept.name)
        roles.append((f"{dept.id}:head", HEAD_ROLE_NAMES.get(dept.id, f"{dept.name} Head"), parent))
        staff_parent = f"{dept.id}:head"
        if _has_manager_tier(world, dept):
            roles.append((f"{dept.id}:manager", f"{short} Manager", f"{dept.id}:head"))
            staff_parent = f"{dept.id}:manager"
        roles.append((f"{dept.id}:staff", f"{short} Staff", staff_parent))
    return roles


def _shorten_title(p: Person) -> str:
    title = p.job_title
    if "Executive Assistant" in title:
        return "Executive Assistant"
    return title


def _depth_of_manager(world: World, dept: Department) -> int:
    depth, current = 0, world.person(dept.head_id)
    while current.manager_id and depth < 10:
        current = world.person(current.manager_id)
        depth += 1
    return depth


def _has_manager_tier(world: World, dept: Department) -> bool:
    members = [p for p in world.employees if p.department_id == dept.id]
    return any(
        any(r.department_id == dept.id for r in world.direct_reports(p.id))
        for p in members
        if p.manager_id == dept.head_id
    )


def _role_key_of(world: World, person_id: str | None) -> str:
    if person_id is None:
        return "root"
    p = world.person(person_id)
    dept = world.department(p.department_id) if p.department_id else None
    if dept is None or dept.parent_id is None:
        return "root" if p.manager_id is None else f"person:{p.id}"
    if p.id == dept.head_id:
        return f"{dept.id}:head"
    if _has_manager_tier(world, dept) and p.manager_id == dept.head_id and any(
        r.department_id == dept.id for r in world.direct_reports(p.id)
    ):
        return f"{dept.id}:manager"
    return f"{dept.id}:staff"


def seed_roles(org: Org, world: World) -> dict[str, str]:
    obj = SCHEMA.objects["UserRole"]
    ids: dict[str, str] = {}
    created = at(world.org.founded, 9)
    for key, name, parent in _dept_roles(world):
        rec = blank(obj)
        rec["Id"] = org.new_id("UserRole")
        rec.update(
            Name=name,
            DeveloperName=_slug(name),
            RollupDescription=name,
            ParentRoleId=ids.get(parent) if parent else None,
            OpportunityAccessForAccountOwner="None",
            CaseAccessForAccountOwner="None",
            ContactAccessForAccountOwner="Edit",
            MayForecastManagerShare=False,
            PortalType="None",
        )
        rec["LastModifiedDate"] = fmt_dt(created)
        rec["SystemModstamp"] = fmt_dt(created)
        ids[key] = rec["Id"]
        org.put("UserRole", rec)
    return ids


# -- users ---------------------------------------------------------------------------------


def _profile_for(p: Person) -> str:
    if p.is_admin:
        return "System Administrator"
    if p.department_id == "growth":
        return "Sales User"
    if p.job_title in READ_ONLY_TITLES:
        return "Read Only"
    return "Standard User"


def _alias(p: Person) -> str:
    base = re.sub(r"[^a-z0-9]", "", (p.first_name[:1] + p.last_name).lower())
    return base[:8]


def seed_users(
    org: Org, world: World, state: SeedState, profiles: dict[str, str], roles: dict[str, str]
) -> None:
    obj = SCHEMA.objects["User"]
    anchor = world.anchor
    domain = world.org.domain
    offices = {o.id: o for o in world.offices}
    first_admin = next((p for p in world.employees if p.is_admin), world.employees[0])
    # Pre-allocate ids in world order so managers can be referenced before they are saved.
    records: dict[str, dict[str, Any]] = {}
    for p in world.employees:
        rec = blank(obj)
        rec["Id"] = org.new_id("User")
        records[p.id] = rec
        state.user_by_person[p.id] = rec["Id"]
    state.admin_id = state.user_by_person[first_admin.id]
    for p in world.employees:
        rec = records[p.id]
        office = offices.get(p.office_id or "")
        dept = world.department(p.department_id) if p.department_id else None
        created = at(p.hire_date, 9) if p.hire_date else at(world.org.founded, 9)
        created = min(created, anchor - timedelta(days=1))
        rec.update(
            Username=f"{p.username}@{domain}.crm",
            LastName=p.last_name,
            FirstName=p.first_name,
            Name=p.display_name,
            CompanyName=world.org.name,
            Department=dept.name if dept else None,
            Title=p.job_title,
            Street=office.address.street if office else None,
            City=office.address.city if office else None,
            State=office.address.state if office else None,
            PostalCode=office.address.postal_code if office else None,
            Country=office.address.country if office else None,
            Email=p.email,
            SenderEmail=p.email,
            SenderName=p.display_name,
            Phone=p.phone_business,
            MobilePhone=p.phone_mobile,
            Alias=_alias(p),
            CommunityNickname=p.username,
            IsActive=p.active,
            TimeZoneSidKey=p.timezone,
            UserRoleId=roles[_user_role_key(world, p)],
            LocaleSidKey=p.locale.replace("-", "_"),
            ReceivesInfoEmails=False,
            ReceivesAdminInfoEmails=p.is_admin,
            EmailEncodingKey="UTF-8",
            ProfileId=profiles[_profile_for(p)],
            UserType="Standard",
            LanguageLocaleKey="en_US",
            EmployeeNumber=p.employee_id,
            ManagerId=state.user_by_person.get(p.manager_id) if p.manager_id else None,
            LastLoginDate=fmt_dt(anchor - timedelta(hours=1 + hnum("login", p.id, mod=96)))
            if p.active
            else None,
            LastPasswordChangeDate=fmt_dt(anchor - timedelta(days=20 + hnum("pw", p.id, mod=60))),
            NumberOfFailedLogins=0,
            FederationIdentifier=p.email,
            FullPhotoUrl=f"https://{domain.split('.')[0]}.file.force.com/profilephoto/005/F",
            SmallPhotoUrl=f"https://{domain.split('.')[0]}.file.force.com/profilephoto/005/T",
            IsExtIndicatorVisible=False,
            IsProfilePhotoActive=False,
            _person_id=p.id,
        )
        stamp(
            rec,
            created,
            modified=modified_after(anchor, created, "user", p.id, max_days=90),
            created_by=state.admin_id,
        )
        org.put("User", rec)
    _integration_user(org, world, state, profiles)


def _user_role_key(world: World, p: Person) -> str:
    key = _role_key_of(world, p.id)
    if key.startswith("person:") or key == "root":
        return key
    return key


def _integration_user(org: Org, world: World, state: SeedState, profiles: dict[str, str]) -> None:
    obj = SCHEMA.objects["User"]
    rec = blank(obj)
    rec["Id"] = org.new_id("User")
    created = at(world.org.founded.replace(year=world.org.founded.year + 8), 9)
    created = min(created, world.anchor - timedelta(days=30))
    domain = world.org.domain
    rec.update(
        Username=f"integration@{domain}.crm",
        LastName="User",
        FirstName="Integration",
        Name="Integration User",
        CompanyName=world.org.name,
        Department="Technology",
        Title="API integration service account",
        Email=f"salesforce-integration@{domain}",
        SenderEmail=f"salesforce-integration@{domain}",
        SenderName="Integration User",
        Alias="integ",
        CommunityNickname="integration.user",
        IsActive=True,
        TimeZoneSidKey="America/Toronto",
        LocaleSidKey="en_US",
        EmailEncodingKey="UTF-8",
        ProfileId=profiles["Salesforce API Only System Integrations"],
        UserType="Standard",
        LanguageLocaleKey="en_US",
        NumberOfFailedLogins=0,
        ReceivesInfoEmails=False,
        ReceivesAdminInfoEmails=False,
        IsExtIndicatorVisible=False,
        IsProfilePhotoActive=False,
        _person_id="integration",
    )
    stamp(rec, created, created_by=state.admin_id)
    state.integration_id = rec["Id"]
    org.put("User", rec)

