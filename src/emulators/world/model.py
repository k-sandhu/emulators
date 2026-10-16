"""The shared synthetic organisation every emulator projects into its own data model.

One deterministic :class:`World` describes a fictional professional-services firm,
*Halcyon Advisory Group*, with its people (and org chart), departments, groups, teams,
offices, clients and their contacts, vendors, engagements, a document library with
folder hierarchy and permissions, meetings (with transcripts for past ones), email
threads, chat channels, invoices, bills, payments, CRM deals and leads, card-paying
customers and SMS/WhatsApp/Messenger conversations.

Because every provider is seeded from the same world, the same person is
``maya.chen@halcyonadvisory.example`` in Microsoft Graph, Google Workspace, Slack, Zoom
and Salesforce, and the same client shows up as a Salesforce Account, a QuickBooks
Customer, a Stripe Customer, a Box folder and a Zoom meeting attendee. That makes
cross-system integration tests (sync a CRM account to a drive folder, match a Zoom
participant to a directory user...) meaningful.

Everything here is plain data. Providers must treat it as read-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

Role = Literal["owner", "editor", "commenter", "viewer"]
Platform = Literal["teams", "zoom", "meet"]


@dataclass(frozen=True, slots=True)
class Address:
    street: str
    city: str
    state: str  # state / province / region code ("ON", "NY", "ENG")
    postal_code: str
    country: str  # "Canada"
    country_code: str  # ISO 3166-1 alpha-2 ("CA")

    def one_line(self) -> str:
        return f"{self.street}, {self.city}, {self.state} {self.postal_code}, {self.country}"


@dataclass(frozen=True, slots=True)
class Office:
    id: str  # "toronto"
    name: str  # "Toronto (HQ)"
    address: Address
    timezone: str  # IANA, e.g. "America/Toronto"
    windows_timezone: str  # "Eastern Standard Time" (Graph mailboxSettings)
    phone: str  # E.164


@dataclass(frozen=True, slots=True)
class Organization:
    name: str  # "Halcyon Advisory Group"
    legal_name: str
    slug: str  # "halcyonadvisory" (tenant / workspace subdomain)
    domain: str  # primary email domain
    website: str
    phone: str
    address: Address
    founded: date
    industry: str
    currency: str  # "USD"
    fiscal_year_start_month: int
    # Stable vendor-side identifiers so tests can hard-code them.
    microsoft_tenant_id: str
    google_customer_id: str
    salesforce_org_id: str
    quickbooks_realm_id: str
    stripe_account_id: str
    zoom_account_id: str
    slack_team_id: str
    box_enterprise_id: str
    dropbox_team_id: str
    meta_business_id: str
    twilio_account_sid: str


@dataclass(frozen=True, slots=True)
class Person:
    """An employee (``is_external=False``) or a client/vendor contact (``is_external=True``)."""

    id: str  # "p001" employees, "c012-2" client contacts
    first_name: str
    last_name: str
    display_name: str
    email: str
    username: str  # local part, "maya.chen"
    job_title: str
    department_id: str | None
    office_id: str | None
    manager_id: str | None
    timezone: str
    phone_business: str | None
    phone_mobile: str | None
    employee_id: str | None
    hire_date: date | None
    is_external: bool = False
    company_id: str | None = None  # client id for external contacts
    is_admin: bool = False  # tenant / workspace admin
    is_partner: bool = False
    locale: str = "en-US"
    pronouns: str | None = None
    birthday: date | None = None
    skills: tuple[str, ...] = ()
    active: bool = True  # False = departed employee (disabled account)

    @property
    def initials(self) -> str:
        return (self.first_name[:1] + self.last_name[:1]).upper()


@dataclass(frozen=True, slots=True)
class Department:
    id: str  # "finance"
    name: str  # "Finance & Operations"
    head_id: str
    parent_id: str | None
    cost_center: str
    description: str
    mail_nickname: str


@dataclass(frozen=True, slots=True)
class Group:
    """A directory group. ``kind`` hints how providers render it (M365 unified group,
    security group, distribution list, Google group...)."""

    id: str
    name: str
    email: str
    description: str
    kind: Literal["department", "all_staff", "role", "project", "distribution"]
    member_ids: tuple[str, ...]
    owner_ids: tuple[str, ...]
    department_id: str | None = None
    visibility: Literal["Public", "Private"] = "Private"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    id: str
    author_id: str
    created: datetime
    text: str
    reply_to_id: str | None = None  # thread parent
    mentions: tuple[str, ...] = ()
    reactions: tuple[tuple[str, str], ...] = ()  # (emoji_name, person_id)
    attachment_doc_ids: tuple[str, ...] = ()
    edited: datetime | None = None


@dataclass(frozen=True, slots=True)
class Channel:
    id: str
    name: str  # "General", "audit-fieldwork"
    description: str
    is_private: bool
    member_ids: tuple[str, ...]  # only meaningful for private channels
    messages: tuple[ChatMessage, ...]


@dataclass(frozen=True, slots=True)
class Team:
    """A collaboration space: Microsoft Team, Slack channel set, Google group + space."""

    id: str
    name: str
    description: str
    owner_ids: tuple[str, ...]
    member_ids: tuple[str, ...]
    visibility: Literal["Public", "Private"]
    mail_nickname: str
    channels: tuple[Channel, ...]
    group_id: str | None = None  # backing directory group
    client_id: str | None = None


@dataclass(frozen=True, slots=True)
class DirectChat:
    id: str
    member_ids: tuple[str, ...]
    topic: str | None
    messages: tuple[ChatMessage, ...]


@dataclass(frozen=True, slots=True)
class Client:
    id: str  # "c001"
    name: str
    legal_name: str
    domain: str
    industry: str
    employees: int
    annual_revenue: int
    address: Address
    phone: str
    website: str
    owner_id: str  # relationship partner / account owner (employee)
    contact_ids: tuple[str, ...]
    primary_contact_id: str
    status: Literal["Active", "Former", "Prospect"]
    client_since: date
    payment_terms_days: int
    pays_by_card: bool  # appears in Stripe
    preferred_platform: Platform
    tax_id: str
    description: str
    rating: Literal["Hot", "Warm", "Cold"]


@dataclass(frozen=True, slots=True)
class Vendor:
    id: str
    name: str
    email: str
    phone: str
    category: str  # "Software", "Rent", "Insurance"...
    address: Address
    payment_terms_days: int
    expense_account: str  # chart-of-accounts name
    monthly_amount: float
    tax_id: str


@dataclass(frozen=True, slots=True)
class Engagement:
    id: str  # "e001"
    client_id: str
    name: str  # "FY2026 Financial Statement Audit"
    service: str  # "Audit", "Tax Compliance", "Advisory", ...
    service_item: str  # item name used on invoices
    lead_id: str
    team_ids: tuple[str, ...]
    start: date
    end: date
    status: Literal["Planned", "In Progress", "Completed", "On Hold"]
    budget_hours: int
    hourly_rate: float
    fixed_fee: float | None
    billing: Literal["Hourly", "Fixed fee", "Retainer"]
    team_id: str | None = None  # collaboration team (Teams/Slack) if any


@dataclass(frozen=True, slots=True)
class Space:
    """A document area: a SharePoint site / shared drive / Box or Dropbox team folder
    (``kind="team"``) or a person's own drive (``kind="personal"``)."""

    id: str  # "clients", "finance", "personal-p004"
    name: str  # "Client Engagements"
    description: str
    kind: Literal["team", "personal"]
    owner_id: str
    # (principal id, role). Principals are person ids or group ids.
    members: tuple[tuple[str, Role], ...]
    slug: str  # url-safe name ("ClientEngagements")


