# Xero

Xero Accounting API 2.0 with OAuth 2.0 identity, organisation connections, accounting
transactions, accrual reports, and signed webhooks. No real Xero account is needed.

## Hosts and routing

| Host | Purpose |
| --- | --- |
| `api.xero.com` | Connections and `/api.xro/2.0/` accounting routes |
| `login.xero.com` | `/identity/connect/authorize` |
| `identity.xero.com` | Tokens, revocation, discovery, and JWKS |
| `go.xero.com` | Host used to construct online invoice links |

Dedicated port: **44112**. Prefix mode:
`http://localhost:44080/xero/api.xro/2.0/Invoices`. Host mode keeps the vendor URLs.
Generated links use the incoming routing mode. Accounting resource paths accept
case variations, including `/api.xro/2.0/invoices`.

```python
from emulators.core.hub import EmulatorHub

hub = EmulatorHub(
    providers=["xero"], anchor="2026-10-16", webhook_mode="sync"
)
```

## Authentication and tenants

Retrieve the fictional OAuth client ID and secret from `GET /_emulator/info`
under `credentials`. Unknown clients are also
accepted with the hub's default lenient client policy. In strict mode use the seeded
test client, or register clients with `ctx.tokens.register_client(...)` during setup.

Authorize with `response_type=code`, `client_id`, `redirect_uri`, `scope`, and `state`.
`login_hint=eleanor.whitfield@halcyonadvisory.example` approves automatically; without
a hint an HTML account picker lists active employees. Code redemption checks the
client, redirect URI, and S256 PKCE verifier. Codes are single use. `nonce` is
preserved in the ID token.

`POST https://identity.xero.com/connect/token` accepts form-encoded
`authorization_code`, `refresh_token`, and `client_credentials` grants. Client
authentication supports HTTP Basic and form credentials. Access tokens are signed
RS256 JWTs with a 1,800 second lifetime, scopes, `authentication_event_id`, and
`xero_userid` for delegated grants. Custom Connection tokens include
`xero_tenant_id`. ID tokens require `openid`; refresh tokens require `offline_access`
and expire after 60 days. Refresh tokens rotate, with the old token usable for a
30 minute retry grace period measured from its first rotation (the old token is invalid
at the 1,800 second boundary). PKCE clients can refresh without a client secret,
including under the strict client policy. Revocation invalidates the token family and
removes the user's tenant connections for that client. Token and revocation endpoints
require form-encoded bodies; malformed JSON and multipart uploads are rejected.

Example scopes:

```text
openid profile email offline_access accounting.transactions accounting.contacts
accounting.settings accounting.reports.read accounting.attachments files
```

Read-only variants such as `accounting.contacts.read`, plus granular transaction
scopes such as `accounting.invoices.read` and report scopes such as
`accounting.reports.balancesheet.read`, are enforced. `accounting.invoices[.read]`
covers invoices, credit notes, quotes, and items; `accounting.banktransactions[.read]`
covers bank transactions and transfers. Both aged reports use
`accounting.reports.aged.read`. Settings includes chart, tracking, items, users,
currencies, and tax rates. Accounting attachments require
`accounting.attachments[.read]`; Files API scopes do not grant accounting attachment access.

For direct setup, `POST /_emulator/tokens` and
`POST /_emulator/actions/mint_token` both accept:

```json
{"user":"p001","client_id":"emulator-client","expires_in":1800}
```

The response contains a signed access token. Omit `user` for an app-only token;
pass `scopes` to restrict it. Omitted scopes grant the supported broad scopes.
User IDs, employee emails, and Xero user GUIDs are accepted. Departed employees
and external contacts cannot obtain delegated access.

Call `GET https://api.xero.com/connections` to select a tenant. Delegated clients
receive two connections; Custom Connections receive only Halcyon. Connections
are bound to client and user. Deleting a connection immediately removes that
client/user's access to that organisation; refreshing does not recreate it.
Delegated accounting requests require both `Authorization: Bearer ...` and
`xero-tenant-id`. Custom Connections can omit the tenant header, since their token
is already bound to Halcyon; an explicitly supplied tenant must still be connected.
Missing delegated tenants and disconnected tenants
return Xero's 403 problem shape with `Detail: AuthenticationUnsuccessful`.

