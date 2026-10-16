from __future__ import annotations

import time
from datetime import timedelta

import httpx

from emulators.core.hub import EmulatorHub
from emulators.testing.helpers import mint

from .conftest import API, BASE, create_invoice
from .test_reports import fetch, total


def test_seed_is_fast_deterministic_and_lazy() -> None:
    first = EmulatorHub(providers=["xero"], anchor="2026-10-16")
    started = time.perf_counter()
    second = EmulatorHub(providers=["xero"], anchor="2026-10-16")
    assert time.perf_counter() - started < 2
    assert first.context("xero").store.dump() == second.context("xero").store.dump()
    assert len(first.context("xero").store.blobs) == 0


async def test_financial_guards_and_settlement_invalidate_pdf(client: httpx.AsyncClient) -> None:
    invoice = await create_invoice(client)
    path = BASE + "/Invoices/" + invoice["InvoiceID"]
    before = (await client.get(path, headers={"Accept": "application/pdf"})).content
    payment = await client.put(
        BASE + "/Payments",
        json={
            "Invoice": {"InvoiceID": invoice["InvoiceID"]},
            "Date": "2026-10-16",
            "Account": {"Code": "1000"},
            "Amount": 25,
        },
    )
    assert payment.status_code == 200
    after = (await client.get(path, headers={"Accept": "application/pdf"})).content
    assert before != after
    assert b"Amount due: 75.00 USD" in after
    assert (await client.post(path, json={"Type": "ACCPAY"})).status_code == 400
    bad_payment = await client.put(
        BASE + "/Payments",
        json={
            "Invoice": {"InvoiceID": invoice["InvoiceID"]},
            "Date": "2026-10-16",
            "Account": {"Code": "bad"},
            "Amount": 25,
        },
    )
    assert bad_payment.status_code == 400
    accounts = (await client.get(BASE + "/Accounts", params={"where": 'Code=="4000"'})).json()["Accounts"]
    assert (
        await client.post(BASE + "/Accounts/" + accounts[0]["AccountID"], json={"Code": "4050"})
    ).status_code == 400
    credit = await client.put(
        BASE + "/CreditNotes",
        json={
            "Contact": invoice["Contact"],
            "Type": "ACCRECCREDIT",
            "Status": "AUTHORISED",
            "Date": "2026-10-16",
            "LineItems": [{"Quantity": 1, "UnitAmount": 40, "AccountCode": "4200"}],
        },
    )
    assert credit.status_code == 200
    cid = credit.json()["CreditNotes"][0]["CreditNoteID"]
    forged = await client.post(
        BASE + "/CreditNotes/" + cid,
        json={"Allocations": [{"Invoice": {"InvoiceID": invoice["InvoiceID"]}, "Amount": 999}]},
    )
    assert forged.status_code == 400
    allocated = await client.put(
        BASE + "/CreditNotes/" + cid + "/Allocations",
        json={
            "Allocations": [
                {"Invoice": {"InvoiceID": invoice["InvoiceID"]}, "Amount": 20, "Date": "2026-10-16"}
            ]
        },
    )
    assert allocated.status_code == 200
    assert (await client.post(BASE + "/CreditNotes/" + cid, json={"Status": "VOIDED"})).status_code == 400
    trial = await fetch(client, "TrialBalance", date="2026-10-16")
    assert total(trial, "Total", 3) == total(trial, "Total", 4)


