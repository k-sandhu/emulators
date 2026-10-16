"""Dynamic test subscriptions use Atlassian's HMAC signature header."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx
from fastapi import Request

from emulators.core.clock import parse_instant
from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext
from emulators.core.webhooks import hmac_sha256_hex

from .common import Record, api_url, clean, error_body, site_url

EVENTS = {"page_created", "page_updated", "jira:issue_created", "jira:issue_updated"}


def configure_webhook(ctx: ProviderContext, payload: Record) -> Record:
    """Subscribe a receiver to page_created/page_updated or Jira issue events."""
    url = payload.get("url", "")
    events = payload.get("events", sorted(EVENTS))
    try:
        valid_url = (
            isinstance(url, str) and urlsplit(url).scheme in ("http", "https") and bool(httpx.URL(url).host)
        )
    except (ValueError, httpx.InvalidURL):
        valid_url = False
    if not valid_url:
        raise EmulatorError(400, error_body(400, "An HTTP(S) url is required."))
    if (
        not isinstance(events, list)
        or not events
        or any(not isinstance(e, str) or e not in EVENTS for e in events)
    ):
        raise EmulatorError(400, error_body(400, "Unsupported webhook event."))
    if not isinstance(payload.get("secret", ""), str):
        raise EmulatorError(400, error_body(400, "secret must be a string."))
    record = {
        "id": ctx.ids.prefixed("hook", 16),
        "url": url,
        "events": events,
        "secret": payload.get("secret", ""),
    }
    ctx.store["_subscriptions"].put(record)
    return {k: v for k, v in record.items() if k != "secret"}


async def emit(
    ctx: ProviderContext,
    event: str,
    content: Record,
    user_id: str,
    *,
    request: Request,
    changelog: list[Record] | None = None,
) -> None:
    payload: Record = {"timestamp": ctx.clock.timestamp() * 1000}
    if event.startswith("jira:"):
        user = ctx.store["users"].get(user_id)
        payload.update(
            {
                "webhookEvent": event,
                "issue_event_type_name": "issue_created" if event.endswith("created") else "issue_updated",
                "user": {
                    "accountId": user_id,
                    "displayName": user["displayName"] if user else "",
                    "emailAddress": user["email"] if user else "",
                    "accountType": "atlassian",
                    "active": True,
                    "self": api_url(request, "/rest/api/3/user?accountId=" + user_id),
                },
                "issue": clean(content),
            }
        )
        if changelog is not None:
            payload["changelog"] = {"id": str(ctx.clock.timestamp()), "items": changelog}
    else:
        payload.update(
            {
                "userAccountId": user_id,
                "accountType": "atlassian",
                "page": {
                    "id": int(content["id"]),
                    "title": content["title"],
                    "spaceKey": ctx.store["spaces"].get(content["spaceId"])["key"],
                    "version": content["version"]["number"],
                    "contentType": "page",
                    "self": site_url(
                        request,
                        ctx,
                        "/wiki/spaces/"
                        + ctx.store["spaces"].get(content["spaceId"])["key"]
                        + "/pages/"
                        + content["id"],
                    ),
                    "creatorAccountId": content["authorId"],
                    "lastModifierAccountId": user_id,
                    "creationDate": int(parse_instant(content["createdAt"]).timestamp() * 1000),
                    "modificationDate": int(
                        parse_instant(content["version"]["createdAt"]).timestamp() * 1000
                    ),
                },
            }
        )
    body = json.dumps(payload, separators=(",", ":")).encode()
    for sub in ctx.store["_subscriptions"]:
        if event in sub["events"]:
            headers = {"X-Hub-Signature": "sha256=" + hmac_sha256_hex(sub["secret"], body)}
            if event.startswith("jira:"):
                headers.update(
                    {
                        "X-Atlassian-Webhook-Identifier": ctx.ids.guid(),
                        "X-Atlassian-Webhook-Flow": "Primary",
                    }
                )
                trace = request.headers.get("X-Atlassian-Webhook-Trace")
                if trace and len(trace) <= 1024 and all(32 <= ord(c) <= 126 for c in trace):
                    headers["X-Atlassian-Webhook-Trace"] = trace
            await ctx.webhooks.deliver(
                sub["url"],
                body,
                event_type=event,
                headers=headers,
            )
