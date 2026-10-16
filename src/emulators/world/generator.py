"""Deterministic construction of the shared :class:`~emulators.world.model.World`.

``build_world(seed, anchor)`` is pure: the same seed and anchor date always produce the
same world. The anchor is truncated to midnight UTC so a hub started twice on the same
day sees identical timestamps; pass ``EMULATORS_ANCHOR=2026-10-01`` to pin it in CI.

Dates are generated relative to the anchor: meetings run from six weeks before to three
weeks after it, emails cover the last two months, invoices and bills the last twelve.
"""

from __future__ import annotations

import random
import re
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

from emulators.core.ids import stable_guid, stable_hash
from emulators.world import content as C
from emulators.world import data as D
from emulators.world.model import (
    Account,
    Address,
    Bill,
    BillLine,
    CardCustomer,
    Channel,
    ChatMessage,
    Client,
    Conversation,
    Deal,
    Department,
    DirectChat,
    Document,
    EmailMessage,
    EmailThread,
    Engagement,
    Group,
    Invoice,
    InvoiceLine,
    Item,
    Lead,
    Meeting,
    Office,
    Organization,
    Participation,
    Payment,
    Person,
    Product,
    Share,
    Space,
    SupportCase,
    Team,
    TextMessage,
    TranscriptTurn,
    Vendor,
    World,
)

MOBILE_AREA = {"toronto": "647", "new_york": "917", "chicago": "773", "london": None}
OFFICE_AREA = {"toronto": "416", "new_york": "212", "chicago": "312", "london": None}


def slugify(text: str, sep: str = "-") -> str:
    text = text.lower().replace("&", "and").replace("'", "")
    text = re.sub(r"[^a-z0-9]+", sep, text)
    return text.strip(sep)


def nanp(area: str, n: int) -> str:
    return f"+1{area}55501{n % 100:02d}"


def uk(n: int, mobile: bool = False) -> str:
    return f"+447700900{n % 1000:03d}" if mobile else f"+4420794600{n % 100:02d}"


def build_world(seed: str, anchor: datetime) -> World:
    """Build (and memoise) the world for ``seed`` anchored at ``anchor``'s UTC date."""
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=UTC)
    return _build_cached(str(seed), anchor.astimezone(UTC).date())


@lru_cache(maxsize=8)
def _build_cached(seed: str, day: date) -> World:
    return _Builder(seed, datetime.combine(day, time(0, 0), tzinfo=UTC)).build()