@dataclass(frozen=True, slots=True)
class Share:
    """An explicit per-item share on top of space membership."""

    principal_id: str | None  # person/group id, or None for link shares
    email: str | None  # for external people / link recipients
    role: Role
    link: Literal["none", "anyone", "organization"] = "none"
    created: datetime | None = None
    expires: datetime | None = None


@dataclass(frozen=True, slots=True)
class Document:
    id: str  # "d0042"
    space_id: str
    folder: str  # "Clients/Northwind Logistics/FY2026 Audit" ("" = space root)
    name: str  # "Engagement Letter.docx"
    kind: str  # extension: docx, xlsx, pptx, pdf, csv, md, txt, png
    title: str
    paragraphs: tuple[str, ...]
    table: tuple[tuple[object, ...], ...] | None
    owner_id: str
    created: datetime
    modified: datetime
    modified_by_id: str
    shares: tuple[Share, ...]
    client_id: str | None = None
    engagement_id: str | None = None
    description: str = ""
    versions: int = 1
    starred: bool = False
    trashed: bool = False
    # Broken inheritance: space members do NOT get access; only the owner and ``shares``.
    restricted: bool = False

    @property
    def path(self) -> str:
        return f"{self.folder}/{self.name}" if self.folder else self.name


@dataclass(frozen=True, slots=True)
class Participation:
    person_id: str
    joined: datetime
    left: datetime
    role: Literal["organizer", "presenter", "attendee"] = "attendee"
    device: Literal["desktop", "web", "mobile", "phone"] = "desktop"


