"""Text generators: document bodies, email bodies, chat lines, meeting dialogue, SMS.

Templates are written to read like a real accounting/advisory firm's paper trail. They
are filled from world entities, and the seeded ``random.Random`` passed in decides which
variant is used, so output is deterministic per seed.
"""

from __future__ import annotations

import random
from datetime import date

from emulators.world.model import Client, Engagement, Person


def money(amount: float) -> str:
    return f"${amount:,.0f}" if amount >= 1000 else f"${amount:,.2f}"


def fy(d: date) -> str:
    return f"FY{d.year}"


def signature(p: Person, org_name: str, phone: str | None) -> str:
    lines = ["", "Best regards,", p.display_name, p.job_title, org_name]
    if phone:
        lines.append(phone)
    return "\n".join(lines)


# ---------------------------------------------------------------------------------------
# Document bodies
# ---------------------------------------------------------------------------------------


def engagement_letter(client: Client, eng: Engagement, lead: Person, org: str) -> list[str]:
    fee = (
        f"a fixed fee of {money(eng.fixed_fee)}"
        if eng.fixed_fee
        else f"our standard hourly rates (blended {money(eng.hourly_rate)} per hour, estimated "
        f"{eng.budget_hours} hours)"
    )
    return [
        f"{client.legal_name}\n{client.address.one_line()}",
        f"Dear {client.name} leadership team,",
        f"This letter confirms the terms of our engagement to provide {eng.service.lower()} services "
        f"({eng.name}) for the period beginning {eng.start:%B %d, %Y}.",
        "Scope of services. We will perform the procedures described in Appendix A. Our work is designed to "
        "provide the deliverables listed there and does not include services outside that scope unless agreed "
        "in writing.",
        "Management responsibilities. Management is responsible for the preparation of the financial records, "
        "for designing and maintaining internal controls, and for providing us with complete and accurate "
        "information on a timely basis.",
        f"Fees and billing. Our fees will be based on {fee}. Invoices are issued monthly and are payable within "
        f"{client.payment_terms_days} days of the invoice date.",
        f"Engagement team. {lead.display_name} ({lead.job_title}) will lead the engagement and be your primary "
        "point of contact.",
        f"Please sign and return a copy of this letter to indicate your agreement. We appreciate the opportunity "
        f"to serve {client.name}.\n\nSincerely,\n{lead.display_name}\n{org}",
    ]


def meeting_notes(client: Client, eng: Engagement, people: list[Person], rng: random.Random) -> list[str]:
    attendees = ", ".join(p.display_name for p in people)
    topics = rng.sample(
        [
            "Timeline for fieldwork and key reporting deadlines",
            "Status of the prepared-by-client (PBC) request list",
            "Revenue cut-off testing approach for the year-end",
            "Treatment of the new equipment lease under ASC 842",
            "Inventory count logistics and observation dates",
            "Open questions on intercompany balances",
            "Access to the ERP and bank portals for the engagement team",
            "Expected audit adjustments and materiality thresholds",
        ],
        4,
    )
    actions = rng.sample(
        [
            f"{client.name} finance team to upload bank reconciliations for all accounts by Friday.",
            "Engagement team to circulate the updated PBC list with owners and due dates.",
            "Schedule a follow-up call on lease accounting with the controller.",
            "Confirm inventory observation date and attendees.",
            "Provide read-only access to the accounts payable subledger.",
            "Share the draft management representation letter for review.",
        ],
        3,
    )
    return [
        f"Attendees: {attendees}",
        f"Purpose: {eng.name} working session.",
        "Discussion topics:\n" + "\n".join(f"- {t}" for t in topics),
        "Decisions: The team agreed to keep the current timeline and to escalate any blockers in the weekly "
        "status meeting.",
        "Action items:\n" + "\n".join(f"- {a}" for a in actions),
    ]


