# Stripe

The `stripe` provider emulates a single Stripe test-mode merchant account for Halcyon
Advisory Group. It runs on port **44110**, through the gateway's `/stripe` prefix,
or through the vendor hostnames below. No Stripe network connection is made.

## Hosts and versions

API hosts: `api.stripe.com`, `files.stripe.com`, `uploads.stripe.com`,
`checkout.stripe.com`, `connect.stripe.com`, and `billing.stripe.com`.
`invoice.stripe.com` and `pay.stripe.com` also route to the provider so hosted
invoice links and invoice PDFs can be fetched locally.

Responses include `Request-Id` and `Stripe-Version`. The default is
`2025-09-30.clover`; a supplied `Stripe-Version` is echoed. Billing uses the modern
`subscription.items.data[].current_period_start/current_period_end`, invoice
`parent`, `payments`, and expandable `confirmation_secret` fields. Versions before
`2025-03-31` receive the older invoice `payment_intent` and `subscription` links
and subscription-level period fields. Versions before `2022-11-15` also receive
the PaymentIntent `charges` list. This is targeted compatibility, not a complete
implementation of every historical Stripe API version.

All generated links use the request's routing mode. For example, creating and
finalizing an invoice through `http://localhost:44080/stripe` returns a PDF URL
under that same base. PDFs are rendered lazily with the world document generator
and cached in the shared content-addressed blob store.

## Auth flows and test credentials

| Credential | Value |
| --- | --- |
| Secret key | `sk_test_51HalcyonAdvisoryEmulator000000000000` |
| Publishable key | `pk_test_51HalcyonAdvisoryEmulator000000000000` |
| Account | `acct_1278FC417A4145FE` |
| Successful test card | `4242424242424242`, future expiration, any three-digit CVC |

Send `Authorization: Bearer <key>` or HTTP Basic with the key as username and an
empty password. With the hub's default `lenient_clients=True`, arbitrary
`sk_test_` and `rk_test_` keys access the seeded account. Strict mode accepts the
documented secret key. Live-mode keys are rejected with a Stripe-shaped 401.

Publishable keys can create tokens and card PaymentMethods. They can retrieve or
confirm a PaymentIntent or SetupIntent when its matching `client_secret` is
supplied. Merchant resources require a secret key. `Stripe-Account` can select the
seeded account; other account IDs are denied.

`POST /_emulator/tokens` also works. App-only tokens access the merchant account.
Delegated tokens for active Finance employees (`p025`–`p030`) or world admins
(`p035`, `p036`) access the account; other employees are denied. External client
contacts are restricted to their own customer's records, payment methods,
subscriptions, and invoices. Cross-customer retrieval returns 404 and merchant
balance/webhook administration returns 403. Optional token scopes use
`customers:read`, `customers:write`, equivalent collection names, or `*`.
Expired and revoked grants remain invalid even when their token has a test-key
prefix or their idempotency key was previously cached. These delegated grants
are a control-plane testing convenience; Stripe's ordinary secret keys remain
account-scoped.

Connect's headless OAuth flow supports `/oauth/authorize` with `client_id`,
`redirect_uri`, `scope=read_only|read_write`, and `state`, followed by a form POST
to `/oauth/token` with `grant_type=authorization_code` and the returned `code`.
Refresh grants and `/oauth/deauthorize` are available; deauthorization revokes
the client's local access/refresh grants. Read-only access tokens
cannot mutate merchant resources.

## Wire conventions

POST bodies use `application/x-www-form-urlencoded`, except file uploads, which
use multipart. Bracket forms support dictionaries, indexed arrays, `[]` arrays,
and metadata keys containing digits. Metadata merges on update; an empty value
removes one key, and `metadata=` clears the map.