@dataclass(frozen=True, slots=True)
class TranscriptTurn:
    speaker_id: str
    offset_s: float  # from meeting start
    duration_s: float
    text: str


@dataclass(frozen=True, slots=True)
class Meeting:
    id: str  # "m0012"
    title: str
    description: str
    organizer_id: str
    attendee_ids: tuple[str, ...]  # excludes organizer
    optional_ids: tuple[str, ...]
    start: datetime
    end: datetime
    timezone: str
    platform: Platform
    kind: Literal["client", "internal", "one_on_one", "all_hands", "interview", "external"]
    series_id: str | None  # recurring series key
    recurrence: Literal["weekly", "biweekly", "monthly"] | None
    client_id: str | None
    engagement_id: str | None
    location: str | None
    cancelled: bool
    # Past meetings only:
    happened: bool
    recorded: bool
    transcribed: bool
    participations: tuple[Participation, ...]
    transcript: tuple[TranscriptTurn, ...]
    chat: tuple[ChatMessage, ...]
    # Responses of each invitee: "accepted" | "tentativelyAccepted" | "declined" | "none"
    responses: tuple[tuple[str, str], ...] = ()

    @property
    def duration_minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def all_attendee_ids(self) -> tuple[str, ...]:
        return (self.organizer_id, *self.attendee_ids, *self.optional_ids)


@dataclass(frozen=True, slots=True)
class EmailMessage:
    id: str
    thread_id: str
    from_id: str
    to_ids: tuple[str, ...]
    cc_ids: tuple[str, ...]
    sent: datetime
    subject: str
    body: str  # plain text; providers derive HTML
    attachment_doc_ids: tuple[str, ...] = ()
    importance: Literal["low", "normal", "high"] = "normal"
    in_reply_to: str | None = None
    # Read state per internal recipient (person id -> read?)
    read_by: tuple[str, ...] = ()
    flagged_by: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class EmailThread:
    id: str
    subject: str
    messages: tuple[EmailMessage, ...]
    client_id: str | None
    category: Literal["client", "internal", "vendor", "notification", "newsletter"]
    labels: tuple[str, ...] = ()  # "Clients/Northwind Logistics" style folder hints


@dataclass(frozen=True, slots=True)
class Account:
    """Chart of accounts entry (QuickBooks/Xero)."""

    id: str
    number: str
    name: str
    type: str  # QBO AccountType: "Bank", "Income", "Expense", ...
    subtype: str  # QBO AccountSubType
    classification: Literal["Asset", "Liability", "Equity", "Revenue", "Expense"]
    description: str
    opening_balance: float = 0.0
    parent_id: str | None = None


@dataclass(frozen=True, slots=True)
class Item:
    id: str
    name: str
    description: str
    rate: float
    income_account_id: str
    type: Literal["Service", "NonInventory"]
    sku: str


@dataclass(frozen=True, slots=True)
class InvoiceLine:
    item_id: str
    description: str
    quantity: float
    rate: float

    @property
    def amount(self) -> float:
        return round(self.quantity * self.rate, 2)


@dataclass(frozen=True, slots=True)
class Invoice:
    id: str
    number: str  # "INV-1043"
    client_id: str
    engagement_id: str | None
    issue_date: date
    due_date: date
    lines: tuple[InvoiceLine, ...]
    tax_rate: float
    status: Literal["Draft", "Open", "Paid", "Overdue", "Void", "Partial"]
    memo: str
    paid_amount: float
    created_by_id: str

    @property
    def subtotal(self) -> float:
        return round(sum(line.amount for line in self.lines), 2)

    @property
    def tax(self) -> float:
        return round(self.subtotal * self.tax_rate, 2)

    @property
    def total(self) -> float:
        return round(self.subtotal + self.tax, 2)

    @property
    def balance(self) -> float:
        return round(self.total - self.paid_amount, 2)