def due_diligence_report(client: Client, rng: random.Random) -> list[str]:
    ebitda = rng.randint(6, 18) * 1_000_000
    adj = rng.randint(4, 12) * 100_000
    return [
        f"Executive summary. We performed financial due diligence on {client.name} covering the trailing "
        "twenty-four months. Reported EBITDA was "
        f"{money(ebitda)}; our adjusted EBITDA is {money(ebitda - adj)} after normalisation adjustments.",
        "Quality of earnings. The principal adjustments relate to owner compensation in excess of market, "
        "one-time legal costs, and a change in the warranty reserve methodology.",
        "Net working capital. Average net working capital over the period was within the expected range; "
        "seasonality peaks in the third quarter.",
        "Debt and debt-like items. We identified deferred revenue, accrued bonuses and an unrecorded "
        "litigation contingency that should be considered in the purchase price.",
        "Key risks. Customer concentration (top five customers represent 46% of revenue) and dependence on two "
        "contract manufacturers.",
    ]


def generic_policy(title: str, org: str, rng: random.Random) -> list[str]:
    return [
        f"Purpose. This {title.lower()} sets out how {org} protects its people, clients and information.",
        "Scope. It applies to all partners, employees, contractors and interns, in every office.",
        "Responsibilities. Managers are responsible for ensuring their teams understand and follow this "
        "policy. Questions should be directed to the policy owner.",
        rng.choice(
            [
                "Exceptions. Any exception must be approved in writing by the policy owner and reviewed annually.",
                "Compliance. Violations may result in disciplinary action up to and including termination.",
            ]
        ),
        "Review. This policy is reviewed every twelve months or after any significant change.",
    ]


KNOWLEDGE_ARTICLES = {
    "2026 Tax Law Changes Summary": [
        "This summary highlights legislative changes effective for tax years beginning in 2026.",
        "Research and experimental expenditures: domestic costs are again immediately deductible; foreign "
        "research costs remain subject to fifteen-year amortisation.",
        "Bonus depreciation has been restored to 100% for qualified property placed in service after January 19, "
        "2025.",
        "State conformity varies: several states decoupled from the federal changes, so maintain separate "
        "depreciation schedules.",
    ],
    "Revenue Recognition (ASC 606) Primer": [
        "ASC 606 applies a five-step model: identify the contract, identify performance obligations, determine "
        "the transaction price, allocate it, and recognise revenue when obligations are satisfied.",
        "SaaS arrangements typically recognise subscription revenue ratably; implementation services may be "
        "distinct performance obligations.",
        "Common audit findings include incorrect treatment of contract modifications and variable consideration.",
    ],
    "Sales Tax Nexus Guide": [
        "Economic nexus thresholds generally range from $100,000 to $500,000 in sales into a state.",
        "Marketplace facilitator rules shift collection responsibility for sales through platforms.",
        "Document nexus studies annually and register before crossing thresholds where possible.",
    ],
    "Client Service Standards": [
        "Respond to client emails within one business day, even if only to acknowledge receipt.",
        "Every engagement has a named partner and manager; clients should never be unsure whom to call.",
        "Deliverables are reviewed by a second qualified professional before they leave the firm.",
    ],
    "Audit Methodology Overview": [
        "Planning: understand the entity, assess risks and set materiality.",
        "Execution: test controls where relied upon and perform substantive procedures.",
        "Completion: evaluate misstatements, obtain representations and issue the report.",
    ],
}


# ---------------------------------------------------------------------------------------
# Email bodies
# ---------------------------------------------------------------------------------------