class _Builder:
    def __init__(self, seed: str, anchor: datetime) -> None:
        self.seed = seed
        self.anchor = anchor
        self.today = anchor.date()

    def rng(self, name: str) -> random.Random:
        return random.Random(f"{self.seed}/{name}")

    # -----------------------------------------------------------------------------
    def build(self) -> World:
        self.org = self._org()
        self.offices = self._offices()
        self.employees = self._employees()
        self.departments = self._departments()
        self.vendors, vendor_contacts = self._vendors()
        self.clients, client_contacts = self._clients()
        self.people = [*self.employees, *client_contacts, *vendor_contacts]
        self.by_id = {p.id: p for p in self.people}
        self.groups = self._groups()
        self.engagements = self._engagements()
        self.teams = self._teams()
        self.chats = self._direct_chats()
        self.spaces = self._spaces()
        self.documents = self._documents()
        self.meetings = self._meetings()
        self.accounts, self.items = self._ledger()
        self.invoices, self.payments = self._invoices()
        self.bills = self._bills()
        self.email_threads = self._emails()
        self.deals, self.leads, self.cases = self._crm()
        self.products, self.card_customers = self._stripe()
        self.conversations = self._conversations()
        return World(
            seed=self.seed,
            anchor=self.anchor,
            org=self.org,
            offices=self.offices,
            departments=self.departments,
            people=self.people,
            groups=self.groups,
            teams=self.teams,
            chats=self.chats,
            clients=self.clients,
            vendors=self.vendors,
            engagements=self.engagements,
            spaces=self.spaces,
            documents=self.documents,
            meetings=self.meetings,
            email_threads=self.email_threads,
            accounts=self.accounts,
            items=self.items,
            invoices=self.invoices,
            payments=self.payments,
            bills=self.bills,
            deals=self.deals,
            leads=self.leads,
            cases=self.cases,
            products=self.products,
            card_customers=self.card_customers,
            conversations=self.conversations,
        )

    # -- helpers ---------------------------------------------------------------------
    def at(self, day: date, hour: int, minute: int, tz: str) -> datetime:
        local = datetime.combine(day, time(hour, minute), tzinfo=ZoneInfo(tz))
        return local.astimezone(UTC)

    def business_days(self, start: date, end: date) -> list[date]:
        days, d = [], start
        while d <= end:
            if d.weekday() < 5:
                days.append(d)
            d += timedelta(days=1)
        return days

    def p(self, person_id: str) -> Person:
        return self.by_id[person_id]

    def dept_members(self, dept: str, *, active: bool = True) -> list[Person]:
        return [e for e in self.employees if e.department_id == dept and (e.active or not active)]

    # -- organisation ------------------------------------------------------------------
    def _org(self) -> Organization:
        o = D.ORG
        h = lambda *k: stable_hash(self.seed, *k, length=40)  # noqa: E731
        return Organization(
            name=o["name"],
            legal_name=o["legal_name"],
            slug=o["slug"],
            domain=o["domain"],
            website=o["website"],
            phone="+14165550100",
            address=Address("200 Bay Street, Suite 2400", "Toronto", "ON", "M5J 2J2", "Canada", "CA"),
            founded=date(2009, 3, 2),
            industry=o["industry"],
            currency=o["currency"],
            fiscal_year_start_month=o["fiscal_year_start_month"],
            microsoft_tenant_id=stable_guid(self.seed, "microsoft-tenant"),
            google_customer_id="C0" + h("google")[:7],
            salesforce_org_id="00D5g000004" + h("sf")[:4].upper(),
            quickbooks_realm_id="9341" + str(int(h("qbo")[:12], 16))[:12].rjust(12, "7"),
            stripe_account_id="acct_1" + h("stripe")[:15].upper(),
            zoom_account_id="hAlCy0n" + h("zoom")[:15],
            slack_team_id="T0" + h("slack")[:9].upper(),
            box_enterprise_id=str(int(h("box")[:8], 16))[:9],
            dropbox_team_id="dbtid:AAD" + h("dropbox")[:30],
            meta_business_id="10" + str(int(h("meta")[:12], 16))[:13],
            twilio_account_sid="AC" + h("twilio")[:32],
        )

    def _offices(self) -> list[Office]:
        return [
            Office(i, n, Address(st, city, state, pc, country, cc), tz, wtz, phone)
            for (i, n, st, city, state, pc, country, cc, tz, wtz, phone) in D.OFFICES
        ]

    def _employees(self) -> list[Person]:
        rng = self.rng("employees")
        tz_by_office = {o[0]: o[8] for o in D.OFFICES}
        people = []
        for idx, (first, last, title, dept, office, mgr, flags) in enumerate(D.EMPLOYEES, start=1):
            username = f"{first.lower().replace(' ', '')}.{re.sub(r'[^a-z]', '', last.lower())}"
            area = OFFICE_AREA[office]
            hire = date(2010 + (idx * 7) % 15, 1 + (idx * 5) % 12, 1 + (idx * 11) % 27)
            people.append(
                Person(
                    id=f"p{idx:03d}",
                    first_name=first,
                    last_name=last,
                    display_name=f"{first} {last}",
                    email=f"{username}@{D.ORG['domain']}",
                    username=username,
                    job_title=title,
                    department_id=dept,
                    office_id=office,
                    manager_id=f"p{mgr:03d}" if mgr else None,
                    timezone=tz_by_office[office],
                    phone_business=nanp(area, idx) if area else uk(idx),
                    phone_mobile=(
                        nanp(MOBILE_AREA[office], 50 + idx) if MOBILE_AREA[office] else uk(idx, True)
                    ),
                    employee_id=f"HAG-{1000 + idx}",
                    hire_date=hire,
                    is_admin="A" in flags,
                    is_partner="P" in flags,
                    locale="en-GB" if office == "london" else ("en-CA" if office == "toronto" else "en-US"),
                    pronouns=rng.choice(D.PRONOUNS),
                    birthday=date(1970 + (idx * 3) % 30, 1 + (idx * 7) % 12, 1 + (idx * 13) % 28),
                    skills=tuple(rng.sample(D.SKILLS[dept], k=min(3, len(D.SKILLS[dept])))),
                    active="X" not in flags,
                )
            )
        return people

    def _departments(self) -> list[Department]:
        heads = {}
        for e in self.employees:
            if e.department_id not in heads and e.active:
                heads[e.department_id] = e.id
        return [
            Department(i, n, heads[i], parent, cc, desc, slugify(n, ""))
            for (i, n, parent, cc, desc) in D.DEPARTMENTS
        ]

    def _vendors(self) -> tuple[list[Vendor], list[Person]]:
        vendors, contacts = [], []
        for idx, (name, cat, acct, monthly, terms, st, city, state, pc, country, cc, area) in enumerate(
            D.VENDORS, start=1
        ):
            vid = f"v{idx:03d}"
            dom = slugify(name, "") + ".example"
            vendors.append(
                Vendor(
                    vid,
                    name,
                    f"billing@{dom}",
                    nanp(area, 60 + idx),
                    cat,
                    Address(st, city, state, pc, country, cc),
                    terms,
                    acct,
                    monthly,
                    f"{10 + idx}-{1000000 + idx * 7919}",
                )
            )
            first = D.CONTACT_FIRST[(idx * 7 + 3) % len(D.CONTACT_FIRST)]
            last = D.CONTACT_LAST[(idx * 11 + 5) % len(D.CONTACT_LAST)]
            username = f"{first.lower()}.{slugify(last, '')}"
            contacts.append(
                Person(
                    id=f"{vid}-1",
                    first_name=first,
                    last_name=last,
                    display_name=f"{first} {last}",
                    email=f"{username}@{dom}",
                    username=username,
                    job_title="Accounts Receivable",
                    department_id=None,
                    office_id=None,
                    manager_id=None,
                    timezone="America/Toronto",
                    phone_business=nanp(area, 60 + idx),
                    phone_mobile=None,
                    employee_id=None,
                    hire_date=None,
                    is_external=True,
                    company_id=vid,
                )
            )
        return vendors, contacts

    def _clients(self) -> tuple[list[Client], list[Person]]:
        rng = self.rng("clients")
        owners = ["p004", "p012", "p018", "p001", "p040", "p019"]
        clients, contacts = [], []
        n_contact = 0
        for idx, row in enumerate(D.CLIENTS, start=1):
            (
                name,
                suffix,
                industry,
                emp,
                revenue,
                st,
                city,
                state,
                pc,
                country,
                cc,
                area,
                platform,
                card,
                status,
                desc,
            ) = row
            cid = f"c{idx:03d}"
            dom = slugify(name, "") + ".example"
            k = 2 + (idx % 3)
            contact_ids = []
            tz = (
                "Europe/London"
                if cc == "GB"
                else (
                    "America/Los_Angeles"
                    if state in ("CA", "OR", "BC")
                    else "America/Denver"
                    if state in ("CO", "AB")
                    else "America/Chicago"
                    if state in ("IL", "TX", "WI")
                    else "America/Halifax"
                    if state == "NS"
                    else "America/New_York"
                    if cc == "US"
                    else "America/Toronto"
                )
            )
            for j in range(k):
                n_contact += 1
                first = D.CONTACT_FIRST[(idx * 5 + j * 17) % len(D.CONTACT_FIRST)]
                last = D.CONTACT_LAST[(idx * 7 + j * 13) % len(D.CONTACT_LAST)]
                username = f"{first.lower()}.{slugify(last, '')}"
                pid = f"{cid}-{j + 1}"
                contact_ids.append(pid)
                phone = uk(500 + n_contact) if cc == "GB" else nanp(area, n_contact)
                mobile = uk(500 + n_contact, True) if cc == "GB" else nanp(area, 40 + n_contact)
                contacts.append(
                    Person(
                        id=pid,
                        first_name=first,
                        last_name=last,
                        display_name=f"{first} {last}",
                        email=f"{username}@{dom}",
                        username=username,
                        job_title=D.CONTACT_TITLES[(idx + j * 3) % len(D.CONTACT_TITLES)],
                        department_id=None,
                        office_id=None,
                        manager_id=None,
                        timezone=tz,
                        phone_business=phone,
                        phone_mobile=mobile,
                        employee_id=None,
                        hire_date=None,
                        is_external=True,
                        company_id=cid,
                        locale="en-GB" if cc == "GB" else "en-US",
                    )
                )
            since = date(self.today.year - 1 - (idx % 6), 1 + idx % 12, 1 + idx % 27)
            clients.append(
                Client(
                    id=cid,
                    name=name,
                    legal_name=f"{name} {suffix}",
                    domain=dom,
                    industry=industry,
                    employees=emp,
                    annual_revenue=revenue,
                    address=Address(st, city, state, pc, country, cc),
                    phone=uk(400 + idx) if cc == "GB" else nanp(area, 10 + idx),
                    website=f"https://www.{dom}",
                    owner_id=owners[idx % len(owners)],
                    contact_ids=tuple(contact_ids),
                    primary_contact_id=contact_ids[0],
                    status=status,
                    client_since=since if status != "Prospect" else self.today,
                    payment_terms_days=rng.choice([15, 30, 30, 30, 45]),
                    pays_by_card=card,
                    preferred_platform=platform,
                    tax_id=f"{rng.randint(10, 99)}-{rng.randint(1000000, 9999999)}",
                    description=desc,
                    rating=rng.choice(["Hot", "Warm", "Warm", "Cold"]),
                )
            )
        return clients, contacts

    def _groups(self) -> list[Group]:
        dom = D.ORG["domain"]
        active = [e for e in self.employees if e.active]
        groups: list[Group] = []
        for dept_id, name, _, _, desc in D.DEPARTMENTS:
            members = [e.id for e in active if e.department_id == dept_id]
            head = next(e.id for e in active if e.department_id == dept_id)
            nick = slugify(name, "")
            groups.append(
                Group(
                    f"g-{dept_id}",
                    name,
                    f"{nick}@{dom}",
                    desc,
                    "department",
                    tuple(members),
                    (head,),
                    dept_id,
                    "Private",
                )
            )
        groups.append(
            Group(
                "g-all",
                "All Staff",
                f"allstaff@{dom}",
                "Everyone at Halcyon Advisory Group.",
                "all_staff",
                tuple(e.id for e in active),
                ("p001", "p031"),
                None,
                "Public",
            )
        )
        partners = tuple(e.id for e in active if e.is_partner)
        groups.append(
            Group(
                "g-partners",
                "Partners",
                f"partners@{dom}",
                "Equity partners of the firm.",
                "role",
                partners,
                ("p001",),
                None,
                "Private",
            )
        )
        leadership = (*partners, "p002", "p025")
        groups.append(
            Group(
                "g-leadership",
                "Leadership Team",
                f"leadership@{dom}",
                "Partners plus the COO and CFO.",
                "role",
                leadership,
                ("p001",),
                None,
                "Private",
            )
        )
        managers = tuple(sorted({e.manager_id for e in active if e.manager_id}))
        groups.append(
            Group(
                "g-managers",
                "People Managers",
                f"managers@{dom}",
                "Everyone with direct reports.",
                "role",
                managers,
                ("p031",),
                None,
                "Private",
            )
        )
        admins = tuple(e.id for e in active if e.is_admin)
        groups.append(
            Group(
                "g-it-admins",
                "IT Administrators",
                f"it-admins@{dom}",
                "Tenant and workspace administrators.",
                "role",
                admins,
                ("p035",),
                None,
                "Private",
            )
        )
        for office in D.OFFICES:
            oid, oname = office[0], office[1]
            members = tuple(e.id for e in active if e.office_id == oid)
            groups.append(
                Group(
                    f"g-office-{oid}",
                    f"{oname.split(' (')[0]} Office",
                    f"office-{slugify(oid)}@{dom}",
                    f"Everyone based in {oname}.",
                    "distribution",
                    members,
                    ("p029",),
                    None,
                    "Public",
                )
            )
        return groups

    def _engagements(self) -> list[Engagement]:
        rng = self.rng("engagements")
        services = {s[0]: s for s in D.SERVICES}
        engagements: list[Engagement] = []
        n = 0
        for client in self.clients:
            if client.status == "Prospect":
                continue
            options = D.INDUSTRY_SERVICES.get(client.industry) or rng.sample(
                [
                    "Audit",
                    "Tax Compliance",
                    "Systems Advisory",
                    "Bookkeeping",
                    "Fractional CFO",
                    "Transaction Advisory",
                    "Review Engagement",
                    "Tax Advisory",
                ],
                2,
            )
            count = 1 if client.status == "Former" else (2 if rng.random() < 0.45 else 1)
            for service in options[:count]:
                n += 1
                _svc, item, rate, (lo, hi), billing, dept = services[service]
                staff = [e for e in self.dept_members(dept) if not e.is_partner]
                lead = next(e for e in self.dept_members(dept))
                manager = staff[0] if staff else lead
                team = [manager.id, *(e.id for e in rng.sample(staff[1:], k=min(2, len(staff) - 1)))]
                if client.status == "Former":
                    start = self.today - timedelta(days=rng.randint(300, 420))
                    end = start + timedelta(days=rng.randint(60, 120))
                else:
                    start = self.today - timedelta(days=rng.randint(30, 260))
                    end = start + timedelta(
                        days=rng.randint(120, 330) if billing == "Retainer" else rng.randint(60, 200)
                    )
                status = (
                    "Completed" if end < self.today else ("Planned" if start > self.today else "In Progress")
                )
                hours = rng.randint(lo, hi)
                fixed = round(hours * rate / 500) * 500.0 if billing == "Fixed fee" else None
                year = start.year if start.month >= 3 else start.year - 1
                label = {
                    "Audit": f"FY{year} Financial Statement Audit",
                    "Review Engagement": f"FY{year} Review",
                    "Tax Compliance": f"{year} Corporate Tax Returns",
                    "SR&ED Claim": f"FY{year} SR&ED Claim",
                }.get(service, service)
                engagements.append(
                    Engagement(
                        id=f"e{n:03d}",
                        client_id=client.id,
                        name=f"{client.name} — {label}",
                        service=service,
                        service_item=item,
                        lead_id=lead.id if lead.is_partner else client.owner_id,
                        team_ids=tuple(team),
                        start=start,
                        end=end,
                        status=status,
                        budget_hours=hours,
                        hourly_rate=rate,
                        fixed_fee=fixed,
                        billing=billing,
                    )
                )
        return engagements

    def _chat(
        self,
        rng: random.Random,
        prefix: str,
        authors: list[str],
        pool: list[str],
        count: int,
        start: datetime,
        clients: list[str],
    ) -> tuple[ChatMessage, ...]:
        msgs: list[ChatMessage] = []
        t = start
        for i in range(count):
            t += timedelta(hours=rng.randint(3, 40), minutes=rng.randint(0, 59))
            if t > self.anchor + timedelta(hours=8):
                break
            text = rng.choice(pool).format(client=rng.choice(clients) if clients else "the client")
            mid = f"{prefix}-{i + 1:03d}"
            author = rng.choice(authors)
            mentions = (
                (rng.choice([a for a in authors if a != author]),)
                if rng.random() < 0.15 and len(authors) > 1
                else ()
            )
            if mentions:
                text = f"@{self.p(mentions[0]).first_name} {text}"
            reactions = tuple(
                (rng.choice(["like", "heart", "laugh", "+1"]), rng.choice(authors))
                for _ in range(rng.randint(0, 2))
            )
            msgs.append(ChatMessage(mid, author, t, text, None, mentions, reactions))
            for r in range(rng.randint(0, 2) if rng.random() < 0.45 else 0):
                rt = t + timedelta(minutes=rng.randint(2, 90))
                msgs.append(
                    ChatMessage(f"{mid}-r{r + 1}", rng.choice(authors), rt, rng.choice(C.REPLY_LINES), mid)
                )
        return tuple(sorted(msgs, key=lambda m: m.created))

    def _teams(self) -> list[Team]:
        rng = self.rng("teams")
        start = self.anchor - timedelta(days=45)
        names = [c.name for c in self.clients if c.status == "Active"]
        active = [e.id for e in self.employees if e.active]
        g = {grp.id: grp for grp in self.groups}

        def team(
            tid: str,
            name: str,
            desc: str,
            owners: tuple[str, ...],
            members: tuple[str, ...],
            vis: str,
            channels: list[tuple[str, str, str, bool]],
            group_id: str | None = None,
            client_id: str | None = None,
        ) -> Team:
            chans = []
            for i, (cname, cdesc, pool, private) in enumerate(channels):
                ch_members = tuple(members[: max(3, len(members) // 2)]) if private else ()
                authors = list(ch_members or members)
                msgs = self._chat(
                    rng,
                    f"{tid}-ch{i}",
                    authors,
                    C.CHANNEL_LINES[pool],
                    rng.randint(8, 22),
                    start,
                    [self._client_name(client_id)] if client_id else names,
                )
                chans.append(Channel(f"{tid}-ch{i}", cname, cdesc, private, ch_members, msgs))
            return Team(
                tid, name, desc, owners, members, vis, slugify(name, ""), tuple(chans), group_id, client_id
            )

        teams = [
            team(
                "t-firm",
                "Halcyon Firmwide",
                "Firm-wide announcements and conversation.",
                ("p001", "p031"),
                tuple(active),
                "Public",
                [
                    ("General", "Firm-wide conversation", "general", False),
                    ("Announcements", "Official news from leadership", "general", False),
                    ("IT Help", "Ask Technology for help", "it", False),
                    ("Social", "Events, clubs and fun", "social", False),
                ],
                "g-all",
            ),
            team(
                "t-audit",
                "Audit & Assurance",
                "Audit practice collaboration.",
                ("p004",),
                g["g-audit"].member_ids,
                "Private",
                [
                    ("General", "", "general", False),
                    ("Fieldwork", "Day-to-day fieldwork coordination", "fieldwork", False),
                    ("Technical Accounting", "Questions on standards", "fieldwork", False),
                ],
                "g-audit",
            ),
            team(
                "t-tax",
                "Tax Practice",
                "Tax practice collaboration.",
                ("p012",),
                g["g-tax"].member_ids,
                "Private",
                [
                    ("General", "", "general", False),
                    ("Tax Updates", "Legislative changes", "tax", False),
                    ("Busy Season", "Extension and filing tracker", "tax", False),
                ],
                "g-tax",
            ),
            team(
                "t-advisory",
                "Advisory",
                "Advisory practice collaboration.",
                ("p018",),
                g["g-advisory"].member_ids,
                "Private",
                [
                    ("General", "", "general", False),
                    ("Deals", "Live transactions", "deals", False),
                    ("Deal Team - Confidential", "Restricted deal discussions", "deals", True),
                ],
                "g-advisory",
            ),
            team(
                "t-growth",
                "Business Development",
                "Pipeline and marketing.",
                ("p040",),
                (*g["g-growth"].member_ids, "p001", "p018"),
                "Private",
                [
                    ("General", "", "general", False),
                    ("Pipeline", "Opportunities and proposals", "pipeline", False),
                ],
                "g-growth",
            ),
            team(
                "t-leadership",
                "Partner Group",
                "Partners, COO and CFO.",
                ("p001",),
                g["g-leadership"].member_ids,
                "Private",
                [("General", "", "pipeline", False), ("Finance", "Firm financials", "general", False)],
                "g-leadership",
            ),
        ]
        for eng in self.engagements[:4]:
            if eng.status != "In Progress":
                continue
            client = self._client(eng.client_id)
            members = tuple(dict.fromkeys((eng.lead_id, *eng.team_ids)))
            teams.append(
                team(
                    f"t-{eng.id}",
                    f"{client.name} - {eng.service}",
                    f"Engagement team for {eng.name}.",
                    (eng.lead_id,),
                    members,
                    "Private",
                    [
                        ("General", "", "fieldwork", False),
                        ("Client Requests", "Requests and uploads", "requests", False),
                        ("Fees & Staffing", "Budget and staffing (private)", "general", True),
                    ],
                    None,
                    client.id,
                )
            )
        # Link engagement -> team id
        linked = {t.client_id: t.id for t in teams if t.client_id}
        self.engagements = [
            e if e.client_id not in linked else Engagement(**{**_asdict(e), "team_id": linked[e.client_id]})
            for e in self.engagements
        ]
        return teams

    def _client(self, cid: str) -> Client:
        return next(c for c in self.clients if c.id == cid)

    def _client_name(self, cid: str | None) -> str:
        return self._client(cid).name if cid else ""

    def _direct_chats(self) -> list[DirectChat]:
        rng = self.rng("chats")
        active = [e for e in self.employees if e.active]
        chats = []
        start = self.anchor - timedelta(days=20)
        pool = (
            C.CHANNEL_LINES["general"]
            + C.REPLY_LINES
            + [
                "Do you have 10 minutes after lunch?",
                "Can you review my workpapers before EOD?",
                "Running 5 min late to the call.",
                "Sent you the file on email.",
                "Thanks for covering today!",
            ]
        )
        names = [c.name for c in self.clients]
        for i, person in enumerate(active[:24]):
            if person.manager_id:
                members = (person.id, person.manager_id)
                chats.append(
                    DirectChat(
                        f"chat-{i + 1:03d}",
                        members,
                        None,
                        self._chat(
                            rng, f"chat-{i + 1:03d}", list(members), pool, rng.randint(4, 12), start, names
                        ),
                    )
                )
        groups = [
            ("Busy season crew", ["p013", "p014", "p015", "p016", "p017"]),
            ("Toronto lunch club", ["p009", "p011", "p027", "p029", "p034"]),
            ("Proposal - Willowbrook RFP", ["p004", "p040", "p042", "p005"]),
        ]
        for j, (topic, members) in enumerate(groups):
            cid = f"chat-g{j + 1:02d}"
            chats.append(
                DirectChat(
                    cid,
                    tuple(members),
                    topic,
                    self._chat(rng, cid, members, pool, rng.randint(6, 14), start, names),
                )
            )
        return chats

    # -- documents ---------------------------------------------------------------------
    def _spaces(self) -> list[Space]:
        spaces = [
            Space(
                "clients",
                "Client Engagements",
                "Engagement files for every client.",
                "team",
                "p001",
                (
                    ("g-partners", "owner"),
                    ("g-audit", "editor"),
                    ("g-tax", "editor"),
                    ("g-advisory", "editor"),
                    ("g-finance", "viewer"),
                ),
                "ClientEngagements",
            ),
            Space(
                "finance",
                "Finance & Operations",
                "Budgets, close, billing and vendor management.",
                "team",
                "p025",
                (("g-finance", "editor"), ("g-leadership", "viewer")),
                "FinanceOperations",
            ),
            Space(
                "people",
                "People & Culture",
                "Policies, onboarding, benefits and recruiting.",
                "team",
                "p031",
                (("g-people", "editor"), ("g-all", "viewer")),
                "PeopleCulture",
            ),
            Space(
                "it",
                "Technology",
                "Runbooks, security policies and inventories.",
                "team",
                "p035",
                (("g-it", "editor"), ("g-leadership", "viewer")),
                "Technology",
            ),
            Space(
                "growth",
                "Marketing & BD",
                "Brand, campaigns, proposals and pipeline.",
                "team",
                "p040",
                (("g-growth", "editor"), ("g-partners", "editor")),
                "MarketingBD",
            ),
            Space(
                "knowledge",
                "Knowledge Base",
                "Technical guidance and templates for the whole firm.",
                "team",
                "p002",
                (("g-all", "viewer"), ("g-managers", "editor"), ("g-partners", "editor")),
                "KnowledgeBase",
            ),
            Space(
                "leadership",
                "Partner Group",
                "Confidential partner materials.",
                "team",
                "p001",
                (("g-partners", "owner"), ("p002", "editor"), ("p025", "editor")),
                "PartnerGroup",
            ),
        ]
        for e in self.employees:
            if e.active:
                spaces.append(
                    Space(
                        f"personal-{e.id}",
                        f"{e.display_name}'s files",
                        "Personal files.",
                        "personal",
                        e.id,
                        ((e.id, "owner"),),
                        e.username.replace(".", "_"),
                    )
                )
        return spaces

    def _documents(self) -> list[Document]:
        rng = self.rng("documents")
        docs: list[Document] = []
        org = self.org.name

        def add(
            space: str,
            folder: str,
            name: str,
            title: str,
            paragraphs: list[str],
            owner: str,
            *,
            table: list[list[object]] | None = None,
            days_ago: int | None = None,
            shares: tuple[Share, ...] = (),
            client_id: str | None = None,
            engagement_id: str | None = None,
            restricted: bool = False,
            editor: str | None = None,
            description: str = "",
        ) -> None:
            created = self.anchor - timedelta(
                days=days_ago if days_ago is not None else rng.randint(5, 400),
                hours=rng.randint(0, 9),
                minutes=rng.randint(0, 59),
            )
            created = created.replace(hour=13 + rng.randint(0, 8))
            modified = min(
                created + timedelta(days=rng.randint(0, 30), hours=rng.randint(0, 6)),
                self.anchor + timedelta(hours=9),
            )
            kind = name.rsplit(".", 1)[-1].lower()
            docs.append(
                Document(
                    id=f"d{len(docs) + 1:04d}",
                    space_id=space,
                    folder=folder,
                    name=name,
                    kind=kind,
                    title=title,
                    paragraphs=tuple(paragraphs),
                    table=tuple(tuple(r) for r in table) if table else None,
                    owner_id=owner,
                    created=created,
                    modified=modified,
                    modified_by_id=editor or owner,
                    shares=shares,
                    client_id=client_id,
                    engagement_id=engagement_id,
                    description=description,
                    versions=1 + rng.randint(0, 6),
                    starred=rng.random() < 0.08,
                    restricted=restricted,
                )
            )

        # Client engagement files ------------------------------------------------------
        for eng in self.engagements:
            client = self._client(eng.client_id)
            lead = self.p(eng.lead_id)
            team = [self.p(t) for t in eng.team_ids]
            contact = self.p(client.primary_contact_id)
            folder = f"{client.name}/{eng.name.split(' — ')[1]}"
            ext_editor = Share(
                contact.id, contact.email, "editor", "none", self.anchor - timedelta(days=rng.randint(10, 60))
            )
            ext_viewer = Share(
                contact.id,
                contact.email,
                "viewer",
                "none",
                self.anchor - timedelta(days=rng.randint(1, 20)),
                self.anchor + timedelta(days=30),
            )
            age = (self.today - eng.start).days
            add(
                "clients",
                folder,
                f"Engagement Letter - {client.name}.docx",
                f"Engagement Letter — {eng.name}",
                C.engagement_letter(client, eng, lead, org),
                lead.id,
                days_ago=max(age + 10, 1),
                client_id=client.id,
                engagement_id=eng.id,
            )
            add(
                "clients",
                folder,
                "Engagement Letter - Signed.pdf",
                f"Signed Engagement Letter — {client.name}",
                C.engagement_letter(client, eng, lead, org),
                team[0].id,
                days_ago=max(age, 1),
                client_id=client.id,
                engagement_id=eng.id,
            )
            pbc = [["Item #", "Request", "Owner", "Due Date", "Status"]] + [
                [
                    i + 1,
                    req,
                    contact.display_name if i % 3 else self.p(team[-1].id).display_name,
                    eng.start + timedelta(days=14 + i * 2),
                    rng.choice(["Received", "Received", "Outstanding", "In Progress"]),
                ]
                for i, req in enumerate(
                    [
                        "Year-end bank statements and reconciliations",
                        "Trial balance and general ledger detail",
                        "Fixed asset rollforward",
                        "Accounts receivable aging",
                        "Accounts payable aging",
                        "Debt agreements and covenant calculations",
                        "Payroll registers and year-end reports",
                        "Board minutes",
                        "Revenue contracts for top 10 customers",
                        "Inventory count sheets",
                    ]
                )
            ]
            add(
                "clients",
                f"{folder}/Requests",
                "PBC Request List.xlsx",
                f"PBC Request List — {client.name}",
                ["Prepared-by-client request list"],
                team[0].id,
                table=pbc,
                days_ago=max(age - 3, 1),
                shares=(ext_editor,),
                client_id=client.id,
                engagement_id=eng.id,
                editor=contact.id,
            )
            tb_rows = [["Account No.", "Account Name", "Debit", "Credit"]]
            for acct, label in [
                (1000, "Cash - Operating"),
                (1100, "Accounts Receivable"),
                (1200, "Inventory"),
                (1500, "Property & Equipment"),
                (2000, "Accounts Payable"),
                (2100, "Accrued Liabilities"),
                (2500, "Long-term Debt"),
                (3000, "Retained Earnings"),
                (4000, "Revenue"),
                (5000, "Cost of Sales"),
                (6000, "Salaries & Wages"),
                (6100, "Rent"),
                (6200, "Professional Fees"),
            ]:
                amount = round(client.annual_revenue * rng.uniform(0.01, 0.4), 2)
                debit = acct < 2000 or acct >= 5000
                tb_rows.append([acct, label, amount if debit else None, None if debit else amount])
            add(
                "clients",
                f"{folder}/Working Papers",
                f"Trial Balance {eng.start.year}.xlsx",
                f"Trial Balance — {client.name}",
                ["Trial balance"],
                team[-1].id,
                table=tb_rows,
                days_ago=max(age - 7, 1),
                client_id=client.id,
                engagement_id=eng.id,
            )
            add(
                "clients",
                f"{folder}/Meeting Notes",
                "Kickoff Meeting Notes.docx",
                f"Kickoff — {eng.name}",
                C.meeting_notes(client, eng, [lead, *team, contact], rng),
                team[0].id,
                days_ago=max(age - 1, 1),
                client_id=client.id,
                engagement_id=eng.id,
            )
            if eng.service in ("Audit", "Review Engagement"):
                add(
                    "clients",
                    f"{folder}/Deliverables",
                    "Draft Financial Statements.pdf",
                    f"{client.legal_name} — Draft Financial Statements",
                    [
                        f"{client.legal_name}",
                        "Statement of Financial Position",
                        "Statement of Operations",
                        "Notes to the Financial Statements",
                        "DRAFT — FOR DISCUSSION PURPOSES ONLY",
                    ],
                    team[0].id,
                    days_ago=rng.randint(1, 20),
                    shares=(ext_viewer,),
                    client_id=client.id,
                    engagement_id=eng.id,
                )
                add(
                    "clients",
                    f"{folder}/Deliverables",
                    "Management Letter.docx",
                    "Management Letter",
                    C.generic_policy("management letter", org, rng),
                    lead.id,
                    days_ago=rng.randint(1, 15),
                    client_id=client.id,
                    engagement_id=eng.id,
                )
            elif eng.service == "Transaction Advisory":
                add(
                    "clients",
                    f"{folder}/Deliverables",
                    "Due Diligence Report - Draft.docx",
                    f"Financial Due Diligence — {client.name}",
                    C.due_diligence_report(client, rng),
                    lead.id,
                    days_ago=rng.randint(1, 20),
                    client_id=client.id,
                    engagement_id=eng.id,
                )
                add(
                    "clients",
                    f"{folder}/Deliverables",
                    "QoE Databook.xlsx",
                    "Quality of Earnings Databook",
                    ["QoE"],
                    team[0].id,
                    table=[
                        ["Metric", "FY-2", "FY-1", "TTM"],
                        ["Revenue", 41.2, 48.9, 52.3],
                        ["Reported EBITDA", 6.1, 7.4, 8.2],
                        ["Adjustments", 0.4, 0.9, 0.6],
                        ["Adjusted EBITDA", 6.5, 8.3, 8.8],
                    ],
                    client_id=client.id,
                    engagement_id=eng.id,
                )
            elif eng.service in ("Fractional CFO", "Bookkeeping", "Systems Advisory"):
                add(
                    "clients",
                    f"{folder}/Deliverables",
                    "Monthly Board Pack.pptx",
                    f"{client.name} — Board Pack",
                    [
                        "Highlights",
                        "Revenue grew 4% month over month",
                        "Cash runway: 19 months",
                        "Hiring plan on track",
                        "Risks",
                        "Customer concentration",
                        "Collections slipping on two accounts",
                    ],
                    lead.id,
                    days_ago=rng.randint(2, 25),
                    shares=(ext_viewer,),
                    client_id=client.id,
                    engagement_id=eng.id,
                )
                add(
                    "clients",
                    f"{folder}/Working Papers",
                    "Month-End Close Checklist.xlsx",
                    "Close Checklist",
                    ["Checklist"],
                    team[-1].id,
                    table=[
                        ["Task", "Owner", "Due (business day)", "Status"],
                        ["Bank reconciliations", team[-1].display_name, 3, "Done"],
                        ["AP accruals", team[0].display_name, 4, "Done"],
                        ["Revenue review", lead.display_name, 5, "In Progress"],
                        ["Payroll journal", team[-1].display_name, 3, "Done"],
                        ["Flux analysis", team[0].display_name, 6, "Not Started"],
                    ],
                    client_id=client.id,
                    engagement_id=eng.id,
                )
            else:
                add(
                    "clients",
                    f"{folder}/Working Papers",
                    "Tax Provision Workpapers.xlsx",
                    "Tax Provision",
                    ["x"],
                    team[0].id,
                    table=[
                        ["Line", "Book", "Tax", "Difference"],
                        ["Depreciation", 412000, 655000, -243000],
                        ["Meals (50%)", 18400, 9200, 9200],
                        ["Accrued bonus", 120000, 0, 120000],
                        ["Stock compensation", 86000, 0, 86000],
                    ],
                    client_id=client.id,
                    engagement_id=eng.id,
                )
                add(
                    "clients",
                    f"{folder}/Deliverables",
                    "Tax Planning Memo.docx",
                    f"Tax Planning — {client.name}",
                    C.KNOWLEDGE_ARTICLES["2026 Tax Law Changes Summary"],
                    lead.id,
                    days_ago=rng.randint(3, 40),
                    client_id=client.id,
                    engagement_id=eng.id,
                )
            add(
                "clients",
                f"{client.name}/Correspondence",
                "Client Onboarding Form.pdf",
                f"Client Onboarding — {client.name}",
                [
                    f"Legal name: {client.legal_name}",
                    f"Tax ID: {client.tax_id}",
                    f"Address: {client.address.one_line()}",
                    f"Primary contact: {contact.display_name} ({contact.email}, {contact.phone_business})",
                    f"Industry: {client.industry}",
                ],
                client.owner_id,
                days_ago=(self.today - client.client_since).days or 1,
                client_id=client.id,
            )
        for client in self.clients:
            if client.status == "Prospect":
                add(
                    "clients",
                    f"Prospects/{client.name}",
                    f"Proposal - {client.name}.pdf",
                    f"Proposal for Services — {client.name}",
                    [
                        f"Prepared for {client.legal_name}",
                        "Understanding of your needs",
                        client.description,
                        "Our approach",
                        "Proposed team and fees",
                        "Why Halcyon Advisory Group",
                    ],
                    "p040",
                    days_ago=rng.randint(3, 25),
                    client_id=client.id,
                    shares=(Share(None, None, "viewer", "anyone", self.anchor - timedelta(days=4)),),
                )

        # Firm areas -----------------------------------------------------------------
        months = [(self.anchor.replace(day=1) - timedelta(days=30 * i)) for i in range(1, 4)]
        budget = [["Category", "Q1", "Q2", "Q3", "Q4", "Total"]]
        for cat in [
            "Salaries & Benefits",
            "Rent",
            "Technology",
            "Travel",
            "Marketing",
            "Training",
            "Professional Fees",
            "Insurance",
        ]:
            q = [rng.randint(20, 900) * 1000 for _ in range(4)]
            budget.append([cat, *q, sum(q)])
        add(
            "finance",
            "Budgets",
            f"FY{self.today.year} Operating Budget.xlsx",
            "Operating Budget",
            ["Budget"],
            "p025",
            table=budget,
            days_ago=280,
        )
        add(
            "finance",
            "Budgets",
            f"FY{self.today.year + 1} Budget - Draft.xlsx",
            "Budget Draft",
            ["Draft"],
            "p026",
            table=budget,
            days_ago=12,
            editor="p025",
        )
        for m in months:
            add(
                "finance",
                "Month-End Close",
                f"{m:%Y-%m} Close Checklist.xlsx",
                f"{m:%B %Y} Close",
                ["Close"],
                "p027",
                table=[
                    ["Task", "Owner", "Status"],
                    ["Bank recs", "Ana Sousa", "Done"],
                    ["WIP review", "Kevin O'Brien", "Done"],
                    ["Revenue accrual", "Noah Williams", "Done"],
                ],
                days_ago=(self.anchor.date() - m.date()).days - 25,
            )
        aging = [["Customer", "Current", "1-30", "31-60", "61-90", "90+", "Total"]]
        for c in self.clients[:16]:
            vals = [rng.choice([0, 0, rng.randint(1, 60) * 500]) for _ in range(5)]
            aging.append([c.name, *vals, sum(vals)])
        add(
            "finance",
            "Reports",
            f"AR Aging - {months[0]:%B %Y}.xlsx",
            "AR Aging",
            ["AR"],
            "p028",
            table=aging,
            days_ago=10,
        )
        add(
            "finance",
            "Reports",
            f"Q3 {self.today.year} Management Report.pdf",
            "Q3 Management Report",
            [
                "Revenue was 6% ahead of plan.",
                "Utilisation averaged 74% against a target of 72%.",
                "DSO improved to 48 days.",
                "Headcount: 45 FTEs.",
            ],
            "p025",
            days_ago=14,
        )
        add(
            "finance",
            "Policies",
            "Expense Reimbursement Policy.docx",
            "Expense Reimbursement Policy",
            C.generic_policy("Expense Reimbursement Policy", org, rng),
            "p025",
            days_ago=120,
        )
        add(
            "finance",
            "Vendors",
            "Vendor Master List.csv",
            "Vendor Master",
            ["vendors"],
            "p028",
            table=[["Vendor", "Category", "Email", "Phone", "Terms"]]
            + [[v.name, v.category, v.email, v.phone, f"Net {v.payment_terms_days}"] for v in self.vendors],
            days_ago=60,
        )
        add(
            "finance",
            "Payroll",
            f"Payroll Calendar {self.today.year}.xlsx",
            "Payroll Calendar",
            ["payroll"],
            "p025",
            table=[["Pay Date", "Period"]]
            + [[date(self.today.year, m, 15), f"{m:02d}-A"] for m in range(1, 13)],
            restricted=True,
            shares=(Share("p026", None, "editor"),),
            days_ago=300,
        )
        add(
            "people",
            "Policies",
            f"Employee Handbook {self.today.year}.pdf",
            "Employee Handbook",
            C.generic_policy("Employee Handbook", org, rng) * 3,
            "p031",
            days_ago=250,
        )
        add(
            "people",
            "Policies",
            "Remote Work Policy.docx",
            "Remote Work Policy",
            C.generic_policy("Remote Work Policy", org, rng),
            "p031",
            days_ago=200,
        )
        add(
            "people",
            "Policies",
            "Code of Conduct.pdf",
            "Code of Conduct",
            C.generic_policy("Code of Conduct", org, rng),
            "p031",
            days_ago=380,
        )
        add(
            "people",
            "Onboarding",
            "New Hire Checklist.docx",
            "New Hire Checklist",
            [
                "Before day one: laptop, accounts, welcome email.",
                "Day one: office tour, IT setup, security training.",
                "First week: meet your buddy, shadow an engagement.",
                "First month: complete CPE plan.",
            ],
            "p032",
            days_ago=90,
        )
        add(
            "people",
            "Onboarding",
            "Welcome to Halcyon.pptx",
            "Welcome to Halcyon",
            ["Who we are", "Our values: candour, craft, care", "How we work", "Your first 30 days"],
            "p034",
            days_ago=150,
        )
        add(
            "people",
            "Benefits",
            f"{self.today.year + 1} Benefits Overview.pdf",
            "Benefits Overview",
            [
                "Medical, dental and vision coverage from day one.",
                "RRSP/401(k) match up to 5%.",
                "Wellness stipend of $600 per year.",
            ],
            "p032",
            days_ago=6,
        )
        add(
            "people",
            "Recruiting",
            f"Open Roles - Q4 {self.today.year}.xlsx",
            "Open Roles",
            ["roles"],
            "p033",
            table=[
                ["Role", "Department", "Location", "Hiring Manager", "Status"],
                ["Senior Audit Associate", "Audit & Assurance", "Toronto", "James Thornton", "Interviewing"],
                ["Tax Manager", "Tax", "Chicago", "Rajesh Iyer", "Sourcing"],
                ["Data Analyst", "Advisory", "New York", "Benjamin Adler", "Offer"],
            ],
            days_ago=8,
        )
        add(
            "people",
            "Compensation",
            f"Salary Bands {self.today.year}.xlsx",
            "Salary Bands",
            ["bands"],
            "p031",
            table=[
                ["Level", "Title", "Min", "Mid", "Max"],
                ["A1", "Associate", 62000, 70000, 78000],
                ["A2", "Senior Associate", 76000, 86000, 96000],
                ["M1", "Manager", 98000, 112000, 126000],
                ["M2", "Senior Manager", 120000, 138000, 156000],
                ["D1", "Director", 150000, 172000, 194000],
            ],
            restricted=True,
            shares=(Share("p001", None, "viewer"), Share("p025", None, "viewer")),
            days_ago=210,
        )
        add(
            "it",
            "Security",
            "Information Security Policy.docx",
            "Information Security Policy",
            C.generic_policy("Information Security Policy", org, rng),
            "p037",
            days_ago=160,
        )
        add(
            "it",
            "Security",
            "Incident Response Plan.docx",
            "Incident Response Plan",
            ["Detect", "Contain", "Eradicate", "Recover", "Lessons learned"],
            "p037",
            days_ago=140,
        )
        add(
            "it",
            "Runbooks",
            "Laptop Provisioning.md",
            "Laptop Provisioning",
            [
                "Enroll the device in MDM.",
                "Install the standard image and security agent.",
                "Assign to the user and record the asset tag.",
            ],
            "p039",
            days_ago=75,
        )
        add(
            "it",
            "Runbooks",
            "New Employee Account Setup.md",
            "Account Setup",
            [
                "Create the directory account with the naming convention first.last.",
                "Add to department and office groups.",
                "Require MFA registration at first sign-in.",
            ],
            "p036",
            days_ago=40,
        )
        add(
            "it",
            "Inventory",
            "Hardware Inventory.xlsx",
            "Hardware Inventory",
            ["inv"],
            "p039",
            table=[["Asset Tag", "Device", "Assigned To", "Purchase Date"]]
            + [
                [
                    f"HAG-LT-{100 + i}",
                    rng.choice(["ThinkPad X1 Carbon", "MacBook Pro 14", "Surface Laptop 6"]),
                    e.display_name,
                    date(self.today.year - 1, 1 + i % 12, 1 + i % 27),
                ]
                for i, e in enumerate(self.employees)
                if e.active
            ],
            days_ago=20,
        )
        add(
            "it",
            "Architecture",
            "Data Platform Overview.pptx",
            "Data Platform Overview",
            ["Sources", "Ingestion", "Warehouse", "Semantic layer", "Dashboards"],
            "p038",
            days_ago=55,
        )
        add(
            "growth",
            "Brand",
            "Brand Guidelines.pdf",
            "Brand Guidelines",
            [
                "Logo usage",
                "Colour palette: Halcyon Blue #1F4E79, Sand #E8DCC4",
                "Typography",
                "Tone of voice",
            ],
            "p041",
            days_ago=330,
        )
        add("growth", "Brand", "Halcyon Logo.png", "Logo", [], "p041", days_ago=330)
        add(
            "growth",
            "Campaigns",
            f"Q4 {self.today.year} Webinar Plan.docx",
            "Webinar Plan",
            [
                "Topic: Year-end tax planning for growing companies",
                "Speakers: Rajesh Iyer, Claire Dubois",
                "Target: 250 registrations",
                "Promotion: email, LinkedIn, partner newsletters",
            ],
            "p041",
            days_ago=30,
        )
        add(
            "growth",
            "Proposals",
            "Proposal Template.docx",
            "Proposal Template",
            ["Executive summary", "Understanding of your needs", "Approach", "Team", "Fees", "Next steps"],
            "p040",
            days_ago=180,
        )
        add(
            "growth",
            "Pipeline",
            f"Pipeline Snapshot - {months[0]:%B %Y}.xlsx",
            "Pipeline",
            ["pipe"],
            "p042",
            table=[["Opportunity", "Stage", "Amount", "Owner"]]
            + [
                [
                    c.name,
                    rng.choice(["Qualification", "Proposal/Price Quote", "Negotiation/Review"]),
                    rng.randint(20, 180) * 1000,
                    self.p(c.owner_id).display_name,
                ]
                for c in self.clients[-6:]
            ],
            days_ago=9,
        )
        for title, paras in C.KNOWLEDGE_ARTICLES.items():
            ext = (
                "pptx"
                if "Overview" in title
                else ("pdf" if "Guide" in title or "Standards" in title else "docx")
            )
            area = "Tax" if "Tax" in title else ("Audit" if "606" in title or "Audit" in title else "Firm")
            add(
                "knowledge",
                area,
                f"{title}.{ext}",
                title,
                paras,
                rng.choice(["p004", "p012", "p018", "p013"]),
                days_ago=rng.randint(20, 300),
            )
        add(
            "knowledge",
            "Advisory",
            "Due Diligence Checklist.xlsx",
            "DD Checklist",
            ["dd"],
            "p019",
            table=[
                ["Area", "Request", "Priority"],
                ["Financial", "Monthly P&L 36 months", "High"],
                ["Tax", "Returns 3 years", "High"],
                ["HR", "Census and comp", "Medium"],
            ],
            days_ago=100,
        )
        add(
            "knowledge",
            "Advisory",
            "13-Week Cash Flow Template.xlsx",
            "13-Week Cash Flow",
            ["cf"],
            "p020",
            table=[["Week", "Receipts", "Disbursements", "Net"]] + [[w, 0, 0, 0] for w in range(1, 14)],
            days_ago=210,
        )
        add(
            "knowledge",
            "Firm",
            "Glossary.md",
            "Glossary",
            [
                "PBC — prepared by client.",
                "QoE — quality of earnings.",
                "WIP — work in progress.",
                "SR&ED — Scientific Research and Experimental Development tax credit.",
            ],
            "p002",
            days_ago=365,
        )
        add(
            "leadership",
            "Board",
            f"Board Deck - Q3 {self.today.year}.pptx",
            "Board Deck Q3",
            ["Financial results", "Pipeline", "People", "Risks", "Decisions requested"],
            "p001",
            days_ago=16,
        )
        add(
            "leadership",
            "Strategy",
            f"{self.today.year + 1} Strategic Plan - Draft.docx",
            "Strategic Plan",
            [
                "Grow advisory to 40% of revenue.",
                "Open a Chicago office.",
                "Invest in AI-assisted audit.",
                "Raise utilisation to 75%.",
            ],
            "p001",
            days_ago=22,
            editor="p002",
        )
        for m in months:
            add(
                "leadership",
                "Partner Meetings",
                f"Partner Meeting Minutes - {m:%B %Y}.docx",
                "Partner Minutes",
                [
                    "Attendees: all partners, COO, CFO",
                    "Approved: Q4 bonus pool",
                    "Discussed: Project Lighthouse",
                    "Next meeting: first Monday of the month",
                ],
                "p003",
                days_ago=(self.anchor.date() - m.date()).days,
            )
        add(
            "leadership",
            "M&A",
            "Project Lighthouse - Target Screen.xlsx",
            "Project Lighthouse",
            ["confidential"],
            "p018",
            table=[
                ["Target", "City", "Revenue", "Staff"],
                ["Codename Beacon", "Chicago", 8_200_000, 38],
                ["Codename Harbor", "Boston", 5_600_000, 24],
            ],
            restricted=True,
            shares=(Share("p001", None, "editor"), Share("p012", None, "viewer")),
            days_ago=33,
        )
        # Personal drives -------------------------------------------------------------
        for e in [x for x in self.employees if x.active][:30]:
            space = f"personal-{e.id}"
            if e.manager_id:
                mgr = self.p(e.manager_id)
                add(
                    space,
                    "Notes",
                    f"1-1 Notes - {mgr.first_name}.docx",
                    f"1:1 notes with {mgr.display_name}",
                    [
                        "Priorities for the quarter",
                        "Feedback received",
                        "Development goals",
                        "Time off plans",
                    ],
                    e.id,
                    days_ago=rng.randint(1, 60),
                    shares=(Share(mgr.id, mgr.email, "commenter"),),
                )
            add(
                space,
                "Expenses",
                f"Expense Report - {months[0]:%B %Y}.xlsx",
                "Expense Report",
                ["exp"],
                e.id,
                table=[["Date", "Description", "Category", "Amount"]]
                + [
                    [
                        months[0].date() + timedelta(days=i * 3),
                        rng.choice(["Client lunch", "Taxi", "Hotel", "Parking"]),
                        "Travel",
                        round(rng.uniform(12, 420), 2),
                    ]
                    for i in range(rng.randint(2, 6))
                ],
                days_ago=rng.randint(1, 30),
            )
            if rng.random() < 0.5:
                other = rng.choice([x for x in self.employees if x.active and x.id != e.id])
                add(
                    space,
                    "Drafts",
                    f"{rng.choice(['Ideas', 'Draft', 'Outline'])} - "
                    f"{rng.choice(['process improvement', 'training plan', 'client newsletter', 'team offsite'])}"
                    ".docx",
                    "Draft",
                    ["Working draft — not for distribution."],
                    e.id,
                    days_ago=rng.randint(1, 90),
                    shares=(Share(other.id, other.email, "editor"),),
                )
        return docs

    # -- meetings ----------------------------------------------------------------------
    def _meetings(self) -> list[Meeting]:
        rng = self.rng("meetings")
        meetings: list[Meeting] = []
        window = self.business_days(self.today - timedelta(days=42), self.today + timedelta(days=21))
        client_names = [c.name for c in self.clients if c.status == "Active"]

        def add(
            title: str,
            desc: str,
            organizer: str,
            attendees: list[str],
            start: datetime,
            minutes: int,
            tz: str,
            platform: str,
            kind: str,
            series: str | None = None,
            recurrence: str | None = None,
            client: Client | None = None,
            eng: Engagement | None = None,
            record_p: float = 0.6,
            optional: list[str] | None = None,
        ) -> None:
            mid = f"m{len(meetings) + 1:04d}"
            attendees = [a for a in dict.fromkeys(attendees) if a != organizer]
            end = start + timedelta(minutes=minutes)
            happened = end <= self.anchor + timedelta(hours=8) and start < self.anchor
            cancelled = rng.random() < 0.04
            happened = happened and not cancelled
            recorded = happened and rng.random() < record_p
            transcribed = recorded and rng.random() < 0.9
            responses = tuple(
                (a, rng.choice(["accepted"] * 6 + ["tentativelyAccepted", "declined", "none"]))
                for a in attendees
            )
            parts: list[Participation] = []
            if happened:
                for pid in [organizer, *attendees]:
                    resp = dict(responses).get(pid, "accepted")
                    if resp == "declined" or (pid != organizer and rng.random() < 0.06):
                        continue
                    joined = start + timedelta(seconds=rng.randint(-120, 300))
                    left = end - timedelta(seconds=rng.randint(0, 400))
                    parts.append(
                        Participation(
                            pid,
                            joined,
                            left,
                            "organizer" if pid == organizer else "attendee",
                            rng.choice(["desktop", "desktop", "web", "mobile"]),
                        )
                    )
            transcript: tuple[TranscriptTurn, ...] = ()
            if transcribed and len(parts) >= 2:
                present = [self.p(x.person_id) for x in parts]
                host = self.p(organizer)
                internal = [x for x in present if not x.is_external and x.id != organizer]
                external = [x for x in present if x.is_external]
                if client is not None:
                    dialogue = C.client_meeting_dialogue(host, internal or [host], external, client, eng, rng)
                else:
                    dialogue = C.internal_meeting_dialogue(kind, host, internal or present, rng, client_names)
                transcript = _timed(dialogue, (end - start).total_seconds() - 120, rng)
            chat: tuple[ChatMessage, ...] = ()
            if happened and rng.random() < 0.35 and parts:
                chat = (
                    ChatMessage(
                        f"{mid}-chat-1",
                        parts[-1].person_id,
                        start + timedelta(minutes=1),
                        rng.choice(
                            [
                                "Running 2 minutes late, sorry!",
                                "Can you share the deck link?",
                                "Audio is a bit choppy for me.",
                            ]
                        ),
                    ),
                )
            meetings.append(
                Meeting(
                    mid,
                    title,
                    desc,
                    organizer,
                    tuple(attendees),
                    tuple(optional or ()),
                    start,
                    end,
                    tz,
                    platform,
                    kind,
                    series,
                    recurrence,
                    client.id if client else None,
                    eng.id if eng else None,
                    None,
                    cancelled,
                    happened,
                    recorded,
                    transcribed,
                    tuple(parts),
                    transcript,
                    chat,
                    responses,
                )
            )

        leadership = next(x for x in self.groups if x.id == "g-leadership").member_ids
        for day in window:
            wd = day.weekday()
            if wd == 0:
                add(
                    "Leadership Sync",
                    "Weekly leadership meeting: pipeline, utilisation, people, cash.",
                    "p001",
                    list(leadership),
                    self.at(day, 9, 0, "America/Toronto"),
                    60,
                    "America/Toronto",
                    "teams",
                    "internal",
                    "series-leadership",
                    "weekly",
                    record_p=0.8,
                )
                add(
                    "Technology Team Weekly",
                    "Ops review, tickets, projects.",
                    "p035",
                    [e.id for e in self.dept_members("it")],
                    self.at(day, 14, 0, "America/Toronto"),
                    30,
                    "America/Toronto",
                    "teams",
                    "internal",
                    "series-it",
                    "weekly",
                    record_p=0.2,
                )
            if wd == 1:
                add(
                    "Audit Practice Huddle",
                    "Staffing, technical updates and busy-season planning.",
                    "p004",
                    [e.id for e in self.dept_members("audit")],
                    self.at(day, 10, 0, "America/Toronto"),
                    45,
                    "America/Toronto",
                    "teams",
                    "internal",
                    "series-audit",
                    "weekly",
                    record_p=0.5,
                )
            if wd == 2:
                add(
                    "Tax Team Weekly",
                    "Filing tracker and legislative updates.",
                    "p012",
                    [e.id for e in self.dept_members("tax")],
                    self.at(day, 9, 30, "America/Chicago"),
                    45,
                    "America/Chicago",
                    "teams",
                    "internal",
                    "series-tax",
                    "weekly",
                    record_p=0.5,
                )
            if wd == 3:
                add(
                    "Advisory Deal Review",
                    "Live deals and resourcing.",
                    "p018",
                    [e.id for e in self.dept_members("advisory")],
                    self.at(day, 11, 0, "America/New_York"),
                    60,
                    "America/New_York",
                    "zoom",
                    "internal",
                    "series-advisory",
                    "weekly",
                    record_p=0.6,
                )
                if day.day <= 7:
                    add(
                        "Firm All-Hands",
                        "Monthly all-hands: results, announcements and Q&A.",
                        "p001",
                        [e.id for e in self.employees if e.active],
                        self.at(day, 12, 0, "America/Toronto"),
                        60,
                        "America/Toronto",
                        "teams",
                        "all_hands",
                        "series-allhands",
                        "monthly",
                        record_p=1.0,
                    )
            if wd == 4:
                add(
                    "Pipeline Review",
                    "Weekly BD pipeline review.",
                    "p040",
                    [e.id for e in self.dept_members("growth")] + ["p001", "p018"],
                    self.at(day, 10, 0, "America/New_York"),
                    30,
                    "America/New_York",
                    "meet",
                    "internal",
                    "series-pipeline",
                    "weekly",
                    record_p=0.4,
                )
            # biweekly 1:1s
            if (day.toordinal() // 7) % 2 == 0 and wd in (1, 3):
                for mgr_id in ("p004", "p018", "p025", "p035"):
                    reports = [r for r in self.employees if r.manager_id == mgr_id and r.active][:2]
                    for k, r in enumerate(reports):
                        if (wd == 1) == (k == 0):
                            add(
                                f"{self.p(mgr_id).first_name} / {r.first_name} 1:1",
                                "Biweekly 1:1.",
                                mgr_id,
                                [r.id],
                                self.at(day, 15, 30 if k else 0, self.p(mgr_id).timezone),
                                30,
                                self.p(mgr_id).timezone,
                                "teams",
                                "one_on_one",
                                f"series-1on1-{mgr_id}-{r.id}",
                                "biweekly",
                                record_p=0.1,
                            )
        # client meetings
        for eng in self.engagements:
            if eng.status not in ("In Progress", "Planned"):
                continue
            client = self._client(eng.client_id)
            contacts = list(client.contact_ids)
            team = [eng.lead_id, *eng.team_ids]
            tz = self.p(eng.lead_id).timezone
            days = [d for d in window if d >= eng.start]
            if not days:
                continue
            kickoff = days[0] if eng.start >= window[0] else None
            if kickoff:
                add(
                    f"{client.name} — Kickoff",
                    f"Kickoff for {eng.name}.",
                    eng.lead_id,
                    team + contacts,
                    self.at(kickoff, 10, 0, tz),
                    60,
                    tz,
                    client.preferred_platform,
                    "client",
                    client=client,
                    eng=eng,
                    record_p=0.9,
                )
            for d in days[3::10]:
                add(
                    f"{client.name} — Status Update",
                    f"Biweekly status for {eng.name}.",
                    team[1] if len(team) > 1 else eng.lead_id,
                    team + contacts[:2],
                    self.at(d, 14, 0, tz),
                    45,
                    tz,
                    client.preferred_platform,
                    "client",
                    f"series-status-{eng.id}",
                    "biweekly",
                    client,
                    eng,
                    record_p=0.85,
                )
            if len(days) > 15:
                d = days[rng.randint(8, len(days) - 1)]
                add(
                    f"{client.name} — Findings Review",
                    f"Review of preliminary findings for {eng.name}.",
                    eng.lead_id,
                    team + contacts,
                    self.at(d, 11, 0, tz),
                    60,
                    tz,
                    client.preferred_platform,
                    "client",
                    client=client,
                    eng=eng,
                    record_p=0.9,
                )
        for client in self.clients:
            if client.status == "Prospect":
                d = window[rng.randint(5, len(window) - 5)]
                add(
                    f"Intro: {client.name} x Halcyon",
                    f"Introductory call with {client.name}.",
                    "p040",
                    ["p001", client.owner_id, *client.contact_ids[:2]],
                    self.at(d, 13, 0, "America/New_York"),
                    30,
                    "America/New_York",
                    client.preferred_platform,
                    "external",
                    client=client,
                    record_p=0.5,
                )
        meetings.sort(key=lambda m: (m.start, m.id))
        return [
            Meeting(
                **{
                    **_asdict(m),
                    "id": f"m{i + 1:04d}",
                    "transcript": m.transcript,
                    "chat": tuple(
                        ChatMessage(f"m{i + 1:04d}-chat-{j + 1}", c.author_id, c.created, c.text)
                        for j, c in enumerate(m.chat)
                    ),
                }
            )
            for i, m in enumerate(meetings)
        ]

    # -- ledger --------------------------------------------------------------------------
    def _ledger(self) -> tuple[list[Account], list[Item]]:
        rows = [
            ("1000", "Operating Account - Northbridge Bank", "Bank", "Checking", "Asset", 485_000.0),
            ("1010", "Payroll Account - Northbridge Bank", "Bank", "Checking", "Asset", 120_000.0),
            ("1020", "Savings - Northbridge Bank", "Bank", "Savings", "Asset", 900_000.0),
            ("1100", "Accounts Receivable (A/R)", "Accounts Receivable", "AccountsReceivable", "Asset", 0.0),
            ("1200", "Unbilled Work in Progress", "Other Current Asset", "OtherCurrentAssets", "Asset", 0.0),
            ("1300", "Prepaid Expenses", "Other Current Asset", "PrepaidExpenses", "Asset", 24_000.0),
            ("1500", "Computer Equipment", "Fixed Asset", "MachineryAndEquipment", "Asset", 86_000.0),
            ("1510", "Furniture & Fixtures", "Fixed Asset", "FurnitureAndFixtures", "Asset", 140_000.0),
            ("1520", "Leasehold Improvements", "Fixed Asset", "LeaseholdImprovements", "Asset", 310_000.0),
            (
                "1590",
                "Accumulated Depreciation",
                "Fixed Asset",
                "AccumulatedDepreciation",
                "Asset",
                -190_000.0,
            ),
            ("2000", "Accounts Payable (A/P)", "Accounts Payable", "AccountsPayable", "Liability", 0.0),
            ("2100", "Corporate Card - Northbridge Visa", "Credit Card", "CreditCard", "Liability", 0.0),
            (
                "2200",
                "Accrued Liabilities",
                "Other Current Liability",
                "AccruedLiabilities",
                "Liability",
                0.0,
            ),
            ("2300", "Sales Tax Payable", "Other Current Liability", "SalesTaxPayable", "Liability", 0.0),
            (
                "2400",
                "Deferred Revenue",
                "Other Current Liability",
                "OtherCurrentLiabilities",
                "Liability",
                0.0,
            ),
            ("2700", "Term Loan", "Long Term Liability", "NotesPayable", "Liability", 400_000.0),
            ("3000", "Partners' Capital", "Equity", "PartnersEquity", "Equity", 1_000_000.0),
            ("3100", "Partner Distributions", "Equity", "PartnerDistributions", "Equity", 0.0),
            ("3200", "Retained Earnings", "Equity", "RetainedEarnings", "Equity", 0.0),
            ("4000", "Professional Fees - Audit", "Income", "ServiceFeeIncome", "Revenue", 0.0),
            ("4100", "Professional Fees - Tax", "Income", "ServiceFeeIncome", "Revenue", 0.0),
            ("4200", "Professional Fees - Advisory", "Income", "ServiceFeeIncome", "Revenue", 0.0),
            ("4900", "Reimbursed Expenses", "Income", "OtherPrimaryIncome", "Revenue", 0.0),
            ("6000", "Salaries & Wages", "Expense", "PayrollExpenses", "Expense", 0.0),
            ("6100", "Rent or Lease of Buildings", "Expense", "RentOrLeaseOfBuildings", "Expense", 0.0),
            (
                "6200",
                "Software Subscriptions",
                "Expense",
                "OfficeGeneralAdministrativeExpenses",
                "Expense",
                0.0,
            ),
            ("6210", "Office Supplies & Software", "Expense", "SuppliesMaterials", "Expense", 0.0),
            ("6300", "Insurance", "Expense", "Insurance", "Expense", 0.0),
            ("6400", "Advertising & Marketing", "Expense", "AdvertisingPromotional", "Expense", 0.0),
            ("6500", "Contract Labor", "Expense", "CostOfLabor", "Expense", 0.0),
            ("6600", "Meals & Entertainment", "Expense", "EntertainmentMeals", "Expense", 0.0),
            ("6610", "Travel", "Expense", "Travel", "Expense", 0.0),
            ("6700", "Postage & Delivery", "Expense", "ShippingFreightDelivery", "Expense", 0.0),
            ("6800", "Dues & Subscriptions", "Expense", "DuesSubscriptions", "Expense", 0.0),
            ("6810", "Professional Development", "Expense", "OtherMiscellaneousServiceCost", "Expense", 0.0),
            ("6900", "Telephone & Internet", "Expense", "Utilities", "Expense", 0.0),
            ("6910", "Repairs & Maintenance", "Expense", "RepairMaintenance", "Expense", 0.0),
            ("6920", "Payroll Processing Fees", "Expense", "BankCharges", "Expense", 0.0),
            ("6950", "Merchant Processing Fees", "Expense", "BankCharges", "Expense", 0.0),
            ("7000", "Depreciation Expense", "Other Expense", "Depreciation", "Expense", 0.0),
            ("7100", "Interest Expense", "Other Expense", "OtherMiscellaneousExpense", "Expense", 0.0),
        ]
        accounts = [
            Account(f"a{n}", n, name, t, st, cls, f"{name} ({cls.lower()})", bal)
            for (n, name, t, st, cls, bal) in rows
        ]
        income = {"audit": "a4000", "tax": "a4100", "advisory": "a4200"}
        items = [
            Item(
                f"i{idx:02d}",
                item,
                f"{svc} — billed {billing.lower()}",
                rate,
                income.get(dept, "a4200"),
                "Service",
                f"SVC-{idx:03d}",
            )
            for idx, (svc, item, rate, _, billing, dept) in enumerate(D.SERVICES, start=1)
        ]
        items.append(
            Item(
                "i99",
                "Reimbursable Expenses",
                "Out-of-pocket travel and filing fees",
                0.0,
                "a4900",
                "NonInventory",
                "REIMB",
            )
        )
        return accounts, items

    def _invoices(self) -> tuple[list[Invoice], list[Payment]]:
        rng = self.rng("invoices")
        items = {i.name: i for i in self.items}
        invoices, payments = [], []
        number = 1040
        for eng in sorted(self.engagements, key=lambda e: e.start):
            client = self._client(eng.client_id)
            item = items[eng.service_item]
            month = date(eng.start.year, eng.start.month, 1)
            last = min(eng.end, self.today)
            n_inst = 0
            while month <= last:
                bill_day = (month.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
                if bill_day > self.today:
                    break
                n_inst += 1
                if eng.billing == "Fixed fee":
                    if n_inst > 3:
                        break
                    lines = (
                        InvoiceLine(
                            item.id,
                            f"{eng.name} — installment {n_inst} of 3",
                            1,
                            round((eng.fixed_fee or 0) / 3, 2),
                        ),
                    )
                elif eng.billing == "Retainer":
                    lines = (
                        InvoiceLine(
                            item.id, f"{item.name} — {bill_day:%B %Y}", 1, round(eng.hourly_rate * 24, 2)
                        ),
                    )
                else:
                    hours = rng.randint(12, 90)
                    lines = (
                        InvoiceLine(
                            item.id,
                            f"{eng.name} — professional services {bill_day:%B %Y}",
                            hours,
                            eng.hourly_rate,
                        ),
                    )
                if rng.random() < 0.2:
                    lines += (
                        InvoiceLine(
                            "i99", "Travel and out-of-pocket expenses", 1, round(rng.uniform(80, 900), 2)
                        ),
                    )
                number += 1
                due = bill_day + timedelta(days=client.payment_terms_days)
                tax_rate = (
                    {"ON": 0.13, "NS": 0.15, "QC": 0.14975, "AB": 0.05, "BC": 0.05}.get(
                        client.address.state, 0.0
                    )
                    if client.address.country_code == "CA"
                    else 0.0
                )
                inv = Invoice(
                    f"inv{number}",
                    f"INV-{number}",
                    client.id,
                    eng.id,
                    bill_day,
                    due,
                    lines,
                    tax_rate,
                    "Open",
                    f"Thank you for your business. Terms: Net {client.payment_terms_days}.",
                    0.0,
                    "p030",
                )
                age = (self.today - due).days
                paid_p = 0.97 if age > 45 else (0.75 if age > 0 else 0.25)
                if rng.random() < paid_p:
                    pay_date = min(due + timedelta(days=rng.randint(-10, 20)), self.today - timedelta(days=1))
                    pay_date = max(pay_date, bill_day + timedelta(days=1))
                    method = (
                        "Credit Card" if client.pays_by_card else rng.choice(["ACH", "ACH", "Check", "Wire"])
                    )
                    partial = rng.random() < 0.05
                    amount = round(inv.total * (0.5 if partial else 1.0), 2)
                    inv = Invoice(
                        **{**_asdict(inv), "status": "Partial" if partial else "Paid", "paid_amount": amount}
                    )
                    payments.append(
                        Payment(
                            f"pay{len(payments) + 1:04d}",
                            inv.id,
                            client.id,
                            pay_date,
                            amount,
                            method,
                            f"{method[:3].upper()}-{rng.randint(100000, 999999)}",
                            "a1000",
                        )
                    )
                elif age > 0:
                    inv = Invoice(**{**_asdict(inv), "status": "Overdue"})
                invoices.append(inv)
                month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
        # one draft and one void invoice for realism
        if invoices:
            first = invoices[0]
            invoices.append(
                Invoice(
                    **{
                        **_asdict(first),
                        "id": "inv9001",
                        "number": "INV-9001",
                        "status": "Void",
                        "paid_amount": 0.0,
                        "memo": "Voided — issued to wrong entity.",
                    }
                )
            )
            invoices.append(
                Invoice(
                    **{
                        **_asdict(invoices[-2]),
                        "id": "inv9002",
                        "number": "INV-9002",
                        "status": "Draft",
                        "paid_amount": 0.0,
                        "issue_date": self.today,
                        "due_date": self.today + timedelta(days=30),
                    }
                )
            )
        return invoices, payments

    def _bills(self) -> list[Bill]:
        rng = self.rng("bills")
        accounts = {a.name: a.id for a in self.accounts}
        bills = []
        for v in self.vendors:
            quarterly = v.category in ("Insurance", "Training")
            for m in range(12, 0, -1):
                if quarterly and m % 3:
                    continue
                d = (self.anchor.date().replace(day=1) - timedelta(days=30 * m)).replace(
                    day=1 + (len(v.name) % 20)
                )
                amount = round(v.monthly_amount * (3 if quarterly else 1) * rng.uniform(0.9, 1.12), 2)
                due = d + timedelta(days=v.payment_terms_days)
                status = "Paid" if due < self.today - timedelta(days=3) else "Open"
                if status == "Open" and due < self.today:
                    status = "Overdue"
                if rng.random() < 0.03 and due < self.today:
                    status = "Overdue"
                bills.append(
                    Bill(
                        f"bill{len(bills) + 1:04d}",
                        f"{slugify(v.name, '')[:4].upper()}-{d:%y%m}{rng.randint(10, 99)}",
                        v.id,
                        d,
                        due,
                        (
                            BillLine(
                                accounts.get(v.expense_account, "a6200"), f"{v.category} — {d:%B %Y}", amount
                            ),
                        ),
                        status,
                        due + timedelta(days=rng.randint(-5, 3)) if status == "Paid" else None,
                        f"{v.name} {d:%B %Y}",
                    )
                )
        return bills

    # -- email -----------------------------------------------------------------------------
    def _emails(self) -> list[EmailThread]:
        rng = self.rng("emails")
        threads: list[EmailThread] = []
        org = self.org.name
        docs_by_eng: dict[str, list[Document]] = {}
        for d in self.documents:
            if d.engagement_id:
                docs_by_eng.setdefault(d.engagement_id, []).append(d)

        def thread(
            subject: str,
            exchange: list[tuple[str, str, str, list[str], dict[str, str], tuple[str, ...]]],
            start: datetime,
            category: str,
            client: Client | None,
            eng: Engagement | None,
            labels: tuple[str, ...] = (),
        ) -> None:
            tid = f"th{len(threads) + 1:04d}"
            msgs: list[EmailMessage] = []
            t = start
            for i, (kind, sender, recipient, cc, extra, attach) in enumerate(exchange):
                t = t + timedelta(hours=rng.randint(1, 26), minutes=rng.randint(0, 59)) if i else t
                if t > self.anchor + timedelta(hours=10):
                    break
                s, r = self.p(sender), self.p(recipient)
                body = C.email_body(
                    kind,
                    sender=s,
                    recipient=r,
                    client=client,
                    eng=eng,
                    rng=rng,
                    extra={
                        "org": org if not s.is_external else (client.legal_name if client else ""),
                        **extra,
                    },
                )
                recipients = (recipient, *cc)
                fresh = (self.anchor - t) < timedelta(days=2)
                read_by = tuple(
                    x for x in recipients if not self.p(x).is_external and (not fresh or rng.random() < 0.4)
                )
                msgs.append(
                    EmailMessage(
                        f"{tid}-{i + 1}",
                        tid,
                        sender,
                        (recipient,),
                        tuple(cc),
                        t,
                        subject if i == 0 else f"RE: {subject}",
                        body,
                        attach,
                        "high" if "overdue" in subject.lower() else "normal",
                        msgs[-1].id if msgs else None,
                        read_by,
                        tuple(x for x in recipients if rng.random() < 0.05),
                    )
                )
            if msgs:
                threads.append(
                    EmailThread(tid, subject, tuple(msgs), client.id if client else None, category, labels)
                )

        for eng in self.engagements:
            if eng.status == "Planned":
                continue
            client = self._client(eng.client_id)
            contact = client.primary_contact_id
            other = client.contact_ids[1] if len(client.contact_ids) > 1 else contact
            mgr = eng.team_ids[0]
            lead = eng.lead_id
            base = max(self.anchor - timedelta(days=60), datetime.combine(eng.start, time(14), tzinfo=UTC))
            label = (f"Clients/{client.name}",)
            edocs = docs_by_eng.get(eng.id, [])
            pbc = tuple(d.id for d in edocs if d.name.startswith("PBC"))
            deliver = tuple(d.id for d in edocs if "Deliverables" in d.folder)[:1]
            thread(
                f"{client.name}: document requests for {eng.name.split(' — ')[1]}",
                [
                    ("pbc_request", mgr, contact, [lead], {"due": "next Friday"}, pbc),
                    ("pbc_reply", contact, mgr, [other], {}, ()),
                    ("pbc_thanks", mgr, contact, [], {}, ()),
                ],
                base + timedelta(days=rng.randint(0, 10), hours=rng.randint(0, 6)),
                "client",
                client,
                eng,
                label,
            )
            thread(
                f"Scheduling: {client.name} findings review",
                [
                    ("schedule", lead, contact, [mgr], {"slot": "Thursday at 2:00 pm"}, ()),
                    ("schedule_reply", contact, lead, [mgr, other], {"slot": "Thursday at 2:00 pm"}, ()),
                ],
                self.anchor - timedelta(days=rng.randint(3, 25), hours=rng.randint(1, 8)),
                "client",
                client,
                eng,
                label,
            )
            if deliver:
                thread(
                    f"Draft for review — {eng.name}",
                    [
                        ("deliverable", lead, contact, [mgr], {"due": "end of next week"}, deliver),
                        ("deliverable_reply", contact, lead, [mgr], {}, ()),
                    ],
                    self.anchor - timedelta(days=rng.randint(1, 12), hours=rng.randint(1, 8)),
                    "client",
                    client,
                    eng,
                    label,
                )
            if rng.random() < 0.6:
                thread(
                    f"Question from {client.name}",
                    [
                        ("question", other, lead, [contact], {}, ()),
                        ("question_reply", lead, other, [mgr], {}, ()),
                    ],
                    self.anchor - timedelta(days=rng.randint(2, 40), hours=rng.randint(1, 8)),
                    "client",
                    client,
                    eng,
                    label,
                )
        for inv in [i for i in self.invoices if i.status in ("Open", "Overdue", "Paid")][-30:]:
            client = self._client(inv.client_id)
            eng = next((e for e in self.engagements if e.id == inv.engagement_id), None)
            subject = f"Invoice {inv.number} from {self.org.name}" + (
                " — overdue" if inv.status == "Overdue" else ""
            )
            ex = [
                (
                    "invoice",
                    "p030",
                    client.primary_contact_id,
                    ["p028"],
                    {"invoice": inv.number, "amount": C.money(inv.total), "due": f"{inv.due_date:%B %d, %Y}"},
                    (),
                )
            ]
            if inv.status == "Paid":
                ex.append(("invoice_reply", client.primary_contact_id, "p030", [], {}, ()))
            thread(
                subject,
                ex,
                datetime.combine(inv.issue_date, time(15, 10), tzinfo=UTC),
                "client",
                client,
                eng,
                (f"Clients/{client.name}",),
            )
        for subject, text, dept in C.ANNOUNCEMENTS:
            head = next(d for d in self.departments if d.id == dept).head_id
            thread(
                subject,
                [("announcement", head, "p001", ["p002"], {"text": text}, ())],
                self.anchor - timedelta(days=rng.randint(1, 50), hours=rng.randint(1, 8)),
                "internal",
                None,
                None,
            )
        for eng in self.engagements[:8]:
            if eng.status != "In Progress":
                continue
            client = self._client(eng.client_id)
            dept_head = eng.lead_id
            thread(
                f"Staffing: {eng.name}",
                [
                    ("staffing", eng.team_ids[0], dept_head, ["p002"], {}, ()),
                    ("staffing_reply", eng.team_ids[-1], eng.team_ids[0], [dept_head], {}, ()),
                ],
                self.anchor - timedelta(days=rng.randint(2, 30), hours=rng.randint(1, 8)),
                "internal",
                client,
                eng,
            )
        for e in [x for x in self.employees if x.active and x.manager_id][:10]:
            thread(
                f"Expense report approval — {e.display_name}",
                [
                    (
                        "expense",
                        e.id,
                        e.manager_id or "p001",
                        [],
                        {"month": f"{self.anchor:%B}", "amount": C.money(rng.uniform(80, 1400))},
                        (),
                    ),
                    ("expense_reply", e.manager_id or "p001", e.id, ["p028"], {}, ()),
                ],
                self.anchor - timedelta(days=rng.randint(1, 20), hours=rng.randint(1, 8)),
                "internal",
                None,
                None,
            )
        for bill in self.bills[-20:]:
            vendor = next(v for v in self.vendors if v.id == bill.vendor_id)
            thread(
                f"Your {vendor.name} invoice {bill.number}",
                [
                    (
                        "vendor_invoice",
                        f"{vendor.id}-1",
                        "p028",
                        ["p026"],
                        {
                            "invoice": bill.number,
                            "amount": C.money(bill.total),
                            "due": f"{bill.due_date:%B %d, %Y}",
                        },
                        (),
                    )
                ],
                datetime.combine(bill.bill_date, time(11, 5), tzinfo=UTC),
                "vendor",
                None,
                None,
                ("Vendors",),
            )
        threads.sort(key=lambda t: t.messages[0].sent)
        return threads

    # -- CRM / payments / messaging ------------------------------------------------------
    def _crm(self) -> tuple[list[Deal], list[Lead], list[SupportCase]]:
        rng = self.rng("crm")
        deals: list[Deal] = []
        for eng in self.engagements:
            client = self._client(eng.client_id)
            amount = eng.fixed_fee or eng.budget_hours * eng.hourly_rate
            close = eng.start - timedelta(days=rng.randint(10, 45))
            deals.append(
                Deal(
                    f"o{len(deals) + 1:03d}",
                    client.id,
                    eng.name,
                    "Closed Won",
                    round(amount, -2),
                    100,
                    close,
                    client.owner_id,
                    datetime.combine(close - timedelta(days=rng.randint(20, 90)), time(15), tzinfo=UTC),
                    rng.choice(["Referral", "Existing Client", "Website", "Partner"]),
                    "Existing Business" if deals and rng.random() < 0.4 else "New Business",
                    "Engagement underway",
                    eng.id,
                    True,
                    True,
                )
            )
        stages = [
            ("Prospecting", 10),
            ("Qualification", 20),
            ("Needs Analysis", 40),
            ("Proposal/Price Quote", 65),
            ("Negotiation/Review", 80),
        ]
        for client in self.clients:
            if client.status == "Former":
                continue
            if client.status == "Prospect" or rng.random() < 0.35:
                stage, prob = rng.choice(stages[2:] if client.status == "Prospect" else stages)
                service = rng.choice(["Audit", "Tax Advisory", "Fractional CFO", "Transaction Advisory"])
                deals.append(
                    Deal(
                        f"o{len(deals) + 1:03d}",
                        client.id,
                        f"{client.name} — {service}",
                        stage,
                        rng.randint(25, 220) * 1000.0,
                        prob,
                        self.today + timedelta(days=rng.randint(10, 120)),
                        client.owner_id,
                        self.anchor - timedelta(days=rng.randint(5, 80)),
                        rng.choice(["Referral", "Website", "Webinar", "Partner", "Cold Call"]),
                        "New Business" if client.status == "Prospect" else "Existing Business",
                        rng.choice(
                            ["Send proposal", "Schedule partner call", "Pricing review", "Await RFP decision"]
                        ),
                    )
                )
        for i in range(4):
            client = self.clients[i * 5]
            deals.append(
                Deal(
                    f"o{len(deals) + 1:03d}",
                    client.id,
                    f"{client.name} — Valuation",
                    "Closed Lost",
                    rng.randint(15, 60) * 1000.0,
                    0,
                    self.today - timedelta(days=rng.randint(10, 200)),
                    client.owner_id,
                    self.anchor - timedelta(days=rng.randint(210, 300)),
                    "Referral",
                    "Existing Business",
                    "Lost to competitor on price",
                    None,
                    False,
                    True,
                )
            )
        lead_companies = [
            ("Lakeview Pediatrics", "Healthcare"),
            ("Sterling Auto Group", "Automotive Retail"),
            ("Bramble & Birch Home Goods", "Retail"),
            ("Vertex Analytics", "Software"),
            ("Oakhurst Capital", "Financial Services"),
            ("Juniper Health Labs", "Biotech"),
            ("Redwood Timber Co.", "Forestry"),
            ("Crescent Bay Resorts", "Hospitality"),
            ("Nimbus Drone Works", "Aerospace"),
            ("Harper & Lowe Architects", "Architecture"),
            ("Bayside Marine Supply", "Wholesale"),
            ("Ember Coffee Roasters", "Food & Beverage"),
            ("Tidewater Shipping", "Logistics"),
            ("Aurora Fintech", "Financial Technology"),
            ("Fieldstone Senior Care", "Senior Care"),
            ("Prairie Wind Energy", "Energy"),
            ("Ridgeline Software", "Software"),
            ("Coral Reef Aquatics", "Retail"),
        ]
        cities = [
            ("Toronto", "ON", "Canada", "416"),
            ("New York", "NY", "United States", "212"),
            ("Chicago", "IL", "United States", "312"),
            ("Boston", "MA", "United States", "617"),
            ("Seattle", "WA", "United States", "206"),
            ("Vancouver", "BC", "Canada", "604"),
        ]
        statuses = [
            "Open - Not Contacted",
            "Working - Contacted",
            "Working - Contacted",
            "Closed - Converted",
            "Closed - Not Converted",
        ]
        leads = []
        for i, (company, industry) in enumerate(lead_companies):
            first = D.CONTACT_FIRST[(i * 13 + 29) % len(D.CONTACT_FIRST)]
            last = D.CONTACT_LAST[(i * 17 + 31) % len(D.CONTACT_LAST)]
            city, state, country, area = cities[i % len(cities)]
            leads.append(
                Lead(
                    f"l{i + 1:03d}",
                    first,
                    last,
                    f"{first.lower()}.{slugify(last, '')}@{slugify(company, '')}.example",
                    nanp(area, 70 + i),
                    company,
                    rng.choice(D.CONTACT_TITLES),
                    industry,
                    rng.choice(statuses),
                    rng.choice(["Website", "Webinar", "Referral", "Trade Show", "LinkedIn"]),
                    rng.choice(["p042", "p043", "p040"]),
                    self.anchor - timedelta(days=rng.randint(1, 120), hours=rng.randint(0, 9)),
                    rng.choice(["Hot", "Warm", "Cold"]),
                    city,
                    state,
                    country,
                    rng.randint(2, 90) * 1_000_000,
                    rng.randint(10, 600),
                    rng.choice(
                        [
                            "Asked about outsourced bookkeeping.",
                            "Needs an audit for a bank loan.",
                            "Expanding to the US; asked about tax nexus.",
                            "Downloaded the year-end tax planning guide.",
                        ]
                    ),
                )
            )
        subjects = [
            ("Portal access issue", "Cannot log in to the client portal after password reset."),
            ("Copy of last year's tax return", "Bank needs a copy of the filed return."),
            ("Question on invoice", "Hours on the latest invoice look higher than expected."),
            ("Update banking details", "We changed banks and need to update our ACH details."),
            ("Request for audit confirmation letter", "Lender requires a comfort letter."),
            ("Missing 1099 forms", "Haven't received the 1099s for contractors."),
            ("Change of primary contact", "Our controller left; please update the contact."),
            ("Engagement letter signature", "DocuSign link expired."),
            ("Data room access", "Need access for two new team members."),
            ("Payment plan request", "Can we split the remaining balance into two payments?"),
        ]
        cases = []
        for i, (subject, desc) in enumerate(subjects):
            client = self.clients[(i * 3) % 20]
            created = self.anchor - timedelta(days=rng.randint(1, 50), hours=rng.randint(0, 9))
            status = rng.choice(["New", "Working", "Escalated", "Closed", "Closed"])
            cases.append(
                SupportCase(
                    f"cs{i + 1:03d}",
                    f"{1000 + i:08d}",
                    client.id,
                    client.primary_contact_id,
                    subject,
                    desc,
                    status,
                    rng.choice(["Low", "Medium", "High"]),
                    rng.choice(["Email", "Phone", "Web"]),
                    client.owner_id,
                    created,
                    created + timedelta(days=rng.randint(1, 6)) if status == "Closed" else None,
                )
            )
        return deals, leads, cases

    def _stripe(self) -> tuple[list[Product], list[CardCustomer]]:
        rng = self.rng("stripe")
        products = [
            Product(
                "prod-cfo",
                "Fractional CFO Retainer",
                "Monthly fractional CFO services.",
                450000,
                "month",
                "fractional_cfo_monthly",
            ),
            Product(
                "prod-books",
                "Bookkeeping Retainer",
                "Monthly bookkeeping and close support.",
                125000,
                "month",
                "bookkeeping_monthly",
            ),
            Product(
                "prod-taxplan",
                "Tax Planning Membership",
                "Year-round tax planning and filing.",
                360000,
                "year",
                "tax_planning_annual",
            ),
            Product(
                "prod-hours",
                "Advisory Hours — Prepaid Block (10h)",
                "Prepaid block of advisory hours.",
                275000,
                None,
                "advisory_block_10h",
            ),
            Product(
                "prod-filing",
                "Year-End Tax Filing",
                "Preparation and filing of year-end returns.",
                195000,
                None,
                "year_end_filing",
            ),
        ]
        cards = [
            ("visa", "4242", "4242424242424242"),
            ("mastercard", "4444", "5555555555554444"),
            ("amex", "0005", "378282246310005"),
            ("discover", "1117", "6011111111111117"),
            ("visa", "5556", "4000056655665556"),
            ("visa", "0341", "4000000000000341"),
        ]
        plans = ["prod-books", "prod-cfo", "prod-taxplan", None, "prod-books", None, "prod-cfo", None]
        customers = []
        for i, client in enumerate([c for c in self.clients if c.pays_by_card]):
            brand, last4, pan = cards[i % len(cards)]
            plan = plans[i % len(plans)]
            created = self.anchor - timedelta(days=rng.randint(120, 600))
            status = None
            if plan:
                status = "past_due" if last4 == "0341" else ("canceled" if i == 6 else "active")
            customers.append(
                CardCustomer(
                    client.id,
                    client.primary_contact_id,
                    brand,
                    last4,
                    1 + (i * 5) % 12,
                    self.today.year + 1 + i % 4,
                    pan,
                    plan,
                    created + timedelta(days=rng.randint(1, 30)) if plan else None,
                    status,
                    created,
                )
            )
        return products, customers

    def _conversations(self) -> list[Conversation]:
        rng = self.rng("messaging")
        convs = []
        sms_number, wa_number, page_id = "+14165550188", "+14165550177", "104857600123456"
        active_clients = [c for c in self.clients if c.status == "Active"]
        invoices = {i.client_id: i for i in self.invoices if i.status in ("Open", "Overdue")}

        def build(
            channel: str,
            person: Person,
            kinds: list[str],
            sender: Person,
            business: str,
            client: Client | None,
        ) -> None:
            msgs: list[TextMessage] = []
            t = self.anchor - timedelta(days=rng.randint(1, 30), hours=rng.randint(0, 8))
            for kind in kinds:
                inv = invoices.get(client.id) if client else None
                for direction, text in C.TEXT_THREADS[kind]:
                    t += timedelta(minutes=rng.randint(1, 240))
                    filled = text.format(
                        first=person.first_name,
                        sender=sender.display_name,
                        sender_first=sender.first_name,
                        time="10:00 am",
                        doc=rng.choice(["signed engagement letter", "bank statements", "payroll summary"]),
                        client=client.name if client else "your company",
                        invoice=inv.number if inv else "INV-1077",
                        amount=C.money(inv.total) if inv else "$1,250.00",
                        due=f"{inv.due_date:%b %d}" if inv else "Nov 15",
                    )
                    status = (
                        "received"
                        if direction == "inbound"
                        else ("read" if channel != "sms" else "delivered")
                    )
                    msgs.append(
                        TextMessage(
                            f"{channel}-{len(convs) + 1:03d}-{len(msgs) + 1:02d}",
                            direction,
                            min(t, self.anchor + timedelta(hours=9)),
                            filled,
                            sender.id if direction == "outbound" else None,
                            status,
                        )
                    )
            convs.append(
                Conversation(
                    f"conv-{channel}-{len(convs) + 1:03d}", channel, person.id, business, tuple(msgs)
                )
            )

        for i, client in enumerate(active_clients[:10]):
            contact = self.p(client.primary_contact_id)
            sender = self.p(client.owner_id if i % 2 else "p030")
            build(
                "sms",
                contact,
                [["reminder", "documents", "payment", "reschedule"][i % 4]]
                + (["question"] if i % 3 == 0 else []),
                sender,
                sms_number,
                client,
            )
        for i, client in enumerate(active_clients[10:18]):
            contact = self.p(client.contact_ids[-1])
            sender = self.p(client.owner_id)
            build(
                "whatsapp",
                contact,
                [["documents", "reschedule", "question", "payment"][i % 4], "reminder"],
                sender,
                wa_number,
                client,
            )
        leads = [
            p
            for p in self.people
            if p.is_external and p.company_id in {c.id for c in self.clients if c.status == "Prospect"}
        ]
        for person in leads[:6]:
            client = self._client(person.company_id or "c022")
            build("messenger", person, ["lead"], self.p("p042"), page_id, client)
        return convs


def _timed(dialogue: list[tuple[str, str]], span_s: float, rng: random.Random) -> tuple[TranscriptTurn, ...]:
    """Spread dialogue over the meeting: speech at ~155 wpm, pauses filling the rest."""
    durations = [max(1.2, len(text.split()) / 155 * 60) for _, text in dialogue]
    speech = sum(durations)
    slack = max(span_s - speech - 30, len(dialogue) * 0.5)
    weights = [rng.uniform(0.3, 1.7) for _ in dialogue]
    scale = slack / sum(weights)
    turns, t = [], rng.uniform(15, 45)
    for (speaker, text), dur, w in zip(dialogue, durations, weights, strict=True):
        turns.append(TranscriptTurn(speaker, round(t, 2), round(dur, 2), text))
        t += dur + w * scale
    return tuple(turns)


def _asdict(obj: object) -> dict[str, object]:
    return {name: getattr(obj, name) for name in obj.__slots__}  # type: ignore[attr-defined]
