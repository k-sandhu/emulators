# 0002 — One deterministic synthetic organisation for every provider

* Status: accepted
* Date: 2026-10-16

## Context

Integration tests often cross systems: match a Zoom participant to a directory user, file a
Salesforce opportunity's documents into the right Box folder, reconcile Stripe charges with
QuickBooks payments, attach a Teams transcript to a CRM account. Independent random fixtures per
provider make those tests impossible or meaningless.

## Decision

`emulators.world` builds one immutable `World` — *Halcyon Advisory Group* — from a seed and an
anchor date. Every provider's `seed(ctx)` projects it into the vendor's data model with ids derived
from world ids. People, clients and documents therefore line up across providers. All personal
data is fictional; emails use the reserved `.example` TLD and phone numbers use fictional ranges.

## Consequences

* Cross-system tests work out of the box and can hard-code names, emails and (per seed) ids.
* Changing the generator changes many providers' data at once — tests should prefer looking
  records up by stable attributes (email, name) over positional assumptions.
* Dates move with the anchor, so data always looks recent; CI pins `EMULATORS_ANCHOR`.