def email_body(
    kind: str,
    *,
    sender: Person,
    recipient: Person,
    client: Client | None,
    eng: Engagement | None,
    rng: random.Random,
    extra: dict[str, str] | None = None,
) -> str:
    extra = extra or {}
    greeting = f"Hi {recipient.first_name},"
    e = eng.name if eng else ""
    templates: dict[str, list[str]] = {
        "pbc_request": [
            f"As we kick off {e}, I've shared the PBC request list in our client folder. Could your team "
            f"upload the year-end bank statements, the fixed asset rollforward and the AP aging by "
            f"{extra.get('due', 'next Friday')}?",
            "Let me know if any items will take longer and we can re-sequence fieldwork.",
        ],
        "pbc_reply": [
            "Thanks for sending this over. Most of the items are ready; the fixed asset rollforward needs another "
            "couple of days because we're still reconciling two disposals.",
            "I'll upload everything else to the shared folder this afternoon.",
        ],
        "pbc_thanks": [
            "Perfect, thank you. We'll start on cash and payables first and circle back on fixed assets.",
        ],
        "schedule": [
            f"Would {extra.get('slot', 'Tuesday at 2:00 pm')} work for a 45-minute call to walk through the "
            f"preliminary findings for {e}?",
            "I'll send a calendar invite with the meeting link once you confirm.",
        ],
        "schedule_reply": [
            f"{extra.get('slot', 'Tuesday at 2:00 pm')} works for us. Please include our controller as well.",
        ],
        "invoice": [
            f"Please find our invoice {extra.get('invoice', '')} for {e} attached. The amount due is "
            f"{extra.get('amount', '')}, payable by {extra.get('due', '')}.",
            "Payment can be made by ACH or via the card link on the invoice. Thank you for your business.",
        ],
        "invoice_reply": [
            "Thanks — this has been approved and is scheduled for payment on our next run. "
            "Remittance details will follow from our AP team.",
        ],
        "question": [
            rng.choice(
                [
                    "We're considering leasing rather than buying the new delivery vehicles. How would that be "
                    "treated on our balance sheet, and does it change our covenant calculations?",
                    "Our sales into Texas and Washington grew a lot this year. Do we need to register for sales "
                    "tax in those states?",
                    "We'd like to pay year-end bonuses in January. Can we still deduct them for this year?",
                    "The board asked whether we should move to quarterly close instead of monthly. What do you "
                    "recommend for a company our size?",
                ]
            ),
            "Happy to set up a call if that's easier.",
        ],
        "question_reply": [
            "Great question. Short answer: yes, it affects both the balance sheet and the covenant definitions, "
            "but most lenders allow a carve-out. I'll send a short memo with the details and the numbers for "
            "your situation by Thursday.",
        ],
        "deliverable": [
            f"Attached is the draft deliverable for {e}. Please review and send any comments by "
            f"{extra.get('due', 'end of next week')}.",
            "We've highlighted the open items in yellow. Once we receive your comments we'll finalise.",
        ],
        "deliverable_reply": [
            "Thanks. We've reviewed the draft and have a few comments on the inventory note — I've added them as "
            "comments directly in the document.",
        ],
        "staffing": [
            f"We need one more senior on {e} for the next three weeks. Fieldwork is running behind because of "
            "late PBC items. Can anyone free up 50% time?",
        ],
        "staffing_reply": [
            "I can move two days a week from my current engagement starting Monday. Let me know where to start.",
        ],
        "announcement": [
            extra.get("text", ""),
        ],
        "expense": [
            f"Please approve my expense report for {extra.get('month', '')} ({extra.get('amount', '')}). "
            "Receipts are attached in the expense system.",
        ],
        "expense_reply": ["Approved — thanks for submitting on time."],
        "vendor_invoice": [
            f"Your invoice {extra.get('invoice', '')} for {extra.get('amount', '')} is now available. "
            f"Payment is due by {extra.get('due', '')}.",
            "If you have questions about this invoice, reply to this email.",
        ],
    }
    paragraphs = templates.get(kind, ["Following up on this."])
    sig = signature(sender, extra.get("org", ""), sender.phone_business)
    return "\n\n".join([greeting, *paragraphs]) + "\n" + sig


ANNOUNCEMENTS = [
    (
        "Reminder: complete MFA enrollment by Friday",
        "IT is completing the rollout of phishing-resistant MFA. If you haven't registered a passkey or security "
        "key yet, please do so by Friday. Accounts without MFA will be blocked from external sign-in next week.",
        "it",
    ),
    (
        "Updated expense reimbursement policy",
        "We've updated the expense reimbursement policy: meal limits increase to $75 per day for client travel and "
        "receipts are now required for every item over $25. The policy is in the Finance & Operations site.",
        "finance",
    ),
    (
        "Welcome our new team members",
        "Please join me in welcoming the new associates who joined Audit and Tax this month. Say hello on the "
        "General channel and help them find their way around.",
        "people",
    ),
    (
        "Open enrollment for 2027 benefits starts Monday",
        "Open enrollment runs for two weeks. The benefits overview and plan comparison are in the People & Culture "
        "site. Information sessions are scheduled for Tuesday and Thursday at noon.",
        "people",
    ),
    (
        "Lunch & learn: AI tools in the audit workflow",
        "Join us Thursday at noon for a demo of how we're using AI to draft PBC lists, summarise contracts and "
        "flag unusual journal entries. Lunch provided in Toronto and New York.",
        "audit",
    ),
    (
        "Q3 results and year-end priorities",
        "Q3 revenue came in 6% ahead of plan and utilisation averaged 74%. Our priorities for year-end are "
        "collections, busy-season staffing and the client satisfaction survey.",
        "exec",
    ),
    (
        "Office closure for building maintenance",
        "The Toronto office will be closed on Saturday for electrical maintenance. Badge access will be disabled "
        "from 6 pm Friday to 6 am Monday.",
        "finance",
    ),
    (
        "Security notice: suspicious invoice emails",
        "We've seen phishing emails impersonating our vendors and asking to change banking details. Never change "
        "payment details based on an email; call the vendor using the number in our vendor master.",
        "it",
    ),
]