| Tenant | Stable ID |
| --- | --- |
| Halcyon Advisory Group | `c9a579e1-1ac3-4235-b2ee-26257dfb356b` |
| Demo Company (US) | `2d63b337-61d4-491f-898b-c2e2fe0b6a9e` |

The demo has its own empty transaction ledger, chart, settings, items, and users.
Resources and writes are isolated by tenant even when settings IDs are shared.
Partners and tenant admins have wire role `FINANCIALADVISER`; Finance/Operations and executive staff
have `STANDARD`; other active employees have `READONLY`. Read-only users may read
the organisation ledger and reports, and cannot mutate them. Xero permissions
apply to the organisation rather than individual invoice ownership. For example,
`p001` (Eleanor Whitfield) may write; `p005` (Sofia Alvarez) may only read. Departed
employees are omitted from `Users`.

## Accounting endpoints

Paths below are relative to `/api.xro/2.0`. JSON is always returned, including
when `Accept` is absent. Responses include `Id`, `Status`, `ProviderName`,
`DateTimeUTC`, and the named entity array. Dates use Microsoft JSON dates and
transaction date string companions.

| Methods | Path | Notes |
| --- | --- | --- |
| GET | `/Organisation` | `Organisations` array; USD, US partnership, fiscal year end 31 December |
| GET, PUT | `/Accounts` | Chart creation; individual GET/POST/DELETE by ID |
| GET, PUT, POST | `/Contacts` | Customers/suppliers, addresses, secondary contacts, terms, balances, archive |
| GET, POST | `/Contacts/{id}` | Individual contact and updates |
| GET, PUT | `/ContactGroups` | Group creation; individual GET/POST; delete with POST `Status: DELETED` |
| PUT | `/ContactGroups/{id}/Contacts` | Add contacts, updating both sides of membership |
| DELETE | `/ContactGroups/{id}/Contacts/{contactId}` | Remove membership |
| GET, PUT, POST | `/Items` | Untracked service/noninventory items; individual GET/POST/DELETE |
| GET, PUT, POST | `/Invoices` | `ACCREC` invoices and `ACCPAY` bills; individual GET/POST |
| GET | `/Invoices/{id}` | Also accepts invoice number; `Accept: application/pdf` generates a real PDF lazily |
| GET | `/Invoices/{id}/OnlineInvoice` | `OnlineInvoices` array with `OnlineInvoiceUrl` |
| POST | `/Invoices/{id}/Email` | Approved sales invoices with contact email; sets sent flag, records history, returns 204 |
| GET, PUT, POST | `/Payments` | Apply payment to an authorised invoice; individual GET/POST |
| POST | `/Payments/{id}` | `{"Status":"DELETED"}` reverses the payment and reopens the invoice |
| GET, PUT, POST | `/BankTransactions` | Receive/spend, bank or card account; individual GET/POST |
| GET, PUT, POST | `/BankTransfers` | Create between distinct USD bank accounts; POST deletion by ID or batch; both transaction-side IDs exposed |
| GET, PUT, POST | `/CreditNotes` | AR/AP credits; individual GET/POST |
| PUT | `/CreditNotes/{id}/Allocations` | Validate contact/type/date, available credit, and invoice outstanding |
| DELETE | `/CreditNotes/{id}/Allocations/{allocationId}` | Remove allocation and reopen invoice |
| GET, PUT, POST | `/ManualJournals` | Balanced NoTax journals; individual GET/POST; DRAFT/POSTED/VOIDED/DELETED |
| GET, PUT, POST | `/Quotes` | Proposals; individual GET/POST; DRAFT/SENT/ACCEPTED/DECLINED/INVOICED/DELETED |
| GET, PUT | `/TrackingCategories` | Department/Office; two active categories, including reactivation; individual GET/POST/DELETE |
| PUT | `/TrackingCategories/{id}/Options` | Add named option |
| GET | `/TaxRates`, `/Currencies`, `/BrandingThemes`, `/Users` | Organisation reference data |
| GET | `/Invoices/{id}/Attachments` | Attachment metadata and mode-aware download URLs |
| GET, PUT, POST | `/Invoices/{id}/Attachments/{filename}` | Raw bytes; download also accepts attachment GUID; max 10 files, 25 MiB each |
| GET, PUT | `/{resource}/{id}/History` | Invoices, Contacts, Payments, CreditNotes, BankTransactions, Quotes; write `HistoryRecords: [{Details: ...}]` |
| GET | `/Reports/ProfitAndLoss` | Accrual income, expenses, net profit; comparisons and tracking filters |
| GET | `/Reports/BalanceSheet` | Assets, liabilities, equity, retained/current-year earnings; comparisons |
| GET | `/Reports/TrialBalance` | Monthly and YTD debit/credit columns, account attributes, totals |
| GET | `/Reports/AgedReceivablesByContact` | Required `contactId`; invoice totals, payments, credits, outstanding at cutoff |
| GET | `/Reports/AgedPayablesByContact` | Same shape for bills |
| GET | `/Reports/BankSummary` | Opening + received − spent = closing, per account and total |
| GET | `/Reports/ExecutiveSummary` | Cash, profitability, balance sheet, invoice metrics; prior-period comparison and variance |