List responses are Stripe `list` objects with `data`, `has_more`, and `url`.
They default to 10 records, allow 1–100, sort newest first, and support
`starting_after`, `ending_before`, exact resource filters, and `created[gt|gte|lt|lte]`.
Search supports quoted equality (`email:'x'`, `metadata['key']:'v'`), substring
matching (`name~'sub'`), numeric comparisons, AND, OR, and leading `-` negation.
Mixed AND/OR and malformed syntax return 400. Search pagination uses opaque
`page` tokens and includes `total_count`.

`expand[]` supports reference expansion through nested dictionaries and list
`data` fields, up to four segments. Examples are `customer`,
`latest_charge.balance_transaction`, `data.customer`, and
`latest_invoice.confirmation_secret`. As an explicit compatibility convenience,
requesting `latest_invoice.payment_intent` also exposes that legacy relationship
on modern invoices.

POST `Idempotency-Key` values replay the original body, status, and request ID,
with `Idempotent-Replayed: true`. Changing parameters returns 400
`idempotency_error`. Equivalent form parameter ordering is accepted. Concurrent
requests with the same key serialize; validation failures do not reserve the
key, and entries expire after 24 hours on the hub clock. Entries are isolated by
authentication identity and selected account and participate in snapshots/reset.

## Endpoints