# ---------------------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------------------

CHANNEL_LINES = {
    "general": [
        "Morning all — reminder that timesheets are due by 5 pm today.",
        "Has anyone used the new expense app on Android? The receipt scan keeps failing for me.",
        "Congrats to the Tax team on getting every extension filed on time!",
        "The Wi-Fi on floor 24 is being upgraded tonight, expect a short outage around 7 pm.",
        "Who's going to the CPA association dinner next week? Let's carpool.",
        "Friendly reminder to lock your screen when you step away. Security found three unlocked laptops today.",
        "Can someone share the latest version of the engagement letter template?",
        "Coffee machine on 24 is fixed ☕",
    ],
    "fieldwork": [
        "Bank confirmations for {client} are back — two accounts still outstanding.",
        "I'm stuck on the revenue cut-off sample for {client}, can someone take a look at item 14?",
        "Inventory observation for {client} is confirmed for Thursday 7 am.",
        "Uploaded the updated lead schedules for {client} to the engagement folder.",
        "Client says the fixed asset rollforward will be ready Monday.",
        "Reviewed the AP search for unrecorded liabilities — no exceptions so far.",
    ],
    "tax": [
        "IRS released the updated draft of the 2026 Form 1120 instructions.",
        "Reminder: Q4 estimated payments are due on the 15th for calendar-year clients.",
        "Does anyone have a good template for the R&D credit documentation memo?",
        "{client} wants to discuss the sales tax nexus study next week.",
        "Extension for {client} filed and accepted ✅",
    ],
    "deals": [
        "Data room for {client} opens tomorrow — I'll send access to the team.",
        "Management meeting with {client} moved to Thursday 10 am.",
        "QoE draft is in the folder; please review the working capital section first.",
        "Buyer's counsel asked for the debt-like items schedule by EOD Friday.",
    ],
    "pipeline": [
        "{client} RFP responses are due on the 30th — proposal draft in the Proposals folder.",
        "Great intro call with {client}; they're unhappy with their current auditor's responsiveness.",
        "Webinar registrations are at 214, our best yet.",
        "Can partners review the pricing for {client} before Friday?",
    ],
    "it": [
        "Patching window tonight 10 pm to midnight, laptops will restart automatically.",
        "New hires: your laptops are on the shelf in the IT room with your names on them.",
        "If you see a 'password expired' prompt on the VPN, restart and sign in again — known issue.",
        "We're retiring the old file server next month; everything has been migrated to the new sites.",
    ],
    "social": [
        "Pumpkin carving contest entries close Friday 🎃",
        "Who's in for trivia night on Thursday?",
        "Photos from the summer offsite are up in the shared album.",
        "Running club meets at 6 pm by the lake, all paces welcome.",
    ],
    "requests": [
        "Uploaded the September bank statements to the Requests folder.",
        "Could you confirm whether you need the payroll registers for all twelve months?",
        "Item 7 on the PBC list is done.",
        "We're still waiting for the board minutes from the March meeting.",
    ],
}

REPLY_LINES = [
    "Thanks, on it.",
    "Good catch — I'll update it.",
    "+1, same issue for me.",
    "Done 👍",
    "Can we discuss in the sync tomorrow?",
    "I'll take that one.",
    "Shared in the folder now.",
    "Thanks for flagging!",
]


