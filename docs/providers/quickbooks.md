# QuickBooks Online (Intuit) emulator

High-fidelity emulation of the QuickBooks Online Accounting API (`/v3`), Intuit's
OAuth 2.0 / OpenID Connect endpoints, signed webhooks and PDF generation, seeded
from the shared Halcyon Advisory Group world. Balances and reports are computed
from a double-entry ledger over the stored transactions, so invoices, payments,
bills, journals, voids and deletes all move the books.

Provider name `quickbooks`, default port **44111**.

## Hosts

| Host | Used for |
| --- | --- |
| `quickbooks.api.intuit.com`, `sandbox-quickbooks.api.intuit.com` | `/v3/company/{realmId}/...` Accounting API |
| `appcenter.intuit.com` | `GET /connect/oauth2` authorize, `GET /api/v1/user/current` |
| `oauth.platform.intuit.com` | token + revoke endpoints, `GET /op/v1/jwks` |
| `developer.api.intuit.com`, `developer.intuit.com` | `GET /.well-known/openid_sandbox_configuration`, `/.well-known/openid_configuration` |
| `accounts.platform.intuit.com`, `sandbox-accounts.platform.intuit.com`, `accounts.intuit.com` | `GET /v1/openid_connect/userinfo` |

All hosts route to this one provider; prefix mode (`/quickbooks/...` on the
gateway) works for every endpoint as well.

## Auth

### Test credentials

| Field | Value |
| --- | --- |
| Realm / company id | `9341569385067351` |
| Client id and secret | Read `credentials.client_id` and `credentials.client_secret` from `GET /_emulator/info` on a provider host (or `/quickbooks/_emulator/info` on the gateway) |
| Redirect URI | anything (lenient); `https://localhost/callback` is pre-registered |
| Scopes | `com.intuit.quickbooks.accounting openid profile email phone address` |
| Company admin (full read/write) | `victoria.hale@halcyonadvisory.example` (CFO) — any partner, Finance & Operations or IT admin employee also has full access |
| Read-only user | e.g. `sofia.alvarez@halcyonadvisory.example` (Audit): GET/query/reports work, writes fail with 403, fault code 110 |
| Inactive user | `paul.henderson@halcyonadvisory.example` (departed): tokens resolve as 401 |

Clients are lenient by default — any `client_id`/`client_secret` pair works
unless the hub opts into strict clients.

### Flows

1. `GET https://appcenter.intuit.com/connect/oauth2?client_id=...&response_type=code&scope=...&redirect_uri=...&state=...`
   Add `login_hint=<employee email>` to auto-approve; without it an account
   picker page is rendered. Redirects back with `code`, `state` and `realmId`.
2. `POST https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer` with Basic
   client auth and form body. `grant_type=authorization_code` and
   `grant_type=refresh_token` return:
   `token_type: "bearer"`, an opaque access token with five JWE-shaped segments,
   `expires_in: 3600`, an opaque refresh token, and `x_refresh_token_expires_in: 8726400`,
   plus a signed RS256 `id_token` when `openid` was requested (validatable
   against `GET https://oauth.platform.intuit.com/op/v1/jwks`, issuer
   `https://oauth.platform.intuit.com/op/v1`). Errors are `{"error": "invalid_grant"}`.
   Refresh tokens **rotate**; the previous token keeps working until its
   successor is used once, with a maximum 24-hour grace period. Refresh and revoke
   require the client that owns the token, including with lenient client registration.
3. `POST /oauth2/v1/tokens/revoke` (also `/v2/oauth2/tokens/revoke` on the
   developer host) revokes an access or refresh token; revoking a refresh token
   disconnects that user for the owning app, preserving other apps' grants.
4. `GET /v1/openid_connect/userinfo` (production and sandbox hosts) returns
   `{sub, email, emailVerified, givenName, familyName, phoneNumber, address}`.
   OpenID is required and individual profile fields follow the granted scopes.

