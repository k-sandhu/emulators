"""Official ``python-quickbooks`` + ``intuit-oauth`` SDKs against the emulator.

Run with: ``EMULATORS_PROVIDERS=quickbooks uv run pytest tests/sdk/test_quickbooks_sdk.py -q``
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
import requests
from intuitlib.client import AuthClient
from intuitlib.enums import Scopes
from quickbooks import exceptions
from quickbooks.batch import batch_create
from quickbooks.client import QuickBooks
from quickbooks.objects.base import LinkedTxn, Ref
from quickbooks.objects.company_info import CompanyInfo
from quickbooks.objects.customer import Customer
from quickbooks.objects.detailline import SalesItemLine, SalesItemLineDetail
from quickbooks.objects.invoice import Invoice
from quickbooks.objects.item import Item
from quickbooks.objects.payment import Payment, PaymentLine

pytestmark = pytest.mark.sdk

REALM = "9341569385067351"
REDIRECT = "https://localhost/callback"
ADMIN_HINT = "victoria.hale@halcyonadvisory.example"


def make_auth_client() -> AuthClient:
    info = requests.get("https://sandbox-quickbooks.api.intuit.com/_emulator/info", timeout=30).json()
    credentials = info["credentials"]
    auth_client = AuthClient(credentials["client_id"], credentials["client_secret"], REDIRECT, "sandbox")
    url = auth_client.get_authorization_url(
        [Scopes.ACCOUNTING, Scopes.OPENID, Scopes.PROFILE, Scopes.EMAIL, Scopes.PHONE, Scopes.ADDRESS]
    )
    response = requests.get(f"{url}&login_hint={ADMIN_HINT}", allow_redirects=False, timeout=30)
    assert response.status_code == 302, response.text
    params = parse_qs(urlparse(response.headers["Location"]).query)
    assert params["realmId"] == [REALM]
    auth_client.get_bearer_token(params["code"][0], realm_id=REALM)
    return auth_client


def make_qb(auth_client: AuthClient) -> QuickBooks:
    return QuickBooks(
        auth_client=auth_client,
        refresh_token=auth_client.refresh_token,
        company_id=REALM,
        minorversion=75,
    )


@pytest.fixture
def qb(emulator_intercept) -> QuickBooks:
    return make_qb(make_auth_client())


def test_intuit_oauth_discovery_token_userinfo_refresh(emulator_intercept) -> None:
    auth_client = make_auth_client()
    assert auth_client.access_token.startswith("eyJlbmMiOiJBMTI4Q0JDLUhTMjU2Ii")
    assert auth_client.refresh_token.startswith("AB11")
    assert auth_client.id_token  # validated against our JWKS by intuitlib
    user_info = auth_client.get_user_info().json()
    assert user_info["email"] == ADMIN_HINT
    assert user_info["givenName"] == "Victoria"
    old_refresh = auth_client.refresh_token
    auth_client.refresh()
    assert auth_client.refresh_token != old_refresh
    assert auth_client.access_token


def test_companyinfo_customers_query_and_count(qb: QuickBooks) -> None:
    info = CompanyInfo.get(REALM, qb=qb)
    assert info.CompanyName == "Halcyon Advisory Group"
    customers = Customer.all(qb=qb)
    assert len(customers) >= 24
    northwind = Customer.filter(DisplayName="Northwind Logistics", qb=qb)
    assert len(northwind) == 1
    assert northwind[0].PrimaryEmailAddr.Address.endswith("@northwindlogistics.example")
    jobs = [c for c in customers if c.Job]
    assert jobs and ":" in jobs[0].FullyQualifiedName
    assert Customer.count(qb=qb) == len(Customer.all(qb=qb, max_results=1000))
    invoices = Invoice.filter(Balance="0", qb=qb)  # exercises WHERE translation loosely
    assert isinstance(invoices, list)


def test_invoice_lifecycle_create_pay_pdf(qb: QuickBooks) -> None:
    customer = Customer()
    customer.DisplayName = "SDK Roundtrip Co"
    customer.CompanyName = "SDK Roundtrip Co"
    saved_customer = customer.save(qb=qb)
    assert saved_customer.Id
    assert int(saved_customer.Id) > 0

    items = Item.all(qb=qb)
    assert items and items[0].Sku  # SELECT *, Sku FROM Item works

    invoice = Invoice()
    invoice.CustomerRef = saved_customer.to_ref()
    line = SalesItemLine()
    line.DetailType = "SalesItemLineDetail"
    line.Amount = 750
    line.Description = "SDK work"
    detail = SalesItemLineDetail()
    item_ref = Ref()
    item_ref.value = items[0].Id
    item_ref.name = items[0].Name
    detail.ItemRef = item_ref
    detail.Qty = 3
    detail.UnitPrice = 250
    line.SalesItemLineDetail = detail
    invoice.Line = [line]
    saved_invoice = invoice.save(qb=qb)
    assert float(saved_invoice.TotalAmt) == 750
    assert float(saved_invoice.Balance) == 750
    assert saved_invoice.DocNumber

    fetched = Invoice.get(saved_invoice.Id, qb=qb)
    assert fetched.CustomerRef.value == str(saved_customer.Id)
    assert fetched.Line[0].SalesItemLineDetail.UnitPrice == 250

    payment = Payment()
    payment.CustomerRef = saved_customer.to_ref()
    payment.TotalAmt = 750
    pay_line = PaymentLine()
    pay_line.Amount = 750
    linked = LinkedTxn()
    linked.TxnId = saved_invoice.Id
    linked.TxnType = "Invoice"
    pay_line.LinkedTxn = [linked]
    payment.Line = [pay_line]
    saved_payment = payment.save(qb=qb)
    assert saved_payment.Id
    settled = Invoice.get(saved_invoice.Id, qb=qb)
    assert float(settled.Balance) == 0

    pdf = settled.download_pdf(qb=qb)
    assert pdf[:5] == b"%PDF-"
    saved_payment.delete(qb=qb)
    reopened = Invoice.get(saved_invoice.Id, qb=qb)
    assert float(reopened.Balance) == 750
    assert not reopened.LinkedTxn


def test_sdk_query_escaping_and_numeric_order(qb: QuickBooks) -> None:
    customer = Customer()
    customer.DisplayName = "SDK Adam's Candy Shop"
    customer.save(qb=qb)
    found = Customer.filter(DisplayName="SDK Adam's Candy Shop", qb=qb)
    assert len(found) == 1
    invoices = Invoice.all(qb=qb, order_by="TotalAmt ASC", max_results=1000)
    assert [i.TotalAmt for i in invoices] == sorted(i.TotalAmt for i in invoices)


def test_batch_create_via_sdk(qb: QuickBooks) -> None:
    a = Customer()
    a.DisplayName = "SDK Batch A"
    b = Customer()
    b.DisplayName = "SDK Batch B"
    result = batch_create([a, b], qb=qb)
    assert len(result.successes) == 2
    assert not result.faults
    assert {c.DisplayName for c in result.successes} == {"SDK Batch A", "SDK Batch B"}


def test_reports_and_cdc_via_client(qb: QuickBooks) -> None:
    report = qb.get_report("ProfitAndLoss", {"date_macro": "All dates"})
    assert report["Header"]["ReportName"] == "ProfitAndLoss"
    assert report["Rows"]["Row"]
    balance_sheet = qb.get_report("BalanceSheet", {})
    rows = balance_sheet["Rows"]["Row"]
    assets = next(r for r in rows if r.get("group") == "Asset")
    total_assets = float(assets["Summary"]["ColData"][1]["value"])
    total_le = next(
        r["ColData"][1]["value"]
        for r in rows
        if r.get("ColData", [{}])[0].get("value") == "TOTAL LIABILITIES AND EQUITY"
    )
    assert total_assets == float(total_le)

    cdc = qb.change_data_capture("Customer,Invoice", "2026-10-15T00:00:00-07:00")
    assert "CDCResponse" in cdc
    user = qb.get_current_user()
    assert user["emailAddress"] == ADMIN_HINT


def test_sdk_exceptions_match_fault_codes(qb: QuickBooks) -> None:
    with pytest.raises(exceptions.ObjectNotFoundException):
        Customer.get(999999, qb=qb)
    duplicate = Customer()
    duplicate.DisplayName = "Northwind Logistics"
    with pytest.raises(exceptions.QuickbooksException):
        duplicate.save(qb=qb)
