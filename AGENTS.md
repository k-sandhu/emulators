# AGENTS.md — contract for humans and coding agents

This file is the single source of truth for how work is done in this repo. `CLAUDE.md`
is only a pointer to it.

## What this repo is

High-fidelity local emulators of SaaS APIs (Microsoft Graph and identity, Google
Workspace, Box, Dropbox, Stripe, Twilio, WhatsApp and Messenger, Salesforce, QuickBooks
Online, Zoom, Slack…). Consumers run integration tests against them instead of real
vendor accounts. **Wire compatibility is the product.** If an official SDK or a
hand-written httpx client that works against the real vendor does not work unchanged
against the emulator, that is a bug.

## Commands

```bash
uv sync                                    # install (dev + sdk groups)
uv run pytest -q                           # all tests
uv run pytest tests/providers/<name> -q    # one provider
uv run ruff check . && uv run ruff format --check .
uv run emulators serve                     # gateway on :44080, HTTPS :44443, per-provider ports
```

## Layout

```
src/emulators/
  core/        framework: hub, provider contract, store, auth (OAuth/JWT), webhooks,
               middleware (journal, faults), odata, clock, ids, errors, app factory
  world/       the shared synthetic organisation (model.py, generator.py, data.py,
               content.py) + real file generators (documents.py, media.py)
  providers/   one package per vendor; each exposes PROVIDER = <Provider subclass>()
  testing/     in-process httpx transport, live server, interceptors, pytest plugin
tests/
  core/  world/  providers/<name>/  sdk/   (sdk = official vendor SDK contract tests)
docs/
  architecture.md  providers/<name>.md  howto/  decisions/
```

## Architecture in one screen

* `EmulatorHub` discovers `emulators.providers.*`, builds one `World` from the seed and
  anchor date, gives each provider a `ProviderContext` (store, clock, ids, tokens,
  webhooks, journal, faults, world, settings) and calls `provider.seed(ctx)` once. The
  seeded store is snapshotted; `reset` restores it.
* Routing: requests are matched by **real vendor hostname** first (`Host:
  graph.microsoft.com`), then by path prefix (`/microsoft/...`). So consumers keep
  their hard-coded `https://` URLs and swap only the transport (or DNS/CA in Docker).
* Every provider app also serves the control plane under `/_emulator/*` (reset, state,
  request journal, faults, token minting, webhook journal, actions, docs).
* `World` is the same fictional firm everywhere: *Halcyon Advisory Group*
  (`halcyonadvisory.example`), 45 active employees in an org chart, 24 clients with
  contacts, documents with folder hierarchy and ACLs, meetings with transcripts, email
  threads, invoices, bills, CRM deals, card customers, SMS/WhatsApp/Messenger threads.

## Rules for provider authors

1. **Match the vendor exactly.** Paths, HTTP methods, query parameters, request body
   formats (JSON vs form-encoded vs multipart), headers, status codes, JSON field names,
   casing, types, null-vs-absent behaviour, id formats, timestamps formats, pagination
   style and error envelopes must follow the vendor's public API reference. When you
   are unsure, follow the official reference/OpenAPI spec, not intuition. Record any
   deliberate gap in `docs/providers/<name>.md` under **Known gaps**.
2. **Seed from `ctx.world`, deterministically.** Never invent people or companies that
   are not in the world (vendor-specific extras such as Stripe test cards are fine). Ids
   must be stable across runs: derive them from world ids (`stable_guid(...)`,
   `stable_hash(...)`) or from `ctx.ids` used in a fixed order.
3. **Hierarchy and permissions are first-class.** Model folder trees, org charts
   (manager/direct reports), group membership, org units, sharing/ACLs and roles.
   Enforce them: a *delegated* token only sees what its user can see; an *app-only*
   token sees what the vendor allows apps to see. Return the vendor's 403/404 when not.
4. **Auth like the vendor.** Implement the vendor's OAuth/token endpoints with
   `ctx.tokens` (lenient clients by default). Also accept tokens minted through
   `POST /_emulator/tokens` (override the default with a `mint_token` action if the
   vendor needs JWTs or special formats). Missing/invalid/expired → vendor-shaped 401.
5. **Absolute URLs** (next links, download URLs, `self` links) come from
   `external_base(request)` or `url_for_host(request, ctx, "real.host", path)` so they
   work in host mode, prefix mode and per-port mode.
6. **Pagination exactly as the vendor does it** (`@odata.nextLink`, `nextPageToken`,
   `cursor`/`has_more`, `next_page_uri`, `nextRecordsUrl`…), with the vendor's default
   and maximum page sizes.
7. **Webhooks** go through `ctx.webhooks.deliver(...)` with the vendor's signature
   scheme. Validation handshakes use `ctx.webhooks.request(...)`. Expose simulations as
   actions: `@self.action("inbound_sms")` → `POST /_emulator/actions/inbound_sms`.
8. **Binary content** lives in `ctx.store.blobs` (content-addressed). Use
   `emulators.world.documents.render(...)` for Office/PDF/CSV/MD/PNG files and
   `emulators.world.media` for WAV/M4A/MP4/VTT. Render lazily or at seed time — keep a
   full seed under ~2 seconds per provider.
9. **Errors** are raised as `EmulatorError(status, body)` with the vendor envelope;
   override `Provider.error_body` so 404s, validation errors and injected faults look
   right too.
10. **Stay in your lane.** Edit only your provider package, its tests and its doc page.
    Do not edit `pyproject.toml`, `uv.lock`, `core/`, `world/`, `testing/` or another
    provider — if you need a change there, say so in your report. No new runtime
    dependencies. No network calls other than webhook deliveries. Never run git.
11. **Tests**: `tests/providers/<name>/` drives the emulator through real HTTP
    semantics with `httpx.AsyncClient(transport=hub.async_transport())` and the vendor's
    real hostnames (see `tests/conftest.py`). `tests/sdk/test_<name>_sdk.py` drives the
    **official SDK** (dependency group `sdk`) through the `emulator_intercept` fixture —
    the strongest proof of wire compatibility. Mark them `@pytest.mark.sdk`.
12. **Docs**: `docs/providers/<name>.md` — hosts, auth flows and test credentials,
    endpoint table (method, path, notes), seeded data summary with example ids/emails,
    webhooks and signature scheme, actions, known gaps.

## Done means

* `uv run ruff check` and `uv run ruff format --check` are clean on the files you touched;
* your provider tests and SDK tests pass, and the full suite still passes;
* the provider doc page is written;
* your final report has four headings: **Changed / Verified / Deferred / Needs**.

## Style

`from __future__ import annotations`; full type hints; short docstrings that explain
*why*; line length 110; module names snake_case; prefer plain dicts in the vendor's wire
shape inside the store (so routes can return records with minimal mapping); no
`print`; never log tokens or secrets.

## Git

Conventional Commits (`feat(microsoft): …`, `fix(core): …`, `docs: …`). Commits end with
the co-author trailer used by the harness. Only the coordinator commits and pushes.

## Safety

No real credentials, no real personal data. All emails use `*.example`, all phone
numbers fictional ranges (NANP 555-01XX, UK 020 7946 0XXX / 07700 900XXX). Webhook
targets are whatever the test configures; the emulators never call real vendor APIs.
