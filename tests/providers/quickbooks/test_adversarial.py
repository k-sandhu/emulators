"""Adversarial wire contracts: invalid inputs, isolation and mutation reversals."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from xml.etree import ElementTree

import httpx
import jwt
import pytest
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse, Response

from emulators.providers.quickbooks.common import ENTITIES, parse_time, qbo_time
from emulators.providers.quickbooks.identity import CLIENT_ID, CLIENT_SECRET
from tests.providers.quickbooks.conftest import API, HOST, mint_user
from tests.providers.quickbooks.test_auth import AUDITOR, FINANCE, basic

TOKEN = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
REVOKE = "https://oauth.platform.intuit.com/oauth2/v1/tokens/revoke"


def fault(response: httpx.Response, code: str | None = None) -> None:
    assert response.status_code == 400, response.text
    error = response.json()["Fault"]["Error"][0]
    assert error["Message"] and error["Detail"]
    if code:
        assert error["code"] == code
    assert response.headers["intuit_tid"]
    assert response.json()["time"]


async def create_invoice(client: httpx.AsyncClient, headers: dict[str, str], **extra: Any) -> dict[str, Any]:
    payload = {
        "CustomerRef": {"value": "1"},
        "Line": [
            {
                "Amount": 100,
                "DetailType": "SalesItemLineDetail",
                "SalesItemLineDetail": {"ItemRef": {"value": "1"}, "Qty": 1, "UnitPrice": 100},
            }
        ],
        **extra,
    }
    response = await client.post(f"{API}/invoice", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    return response.json()["Invoice"]


async def test_invoice_deposit_moves_cash_and_receivables_and_reverses_on_void(qb: Any) -> None:
    client, headers = qb
    ctx = client.hub.contexts["quickbooks"]
    bank_id = str(ctx.store.meta["system_accounts"]["bank"])
    ar_id = str(ctx.store.meta["system_accounts"]["ar"])

    async def balance(account_id: str) -> float:
        response = await client.get(f"{API}/account/{account_id}", headers=headers)
        assert response.status_code == 200, response.text
        return response.json()["Account"]["CurrentBalance"]

    async def cash_income() -> float:
        response = await client.get(
            f"{API}/reports/ProfitAndLoss",
            headers=headers,
            params={"date_macro": "All dates", "accounting_method": "Cash"},
        )
        assert response.status_code == 200, response.text
        income = next(row for row in response.json()["Rows"]["Row"] if row.get("group") == "Income")
        return float(income["Summary"]["ColData"][1]["value"])

    bank_before, ar_before = await balance(bank_id), await balance(ar_id)
    income_before = await cash_income()
    invoice = await create_invoice(client, headers, Deposit=25, DepositToAccountRef={"value": bank_id})
    assert invoice["Balance"] == 75
    assert await balance(bank_id) == pytest.approx(bank_before + 25)
    assert await balance(ar_id) == pytest.approx(ar_before + 75)
    assert await cash_income() == pytest.approx(income_before + 25)
    updated = await client.post(
        f"{API}/invoice",
        headers=headers,
        json={"Id": invoice["Id"], "SyncToken": invoice["SyncToken"], "sparse": True, "Deposit": 40},
    )
    assert updated.status_code == 200, updated.text
    invoice = updated.json()["Invoice"]
    assert invoice["Balance"] == 60
    assert await balance(bank_id) == pytest.approx(bank_before + 40)
    assert await balance(ar_id) == pytest.approx(ar_before + 60)
    assert await cash_income() == pytest.approx(income_before + 40)
    voided = await client.post(
        f"{API}/invoice?operation=void",
        headers=headers,
        json={"Id": invoice["Id"], "SyncToken": invoice["SyncToken"]},
    )
    assert voided.status_code == 200, voided.text
    assert voided.json()["Invoice"]["Deposit"] == 0
    assert await balance(bank_id) == bank_before
    assert await balance(ar_id) == ar_before
    assert await cash_income() == income_before


@pytest.mark.parametrize("entity", ["Invoice", "SalesReceipt", "Estimate", "CreditMemo"])
async def test_sales_line_without_item_is_documentation_and_does_not_change_totals(
    qb: Any, entity: str
) -> None:
    client, headers = qb
    response = await client.post(
        f"{API}/{entity.lower()}",
        headers=headers,
        json={
            "CustomerRef": {"value": "1"},
            "Line": [
                {
                    "DetailType": "SalesItemLineDetail",
                    "Amount": 100,
                    "SalesItemLineDetail": {"ItemRef": {"value": "1"}},
                },
                {
                    "DetailType": "SalesItemLineDetail",
                    "Description": "Documentation, without an item",
                    "Amount": 1000,
                    "SalesItemLineDetail": {"TaxCodeRef": {"value": "1"}},
                },
            ],
        },
    )
    assert response.status_code == 200, response.text
    record = response.json()[entity]
    assert record["TotalAmt"] == 100
    assert record["TxnTaxDetail"]["TotalTax"] == 0
    assert any(line.get("Description") == "Documentation, without an item" for line in record["Line"])


@pytest.mark.parametrize("entity", list(ENTITIES))
async def test_every_entity_rejects_malformed_writes_atomically(qb: Any, entity: str) -> None:
    client, headers = qb
    ctx = client.hub.contexts["quickbooks"]
    original = copy.deepcopy(ctx.store.collection(entity).values())
    record = original[0] if original else {}
    for field in ("Line", "MetaData", "CustomerRef", "TxnTaxDetail", "BillEmail"):
        for bad in (False, 3, "wrong", [None], {"value": []}):
            payload = {"Id": record.get("Id", "999999"), "SyncToken": "0", "sparse": True, field: bad}
            # Unknown references and malformed structures both use vendor faults.
            response = await client.post(f"{API}/{ENTITIES[entity]}", headers=headers, json=payload)
            fault(response)
            assert ctx.store.collection(entity).values() == original


@pytest.mark.parametrize("segment", [*ENTITIES.values(), "batch"])
async def test_non_object_and_non_json_bodies(qb: Any, segment: str) -> None:
    client, headers = qb
    for raw in (b"", b"null", b"[]", b"true", b"3", b"not json", b"{", b"\xff"):
        response = await client.post(f"{API}/{segment}", headers=headers, content=raw)
        fault(response, "2010")


@pytest.mark.parametrize("bad", [None, False, 3, "bad", [], [None]])
async def test_nested_invalid_shapes(qb: Any, bad: Any) -> None:
    client, headers = qb
    payload = {
        "CustomerRef": {"value": "1"},
        "Line": [{"Amount": 1, "DetailType": "SalesItemLineDetail", "SalesItemLineDetail": bad}],
    }
    fault(await client.post(f"{API}/invoice", headers=headers, json=payload), "2010")


async def test_numeric_unicode_and_internal_fields(qb: Any) -> None:
    client, headers = qb
    for amount in (True, "NaN", "Infinity", 1e300, [], {}, "bad"):
        fault(
            await client.post(
                f"{API}/payment", headers=headers, json={"CustomerRef": {"value": "1"}, "TotalAmt": amount}
            )
        )
    for payload in (
        {"DisplayName": "x" * 501},
        {"DisplayName": "bad\ud800"},
        {"DisplayName": "bad\u0000"},
        {"DisplayName": "x", "_opening": 1},
    ):
        # Encode with ASCII escapes to exercise an invalid Unicode scalar on the wire.
        fault(await client.post(f"{API}/customer", headers=headers, content=json.dumps(payload)))
    response = await client.post(f"{API}/customer", headers=headers, json={"DisplayName": "Élan 東京 & Co"})
    assert response.status_code == 200
    assert response.json()["Customer"]["DisplayName"] == "Élan 東京 & Co"
    fault(
        await client.post(
            f"{API}/payment",
            headers=headers,
            json={"CustomerRef": {"value": "1"}, "TotalAmt": 5, "TxnDate": None},
        )
    )
    for bad_url in ("http://[", [], None, "relative/path"):
        fault(await client.post(f"{HOST}/_emulator/actions/configure_webhook", json={"url": bad_url}))


@pytest.mark.parametrize(
    "suffix",
    ["MAXRESULTS 1.5", "STARTPOSITION 1.5", "MAXRESULTS 0", "STARTPOSITION -1", "MAXRESULTS " + "1" * 5000],
)
async def test_pagination_parse_errors_never_crash(qb: Any, suffix: str) -> None:
    client, headers = qb
    fault(
        await client.get(
            f"{API}/query", headers=headers, params={"query": f"SELECT * FROM Invoice {suffix}"}
        ),
        "4000",
    )


async def test_query_escaped_literals_numeric_order_and_last_page(qb: Any) -> None:
    client, headers = qb
    for name in ("Adam's Candy Shop", r"Back\slash"):
        assert (
            await client.post(f"{API}/customer", headers=headers, json={"DisplayName": name})
        ).status_code == 200
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        response = await client.get(
            f"{API}/query",
            headers=headers,
            params={"query": f"SELECT * FROM Customer WHERE DisplayName = '{escaped}'"},
        )
        assert response.status_code == 200, response.text
        assert response.json()["QueryResponse"]["Customer"][0]["DisplayName"] == name
    response = await client.get(
        f"{API}/query",
        headers=headers,
        params={"query": "SELECT * FROM Invoice ORDERBY TotalAmt MAXRESULTS 1000"},
    )
    records = response.json()["QueryResponse"]["Invoice"]
    assert [r["TotalAmt"] for r in records] == sorted(r["TotalAmt"] for r in records)
    for start, count in ((len(records), 1), (len(records) + 1, 0)):
        response = await client.get(
            f"{API}/query",
            headers=headers,
            params={"query": f"SELECT * FROM Invoice STARTPOSITION {start} MAXRESULTS 1000"},
        )
        assert len(response.json()["QueryResponse"].get("Invoice", [])) == count
    fault(
        await client.get(
            f"{API}/query", headers=headers, params={"query": "SELECT * FROM Invoice WHERE MadeUp = 'x'"}
        ),
        "4001",
    )


async def test_default_xml_and_reference_attributes(qb: Any) -> None:
    client, headers = qb
    response = await client.get(
        f"{API}/customer/25", headers={k: v for k, v in headers.items() if k != "Accept"}
    )
    assert response.headers["content-type"].startswith("application/xml")
    root = ElementTree.fromstring(response.content)
    ns = {"q": "http://schema.intuit.com/finance/v3"}
    customer = root.find("q:Customer", ns)
    assert customer is not None and customer.attrib == {"domain": "QBO", "sparse": "false"}
    parent = customer.find("q:ParentRef", ns)
    assert parent is not None and parent.text == "1" and parent.attrib["name"] == "Northwind Logistics"


async def test_updates_are_atomic_even_for_balanced_journal_validation(qb: Any) -> None:
    client, headers = qb
    original = (await client.get(f"{API}/journalentry/1", headers=headers)).json()["JournalEntry"]
    changed = copy.deepcopy(original)
    changed["Line"][0]["Amount"] += 1
    for sparse in (True, False):
        response = await client.post(
            f"{API}/journalentry", headers=headers, json={**changed, "sparse": sparse}
        )
        fault(response, "6000")
        assert (await client.get(f"{API}/journalentry/1", headers=headers)).json()["JournalEntry"] == original
    fault(await client.post(f"{API}/invoice", headers=headers, json={"Id": "1", "SyncToken": "0"}), "2020")


@pytest.mark.parametrize("entity,record_id", [("payment", "1"), ("billpayment", "1")])
@pytest.mark.parametrize("operation", ["delete", "void"])
async def test_payment_reversal_reopens_target_and_clears_links(
    qb: Any, entity: str, record_id: str, operation: str
) -> None:
    client, headers = qb
    payment = (await client.get(f"{API}/{entity}/{record_id}", headers=headers)).json()[
        "Payment" if entity == "payment" else "BillPayment"
    ]
    target_type = "Invoice" if entity == "payment" else "Bill"
    target_id = payment["Line"][0]["LinkedTxn"][0]["TxnId"]
    before = (await client.get(f"{API}/{target_type.lower()}/{target_id}", headers=headers)).json()[
        target_type
    ]
    response = await client.post(
        f"{API}/{entity}",
        params={"operation": operation},
        headers=headers,
        json={"Id": record_id, "SyncToken": payment["SyncToken"]},
    )
    assert response.status_code == 200, response.text
    after = (await client.get(f"{API}/{target_type.lower()}/{target_id}", headers=headers)).json()[
        target_type
    ]
    assert after["Balance"] == before["Balance"] + payment["Line"][0]["Amount"]
    assert {
        "TxnId": record_id,
        "TxnType": "Payment" if entity == "payment" else "BillPayment",
    } not in after.get("LinkedTxn", [])
    assert int(after["SyncToken"]) > int(before["SyncToken"])


async def test_unapplied_payment_and_invalid_links(qb: Any) -> None:
    client, headers = qb
    before = (await client.get(f"{API}/customer/1", headers=headers)).json()["Customer"]["Balance"]
    response = await client.post(
        f"{API}/payment", headers=headers, json={"CustomerRef": {"value": "1"}, "TotalAmt": 25}
    )
    assert response.status_code == 200, response.text
    assert response.json()["Payment"]["UnappliedAmt"] == 25
    assert (await client.get(f"{API}/customer/1", headers=headers)).json()["Customer"][
        "Balance"
    ] == before - 25
    invoice = await create_invoice(client, headers)
    for customer_id, invoice_id, applied in (
        ("2", invoice["Id"], 1),
        ("1", "999999", 1),
        ("1", invoice["Id"], 101),
    ):
        response = await client.post(
            f"{API}/payment",
            headers=headers,
            json={
                "CustomerRef": {"value": customer_id},
                "TotalAmt": 100,
                "Line": [{"Amount": applied, "LinkedTxn": [{"TxnId": invoice_id, "TxnType": "Invoice"}]}],
            },
        )
        fault(response)
    fault(await client.post(f"{API}/payment", headers=headers, json={"CustomerRef": {"value": "1"}}), "2020")
    fault(
        await client.post(
            f"{API}/invoice",
            headers=headers,
            json={
                "CustomerRef": {"value": "USD"},
                "Line": [{"DetailType": "SalesItemLineDetail", "Amount": 1}],
            },
        ),
        "610",
    )


async def test_invoice_due_date_tax_recalculation_void_and_pdf_cache(qb: Any) -> None:
    client, headers = qb
    invoice = await create_invoice(client, headers, TxnDate="2026-01-01", SalesTermRef={"value": "1"})
    assert invoice["DueDate"] == "2026-01-01"
    before_pdf = (await client.get(f"{API}/invoice/{invoice['Id']}/pdf", headers=headers)).content
    line = copy.deepcopy(invoice["Line"][0])
    line["Amount"] = 200
    line["SalesItemLineDetail"].update({"UnitPrice": 200, "TaxCodeRef": {"value": "1"}})
    response = await client.post(
        f"{API}/invoice",
        headers=headers,
        json={"Id": invoice["Id"], "SyncToken": invoice["SyncToken"], "sparse": True, "Line": [line]},
    )
    assert response.status_code == 200, response.text
    updated = response.json()["Invoice"]
    assert updated["Balance"] == updated["TotalAmt"] == 226
    assert updated["TxnTaxDetail"]["TotalTax"] == 26
    after_pdf = (await client.get(f"{API}/invoice/{invoice['Id']}/pdf", headers=headers)).content
    assert before_pdf != after_pdf
    for operation in ("delete", "void"):
        fault(
            await client.post(
                f"{API}/invoice", headers=headers, params={"operation": operation}, json={"Id": invoice["Id"]}
            ),
            "5010",
        )
    response = await client.post(
        f"{API}/invoice",
        headers=headers,
        params={"operation": "void"},
        json={"Id": invoice["Id"], "SyncToken": updated["SyncToken"]},
    )
    voided = response.json()["Invoice"]
    assert voided["Line"][0]["SalesItemLineDetail"]["Qty"] == 0
    assert voided["TxnTaxDetail"]["TaxLine"][0]["Amount"] == 0
    assert "status" not in voided


async def test_auth_client_binding_revocation_scope_expiry_and_nonce(qb_client: httpx.AsyncClient) -> None:
    client = qb_client

    async def mint(**payload: Any) -> dict[str, Any]:
        response = await client.post(f"{HOST}/_emulator/tokens", json={"user": FINANCE, **payload})
        assert response.status_code == 200, response.text
        return response.json()

    a = await mint(client_id="app-a")
    b = await mint(client_id="app-b")
    for url, data in (
        (TOKEN, {"grant_type": "refresh_token", "refresh_token": a["refresh_token"]}),
        (REVOKE, {"token": a["access_token"]}),
    ):
        response = await client.post(url, headers=basic("app-b", "test"), data=data)
        assert response.status_code == 400 and response.json()["error"] == "invalid_grant"
    assert (
        await client.post(
            TOKEN,
            headers=basic("app-a", "test"),
            data={"grant_type": "refresh_token", "refresh_token": a["refresh_token"]},
        )
    ).status_code == 200
    assert (
        await client.post(
            REVOKE,
            headers={**basic("app-a", "test"), "Content-Type": "application/json"},
            json={"token": a["refresh_token"]},
        )
    ).status_code == 200
    for tokens, expected in ((a, 401), (b, 200)):
        assert (
            await client.get(
                f"{API}/customer/1", headers={"Authorization": f"Bearer {tokens['access_token']}"}
            )
        ).status_code == expected
    for scopes in ([], ["openid"], ["email"]):
        tokens = await mint(scopes=scopes)
        assert (
            await client.get(
                f"{API}/customer/1", headers={"Authorization": f"Bearer {tokens['access_token']}"}
            )
        ).status_code == 403
    tokens = await mint(scopes=["openid"])
    response = await client.get(
        "https://accounts.platform.intuit.com/v1/openid_connect/userinfo",
        headers={"Authorization": f"Bearer {tokens['access_token']}"},
    )
    assert set(response.json()) == {"sub"}
    tokens = await mint(expires_in=-1)
    assert (
        await client.get(f"{API}/customer/1", headers={"Authorization": f"Bearer {tokens['access_token']}"})
    ).status_code == 401
    assert (
        await client.post(f"{HOST}/_emulator/tokens", json={"user": "unknown@halcyonadvisory.example"})
    ).status_code == 400
    response = await client.get(
        "https://appcenter.intuit.com/connect/oauth2",
        params={
            "client_id": CLIENT_ID,
            "redirect_uri": "https://localhost/callback",
            "scope": "openid",
            "login_hint": FINANCE,
            "nonce": "test-nonce",
        },
    )
    from urllib.parse import parse_qs, urlparse

    code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    response = await client.post(
        TOKEN,
        headers=basic(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "authorization_code", "code": code},
    )
    assert response.json() == {"error": "invalid_grant"}
    response = await client.post(
        TOKEN,
        headers=basic(CLIENT_ID, CLIENT_SECRET),
        data={"grant_type": "authorization_code", "code": code, "redirect_uri": "https://localhost/callback"},
    )
    claims = jwt.decode(response.json()["id_token"], options={"verify_signature": False})
    assert claims["nonce"] == "test-nonce"
    assert abs(claims["iat"] - client.hub.clock.timestamp()) < 2


async def test_unknown_delegated_identity_is_denied(qb: Any) -> None:
    client, _ = qb
    ctx = client.hub.contexts["quickbooks"]
    grant = ctx.tokens.issue_access(
        client_id="test", user_id="missing", scopes=["com.intuit.quickbooks.accounting"]
    )
    assert (
        await client.get(f"{API}/customer/1", headers={"Authorization": f"Bearer {grant.token}"})
    ).status_code == 401


async def test_batch_invalid_items_and_query_only_read_permission(qb: Any) -> None:
    client, headers = qb
    items = [
        None,
        1,
        {"bId": "bad-shape", "Invoice": []},
        {"bId": "bad-op", "operation": "nonsense", "Customer": {"DisplayName": "Never created"}},
        {"bId": "bad-query", "Query": []},
        {"bId": "valid", "Query": "SELECT COUNT(*) FROM Invoice"},
    ]
    response = await client.post(f"{API}/batch", headers=headers, json={"BatchItemRequest": items})
    assert response.status_code == 200, response.text
    results = response.json()["BatchItemResponse"]
    assert all(item["Fault"]["Error"][0]["code"] != "10000" for item in results[:-1])
    assert results[-1]["QueryResponse"]["totalCount"] == 115
    readonly = await mint_user(client, AUDITOR)
    response = await client.post(
        f"{API}/batch",
        headers=readonly,
        json={
            "BatchItemRequest": [
                {"bId": "q", "Query": "SELECT COUNT(*) FROM Invoice"},
                {"bId": "w", "Customer": {"DisplayName": "Denied"}},
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["BatchItemResponse"][0]["QueryResponse"]["totalCount"] == 115
    assert response.json()["BatchItemResponse"][1]["Fault"]["Error"][0]["code"] == "110"


async def test_webhooks_filter_each_entity_and_retry(qb: Any) -> None:
    client, headers = qb
    received: list[tuple[bytes, str]] = []
    app = FastAPI()

    @app.post("/hook")
    async def hook(request: Request) -> Response:
        received.append((await request.body(), request.headers["intuit-signature"]))
        return JSONResponse({}, status_code=503 if len(received) == 1 else 200)

    client.hub.webhook_transport = httpx.ASGITransport(app=app)
    await client.post(
        f"{HOST}/_emulator/actions/configure_webhook",
        json={
            "url": "https://consumer.example/hook",
            "verifier_token": "test-verifier",
            "entities": ["Customer"],
        },
    )
    response = await client.post(
        f"{API}/batch",
        headers=headers,
        json={
            "BatchItemRequest": [
                {"bId": "c", "Customer": {"DisplayName": "Hook filter"}},
                {"bId": "v", "Vendor": {"DisplayName": "Vendor hook filter"}},
            ]
        },
    )
    assert response.status_code == 200
    events = json.loads(received[0][0])["eventNotifications"][0]["dataChangeEvent"]["entities"]
    assert [e["name"] for e in events] == ["Customer"]
    assert len(received) == 2 and received[0] == received[1]
    journal = (await client.get(f"{HOST}/_emulator/webhooks")).json()["deliveries"]
    assert journal[-1]["attempts"] == 2 and journal[-1]["status"] == "delivered"


async def test_cdc_rejects_invalid_entity_lists_and_future_dates(qb: Any) -> None:
    client, headers = qb
    since = (client.hub.clock.now() - timedelta(days=1)).isoformat()
    for entities in ("", "DoesNotExist", "TaxCode", "TimeActivity"):
        fault(
            await client.get(
                f"{API}/cdc", headers=headers, params={"entities": entities, "changedSince": since}
            )
        )
    fault(
        await client.get(
            f"{API}/cdc",
            headers=headers,
            params={
                "entities": "Invoice",
                "changedSince": (client.hub.clock.now() + timedelta(days=1)).isoformat(),
            },
        )
    )


async def test_preferences_concurrency_and_hierarchy_validation(qb: Any) -> None:
    client, headers = qb
    before = (await client.get(f"{API}/preferences", headers=headers)).json()["Preferences"]
    fault(
        await client.post(
            f"{API}/preferences", headers=headers, json={"Id": "1", "SyncToken": "999", "sparse": True}
        ),
        "5010",
    )
    assert (await client.get(f"{API}/preferences", headers=headers)).json()["Preferences"] == before
    fault(
        await client.post(
            f"{API}/customer",
            headers=headers,
            json={"Id": "1", "SyncToken": "0", "sparse": True, "ParentRef": {"value": "25"}},
        ),
        "6000",
    )
    fault(
        await client.post(f"{API}/vendor", headers=headers, json={"DisplayName": "Northwind Logistics"}),
        "6240",
    )
    response = await client.post(
        f"{API}/customer",
        headers=headers,
        json={"Id": "25", "SyncToken": "0", "sparse": True, "DisplayName": "Renamed engagement"},
    )
    assert response.json()["Customer"]["FullyQualifiedName"] == "Northwind Logistics:Renamed engagement"


async def test_report_invalid_dates_and_operating_income(qb: Any) -> None:
    client, headers = qb
    for params in (
        {"start_date": "bad"},
        {"start_date": "2026-12-01", "end_date": "2026-01-01"},
        {"start_date": "0001-01-01", "end_date": "9999-12-31"},
    ):
        fault(await client.get(f"{API}/reports/ProfitAndLoss", headers=headers, params=params))
    report = (
        await client.get(f"{API}/reports/ProfitAndLoss", headers=headers, params={"date_macro": "All dates"})
    ).json()
    rows = {r["group"]: r for r in report["Rows"]["Row"]}
    income = float(rows["Income"]["Summary"]["ColData"][1]["value"])
    expenses = float(rows["Expense"]["Summary"]["ColData"][1]["value"])
    assert float(rows["NetOperatingIncome"]["ColData"][1]["value"]) == round(income - expenses, 2)


@pytest.mark.parametrize("operation", ["delete", "void"])
async def test_removing_paid_invoice_releases_customer_credit(qb: Any, operation: str) -> None:
    client, headers = qb
    invoice = await create_invoice(client, headers)
    response = await client.post(
        f"{API}/payment",
        headers=headers,
        json={
            "CustomerRef": {"value": "1"},
            "TotalAmt": 100,
            "Line": [{"Amount": 100, "LinkedTxn": [{"TxnId": invoice["Id"], "TxnType": "Invoice"}]}],
        },
    )
    payment_id = response.json()["Payment"]["Id"]
    invoice = (await client.get(f"{API}/invoice/{invoice['Id']}", headers=headers)).json()["Invoice"]
    response = await client.post(
        f"{API}/invoice",
        headers=headers,
        params={"operation": operation},
        json={"Id": invoice["Id"], "SyncToken": invoice["SyncToken"]},
    )
    assert response.status_code == 200, response.text
    payment = (await client.get(f"{API}/payment/{payment_id}", headers=headers)).json()["Payment"]
    assert payment["UnappliedAmt"] == 100 and payment["Line"] == []


async def test_nested_hierarchy_rollups_and_parent_rename(qb: Any) -> None:
    client, headers = qb
    response = await client.post(
        f"{API}/customer", headers=headers, json={"DisplayName": "Nested job", "ParentRef": {"value": "25"}}
    )
    customer = response.json()["Customer"]
    assert customer["Level"] == 2 and customer["Job"] is True
    before = (await client.get(f"{API}/customer/1", headers=headers)).json()["Customer"]["BalanceWithJobs"]
    await create_invoice(client, headers, CustomerRef={"value": customer["Id"]})
    after = (await client.get(f"{API}/customer/1", headers=headers)).json()["Customer"]
    assert after["BalanceWithJobs"] == before + 100
    response = await client.post(
        f"{API}/customer",
        headers=headers,
        json={"Id": "1", "SyncToken": after["SyncToken"], "sparse": True, "DisplayName": "Renamed parent"},
    )
    assert response.status_code == 200
    nested = (await client.get(f"{API}/customer/{customer['Id']}", headers=headers)).json()["Customer"]
    assert nested["FullyQualifiedName"].startswith("Renamed parent:")
    fault(
        await client.post(
            f"{API}/customer",
            headers=headers,
            params={"operation": "delete"},
            json={"Id": "1", "SyncToken": response.json()["Customer"]["SyncToken"]},
        ),
        "500",
    )


async def test_upload_requires_structured_multipart_metadata(qb: Any) -> None:
    client, headers = qb
    for body in (b"", b"not multipart", b"{}", b"null"):
        fault(await client.post(f"{API}/upload", headers=headers, content=body), "2010")
    response = await client.post(
        f"{API}/upload",
        headers=headers,
        files={
            "file_metadata_0": (
                "metadata.json",
                json.dumps({"FileName": "test.txt", "ContentType": "text/plain"}),
                "application/json",
            ),
            "file_content_0": ("test.txt", b"contents {are not JSON}", "text/plain"),
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["AttachableResponse"][0]["Attachable"]["FileName"] == "test.txt"


async def test_fault_headers_and_injected_throttle(qb: Any) -> None:
    client, headers = qb
    await client.post(
        f"{HOST}/_emulator/faults", json={"path": "/v3/company/.*/query", "status": 429, "times": 1}
    )
    response = await client.get(f"{API}/query", headers=headers, params={"query": "SELECT * FROM Customer"})
    assert response.status_code == 429
    assert response.json()["Fault"]["Error"][0]["code"] == "3001"
    assert response.headers["intuit_tid"] and response.headers["retry-after"]
    response = await client.get(f"{API}/customer/1", headers={"Authorization": "Bearer invalid"})
    assert response.headers["www-authenticate"] == 'Bearer realm="Intuit"'
    response = await client.get(f"{API}/no/such/path", headers=headers)
    assert response.status_code == 404 and response.headers["intuit_tid"] and response.json()["time"]


def test_timestamp_uses_pacific_daylight_and_standard_offsets() -> None:
    assert qbo_time(datetime(2026, 1, 15, 12, tzinfo=UTC)) == "2026-01-15T04:00:00.000-08:00"
    assert qbo_time(datetime(2026, 7, 15, 12, tzinfo=UTC)) == "2026-07-15T05:00:00.000-07:00"


async def test_cash_reports_follow_links_tax_and_original_accounts(qb: Any) -> None:
    client, headers = qb
    invoice = await create_invoice(client, headers, TxnDate="2026-01-01")
    await client.post(
        f"{API}/payment",
        headers=headers,
        json={
            "CustomerRef": {"value": "1"},
            "TotalAmt": 50,
            "TxnDate": "2026-02-01",
            "Line": [{"Amount": 50, "LinkedTxn": [{"TxnId": invoice["Id"], "TxnType": "Invoice"}]}],
        },
    )
    params = {
        "start_date": "2026-02-01",
        "end_date": "2026-02-01",
        "accounting_method": "Cash",
        "customer": "1",
    }
    report = (await client.get(f"{API}/reports/ProfitAndLoss", headers=headers, params=params)).json()
    income = next(r for r in report["Rows"]["Row"] if r.get("group") == "Income")
    assert float(income["Summary"]["ColData"][1]["value"]) == 50
    item = (await client.get(f"{API}/item/1", headers=headers)).json()["Item"]
    assert income["Rows"]["Row"][0]["ColData"][0]["id"] == item["IncomeAccountRef"]["value"]
    # A paid bill's expense moves to the payment date on cash reports.
    bill = (
        await client.post(
            f"{API}/bill",
            headers=headers,
            json={
                "VendorRef": {"value": "1"},
                "TxnDate": "2026-01-01",
                "Line": [
                    {
                        "Amount": 80,
                        "DetailType": "AccountBasedExpenseLineDetail",
                        "AccountBasedExpenseLineDetail": {"AccountRef": {"value": "30"}},
                    }
                ],
            },
        )
    ).json()["Bill"]
    response = await client.post(
        f"{API}/billpayment",
        headers=headers,
        json={
            "VendorRef": {"value": "1"},
            "TotalAmt": 40,
            "TxnDate": "2026-02-01",
            "PayType": "Check",
            "CheckPayment": {"BankAccountRef": {"value": "1"}},
            "Line": [{"Amount": 40, "LinkedTxn": [{"TxnId": bill["Id"], "TxnType": "Bill"}]}],
        },
    )
    assert response.status_code == 200, response.text
    params.pop("customer")
    params["vendor"] = "1"
    report = (await client.get(f"{API}/reports/ProfitAndLoss", headers=headers, params=params)).json()
    expenses = next(r for r in report["Rows"]["Row"] if r.get("group") == "Expense")
    assert float(expenses["Summary"]["ColData"][1]["value"]) == 40


async def test_reports_apply_department_and_historical_aging_filters(qb: Any) -> None:
    client, headers = qb
    customer = (
        await client.post(
            f"{API}/customer", headers=headers, json={"DisplayName": "Historical report customer"}
        )
    ).json()["Customer"]
    invoice = await create_invoice(
        client,
        headers,
        CustomerRef={"value": customer["Id"]},
        TxnDate="2026-01-01",
        DepartmentRef={"value": "1"},
    )
    await create_invoice(
        client,
        headers,
        CustomerRef={"value": customer["Id"]},
        TxnDate="2026-03-01",
        DepartmentRef={"value": "2"},
    )
    await client.post(
        f"{API}/payment",
        headers=headers,
        json={
            "CustomerRef": {"value": customer["Id"]},
            "TotalAmt": 50,
            "TxnDate": "2026-02-01",
            "Line": [{"Amount": 50, "LinkedTxn": [{"TxnId": invoice["Id"], "TxnType": "Invoice"}]}],
        },
    )
    for report_name in ("AgedReceivables", "AgedReceivableDetail"):
        report = (
            await client.get(
                f"{API}/reports/{report_name}",
                headers=headers,
                params={"report_date": "2026-01-15", "customer": customer["Id"]},
            )
        ).json()
        assert float(report["Rows"]["Row"][-1]["ColData"][-1]["value"]) == 100
        if report_name.endswith("Detail"):
            assert len(report["Rows"]["Row"]) == 1
    report = (
        await client.get(
            f"{API}/reports/ProfitAndLoss",
            headers=headers,
            params={"date_macro": "All dates", "customer": customer["Id"], "department": "1"},
        )
    ).json()
    income = next(r for r in report["Rows"]["Row"] if r.get("group") == "Income")
    assert float(income["Summary"]["ColData"][1]["value"]) == 100
    response = await client.get(
        f"{API}/reports/GeneralLedger",
        headers=headers,
        params={"start_date": "0001-01-01", "end_date": "0001-01-02", "account": "1"},
    )
    assert response.status_code == 200


async def test_seed_metadata_and_preferences_references_are_consistent(qb: Any) -> None:
    client, headers = qb
    ctx = client.hub.contexts["quickbooks"]
    for entity in ENTITIES:
        for record in ctx.store.collection(entity):
            for timestamp in record.get("MetaData", {}).values():
                instant = parse_time(timestamp)
                assert instant is not None and instant <= ctx.world.anchor
    prefs = (await client.get(f"{API}/preferences", headers=headers)).json()["Preferences"]
    term_ref = prefs["SalesFormsPrefs"]["DefaultTerms"]
    term = (await client.get(f"{API}/term/{term_ref['value']}", headers=headers)).json()["Term"]
    assert term["DueDays"] == 30 and term["Name"] == term_ref["name"]