# ---------------------------------------------------------------------------------------
# Meeting dialogue
# ---------------------------------------------------------------------------------------


def client_meeting_dialogue(
    host: Person,
    others: list[Person],
    client_people: list[Person],
    client: Client,
    eng: Engagement | None,
    rng: random.Random,
) -> list[tuple[str, str]]:
    eng_name = eng.name if eng else f"our work with {client.name}"
    c = client_people[0] if client_people else (others[0] if others else host)
    staff = others[0] if others else host
    lines: list[tuple[str, str]] = [
        (
            host.id,
            f"Good morning everyone, thanks for joining. Today I'd like to walk through where we are on "
            f"{eng_name}, the open items, and next steps.",
        ),
        (c.id, f"Thanks {host.first_name}. Before we start, our controller may join a few minutes late."),
        (host.id, "No problem. Let's start with the status of the request list."),
        (
            staff.id,
            rng.choice(
                [
                    "We've received about eighty percent of the items. The main outstanding ones are the fixed asset "
                    "rollforward and the support for the accrued liabilities.",
                    "Most of the requests are in. We're still waiting on the revenue contracts for the top ten customers "
                    "and the updated inventory listing.",
                ]
            ),
        ),
        (
            c.id,
            "The rollforward should be done by Wednesday. We had two disposals in the third quarter that took "
            "longer to reconcile than expected.",
        ),
        (
            host.id,
            "That works. Can you also confirm whether the new warehouse lease commenced before or after "
            "year end? It affects how we test the right-of-use asset.",
        ),
        (c.id, "It commenced on the first of November, so after year end."),
        (
            staff.id,
            "Then it'll be a subsequent event disclosure rather than a balance sheet item. I'll note that.",
        ),
        (
            host.id,
            rng.choice(
                [
                    "On revenue, our testing so far hasn't identified any exceptions in cut-off.",
                    "On revenue, we found two invoices recorded in the wrong period; the total is below our threshold but "
                    "we'll propose an adjustment.",
                ]
            ),
        ),
        (c.id, "Okay. Please send us the details so our team can review the two items."),
        (
            host.id,
            "Will do. Next, the timeline. We're still on track to issue the draft by the end of the month "
            "if we have everything by Friday.",
        ),
        (c.id, "Our board meets on the twentieth, so a draft before then would be ideal."),
        (host.id, "Understood. We'll prioritise that. Any other questions from your side?"),
        (
            c.id,
            rng.choice(
                [
                    "One thing — the bank asked for a covenant compliance letter. Is that something you can help with?",
                    "Just one — can we get a breakdown of the fees billed to date versus the estimate?",
                ]
            ),
        ),
        (host.id, "Yes, we can. I'll follow up by email with what we need from you."),
        (staff.id, "I'll also send an updated request list with owners and due dates after this call."),
        (
            host.id,
            "Great. To recap the action items: rollforward by Wednesday, details on the revenue items from "
            "us, and the updated request list today. Thanks everyone.",
        ),
        (c.id, "Thanks all, talk soon."),
    ]
    return lines