PUT creates. POST creates or updates, including the documented entity-array and
single-object request forms. Invoice transitions enforce DRAFT → SUBMITTED →
AUTHORISED, plus direct draft approval, draft deletion, approved voiding, and
payment-driven PAID status. Payments cannot exceed outstanding amounts. Financial
changes to paid/credited invoices and voiding allocated credit notes are rejected.
Removing payments/allocations restores balances. Payment changes invalidate cached
invoice PDFs. Invoice and credit-note drafts need only Type and Contact; submitting
or approving requires line items. Derived totals, payment balances, and attachment
flags cannot be overwritten in document requests. IDs are GUIDs and are normalised
before lookup, including uppercase IDs in update bodies.

Payments accept a nested `InvoiceID` or `InvoiceNumber`. Bank accounts accept payments
regardless of `EnablePaymentsToAccount`; other eligible accounts require that flag.
Bank transfers expose their `SPEND-TRANSFER` and `RECEIVE-TRANSFER` records without
double posting; deleting a transfer deletes both sides and reverses its postings.
Bill numbers are unique per supplier, while sales invoice numbers are unique across
the organisation. Contact renames update transaction references and cached PDFs;
contact-group renames/deletions maintain contact memberships. Voided/deleted manual
journals cannot be restored to a posting state.

Line items support AccountCode or AccountID, description-only rows, and derivation
of a missing quantity or unit price from LineAmount. `unitdp=4` opts into four-place
unit prices on writes and reads; the default is two places. Unit prices round before
multiplication; totals remain at two places. DateString/DueDateString can be used
when their corresponding Date/DueDate fields are absent.

Collections support `where`, `order`, `If-Modified-Since` (ISO UTC or HTTP date),
`page`, and `pageSize`. Paging defaults to 100 when requested, clamps sizes to
1–1000, and returns lowercase `pagination` metadata with total filtered `itemCount`.
Invoices without paging return summaries without line items; `Statuses` and
`summaryOnly=true` imply paging. ID/number filters include `IDs`, `InvoiceNumbers`,
`ContactIDs`, and `Statuses`. `includeArchived=true` includes archived contacts.
`SearchTerm` searches invoice number/reference, or contact name, first/last name,
contact/company number, and email. Quotes support `ContactID`, `Status`, `QuoteNumber`,
`DateFrom`, `DateTo`, `ExpiryDateFrom`, and `ExpiryDateTo`. Numeric ordering preserves
zeroes, Microsoft dates sort chronologically, and explicit orders include the resource
ID as a stable secondary key. Ordering by complex objects/arrays returns validation error.

The parsed `where` subset supports property paths, comparisons, AND/OR/NOT,
parentheses, `DateTime(...)`, booleans/null, and string
`Contains`/`StartsWith`/`EndsWith`/`Equals`. Example:

```text
Type=="ACCREC" AND Date>=DateTime(2026,1,1) AND Contact.Name.Contains("Northwind")
```

