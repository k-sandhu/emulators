"""Entity seeding, CRUD semantics, SyncToken, void/delete, send/PDF, XML."""

from __future__ import annotations

from tests.providers.quickbooks.conftest import API, REALM


async def query(client, headers, text):
    r = await client.get(f"{API}/query", params={"query": text}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["QueryResponse"]


async def test_companyinfo_and_preferences(qb) -> None:
    client, h = qb
    r = await client.get(f"{API}/companyinfo/{REALM}", headers=h)
    assert r.status_code == 200
    assert r.headers.get("intuit_tid")
    info = r.json()["CompanyInfo"]
    assert info["CompanyName"] == "Halcyon Advisory Group"
    assert info["LegalName"] == "Halcyon Advisory Group LLP"
    assert info["FiscalYearStartMonth"] == "January"
    assert info["Email"]["Address"] == "billing@halcyonadvisory.example"
    name_values = {nv["Name"]: nv["Value"] for nv in info["NameValue"]}
    assert name_values["OfferingSku"] == "QuickBooks Online Advanced"
    assert name_values["SubscriptionStatus"] == "SUBSCRIBED"

    r = await client.get(f"{API}/preferences", headers=h)
    assert r.status_code == 200
    assert r.json()["Preferences"]["AccountingInfoPrefs"]["TrackDepartments"] is True


async def test_customer_job_hierarchy(qb) -> None:
    client, h = qb
    parents = await query(client, h, "SELECT * FROM Customer WHERE DisplayName = 'Northwind Logistics'")
    parent = parents["Customer"][0]
    assert parent["Job"] is False
    assert parent["PrimaryEmailAddr"]["Address"].endswith("@northwindlogistics.example")
    jobs = await query(client, h, f"SELECT * FROM Customer WHERE ParentRef = '{parent['Id']}' MAXRESULTS 20")
    assert jobs["Customer"], "Northwind has engagement jobs"
    job = next(j for j in jobs["Customer"] if j["Job"])
    assert job["Level"] == 1
    assert job["FullyQualifiedName"].startswith("Northwind Logistics:")
    assert job["ParentRef"] == {"value": parent["Id"], "name": "Northwind Logistics"}
    # BalanceWithJobs rolls the jobs' open balances into the parent
    fresh_parent = (await client.get(f"{API}/customer/{parent['Id']}", headers=h)).json()["Customer"]
    assert fresh_parent["BalanceWithJobs"] >= fresh_parent["Balance"]


async def test_former_client_is_inactive_and_hidden_by_default(qb) -> None:
    client, h = qb
    default = await query(client, h, "SELECT * FROM Customer WHERE DisplayName = 'Quarry Lane Manufacturing'")
    assert "Customer" not in default  # name lists default to Active = true
    shown = await query(
        client,
        h,
        "SELECT * FROM Customer WHERE DisplayName = 'Quarry Lane Manufacturing' AND Active IN (true, false)",
    )
    assert shown["Customer"][0]["Active"] is False


async def test_invoice_seed_shape(qb) -> None:
    client, h = qb
    invoices = await query(client, h, "SELECT * FROM Invoice WHERE Balance > 0 MAXRESULTS 5")
    inv = invoices["Invoice"][0]
    assert inv["DocNumber"].startswith("INV-")
    assert inv["TotalAmt"] > 0
    assert inv["Balance"] > 0
    line = next(ln for ln in inv["Line"] if ln["DetailType"] == "SalesItemLineDetail")
    detail = line["SalesItemLineDetail"]
    assert detail["ItemRef"]["value"]
    assert detail["TaxCodeRef"]["value"]
    assert line["Amount"] == detail["Qty"] * detail["UnitPrice"]
    assert any(ln["DetailType"] == "SubTotalLineDetail" for ln in inv["Line"])
    assert inv["TxnTaxDetail"]["TotalTax"] >= 0
    # CA invoices carry HST/GST; at least one taxed invoice exists
    taxed = await query(client, h, "SELECT * FROM Invoice MAXRESULTS 100")
    assert any(i["TxnTaxDetail"]["TotalTax"] > 0 for i in taxed["Invoice"])
    voided = await query(client, h, "SELECT * FROM Invoice MAXRESULTS 200")
    void = next(i for i in voided["Invoice"] if i.get("PrivateNote", "").startswith("Voided"))
    assert void["TotalAmt"] == 0 and void["Balance"] == 0


async def test_crud_roundtrip_sparse_update_and_synctoken(qb) -> None:
    client, h = qb
    r = await client.post(f"{API}/customer", headers=h, json={"DisplayName": "Roundtrip Co"})
    assert r.status_code == 200, r.text
    cust = r.json()["Customer"]
    assert cust["SyncToken"] == "0"
    assert cust["domain"] == "QBO" and cust["sparse"] is False
    assert cust["MetaData"]["CreateTime"].endswith("-07:00")

    r = await client.post(
        f"{API}/customer",
        headers=h,
        json={"Id": cust["Id"], "SyncToken": "0", "sparse": True, "Notes": "updated note"},
    )
    assert r.status_code == 200, r.text
    updated = r.json()["Customer"]
    assert updated["Notes"] == "updated note"
    assert updated["DisplayName"] == "Roundtrip Co"
    assert updated["SyncToken"] == "1"

    stale = await client.post(
        f"{API}/customer",
        headers=h,
        json={"Id": cust["Id"], "SyncToken": "0", "sparse": True, "Notes": "stale"},
    )
    assert stale.status_code == 400
    err = stale.json()["Fault"]["Error"][0]
    assert err["code"] == "5010" and err["Message"] == "Stale Object Error"


async def test_create_validation_errors(qb) -> None:
    client, h = qb
    # missing required field -> 2020
    r = await client.post(f"{API}/customer", headers=h, json={"CompanyName": "No Display Name"})
    assert r.status_code == 400
    assert r.json()["Fault"]["Error"][0]["code"] == "2020"
    # duplicate name -> 6240
    r = await client.post(f"{API}/customer", headers=h, json={"DisplayName": "Northwind Logistics"})
    assert r.status_code == 400
    assert r.json()["Fault"]["Error"][0]["code"] == "6240"
    # unknown referenced object -> 610
    r = await client.post(
        f"{API}/invoice",
        headers=h,
        json={
            "CustomerRef": {"value": "99999"},
            "Line": [{"DetailType": "SalesItemLineDetail", "Amount": 10}],
        },
    )
    assert r.status_code == 400
    assert r.json()["Fault"]["Error"][0]["code"] == "610"
    # read of a missing object -> 610
    r = await client.get(f"{API}/invoice/424242", headers=h)
    assert r.status_code == 400
    assert r.json()["Fault"]["Error"][0]["Message"] == "Object Not Found"


async def test_invoice_payment_linkage_moves_balances(qb) -> None:
    client, h = qb
    items = await query(client, h, "SELECT * FROM Item MAXRESULTS 1")
    item = items["Item"][0]
    customers = await query(client, h, "SELECT * FROM Customer WHERE DisplayName = 'Bluefin Seafood'")
    customer = customers["Customer"][0]
    before = customer["BalanceWithJobs"]

    r = await client.post(
        f"{API}/invoice",
        headers=h,
        json={
            "CustomerRef": {"value": customer["Id"]},
            "Line": [
                {
                    "DetailType": "SalesItemLineDetail",
                    "Description": "Ad hoc work",
                    "Amount": 1000,
                    "SalesItemLineDetail": {"ItemRef": {"value": item["Id"]}, "Qty": 4, "UnitPrice": 250},
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    invoice = r.json()["Invoice"]
    assert invoice["TotalAmt"] == 1000
    assert invoice["Balance"] == 1000
    assert invoice["DocNumber"]

    r = await client.post(
        f"{API}/payment",
        headers=h,
        json={
            "CustomerRef": {"value": customer["Id"]},
            "TotalAmt": 400,
            "Line": [{"Amount": 400, "LinkedTxn": [{"TxnId": invoice["Id"], "TxnType": "Invoice"}]}],
        },
    )
    assert r.status_code == 200, r.text
    after = (await client.get(f"{API}/invoice/{invoice['Id']}", headers=h)).json()["Invoice"]
    assert after["Balance"] == 600
    assert after["LinkedTxn"][0]["TxnType"] == "Payment"
    parent = (await client.get(f"{API}/customer/{customer['Id']}", headers=h)).json()["Customer"]
    assert parent["BalanceWithJobs"] == before + 600


async def test_void_and_delete_invoice(qb) -> None:
    client, h = qb
    items = await query(client, h, "SELECT * FROM Item MAXRESULTS 1")
    customers = await query(client, h, "SELECT * FROM Customer MAXRESULTS 1")
    payload = {
        "CustomerRef": {"value": customers["Customer"][0]["Id"]},
        "Line": [
            {
                "DetailType": "SalesItemLineDetail",
                "Amount": 250,
                "SalesItemLineDetail": {
                    "ItemRef": {"value": items["Item"][0]["Id"]},
                    "Qty": 1,
                    "UnitPrice": 250,
                },
            }
        ],
    }
    invoice = (await client.post(f"{API}/invoice", headers=h, json=payload)).json()["Invoice"]
    r = await client.post(
        f"{API}/invoice",
        params={"operation": "void"},
        headers=h,
        json={"Id": invoice["Id"], "SyncToken": invoice["SyncToken"]},
    )
    assert r.status_code == 200, r.text
    voided = r.json()["Invoice"]
    assert voided["TotalAmt"] == 0 and voided["Balance"] == 0
    assert "Voided" in voided["PrivateNote"]

    invoice2 = (await client.post(f"{API}/invoice", headers=h, json=payload)).json()["Invoice"]
    r = await client.post(
        f"{API}/invoice",
        params={"operation": "delete"},
        headers=h,
        json={"Id": invoice2["Id"], "SyncToken": invoice2["SyncToken"]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["Invoice"] == {"Id": invoice2["Id"], "status": "Deleted"}
    r = await client.get(f"{API}/invoice/{invoice2['Id']}", headers=h)
    assert r.status_code == 400  # deleted invoices read as Object Not Found


async def test_send_invoice_and_pdf(qb) -> None:
    client, h = qb
    invoices = await query(client, h, "SELECT * FROM Invoice MAXRESULTS 1")
    inv = invoices["Invoice"][0]
    r = await client.post(
        f"{API}/invoice/{inv['Id']}/send",
        params={"sendTo": "ap@client.example"},
        headers=h,
        content=b"",
    )
    assert r.status_code == 200, r.text
    sent = r.json()["Invoice"]
    assert sent["EmailStatus"] == "EmailSent"
    assert sent["BillEmail"]["Address"] == "ap@client.example"
    assert sent["DeliveryInfo"]["DeliveryType"] == "Email"

    r = await client.get(f"{API}/invoice/{inv['Id']}/pdf", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content.startswith(b"%PDF-")


async def test_bill_and_billpayment_seeded(qb) -> None:
    client, h = qb
    unpaid = await query(client, h, "SELECT * FROM Bill WHERE Balance > 0 MAXRESULTS 5")
    assert unpaid["Bill"], "some bills are still open"
    bill = unpaid["Bill"][0]
    assert bill["VendorRef"]["name"]
    line = bill["Line"][0]
    assert line["DetailType"] == "AccountBasedExpenseLineDetail"
    assert line["AccountBasedExpenseLineDetail"]["AccountRef"]["value"]
    paid = await query(client, h, "SELECT * FROM BillPayment MAXRESULTS 3")
    assert paid["BillPayment"][0]["PayType"] == "Check"
    assert paid["BillPayment"][0]["Line"][0]["LinkedTxn"][0]["TxnType"] == "Bill"


async def test_journal_entry_must_balance(qb) -> None:
    client, h = qb
    accounts = await query(client, h, "SELECT * FROM Account MAXRESULTS 2")
    a1, a2 = accounts["Account"][0], accounts["Account"][1]
    bad = {
        "Line": [
            {
                "Amount": 100,
                "DetailType": "JournalEntryLineDetail",
                "JournalEntryLineDetail": {"PostingType": "Debit", "AccountRef": {"value": a1["Id"]}},
            },
            {
                "Amount": 50,
                "DetailType": "JournalEntryLineDetail",
                "JournalEntryLineDetail": {"PostingType": "Credit", "AccountRef": {"value": a2["Id"]}},
            },
        ]
    }
    r = await client.post(f"{API}/journalentry", headers=h, json=bad)
    assert r.status_code == 400
    assert r.json()["Fault"]["Error"][0]["code"] == "6000"
    bad["Line"][1]["Amount"] = 100
    r = await client.post(f"{API}/journalentry", headers=h, json=bad)
    assert r.status_code == 200, r.text


async def test_timeactivities_and_estimates_seeded(qb) -> None:
    client, h = qb
    activities = await query(client, h, "SELECT * FROM TimeActivity MAXRESULTS 5")
    assert activities["TimeActivity"]
    ta = activities["TimeActivity"][0]
    assert ta["NameOf"] == "Employee"
    assert ta["BillableStatus"] in ("Billable", "HasBeenBilled", "NotBillable")
    assert ta["Hours"] > 0
    estimates = await query(client, h, "SELECT * FROM Estimate MAXRESULTS 20")
    statuses = {e["TxnStatus"] for e in estimates["Estimate"]}
    assert "Pending" in statuses
    prospects = await query(
        client,
        h,
        "SELECT * FROM Customer WHERE DisplayName IN ('Polaris Clean Energy','Willowbrook Senior Living')",
    )
    assert len(prospects["Customer"]) == 2


async def test_xml_response_for_reads(qb) -> None:
    client, h = qb
    r = await client.get(f"{API}/customer/1", headers={**h, "Accept": "application/xml"})
    assert r.status_code == 200
    assert r.text.startswith('<?xml version="1.0"')
    assert '<IntuitResponse xmlns="http://schema.intuit.com/finance/v3"' in r.text
    assert "<DisplayName>Northwind Logistics</DisplayName>" in r.text


async def test_minorversion_validation(qb) -> None:
    client, h = qb
    r = await client.get(f"{API}/customer/1", params={"minorversion": "75"}, headers=h)
    assert r.status_code == 200
    r = await client.get(f"{API}/customer/1", params={"minorversion": "99"}, headers=h)
    assert r.status_code == 400