@dataclass(frozen=True, slots=True)
class Payment:
    id: str
    invoice_id: str
    client_id: str
    date: date
    amount: float
    method: Literal["ACH", "Check", "Credit Card", "Wire"]
    reference: str
    deposit_account_id: str


@dataclass(frozen=True, slots=True)
class BillLine:
    account_id: str
    description: str
    amount: float


@dataclass(frozen=True, slots=True)
class Bill:
    id: str
    number: str
    vendor_id: str
    bill_date: date
    due_date: date
    lines: tuple[BillLine, ...]
    status: Literal["Open", "Paid", "Overdue"]
    paid_date: date | None
    memo: str

    @property
    def total(self) -> float:
        return round(sum(line.amount for line in self.lines), 2)


@dataclass(frozen=True, slots=True)
class Deal:
    """CRM opportunity."""

    id: str
    client_id: str
    name: str
    stage: str  # Salesforce standard stages
    amount: float
    probability: int
    close_date: date
    owner_id: str
    created: datetime
    lead_source: str
    type: Literal["New Business", "Existing Business"]
    next_step: str
    engagement_id: str | None = None
    is_won: bool = False
    is_closed: bool = False


@dataclass(frozen=True, slots=True)
class Lead:
    id: str
    first_name: str
    last_name: str
    email: str
    phone: str
    company: str
    title: str
    industry: str
    status: Literal[
        "Open - Not Contacted", "Working - Contacted", "Closed - Converted", "Closed - Not Converted"
    ]
    source: str
    owner_id: str
    created: datetime
    rating: Literal["Hot", "Warm", "Cold"]
    city: str
    state: str
    country: str
    annual_revenue: int
    employees: int
    description: str


@dataclass(frozen=True, slots=True)
class SupportCase:
    id: str
    number: str
    client_id: str
    contact_id: str
    subject: str
    description: str
    status: Literal["New", "Working", "Escalated", "Closed"]
    priority: Literal["Low", "Medium", "High"]
    origin: Literal["Email", "Phone", "Web"]
    owner_id: str
    created: datetime
    closed: datetime | None


@dataclass(frozen=True, slots=True)
class Product:
    """A sellable plan (Stripe Product + Price)."""

    id: str
    name: str
    description: str
    unit_amount_cents: int
    interval: Literal["month", "year"] | None  # None = one-time
    lookup_key: str


@dataclass(frozen=True, slots=True)
class CardCustomer:
    """A client that pays by card (Stripe)."""

    client_id: str
    contact_id: str
    card_brand: str
    card_last4: str
    card_exp_month: int
    card_exp_year: int
    test_card_number: str  # Stripe test PAN that produces this card
    subscription_product_id: str | None
    subscription_started: datetime | None
    subscription_status: Literal["active", "past_due", "canceled", "trialing"] | None
    created: datetime


@dataclass(frozen=True, slots=True)
class TextMessage:
    id: str
    direction: Literal["inbound", "outbound"]
    sent: datetime
    text: str
    author_id: str | None  # employee for outbound
    status: Literal["queued", "sent", "delivered", "read", "failed", "received"]
    media_doc_id: str | None = None


@dataclass(frozen=True, slots=True)
class Conversation:
    """A business messaging thread with an external person over SMS/WhatsApp/Messenger."""

    id: str
    channel: Literal["sms", "whatsapp", "messenger"]
    person_id: str  # external contact (client contact or lead)
    business_number: str  # E.164 sending number or page id
    messages: tuple[TextMessage, ...]