Paths below are relative to the selected host or `/stripe` base. `GET /v1/<resource>`
means list and `GET /v1/<resource>/{id}` means retrieve, where both are shown.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/v1/account` | Seeded merchant account |
| GET, POST | `/v1/customers` | List/create; address, shipping, invoice settings, metadata |
| GET, POST, DELETE | `/v1/customers/{id}` | Retrieve/update/delete; deleted customer tombstone |
| GET | `/v1/customers/search` | Stripe search result envelope |
| GET | `/v1/customers/{id}/payment_methods` | Customer-bound PaymentMethods |
| GET, POST | `/v1/customers/{id}/sources` | Legacy card sources, including `tok_visa` |
| GET, POST, DELETE | `/v1/customers/{id}/sources/{source}` | Retrieve/update/delete card |
| GET, POST | `/v1/customers/{id}/tax_ids` | Synthetic tax verification |
| GET, DELETE | `/v1/customers/{id}/tax_ids/{tax_id}` | Retrieve/delete |
| GET, POST | `/v1/customers/{id}/balance_transactions` | Customer credit/debit adjustments |
| GET, POST | `/v1/customers/{id}/balance_transactions/{transaction}` | Retrieve/update description and metadata |
| GET, POST | `/v1/payment_methods` | List requires `customer`; create card or card token |
| GET, POST | `/v1/payment_methods/{id}` | Retrieve/update billing details and expiration |
| POST | `/v1/payment_methods/{id}/attach`, `/detach` | Ownership checks; detached methods cannot be reused |
| POST | `/v1/tokens` | Single-use card token |
| GET | `/v1/tokens/{id}` | Retrieve token and used state |
| GET, POST | `/v1/payment_intents` | List/create; optional `confirm`, manual capture |
| GET, POST | `/v1/payment_intents/{id}` | Retrieve/update; metadata can update after success |
| POST | `/v1/payment_intents/{id}/confirm`, `/capture`, `/cancel`, `/increment_authorization` | State checks; partial capture and authorization increases |
| GET | `/v1/payment_intents/search` | Search |
| GET, POST | `/v1/setup_intents` | List/create; optional confirmation |
| GET, POST | `/v1/setup_intents/{id}` | Retrieve/update |
| POST | `/v1/setup_intents/{id}/confirm`, `/cancel` | Successful setup attaches PM to customer |
| GET | `/v1/charges`, `/v1/charges/{id}` | Charges are created by PaymentIntents |
| POST | `/v1/charges/{id}` | Update description, receipt email, shipping, metadata |
| GET | `/v1/charges/search`, `/v1/charges/{id}/refunds` | Search/refund list |
| GET, POST | `/v1/refunds` | List/create on charge or PaymentIntent; partial refunds |
| GET, POST | `/v1/refunds/{id}` | Retrieve/update metadata |
| GET | `/v1/disputes`, `/v1/disputes/{id}` | Seeded and test-card disputes |
| POST | `/v1/disputes/{id}`, `/v1/disputes/{id}/close` | Evidence update/submission; accept dispute as lost |
| GET, POST | `/v1/products`, `/v1/prices` | Catalog; integer per-unit prices and recurring intervals |
| GET, POST | `/v1/products/{id}`, `/v1/prices/{id}` | Retrieve/update mutable catalog properties |
| DELETE | `/v1/products/{id}` | Products with no attached prices |
| GET | `/v1/products/search`, `/v1/prices/search` | Search; prices also support `lookup_keys[]` on lists |
| GET, POST | `/v1/subscriptions` | List/create; trials, quantity, default payment behavior |
| GET, POST, DELETE | `/v1/subscriptions/{id}` | Retrieve/update/cancel; list excludes canceled unless `status=all` |
| POST | `/v1/subscriptions/{id}/resume` | Resume a paused subscription |
| GET | `/v1/subscriptions/search` | Search |
| GET, POST | `/v1/subscription_items` | List with subscription filter; add item |
| GET, POST, DELETE | `/v1/subscription_items/{id}` | Retrieve/update/remove; cannot remove last item |
| GET, POST | `/v1/invoices` | List/create draft; optional pending invoice items |
| GET, POST, DELETE | `/v1/invoices/{id}` | Retrieve/update/delete draft |
| GET | `/v1/invoices/{id}/lines`, `/v1/invoices/search` | Paginated lines and search |
| POST | `/v1/invoices/{id}/finalize`, `/pay`, `/send`, `/void`, `/mark_uncollectible` | Invoice lifecycle and payment side effects |
| GET | `/v1/invoices/upcoming` | Preview for customer or existing subscription |
| POST | `/v1/invoices/create_preview` | Preview; no persisted invoice |
| GET, POST | `/v1/invoiceitems` | List/create pending or draft-bound items |
| GET, POST, DELETE | `/v1/invoiceitems/{id}` | Retrieve/update/delete; draft totals recalculate |
| GET | `/v1/invoice_payments`, `/v1/invoice_payments/{id}` | Modern invoice payment records |
| GET, POST | `/v1/checkout/sessions` | List/create payment, subscription, or setup sessions |
| GET, POST | `/v1/checkout/sessions/{id}` | Retrieve/update metadata |
| GET | `/v1/checkout/sessions/{id}/line_items` | Paginated line items |
| POST | `/v1/checkout/sessions/{id}/expire` | Explicit expiration |
| POST | `/v1/billing_portal/sessions` | Customer portal session URL |
| GET | `/v1/balance`, `/v1/balance_transactions`, `/v1/balance_transactions/{id}` | Merchant accounting and availability |
| GET, POST | `/v1/payouts` | List/create payout; sufficient-funds check |
| GET, POST | `/v1/payouts/{id}` | Retrieve/update metadata |
| POST | `/v1/payouts/{id}/cancel` | Pending payouts only |
| GET | `/v1/events`, `/v1/events/{id}` | Event snapshots; type/types filters |
| GET, POST | `/v1/webhook_endpoints` | List/create; secret returned only on creation |
| GET, POST, DELETE | `/v1/webhook_endpoints/{id}` | Retrieve/update/delete; event filters and disabled flag |
| GET, POST | `/v1/test_helpers/test_clocks` | List/create frozen test clock |
| GET, DELETE | `/v1/test_helpers/test_clocks/{id}` | Retrieve/delete; deletes attached customers |
| POST | `/v1/test_helpers/test_clocks/{id}/advance` | Process subscription renewals synchronously |
| GET, POST | `/v1/files` | List/multipart upload on files or uploads host |
| GET | `/v1/files/{id}`, `/v1/files/{id}/contents` | File metadata and exact binary contents |
| GET | `/c/pay/{session}` | Minimal hosted checkout page |
| GET | `/i/{account}/test_{invoice}`, `/invoice/{invoice}/pdf` | Hosted invoice and genuine PDF |
| GET | `/p/session/{session}` | Minimal customer portal page |
| GET | `/oauth/authorize` | Headless Connect authorization redirect |
| POST | `/oauth/token`, `/oauth/deauthorize` | Connect token/refresh and deauthorization response |

## Payments and billing behavior

PaymentIntents start at `requires_payment_method` or `requires_confirmation`.
Confirmation produces `requires_action`, `requires_capture`, or `succeeded`, or
a 402 `card_error` with a failed charge and `last_payment_error`. Correcting the
payment method permits retry. Successful automatic captures and manual captures
create balance transactions with a fee rounded in cents from `amount × 0.029 + 30`.
Refund totals cannot exceed captured funds. The failed-refund card leaves charge
refund totals unchanged. Disputes withdraw the amount plus a synthetic $15 fee.

| Test card | Outcome |
| --- | --- |
| `4242424242424242`, `5555555555554444`, `378282246310005` | Success |
| `4000000000000002` | `card_declined`, `generic_decline` |
| `4000000000009995` | `card_declined`, `insufficient_funds` |
| `4000000000009987` | `card_declined`, `lost_card` |
| `4000000000000069` | `expired_card` |
| `4000000000000127` | `incorrect_cvc` |
| `4000000000000119` | `processing_error` |
| `4000000000000341` | Setup/attachment succeeds; charge fails |
| `4000002500003155`, `4000002760003184` | 3DS authentication required |
| `4000000000000259` | Successful charge followed by dispute |
| `4000000000005126` | Successful charge; refund fails |

Magic inputs include `pm_card_visa`, `pm_card_mastercard`, `pm_card_amex`,
`pm_card_chargeDeclined`, `pm_card_chargeDeclinedInsufficientFunds`,
`pm_card_chargeDeclinedLostCard`, `pm_card_chargeDeclinedExpiredCard`,
`pm_card_chargeDeclinedIncorrectCvc`, `pm_card_chargeDeclinedProcessingError`,
`pm_card_chargeCustomerFail`, `pm_card_threeDSecure2Required`,
`pm_card_threeDSecureRequired`, `pm_card_createDispute`, and `pm_card_refundFail`.
The same suffixes are accepted as `tok_...` card token inputs. Resolving a magic
PaymentMethod input creates an ordinary, unique `pm_...` object.

Subscriptions create invoices and use the same PaymentIntent and charge flow.
Missing/declined payment leaves an `incomplete` initial subscription; paying its
invoice activates it. Trial invoices are zero-value and paid. Period renewal
creates a new invoice and charge; failures make the subscription `past_due`.
Clock advancement expires unpaid initial subscriptions after 23 hours. Trials
can pause or cancel without a card according to
`trial_settings[end_behavior][missing_payment_method]`; paused subscriptions can
resume. `cancel_at_period_end` stops the next renewal.

Quantity/price changes on subscription updates support `proration_behavior=none`,
`create_prorations` (pending adjustment), and `always_invoice` (immediate invoice).
Credit balances are consumed at invoice finalization. Renewals include pending
invoice items. Month/year boundaries use calendar dates rather than fixed
30/365-day arithmetic. Confirming an invoice's PaymentIntent directly also pays
the invoice and activates its incomplete subscription.

## Seeded data

With seed `emulators` and anchor `2026-10-16`, the provider contains 5 products and
prices, 10 customers with default card PMs, 7 subscriptions (6 active, 1 canceled),
86 historical invoices, 123 PaymentIntents and charges, 2 refunds, 1 open dispute,
8 weekly payouts, and 24 events from the last 30 days. Other world seeds may
produce different subscription statuses, including `past_due`. All customers,
contacts, catalog descriptions, and one-off card payments originate in `ctx.world`.
History includes one invoice per subscription period since its world start date.
No binaries are rendered during seeding; a fresh provider typically seeds in less
than one second.

| Record | Example |
| --- | --- |
| Cedar Ridge Dental Group PC | `cus_iCgZoSz5Vap70Th3CKVXHv3y` |
| Cedar Ridge email | `kenji.walsh@cedarridgedentalgroup.example` |
| Cedar Ridge default PM | `pm_gfCDT8M9MulGH4rPxbSY6g83` |
| Cedar Ridge subscription | `sub_cTB8DWYf1KKmiEr4jfC6Xfed` |
| Maple & Main Bakery Ltd. | `cus_0M7zqSWXMmTJAQAAytUGfUFi` |
| Maple & Main email | `raj.lindqvist@mapleandmainbakery.example` |
| Bookkeeping monthly price | `price_hLrMlw5RmxwKU2VaHIk3bVXm`, lookup `bookkeeping_monthly`, 125000 USD cents |
| Fractional CFO price | `price_zGh55KO0sVUKHsIcIPWhUOfe`, lookup `fractional_cfo_monthly`, 450000 USD cents |
| Annual tax planning price | `price_aSSSCMo3sGdPJeoNBF5KoXxQ`, lookup `tax_planning_annual` |
| First historical invoice | `in_T7Dj031WAlzLbvu7aEdLE40L`, `HALCYON-0001` |

Customer metadata includes `client_id` and `quickbooks_customer`, enabling
cross-provider reconciliation. One-off PaymentIntents include `invoice_number`
and `world_payment_id`. Reset restores all seeded records and clears journals,
idempotency results, and runtime grants through the hub snapshot mechanism.

## Webhooks and events

Create a webhook endpoint with a form such as
`url=https://consumer.example/hook&enabled_events[]=payment_intent.succeeded`.
Creation returns a `whsec_...` secret. `enabled_events[]=*` receives all emitted
events. Disabled endpoints do not receive events. Events contain an immutable
`data.object` snapshot, applicable `previous_attributes`, `api_version`,
`request.id`, `request.idempotency_key`, and `pending_webhooks`.

