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
| Client id | `AB0Emu1atorQuickBooksC1ientId000001` |
| Client secret | `emu1ator-quickbooks-secret` |
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
   `{"token_type": "bearer", "access_token": "eyJlbmMiOiJBMTI4Q0JDLUhTMjU2IiwiYWxnIjoiZGlyIn0..…",
   "expires_in": 3600, "refresh_token": "AB11…", "x_refresh_token_expires_in": 8726400}`,
   plus a signed RS256 `id_token` when `openid` was requested (validatable
   against `GET https://oauth.platform.intuit.com/op/v1/jwks`, issuer
   `https://oauth.platform.intuit.com/op/v1`). Errors are `{"error": "invalid_grant"}`.
   Refresh tokens **rotate**; the previous token keeps working until its
   successor is used once, mirroring Intuit's ~24h grace.
3. `POST /oauth2/v1/tokens/revoke` (also `/v2/oauth2/tokens/revoke` on the
   developer host) revokes an access or refresh token; revoking a refresh token
   disconnects that user entirely.
4. `GET /v1/openid_connect/userinfo` (production and sandbox hosts) returns
   `{sub, email, emailVerified, givenName, familyName, phoneNumber, address}`.

Shortcut: `POST /_emulator/tokens` with `{"user": "<pNNN|email>"}` mints a
QBO-shaped access+refresh pair (and returns `realm_id`); `{"app_only": true}`
mints a company-level full-access token. API calls need
`Authorization: Bearer <access_token>`. Missing/invalid/expired tokens give the
real 401 shape `{"fault": {"error": [{"message": "message=AuthenticationFailed;
errorCode=003200; statusCode=401", "code": "3200", ...}], "type":
"AUTHENTICATION"}}` with `WWW-Authenticate`. A token issued for this realm used
against a different realm in the path gives 401; an unknown realm gives 403.

## API conventions

- Base path `/v3/company/{realmId}/...`. JSON when `Accept: application/json`;
  an explicit `Accept: application/xml` gets
  `<IntuitResponse xmlns="http://schema.intuit.com/finance/v3" time="...">`
  XML for reads/queries/CRUD responses.
- Every JSON response wraps the entity: `{"Invoice": {...}, "time":
  "2026-10-16T07:12:45.123-07:00"}` and carries an `intuit_tid` header.
- `minorversion` query param accepted for 1–75 (other values → 400).
- Faults: `{"Fault": {"Error": [{"Message", "Detail", "code", "element"}],
  "type": "ValidationFault"}, "time"}`. Codes used: 610 Object Not Found,
  5010 Stale Object Error, 6240 Duplicate Name Exists Error, 2020 required
  field, 6000 business validation, 4000 query parse, 110 authorization,
  3001 ThrottleExceeded (429 via fault injection).
- Entities carry numeric-string `Id` (per-entity sequence), `SyncToken`
  (increments on every update; stale token → 5010), `MetaData {CreateTime,
  LastUpdatedTime}` in `-07:00` offset, `domain: "QBO"`, `sparse: false`;
  refs are `{"value": "12", "name": "..."}`.

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
| POST | `/batch` | ≤30 `BatchItemRequest` items; create/update/delete/void/query; per-item `Fault` on failure |
| GET | `/cdc?entities=Invoice,Customer&changedSince=<ISO>` | Changes in the last 30 days; deleted entities as `{"Id", "status": "Deleted", "MetaData"}` |
| GET | `/reports/{Name}` | ProfitAndLoss, BalanceSheet, TrialBalance, AgedReceivables, AgedReceivableDetail, AgedPayables, AgedPayableDetail, CustomerIncome, GeneralLedger, TransactionList |
| POST | `/upload` | Multipart attachable upload (creates an Attachable from the JSON metadata part) |

Delete/void semantics: transactions delete (`{"{Entity}": {"Id", "status":
"Deleted"}}`); name-list entities are deactivated (`Active: false`) instead;
void zeroes amounts, sets `PrivateNote: "Voided …"` and removes the ledger
postings. Creating a Payment/BillPayment updates the linked Invoice/Bill
`Balance` and `LinkedTxn`, and customer/vendor balances roll up dynamically.

### Query language

`SELECT * | COUNT(*) | Field, Field FROM Entity [WHERE cond [AND cond]...]
[ORDERBY Field [ASC|DESC], ...] [STARTPOSITION n] [MAXRESULTS m]` — keywords
case-insensitive; operators `= < > <= >= !=`, `IN ('a','b')`, `LIKE '%x%'`
(`%` wildcards only); dotted fields (`MetaData.LastUpdatedTime > '…'`,
`CustomerRef = '12'` compares the ref value); `Active IN (true, false)`.
Defaults: `STARTPOSITION 1`, `MAXRESULTS 100` (max 1000); name-list entities
default to `Active = true` unless `Active` is mentioned. `OR` and any parse
error → 400 fault 4000 "Error parsing query". Empty results return
`{"QueryResponse": {}}`; counts return `{"QueryResponse": {"totalCount": n}}`.

### Reports

Params: `start_date`, `end_date`, `date_macro` (`This Fiscal Year-to-date`,
`Last Month`, `This Month`, `Last Fiscal Year`, `All dates`, …),
`accounting_method=Accrual|Cash`, `summarize_column_by=Total|Month|Quarter|
Customer|Class|Vendor`, plus `customer`/`vendor`/`class` filters and
`account` for GeneralLedger. Output is the real QBO structure
(`Header`/`Columns`/`Rows` with `Section`/`Data` rows, `Summary` totals and
`group` markers such as `Income`, `NetIncome`). P&L income ties exactly to
invoice subtotals (paid invoices under Cash), expenses to bills/purchases/
journals; the Balance Sheet balances because opening Retained Earnings is the
plug that balances the world's opening trial balance; AR/AP tie to open
invoice/bill balances.

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
- Cash-basis P&L recognises income from payments and keeps bill/purchase
  expenses on their transaction dates — an approximation of QBO's cash-basis
  rules (which also re-times by payment links in more corner cases).
- No SalesReceipt/Deposit records are seeded (both are fully supported via
  CRUD); world payments deposit straight into the operating bank account.
- Payroll, payments (QB Payments API), online invoice payment processing,
  recurring transactions, budgets, purchase orders, price levels, inventory
  quantity tracking and multi-currency revaluation are not emulated.
- Attachables store metadata only; file bytes from `/upload` are not retained.
- Delegated permissions are a two-role model (full vs read-only) rather than
  QBO's per-feature user permissions; OAuth app tokens are full access.
- The opening retained-earnings plug (see Reports) is an emulator bookkeeping
  choice: check `Retained Earnings` opening via TrialBalance rather than the
  world's `opening_balance` of 0.