async def test_ap_and_bank_receive_postings_balance_and_tenant_writes_are_isolated(
    client: httpx.AsyncClient,
) -> None:
    before = await fetch(client, "BalanceSheet", date="2026-10-16")
    contacts = (await client.get(BASE + "/Contacts")).json()["Contacts"]
    supplier = next(c for c in contacts if c["IsSupplier"])
    bill = await create_invoice(
        client,
        Type="ACCPAY",
        Contact={"ContactID": supplier["ContactID"]},
        LineItems=[{"Quantity": 1, "UnitAmount": 200, "AccountCode": "6200", "TaxType": "NONE"}],
    )
    after = await fetch(client, "BalanceSheet", date="2026-10-16")
    assert total(after, "Total Liabilities") == total(before, "Total Liabilities") + 200
    assert total(after, "Total Equity") == total(before, "Total Equity") - 200
    payment = await client.put(
        BASE + "/Payments",
        json={
            "Invoice": {"InvoiceID": bill["InvoiceID"]},
            "Account": {"Code": "1000"},
            "Date": "2026-10-16",
            "Amount": 200,
        },
    )
    assert payment.status_code == 200
    assert payment.json()["Payments"][0]["PaymentType"] == "ACCPAYPAYMENT"
    receive = await client.put(
        BASE + "/BankTransactions",
        json={
            "Type": "RECEIVE",
            "Contact": bill["Contact"],
            "BankAccount": {"Code": "1000"},
            "Date": "2026-10-16",
            "LineItems": [{"Quantity": 1, "UnitAmount": 10, "AccountCode": "4900"}],
        },
    )
    assert receive.status_code == 200
    balance = await fetch(client, "BalanceSheet", date="2026-10-16")
    assert total(balance, "Total Assets") == total(before, "Total Assets") - 190
    assert total(balance, "Total Assets") == total(balance, "Total Liabilities") + total(
        balance, "Total Equity"
    )
    tenants = (await client.get("/connections")).json()
    demo = tenants[1]["tenantId"]
    demo_contact = await client.put(
        BASE + "/Contacts", json={"Name": "Demo-only contact"}, headers={"xero-tenant-id": demo}
    )
    assert demo_contact.status_code == 200
    cid = demo_contact.json()["Contacts"][0]["ContactID"]
    assert (await client.get(BASE + "/Contacts/" + cid)).status_code == 404


async def test_case_insensitive_paths_prefix_and_dedicated_app(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api.xro/2.0/invoices", params={"page": 1})).status_code == 200
    invoice = await create_invoice(client)
    assert (
        await client.get(BASE + "/invoices/" + invoice["InvoiceID"] + "/onlineinvoice")
    ).status_code == 200
    headers = dict(client.headers)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.hub.gateway),
        base_url="http://localhost:44080",
        headers=headers,
    ) as prefix:
        online = await prefix.get("/xero" + BASE + "/Invoices/" + invoice["InvoiceID"] + "/OnlineInvoice")
        assert online.status_code == 200, online.text
        assert online.json()["OnlineInvoices"][0]["OnlineInvoiceUrl"].startswith(
            "http://localhost:44080/xero/"
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=client.hub.apps["xero"]),
        base_url="http://localhost:44112",
        headers=headers,
    ) as dedicated:
        assert (await dedicated.get(BASE + "/Organisation")).status_code == 200


async def test_zero_ttl_granular_scopes_and_known_invalid_payloads(client: httpx.AsyncClient) -> None:
    expired = await mint(client, API, user="p001", expires_in=0)
    assert (
        await client.get(BASE + "/Contacts", headers={"Authorization": "Bearer " + expired})
    ).status_code == 401
    scoped = await mint(client, API, app_only=True, scopes=["accounting.invoices.read"], client_id="granular")
    assert (
        await client.get(BASE + "/Invoices", headers={"Authorization": "Bearer " + scoped})
    ).status_code == 200
    assert (
        await client.get(BASE + "/Payments", headers={"Authorization": "Bearer " + scoped})
    ).status_code == 401
    for data in [[], {"Invoices": []}, {"Invoices": ["bad"]}]:
        response = await client.put(BASE + "/Invoices", json=data)
        assert response.status_code == 400
    assert (
        await client.put(
            BASE + "/Invoices", content=b"broken-json", headers={"Content-Type": "application/json"}
        )
    ).status_code == 400
    assert (await client.post("/_emulator/tokens", json={"app_only": False})).status_code == 403


async def test_rejected_batch_leaves_no_generated_contact_or_partial_financial_state(
    client: httpx.AsyncClient,
) -> None:
    invoice = await create_invoice(client)
    before = await fetch(client, "BalanceSheet", date="2026-10-16")
    response = await client.put(
        BASE + "/Invoices",
        json={
            "Invoices": [
                {
                    "Type": "ACCREC",
                    "Status": "AUTHORISED",
                    "Contact": {"Name": "Rolled-back synthetic contact"},
                    "LineItems": [{"Quantity": 1, "UnitAmount": 100, "AccountCode": "4200"}],
                },
                {"Type": "ACCREC", "Contact": invoice["Contact"], "LineItems": [{"AccountCode": "invalid"}]},
            ]
        },
    )
    assert response.status_code == 400
    contacts = (
        await client.get(BASE + "/Contacts", params={"where": 'Name=="Rolled-back synthetic contact"'})
    ).json()["Contacts"]
    assert contacts == []
    after = await fetch(client, "BalanceSheet", date="2026-10-16")
    assert total(before, "Total Assets") == total(after, "Total Assets")
    client.hub.clock.advance(timedelta(seconds=60))