@dataclass
class World:
    seed: str
    anchor: datetime
    org: Organization
    offices: list[Office]
    departments: list[Department]
    people: list[Person]  # employees first, then external contacts
    groups: list[Group]
    teams: list[Team]
    chats: list[DirectChat]
    clients: list[Client]
    vendors: list[Vendor]
    engagements: list[Engagement]
    spaces: list[Space]
    documents: list[Document]
    meetings: list[Meeting]
    email_threads: list[EmailThread]
    accounts: list[Account]
    items: list[Item]
    invoices: list[Invoice]
    payments: list[Payment]
    bills: list[Bill]
    deals: list[Deal]
    leads: list[Lead]
    cases: list[SupportCase]
    products: list[Product]
    card_customers: list[CardCustomer]
    conversations: list[Conversation]
    _index: dict[str, dict[str, object]] = field(default_factory=dict, repr=False)

    # -- lookups ---------------------------------------------------------------------
    def _lookup(self, kind: str, items: list) -> dict[str, object]:  # type: ignore[type-arg]
        table = self._index.get(kind)
        if table is None:
            table = self._index[kind] = {item.id: item for item in items}
        return table

    def person(self, person_id: str) -> Person:
        return self._lookup("people", self.people)[person_id]  # type: ignore[return-value]

    def person_by_email(self, email: str) -> Person | None:
        email = email.lower()
        return next((p for p in self.people if p.email.lower() == email), None)

    def client(self, client_id: str) -> Client:
        return self._lookup("clients", self.clients)[client_id]  # type: ignore[return-value]

    def department(self, department_id: str) -> Department:
        return self._lookup("departments", self.departments)[department_id]  # type: ignore[return-value]

    def office(self, office_id: str) -> Office:
        return self._lookup("offices", self.offices)[office_id]  # type: ignore[return-value]

    def group(self, group_id: str) -> Group:
        return self._lookup("groups", self.groups)[group_id]  # type: ignore[return-value]

    def space(self, space_id: str) -> Space:
        return self._lookup("spaces", self.spaces)[space_id]  # type: ignore[return-value]

    def document(self, doc_id: str) -> Document:
        return self._lookup("documents", self.documents)[doc_id]  # type: ignore[return-value]

    def engagement(self, engagement_id: str) -> Engagement:
        return self._lookup("engagements", self.engagements)[engagement_id]  # type: ignore[return-value]

    def vendor(self, vendor_id: str) -> Vendor:
        return self._lookup("vendors", self.vendors)[vendor_id]  # type: ignore[return-value]

    def account(self, account_id: str) -> Account:
        return self._lookup("accounts", self.accounts)[account_id]  # type: ignore[return-value]

    def item(self, item_id: str) -> Item:
        return self._lookup("items", self.items)[item_id]  # type: ignore[return-value]

    def product(self, product_id: str) -> Product:
        return self._lookup("products", self.products)[product_id]  # type: ignore[return-value]

    @property
    def employees(self) -> list[Person]:
        return [p for p in self.people if not p.is_external]

    @property
    def active_employees(self) -> list[Person]:
        return [p for p in self.people if not p.is_external and p.active]

    @property
    def external_people(self) -> list[Person]:
        return [p for p in self.people if p.is_external]

    def direct_reports(self, person_id: str) -> list[Person]:
        return [p for p in self.employees if p.manager_id == person_id]

    def documents_in(self, space_id: str) -> list[Document]:
        return [d for d in self.documents if d.space_id == space_id]

    def folders_in(self, space_id: str) -> list[str]:
        """Every folder path in a space, parents before children (``"A"``, ``"A/B"``)."""
        seen: dict[str, None] = {}
        for doc in self.documents_in(space_id):
            parts = doc.folder.split("/") if doc.folder else []
            for i in range(1, len(parts) + 1):
                seen.setdefault("/".join(parts[:i]), None)
        return sorted(seen, key=lambda p: (p.count("/"), p.lower()))

    def group_members(self, group_id: str) -> list[Person]:
        return [self.person(pid) for pid in self.group(group_id).member_ids]

    def expand_principal(self, principal_id: str) -> list[str]:
        """Person ids behind a principal (a person id or a group id)."""
        groups = self._lookup("groups", self.groups)
        if principal_id in groups:
            return list(self.group(principal_id).member_ids)
        return [principal_id]

    def meetings_for(self, person_id: str) -> list[Meeting]:
        return [m for m in self.meetings if person_id in m.all_attendee_ids()]

    def threads_for(self, person_id: str) -> list[EmailThread]:
        return [
            t
            for t in self.email_threads
            if any(person_id in (m.from_id, *m.to_ids, *m.cc_ids) for m in t.messages)
        ]