Deliveries use `ctx.webhooks.deliver` and the hub's sync/async/manual behavior,
retry policy, and delivery journal. The signer runs for each attempt, reading
the hub clock and computing:

```text
Stripe-Signature: t=<unix_seconds>,v1=<hex_digest>
hex_digest = HMAC-SHA256(whsec_secret, ascii(timestamp) + b'.' + raw_json_payload)
```

The official `stripe.Webhook.construct_event` verifies these payloads and
signatures. When freezing the hub far from the actual wall clock, use the SDK's
`tolerance=0` in tests or align its clock; signature bytes are unchanged. Event
retrieval reflects outstanding manual/async deliveries. Mutation events cover
customer, PM, PaymentIntent, SetupIntent, charge, refund, dispute, product, price,
subscription, invoice, invoice item, checkout completion/expiration, payout, file,
and clock lifecycle operations. Idempotent replays emit no new events.

## Actions

| POST path | JSON body | Behavior |
| --- | --- | --- |
| `/_emulator/actions/complete_3ds` | `{"payment_intent":"pi_...","success":true}` | Complete authentication; manual confirmation returns to `requires_confirmation`; false records authentication failure |
| `/_emulator/actions/complete_checkout` | `{"session":"cs_test_...","card":"4242424242424242"}` | Create customer/PM and payment, setup, or subscription; complete successful session and emit event |
| `/_emulator/actions/advance_time` | `{"seconds":2678400}` | Advance shared hub clock and process non-test-clock subscriptions |

