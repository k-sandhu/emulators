# Webhooks, simulations, faults and time

## Webhooks

Every emulator that has webhooks signs deliveries exactly like the vendor, so your verification
code is exercised for real:

| Provider | How you register | Signature |
|---|---|---|
| Stripe | `POST /v1/webhook_endpoints` (returns `whsec_…`) | `Stripe-Signature: t=…,v1=HMAC-SHA256(secret, "t.payload")` |
| Microsoft Graph | `POST /v1.0/subscriptions` (validationToken handshake) | `clientState` echo |
| Google Drive / Calendar | `files.watch`, `changes.watch`, `events.watch` | `X-Goog-Channel-Token` |
| Gmail | `users.watch` + action `configure_pubsub_push` | Pub/Sub push envelope |
| Zoom | action `configure_webhook` (url validation handshake) | `x-zm-signature: v0=HMAC-SHA256(secret, "v0:ts:body")` |
| Slack | action `configure_events` (url_verification) | `X-Slack-Signature: v0=…` |
| Twilio | number `SmsUrl` / `StatusCallback` | `X-Twilio-Signature` (HMAC-SHA1 of URL + sorted params) |
| WhatsApp / Messenger | action `configure_webhook` (hub.challenge) | `X-Hub-Signature-256: sha256=…` |
| Dropbox | action `configure_webhook` (challenge) | `X-Dropbox-Signature` |
| Box | `POST /2.0/webhooks` | `BOX-SIGNATURE-PRIMARY/SECONDARY` |
| QuickBooks / Xero / HubSpot / Webex | action `configure_webhook` | `intuit-signature` / `x-xero-signature` / `X-HubSpot-Signature-v3` / `X-Spark-Signature` |

Delivery modes (`EMULATORS_WEBHOOK_MODE` or `EmulatorHub(webhook_mode=…)`):

* `async` — background delivery with retries (default for `emulators serve`);
* `sync` — the API call that caused the event waits for delivery (deterministic tests);
* `manual` — deliveries queue until `POST /_emulator/webhooks/flush`.

Inspect deliveries: `GET https://<vendor host>/_emulator/webhooks?event_type=…&status=failed`.

## Simulating the outside world

`GET /_emulator/actions` lists each provider's simulations. Examples:

```bash
# A client replies by SMS (Twilio posts to your SmsUrl webhook, TwiML replies are sent back)
curl -X POST localhost:44080/twilio/_emulator/actions/inbound_sms \
  -d '{"from": "+16175550142", "to": "+14165550188", "body": "STOP"}' -H 'content-type: application/json'

# A Zoom meeting ends: recordings + transcript appear, recording.completed is delivered
curl -X POST localhost:44080/zoom/_emulator/actions/end_meeting -d '{"meeting_id": "…"}' -H 'content-type: application/json'

# A WhatsApp customer writes in (opens the 24-hour window)
curl -X POST localhost:44080/meta/_emulator/actions/inbound_whatsapp -d '{"from": "c003-1", "text": "Hi!"}' -H 'content-type: application/json'
```

## Faults

```bash
curl -X POST localhost:44080/microsoft/_emulator/faults -H 'content-type: application/json' \
  -d '{"path": "/v1.0/me/drive/root/delta", "status": 429, "times": 2, "headers": {"Retry-After": "1"}}'
```

Fields: `path` (regex), `method`, `status` (null = latency only), `body` (defaults to the vendor's
error envelope), `headers`, `times` (null = forever), `after` (let N matches through first),
`probability`, `delay_ms`. `DELETE /_emulator/faults` clears them; `reset` clears them too.

## Time

All emulators share one clock. Freeze or move it to test expiry and schedules:

```bash
curl -X POST localhost:44080/_emulator/clock -d '{"advance_seconds": 3700}' -H 'content-type: application/json'   # tokens expire
curl -X POST localhost:44080/_emulator/clock -d '{"freeze_at": "2026-11-01T09:00:00Z"}' -H 'content-type: application/json'
```

Stripe also has test clocks and an `advance_time` action that renews subscriptions.

## Request journal

`GET /_emulator/requests?path=/v2/users&method=GET` returns what your code sent (headers with
secrets redacted, decoded bodies, status, latency) — assert on it instead of mocking.
