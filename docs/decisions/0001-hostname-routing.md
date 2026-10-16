# 0001 — Route by real vendor hostname, not by base-URL override

* Status: accepted
* Date: 2026-10-16

## Context

Every consumer we surveyed (parlance, lumen-copilot, saas_backbone) calls vendors with raw httpx
against hard-coded `https://` constants, pins hostnames in allowlists, checks that pagination links
stay on the vendor host, and refuses private IPs. Almost none of them expose a base-URL setting. All
of them do expose an httpx `transport=` seam for tests.

## Decision

Each provider declares the real hostnames it serves (wildcards allowed). The hub gateway routes by
`Host` (or `X-Forwarded-Host`) first and by `/<provider>` path prefix second. Generated absolute
URLs are built from the base the client used. We ship:

* an in-process httpx transport (no network) for Python tests;
* interceptors for httpx/requests/urllib/httplib2/aiohttp to reroute official SDKs;
* an HTTPS gateway with a generated CA for containers and non-Python clients;
* path-prefix and per-port modes for clients that do allow a base URL.

## Consequences

* Consumers test their *production* URL handling, allowlists and link validation unchanged.
* Hostname collisions between providers are impossible to ignore (they would be routing bugs).
* Apps with SSRF guards still need a test-only switch when talking to the emulators over a network;
  the in-process transport avoids that entirely.