Shortcut: `POST /_emulator/tokens` with `{"user": "<pNNN|email>"}` mints a
QBO-shaped access+refresh pair (and returns `realm_id`); `{"app_only": true}`
mints a company-level full-access token. This shortcut is an emulator facility;
Intuit does not offer an Accounting API client-credentials flow. Explicit empty
scopes stay empty; all Accounting API routes require `com.intuit.quickbooks.accounting`.
Unknown delegated identities are rejected and `expires_in` applies to delegated
and app tokens. API calls need
`Authorization: Bearer <access_token>`. Missing/invalid/expired tokens give the
real 401 shape `{"fault": {"error": [{"message": "message=AuthenticationFailed;
errorCode=003200; statusCode=401", "code": "3200", ...}], "type":
"AUTHENTICATION"}}` with `WWW-Authenticate`. A token issued for this realm used
against a different realm in the path gives 401; an unknown realm gives 403.

## API conventions

- Base path `/v3/company/{realmId}/...`. JSON when `Accept: application/json`;
  omitting a JSON Accept header (including `Accept: */*`) gets
  `<IntuitResponse xmlns="http://schema.intuit.com/finance/v3" time="...">`
  XML for reads/queries/CRUD responses. Entity attributes (`domain`, `sparse`),
  reference text/attributes and query pagination attributes use QBO's XML shape.
- Every JSON response wraps the entity: `{"Invoice": {...}, "time":
  "2026-10-16T07:12:45.123-07:00"}` and carries an `intuit_tid` header.
- `minorversion` query param accepted for 1–75 (other values → 400).
- Faults: `{"Fault": {"Error": [{"Message", "Detail", "code", "element"}],
  "type": "ValidationFault"}, "time"}`. Codes used: 610 Object Not Found,
  5010 Stale Object Error, 6240 Duplicate Name Exists Error, 2010 invalid properties/JSON, 2020 required
  field, 6000 business validation, 4000 query parse, 110 authorization,
  3001 ThrottleExceeded (429 via fault injection).
- Entities carry numeric-string `Id` (per-entity sequence), `SyncToken`
  (increments on every update; stale token → 5010), `MetaData {CreateTime,
  LastUpdatedTime}` in Pacific offset (`-07:00` summer, `-08:00` winter), `domain: "QBO"`, `sparse: false`;
  refs are `{"value": "12", "name": "..."}`.
- Malformed JSON, invalid nested types, non-finite/oversized numbers and internal
  properties return populated QBO faults. Failed updates leave records unchanged.

## Endpoints