Invalid expressions and transactions return HTTP 400 with `ErrorNumber: 10`,
`Type: ValidationException`, and `Elements[].ValidationErrors[].Message`.
`summarizeErrors=false` commits successful batch elements and returns validation
errors on failed elements; the default rejects the batch atomically.
Partial responses include `StatusAttributeString: OK|ERROR`, `HasErrors` for invoices,
and `HasValidationErrors` for other entities. `SummarizeErrors` also accepts lowercase casing.
`Idempotency-Key` replays identical resource mutations without duplicate events,
scoped by client, tenant, method, and path. Changing body or query parameters under
the same key is rejected.
OAuth form credentials and tokens are redacted before entering the request journal.
Unknown resources return a plain-text 404. Per-tenant limits are 60/minute and
5,000/day; the app minute limit is 10,000. Accounting responses expose
`X-MinLimit-Remaining`, `X-DayLimit-Remaining`, and `X-AppMinLimit-Remaining`;
exhaustion returns 429 and `Retry-After`, using the controllable hub clock. Responses
include `xero-correlation-id`, including authentication/validation errors and injected
faults. Custom Connections without tenant headers use the same tenant quota.

## Seed and reconciliation

With seed `emulators`, anchor `2026-10-16`:

| Resource | Seed |
| --- | --- |
| Contacts | 24 world clients + 14 vendors; 37 active, 1 archived |
| Invoices | 115 ACCREC + 152 ACCPAY, preserving world dates, lines, statuses, and totals |
| Payments | 95 world receipts + 148 paid-bill settlements |
| Accounts | 41 world chart accounts; account code `1000` is the operating bank |
| Items | 10 world services, codes `SVC-001` through `SVC-010` |
| Bank transactions | 12 incidental card expenses derived from world vendors and their categories |
| Journals | 11 completed months of equipment depreciation at $1,500/month |
| Quotes | 10 open world CRM opportunities |
| Tracking | Department options from world departments; Office options from world offices |
| Tax | US zero-rate defaults plus paired custom TAXnnn rates from the world's invoice taxes |
| Users | 45 active employees; departed employees removed |

Useful stable IDs:

| Record | ID / example |
| --- | --- |
| Northwind Logistics | `0b5d9748-797c-4caa-8ec8-d8f4fc4da694`, ContactNumber `c001` |
| Northwind primary email | `christopher.ingram@northwindlogistics.example` |
| Operating bank | `a08edfb5-cc2d-45b8-b190-7bae57609f76`, Code `1000` |
| Invoice INV-1041 | `77950a80-b136-4e27-bfb6-693e39cb37e9`, paid $2,760, Quarry Lane Manufacturing |
| Department tracking | `4834ff56-2eaf-45e1-8210-5d00da0bc45c` |
| Office tracking | `b7f56f24-0fb4-46bc-b0a0-731079937a2a` |

Reports derive double-entry postings from transaction state. Draft/deleted/voided
transactions do not post. Invoice approvals post receivable/payable, revenue/expense,
and tax control; payments move bank and the relevant control account. Credits,
bank activity, transfers, and posted manual journals share the same ledger.
Historical reports cut payments and allocations off at their dates. Aged reports
include unapplied credits so their closing totals reconcile to control accounts.
`fromDate`/`toDate` bound invoice issue dates; `date` sets the payment cutoff.
Balance sheets carry earlier financial-year profit into retained earnings.

The world chart's opening balances have a $475,000 difference. Xero explicitly
puts that amount in opening retained earnings, Code `3200`, on the first day of
the anchor month in the preceding year. This balances the opening trial balance
without altering the shared world. All other opening balances are copied.

`ProfitAndLoss` and `BalanceSheet` accept `periods` (up to 12) and
`timeframe=MONTH|QUARTER|YEAR`. Profit-and-loss tracking supports
`trackingCategoryID`/`trackingOptionID` and their `2` variants. Selecting a category
without an option aggregates all lines tagged with that category. Reports use the
standard Xero `Reports[].Rows`, nested sections, cells, and account attributes.

## Webhooks and actions

`POST /_emulator/actions/configure_webhook`:

```json
{"url":"https://consumer.example/xero","signing_key":"fictional-test-key"}
```

Configuration sends an empty-events POST with an incorrect signature, expecting
401, then a correctly signed POST, expecting 200. Each validation request must
complete within five seconds. Failed validation leaves the subscription disabled.
In-process tests can set `hub.webhook_transport = httpx.ASGITransport(app=receiver)`.

Contact and invoice creation/updates then emit Xero event envelopes with
`resourceUrl`, `resourceId`, `eventDateUtc`, `eventType`, `eventCategory`, `tenantId`,
`tenantType`, first/last event sequence, and entropy. The `x-xero-signature` header
is Base64(HMAC-SHA256(signing_key, exact_body_bytes)). Events go through the hub's
dispatcher, so sync/manual/async delivery, retries, journal, and fault tooling work.
Validation uses the dispatcher's direct request method. Reset removes webhook
configuration, tokens, mutations, rate counters, and event sequence state.