Test clocks advance their own attached customers without moving the shared hub
clock. Use `POST /v1/test_helpers/test_clocks/{id}/advance` for those subscriptions.
`advance_time` also expires overdue Checkout sessions and settles pending payouts
whose arrival date has passed.

## Known gaps

- Card payments only: no bank debits, wallets, payment links, Connect account
  management, transfers, application fees, live mode, real card network processing,
  Radar, Terminal, or tax calculation. Browser pages are minimal local fixtures.
- Optional coupons, promotion codes, and credit notes are not implemented.
  Prices support integer per-unit amounts; fractional unit amounts, tiers,
  usage metering, and tax-inclusive calculations are not modeled.
- Proration covers ordinary per-unit quantity/price changes through subscription
  updates. Subscription schedules, mixed-interval renewal accounting, advanced
  billing anchors, pending updates, and prorations through the standalone
  subscription-item endpoints are not complete. Preview supports existing
  subscription/customer data rather than every hypothetical update parameter.
- Clock processing is synchronous and explicit. There is no background billing
  scheduler, automatic invoice finalization timer, full retry/dunning schedule,
  `unpaid` transition policy, or delayed asynchronous payment settlement. Test
  clock advance limits and customer-count restrictions are not enforced.
- Basic OAuth exchanges work locally. Deauthorization revokes local grants but
  does not model Connect platform membership removal. Multi-account
  data isolation is limited to rejecting account IDs other than Halcyon's.