def internal_meeting_dialogue(
    kind: str, host: Person, others: list[Person], rng: random.Random, client_names: list[str]
) -> list[tuple[str, str]]:
    a = others[0] if others else host
    b = others[1] if len(others) > 1 else a
    cname = rng.choice(client_names) if client_names else "a key client"
    if kind == "all_hands":
        return [
            (
                host.id,
                "Welcome everyone to this month's all-hands. We have a packed agenda: results, a few "
                "announcements, and time for questions at the end.",
            ),
            (
                host.id,
                "Revenue for the quarter came in ahead of plan, driven by advisory and a strong tax "
                "extension season. Thank you all for the effort.",
            ),
            (
                a.id,
                "From People and Culture: open enrollment starts next week and the information sessions are on "
                "the calendar.",
            ),
            (
                b.id,
                "From Technology: we're finishing the MFA rollout. If you haven't registered a security key, "
                "please do it this week.",
            ),
            (host.id, "Let's open it up for questions."),
            (a.id, "Will we keep the hybrid schedule through busy season?"),
            (
                host.id,
                "Yes. Two anchor days in the office stay the same; teams can add on-site days for "
                "fieldwork as needed.",
            ),
            (b.id, "Any update on the new office in Chicago?"),
            (host.id, "We signed the lease last week. Move-in is planned for the spring. Thanks everyone."),
        ]
    if kind == "one_on_one":
        return [
            (host.id, f"Thanks for making time, {a.first_name}. How are things going this week?"),
            (
                a.id,
                f"Pretty busy. The {cname} work is taking more time than budgeted because the client is late "
                "with their documents.",
            ),
            (
                host.id,
                "Let's flag that in the staffing meeting so we can rebalance. How's your development plan "
                "coming along?",
            ),
            (
                a.id,
                "I finished the data analytics course and I'd like to lead the analytics on the next audit.",
            ),
            (host.id, "That's a great goal. I'll talk to the engagement manager about it."),
            (a.id, "Thanks. Also, I'd like to take a few days off in December."),
            (host.id, "Sounds fine, just put it in the calendar so we can plan coverage."),
        ]
    return [
        (host.id, "Okay, let's get started. First item is utilisation and staffing for the next two weeks."),
        (
            a.id,
            f"We're at about seventy-eight percent this week. {cname} needs two more people for fieldwork.",
        ),
        (host.id, "Can we move someone from the review engagements that wrapped up?"),
        (b.id, "Yes, I can free up one associate from Monday."),
        (host.id, "Good. Next, pipeline. Anything new since last week?"),
        (
            a.id,
            rng.choice(
                [
                    "Two new proposals went out, and one prospect asked for a meeting next Tuesday.",
                    "We had a good intro call with a referral from our banking partner; they need help with a carve-out.",
                ]
            ),
        ),
        (host.id, "Let's make sure the proposals go through partner review before they're sent."),
        (b.id, "Last item: collections. We have three invoices over sixty days."),
        (host.id, "I'll call the clients directly this week. Anything else? Okay, thanks everyone."),
    ]


# ---------------------------------------------------------------------------------------
# SMS / WhatsApp / Messenger
# ---------------------------------------------------------------------------------------

TEXT_THREADS = {
    "reminder": [
        (
            "outbound",
            "Hi {first}, this is {sender} from Halcyon Advisory. Reminder of our call tomorrow at {time}. "
            "Reply C to confirm or R to reschedule.",
        ),
        ("inbound", "C"),
        ("outbound", "Thanks {first}, you're confirmed. Talk tomorrow!"),
    ],
    "documents": [
        (
            "outbound",
            "Hi {first}, quick reminder that we still need the {doc} for {client}. You can upload it to "
            "the shared folder or reply here with a photo.",
        ),
        ("inbound", "Sorry for the delay! Will upload it this afternoon."),
        ("inbound", "Uploaded now. Let me know if you need anything else."),
        ("outbound", "Got it, thank you!"),
    ],
    "reschedule": [
        ("inbound", "Hi {sender_first}, something came up — can we move our meeting to Thursday?"),
        ("outbound", "No problem. Does Thursday at 2 pm work?"),
        ("inbound", "Perfect, thanks."),
        ("outbound", "Updated the invite. See you Thursday."),
    ],
    "payment": [
        (
            "outbound",
            "Hi {first}, invoice {invoice} for {amount} is due on {due}. Pay securely here: "
            "https://pay.halcyonadvisory.example/i/{invoice}",
        ),
        ("inbound", "Paid this morning by card."),
        ("outbound", "Received, thank you! A receipt has been emailed to you."),
    ],
    "question": [
        ("inbound", "Quick question — do I need to keep paper receipts if I have photos of them?"),
        ("outbound", "Photos are fine as long as they're legible and show the date, vendor and amount."),
        ("inbound", "Great, thanks!"),
    ],
    "lead": [
        ("inbound", "Hi, do you work with small businesses on bookkeeping and tax?"),
        (
            "outbound",
            "Hi {first}! Yes, we do. Roughly how many employees do you have, and which accounting "
            "software do you use?",
        ),
        ("inbound", "About 25 people, we use QuickBooks Online."),
        (
            "outbound",
            "That's a great fit for our bookkeeping retainer. Would you like to book a free 20-minute "
            "consultation? https://halcyonadvisory.example/book",
        ),
        ("inbound", "Yes please, next week works."),
    ],
}