Actions: `configure_webhook` and `mint_token`. The latter is also used by the
standard `/_emulator/tokens` route.

## Known gaps

- JSON only; XML request/response serialization is not implemented.
- Transaction posting is USD only. Currency reference data includes CAD and GBP;
  currency conversion, realised gains, and multicurrency payments are not implemented.
- Purchase orders, repeating invoices, prepayments/overpayments, batch payments,
  expense claims, payroll, projects, budgets, and Files API are not implemented.
- TaxRates, Currencies, BrandingThemes, and Users are read-only. Tracking option
  rename/archive/delete endpoints are not implemented. Inventory is untracked only.
- Invoice email records the action and sent flag; it does not send email. Online
  invoice links are constructed but a hosted public invoice UI is not implemented.
- Attachments are implemented for invoices only. Deleted resource history is not a
  complete reconstruction of Xero's historical audit stream.
- Reports use accrual accounting; `paymentsOnly=true` returns validation error.
  Regional/custom layouts, performance ratios, and tracking-filtered balance sheets
  are not implemented. Status edits apply to current transaction state; there is no
  historical status-version reconstruction after a later void or deletion.
- The where parser implements the documented subset above, not arbitrary .NET
  expressions. Unsupported syntax returns validation error.
- Used account codes/types cannot be changed; archive them instead. Idempotency
  results persist until reset. Quota counters use fixed windows from the first call.
- Each delegated authorization connects both seeded tenants. User-configurable
  tenant consent selection, fine-grained custom roles, and Xero's full regional
  validation rules are not implemented. OAuth issuance accepts arbitrary scope names;
  resource access checks the documented supported scope mappings.
- Webhooks use the core retry schedule rather than Xero's 24-hour retry/replay
  schedule. A provider supports one configured webhook subscription.
- Invoice-line updates replace the line-item array rather than merging partial
  individual lines. Transfer-side tracking and newer credit-note webhook schemas
  are not implemented.

## Verification and references

```powershell
uv run pytest tests/providers/xero -q
$env:EMULATORS_PROVIDERS='xero'
uv run pytest tests/sdk -q -k xero
uv run pytest tests/e2e -q
uv run ruff check src/emulators/providers/xero tests/providers/xero tests/sdk/test_xero_http.py
uv run ruff format --check src/emulators/providers/xero tests/providers/xero tests/sdk/test_xero_http.py
```

No Xero SDK is installed. The SDK-folder test follows Xero's documented HTTP
sequence using unmodified httpx against real vendor hostnames through
`emulator_intercept` and the live gateway. Provider tests cover world projection,
permissions, tenants, queries, accounting invariants, settlement/reversal, files,
OAuth/PKCE/rotation/revocation, quota/reset, and webhook signatures.
Adversarial regression coverage additionally exercises malformed bodies, wrong nested
types, nonfinite/extreme values, GUID casing, date overflow, stable ordering, calculated
field protection, scoped idempotency, resource methods, granular scopes, and accounting
state transitions. Seed checks verify deterministic lazy construction, transaction
cross-references, line account IDs, and the absence of future accounting activity.

Wire references: [official Accounting OpenAPI](https://github.com/XeroAPI/Xero-OpenAPI/blob/master/xero_accounting.yaml),
[requests and responses](https://developer.xero.com/documentation/api/accounting/requests-and-responses/),
[invoices](https://developer.xero.com/documentation/api/accounting/invoices),
[payments](https://developer.xero.com/documentation/api/accounting/payments),
[types and codes](https://developer.xero.com/documentation/api/accounting/types/),
[token types](https://developer.xero.com/documentation/guides/oauth2/token-types/),
[Custom Connections](https://developer.xero.com/faq/custom-integration),
[granular scopes](https://developer.xero.com/documentation/guides/oauth2/scopes/),
[bank transfers](https://developer.xero.com/documentation/api/accounting/banktransfers),
[unit price rounding](https://developer.xero.com/documentation/guides/how-to-guides/rounding-in-xero),
and [webhooks](https://developer.xero.com/documentation/guides/webhooks/overview).