All under `https://sandbox-quickbooks.api.intuit.com/v3/company/9341569385067351`
(production host identical).

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/companyinfo/{realmId}` | CompanyInfo incl. `NameValue` (NeoEnabled, IndustryType, CompanyType, SubscriptionStatus, OfferingSku "QuickBooks Online Advanced") |
| GET / POST | `/preferences` | Preferences singleton (class/department tracking on, account numbers on) |
| POST | `/{entity}` | Create (no `Id`), full update (`Id` + `SyncToken`), or sparse update (`"sparse": true`). `?operation=delete`, `?operation=void`, and SDK-style `?operation=update&include=void` supported. Entities: account, customer, vendor, employee, item, class, department, term, paymentmethod, taxagency, taxcode, taxrate, invoice, payment, salesreceipt, estimate, creditmemo, bill, billpayment, purchase, journalentry, deposit, timeactivity, attachable |
| GET | `/{entity}/{id}` | Read one. `?include=invoiceLink` adds `InvoiceLink` on invoices |
| POST | `/invoice/{id}/send?sendTo=` | Sets `EmailStatus: EmailSent`, `DeliveryInfo` (also estimate/salesreceipt/creditmemo) |
| GET | `/invoice/{id}/pdf`, `/estimate/{id}/pdf`, `/salesreceipt/{id}/pdf`, `/creditmemo/{id}/pdf` | Real PDF bytes, lazily rendered and cached |
| GET / POST | `/query` | QBO SQL below. POST body is the raw query (`application/text`), GET uses `?query=` |
| POST | `/batch` | ≤30 `BatchItemRequest` items; create/update/delete/void/query; per-item `Fault` on failure. Read-only users may query, with writes denied per item |
| GET | `/cdc?entities=Invoice,Customer&changedSince=<ISO>` | Changes in the last 30 days; deleted entities as `{"Id", "status": "Deleted", "MetaData"}` |
| GET | `/reports/{Name}` | ProfitAndLoss, BalanceSheet, TrialBalance, AgedReceivables, AgedReceivableDetail, AgedPayables, AgedPayableDetail, CustomerIncome, GeneralLedger, TransactionList |
| POST | `/upload` | Requires multipart/form-data with a JSON metadata part; creates an Attachable from that part |

Delete/void semantics: transactions delete (`{"{Entity}": {"Id", "domain": "QBO", "status":
"Deleted"}}`); name-list entities are deactivated through updates (`Active: false`)
and reject `operation=delete`. Delete/void require the current SyncToken.
Void is supported for Invoice, Payment, SalesReceipt and BillPayment; it zeroes
amounts and sales quantities, sets `PrivateNote: "Voided …"` and removes the ledger
postings. Creating a Payment/BillPayment updates the linked Invoice/Bill
`Balance` and `LinkedTxn`; reversing the payment reopens its target and clears the link.
Deleting/voiding a paid invoice releases the payment as unapplied customer credit.
Payments require TotalAmt, enforce linked-party consistency, and cannot apply more
than their total. Unapplied payments reduce customer balances. Customer/account
balances roll up through nested ancestors; renamed parents update descendant names.
Invoices derive default due dates from TxnDate and terms (including due on receipt).
Replacing lines recalculates tax; sales lines without `ItemRef` are documentation
whose amounts and tax do not contribute to totals. PDF caches invalidate when transactions change.
Invoice deposits debit `DepositToAccountRef` (default Undeposited Funds) and reduce
receivables alongside invoice/customer balances. Edits and voids reverse these postings;
cash-basis P&L recognises the deposited share on the invoice date.

### Query language

`SELECT * | COUNT(*) FROM Entity [WHERE cond [AND cond]...]
[ORDERBY Field [ASC|DESC], ...] [STARTPOSITION n] [MAXRESULTS m]` — keywords
case-insensitive; operators `= < > <= >= !=`, `IN ('a','b')`, `LIKE '%x%'`
(`%` wildcards only); dotted fields (`MetaData.LastUpdatedTime > '…'`,
`CustomerRef = '12'` compares the ref value); `Active IN (true, false)`.
Backslashes escape apostrophes/backslashes in string literals. Numeric amounts sort
numerically. Pagination requires positive integer values and returns an empty object
after the last record. Unknown properties return query validation code 4001.
Defaults: `STARTPOSITION 1`, `MAXRESULTS 100` (max 1000); name-list entities
default to `Active = true` unless `Active` is mentioned. `OR` and any parse
error → 400 fault 4000 "Error parsing query". Empty results return
`{"QueryResponse": {}}`; counts return `{"QueryResponse": {"totalCount": n}}`.
Field projections are rejected, as in Intuit's query language. The SDK's
`SELECT *, Sku FROM Item` extension is accepted.

### Reports

Params: `start_date`, `end_date`, `date_macro` (`This Fiscal Year-to-date`,
`Last Month`, `This Month`, `Last Fiscal Year`, `All dates`, …),
`accounting_method=Accrual|Cash`, `summarize_column_by=Total|Month|Quarter|
Customer|Class|Vendor`, plus `customer`/`vendor`/`class` filters and
`department` filters on P&L/ledger reports, and `account` for GeneralLedger.
Filter lists may contain comma-separated ids. Aging reports honor customer/vendor
filters and reconstruct balances at `report_date` from dated payment allocations.
Output is the real QBO structure
(`Header`/`Columns`/`Rows` with `Section`/`Data` rows, `Summary` totals and
`group` markers such as `Income`, `NetIncome`). P&L income ties exactly to
invoice subtotals (linked receipts under Cash), expenses to bills/purchases/
journals; the Balance Sheet balances because opening Retained Earnings is the
plug that balances the world's opening trial balance; AR/AP tie to open
invoice/bill balances.
Cash P&L retimes linked invoice/bill allocations to payment dates, prorating tax
and preserving income/expense accounts. Net Operating Income excludes other expenses;
Net Income includes them. Invalid dates, reversed periods and spans over 100 years fail.

## Seeded data

Counts: Account 42, Customer 54 (24 clients + 30 engagement jobs), Vendor 14,
Employee 47 (45 active, 2 departed), Item 10, Class 9, Department 4, Term 7,
PaymentMethod 5, TaxCode/TaxRate 5 each, TaxAgency 2, Invoice 115, Payment 95,
Bill 152, BillPayment 148, Estimate 17, CreditMemo 2, JournalEntry 24,
Purchase 24, TimeActivity 544.

Examples a test writer can hard-code (stable across runs):

| Entity | Id | Example |
| --- | --- | --- |
| Customer | `1` | Northwind Logistics (primary contact `christopher.ingram@northwindlogistics.example`); `2` Bluefin Seafood, `3` Cedar Ridge Dental Group |
| Customer (job) | `25` | `Northwind Logistics:FY2025 Review` (`Job: true`, `Level: 1`); jobs are Ids 25–54 |
| Customer (prospects) | — | Polaris Clean Energy, Willowbrook Senior Living (no invoices, each has a Pending Estimate) |
| Customer (former) | — | Quarry Lane Manufacturing, `Active: false` |
| Account | `1`–`4` | `1000` Operating Account – Northbridge Bank, `1010` Payroll, `1020` Savings, `1100` Accounts Receivable (A/R); full 41-account world chart plus `1150` Undeposited Funds (Id 42) |
| Item | `1`–`10` | Audit & Assurance Services, Corporate Tax Preparation, Financial Due Diligence, Bookkeeping & Close Support, Reimbursable Expenses, … |
| Invoice | `2` | `INV-1042`, Balance 1380.00 open; invoices are Ids 1–115 with DocNumbers `INV-…`, statuses: 90 paid, 10 open, 8 overdue, 5 partial, 1 draft, 1 voided (zero amounts, `PrivateNote: "Voided"`) |
| Vendor | `1` | Brightline Office Supply (`billing@brightlineofficesupply.example`, masked TaxIdentifier) |
| Employee | `1` | Eleanor Whitfield, Managing Partner; Employee Ids follow world order p001→1 |
| Term | `1`–`7` | Due on receipt, Net 5/10/15/20/30/45 |
| TaxCode | `1`–`5` | HST ON 13%, HST ATL 15%, GST/QST 14.975%, GST 5%, Zero-rated (US/GB customers invoice at 0%) |
| Class | `1`–`9` | Audit & Assurance, Tax, Advisory, Operations, plus `Advisory:Transaction Advisory` etc. |
| Department | `1`–`4` | Toronto, New York, Chicago, London (locations) |

Journals: 12 months (Oct 2025–Sep 2026) of month-end accruals — payroll
(scaled to world revenue), depreciation (fixed assets over 10 years) and term
loan interest — each followed by the payroll payment journal, so Accrued
Liabilities nets to zero. TimeActivities are weekly per in-progress engagement
team member for the last 12 weeks (`HasBeenBilled` older than 4 weeks, else
`Billable`). Estimates come from CRM deals (Pending for open stages, Rejected
for Closed Lost, Accepted for the first Closed Won deals).

## Webhooks

`POST /_emulator/actions/configure_webhook` with
`{"url": "...", "verifier_token": "...", "entities": ["Invoice", ...]?}`.
On every entity create/update/delete/void the emulator POSTs
`{"eventNotifications": [{"realmId": "9341569385067351", "dataChangeEvent":
{"entities": [{"name": "Invoice", "id": "145", "operation": "Create",
"lastUpdated": "2026-10-16T14:21:22.000Z"}]}}]}` with header
`intuit-signature: base64(HMAC-SHA256(verifier_token, body))` — the exact
scheme `QuickBooks.validate_webhook_signature` checks. Deliveries are journaled
at `GET /_emulator/webhooks`.
Each configured subscription receives only its selected entities, including mixed
batches. There is no URL validation handshake. Retries preserve the signed body.

## Actions

| Action | Payload | Effect |
| --- | --- | --- |
| `mint_token` | `{"user", "scopes"?, "app_only"?}` | Same as `POST /_emulator/tokens`; QBO-shaped tokens |
| `configure_webhook` | `{"url", "verifier_token", "entities"?}` | Register/replace the webhook endpoint for this realm |
| `simulate_merge` | `{"entity", "id"}` | Emit a `Merge` webhook event without changing data |

## Known gaps

- XML serialisation covers reads, queries and CRUD responses, but reports and
  error bodies are JSON only, and the XML shape is a faithful field-for-field
  projection rather than Intuit's exact per-entity XSD ordering.
- Cash-basis P&L supports linked Invoice/Payment and Bill/BillPayment allocations
  plus immediate sales receipts/purchases. Credit applications, unapplied cash income,
  cash-basis balance sheets and tax-specific reporting adjustments are not complete.
- BalanceSheet/TrialBalance dimension filters, historical edit/deletion reconstruction,
  and credit-memo aging are not complete. NoReportData is not inferred for every report.
- No SalesReceipt/Deposit records are seeded; world payments deposit straight
  into the operating bank account.
- Payroll, payments (QB Payments API), online invoice payment processing,
  recurring transactions, budgets, purchase orders, price levels, inventory
  quantity tracking and multi-currency revaluation are not emulated.
- Attachables store metadata only; file bytes from `/upload` are not retained.
- Transaction line support is sales-item/subtotal/description lines for sales,
  account-based expense lines for bills/purchases, journal lines and deposit lines.
  Group, discount, inventory and item-based expense lines are rejected. Inclusive
  tax and jurisdiction-specific tax-service creation are not complete.
- Query property validation detects unknown fields but does not exhaustively model
  every entity's filterable/sortable annotations. Query text is bounded to 64 KiB.
- CDC rejects unsupported/unknown entity names, future dates and missing entity lists.
  It does not model Intuit's 1000-object response cap or every entity-specific exclusion.
- Payment links currently support Invoice targets and BillPayment links support Bill
  targets; CreditMemo/VendorCredit/JournalEntry applications are not implemented.
- OAuth refresh rotation follows the original emulator brief's predecessor/successor
  model, bounded to 24 hours, rather than reproducing Intuit's exact daily token reuse.
- Webhook retries use the shared dispatcher's three attempts with short test-friendly
  delays rather than Intuit's production retry schedule. Related balance changes are
  visible to CDC but do not emit additional webhook events for every affected record.
- Delegated permissions are a two-role model (full vs read-only) rather than
  QBO's per-feature user permissions; OAuth app tokens are full access.
- The opening retained-earnings plug (see Reports) is an emulator bookkeeping
  choice: check `Retained Earnings` opening via TrialBalance rather than the
  world's `opening_balance` of 0.

## Fidelity audit references and validation

The audit checked [Intuit's query restrictions and escaping](https://help.developer.intuit.com/s/article/SQL-Queries),
[Invoice delete/void contracts](https://developer.intuit.com/docs/api/accounting/invoice),
[Payment required fields](https://developer.intuit.com/app/developer/qbo/docs/api/accounting/all-entities/payment),
and [OAuth authorization](https://developer.intuit.com/app/developer/qbo/docs/develop/authentication-and-authorization/oauth-2.0).
Deposit posting and itemless-line handling follow the
[Invoice reference](https://developer.intuit.com/docs/api/accounting/invoice) and
[SalesItemLineDetail contract](https://static.developer.intuit.com/sdkdocs/qbv3doc/ippdotnetdevkitv3/html/0efbacc4-d46c-1b41-513e-6eca01825ab4.htm).
The original field-projection and cross-client refresh tests were corrected to follow
these contracts. An in-process adversarial probe sent 2,296 malformed-field requests
after fixes with no exceptions or 500 responses; seed construction was about 0.2 seconds.
Regression tests cover malformed bodies and nested fields across every entity, mutation
atomicity, payment reversals, auth isolation, query ordering/escaping, default XML,
webhook filtering, reports and SDK flows.

Latest provider validation: 129 QuickBooks provider tests and 7 QuickBooks SDK tests
passed using `uv run --frozen pytest`; Ruff lint and formatting checks passed on
the QuickBooks paths. The integration coordinator runs the complete repository suite.