- Version handling covers the billing field migrations described above. Unknown
  optional request parameters and unsupported expansion names are not exhaustively
  validated; restricted publishable responses are not a complete Stripe.js schema.
- Event coverage is focused on modeled effects. Synthetic PM creation, webhook
  creation, and file creation events are additionally exposed for local mutation
  tracing. The hub's generic retry schedule differs from Stripe's production
  retry schedule; event API version is the triggering request version.
- Injected faults are shaped by the provider and carry Stripe headers, but the
  core fault callback has no request argument, so their `Stripe-Version` header
  uses the default rather than echoing a custom request version.

## Verification and references

```powershell
uv run pytest tests/providers/stripe -q
$env:EMULATORS_PROVIDERS = 'stripe'
uv run pytest tests/sdk/test_stripe_sdk.py -q
uv run ruff check src/emulators/providers/stripe tests/providers/stripe tests/sdk/test_stripe_sdk.py
uv run ruff format --check src/emulators/providers/stripe tests/providers/stripe tests/sdk/test_stripe_sdk.py
```

The official SDK contract suite covers classic resource calls and
`StripeClient.v1`, automatic pagination, error classes, idempotency, card tokens,
setup, capture, refunds, catalog, subscriptions, invoice payment, Checkout,
webhook configuration, multipart uploads, events, balance, and test clocks through
`emulator_intercept`. HTTP tests also verify official SDK webhook signatures,
retries, permission denials, prefix routing, real PDF bytes, and accounting effects.

The focused suites pass: **73 HTTP tests and 9 official SDK tests**. Ruff check and
format checks cover only the provider and its tests, respecting ownership of the
shared working tree.

A repository-wide run during concurrent provider development recorded **746 passed,
5 failed, 25 errors, and 2 skipped**. Its blockers were outside Stripe's allowed
edit paths: Box route initialization raised `UnboundLocalError` for `content`,
Dropbox was being edited and raised a syntax error, Zoom's acceptance flow returned
404, and a Microsoft notification test received five notifications instead of three.
The shared Stripe acceptance test's receiver returned 422 because its function-local
`Request` import and deferred annotation were interpreted as a required query
parameter. The provider recorded those failed deliveries correctly; the dedicated
HTTP and SDK receiver tests use a module-level annotation and verify delivery and
signatures successfully. A fresh temporary directory was used for that full run
to avoid the machine's locked default pytest temp directory. These cross-provider
and acceptance-fixture fixes belong to the coordinator.

Reference contracts: [PaymentIntents](https://docs.stripe.com/api/payment_intents/confirm),
[modern invoices](https://docs.stripe.com/api/invoices/object?api-version=2025-09-30.clover),
[subscriptions](https://docs.stripe.com/api/subscriptions/object?api-version=2025-09-30.clover),
[idempotency](https://docs.stripe.com/api/idempotent_requests), and
[webhook signatures](https://docs.stripe.com/webhooks/signature).
