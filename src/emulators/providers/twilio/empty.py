"""Collections the account resource advertises in ``subresource_uris`` that the emulator keeps empty.

A client that walks ``subresource_uris`` (or lists ``Notifications``/``Queues``/``Conferences``...)
gets a correctly shaped, empty page instead of a 404, exactly like a fresh Twilio account.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Request

from emulators.providers.twilio.common import (
    API_VERSION,
    CONVERSATIONS_HOST,
    MESSAGING_HOST,
    VERIFY_HOST,
    check_account,
    not_found,
    page_2010,
    page_meta,
    require_auth,
    route_path,
)

if TYPE_CHECKING:
    from emulators.core.provider import ProviderContext

# (path under /Accounts/{sid}, JSON list key)
ACCOUNT_COLLECTIONS: tuple[tuple[str, str], ...] = (
    ("Notifications", "notifications"),
    ("Conferences", "conferences"),
    ("Queues", "queues"),
    ("Addresses", "addresses"),
    ("Applications", "applications"),
    ("OutgoingCallerIds", "outgoing_caller_ids"),
    ("ConnectApps", "connect_apps"),
    ("AuthorizedConnectApps", "authorized_connect_apps"),
    ("Transcriptions", "transcriptions"),
    ("SMS/ShortCodes", "short_codes"),
    ("Usage/Triggers", "usage_triggers"),
)
# (path under /Accounts/{sid}/{Parent}/{sid}, JSON list key) for calls and recordings.
CALL_COLLECTIONS: tuple[tuple[str, str], ...] = (
    ("Notifications", "notifications"),
    ("Transcriptions", "transcriptions"),
    ("Payments", "payments"),
    ("Events", "events"),
)
RECORDING_COLLECTIONS: tuple[tuple[str, str], ...] = (
    ("AddOnResults", "add_on_results"),
    ("Transcriptions", "transcriptions"),
)
# v1/v2 children advertised in a resource's ``links``: (parent collection, URL prefix, host, children).
LINKED_COLLECTIONS: tuple[tuple[str, str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "messaging_services",
        "/v1/Services",
        MESSAGING_HOST,
        (
            ("ShortCodes", "short_codes"),
            ("AlphaSenders", "alpha_senders"),
            ("ChannelSenders", "senders"),
            ("DestinationAlphaSenders", "alpha_senders"),
            ("Compliance/Usa2p", "compliance"),
            ("Compliance/Usa2p/Usecases", "us_app_to_person_usecases"),
        ),
    ),
    (
        "verify_services",
        "/v2/Services",
        VERIFY_HOST,
        (
            ("RateLimits", "rate_limits"),
            ("MessagingConfigurations", "messaging_configurations"),
            ("Entities", "entities"),
            ("Webhooks", "webhooks"),
        ),
    ),
    ("conversations", "/v1/Conversations", CONVERSATIONS_HOST, (("Webhooks", "webhooks"),)),
)


def register(app: FastAPI, ctx: ProviderContext) -> None:
    base = f"/{API_VERSION}/Accounts/{{account_sid}}"

    def make(key: str, parent: str | None) -> Any:
        async def list_empty(request: Request, account_sid: str, sid: str = "") -> Any:
            require_auth(request, ctx)
            account = check_account(ctx, account_sid, request)
            if parent is not None:
                record = ctx.store.collection(parent.lower()).get(sid)
                if record is None or record.get("_account") != account:
                    raise not_found(route_path(request.scope))
            return page_2010(request, key, [], route_path(request.scope))

        return list_empty

    for suffix, key in ACCOUNT_COLLECTIONS:
        app.get(f"{base}/{suffix}.json")(make(key, None))
    for parent, entries in (("Calls", CALL_COLLECTIONS), ("Recordings", RECORDING_COLLECTIONS)):
        for suffix, key in entries:
            app.get(f"{base}/{parent}/{{sid}}/{suffix}.json")(make(key, parent))

    def make_linked(collection: str, prefix: str, host: str, key: str) -> Any:
        async def list_linked(request: Request, sid: str) -> Any:
            auth = require_auth(request, ctx)
            record = ctx.store.collection(collection).get(sid)
            if record is None and collection == "conversations":
                record = ctx.store.collection(collection).first(unique_name=sid)
            if record is None or record["account_sid"] != auth.account_sid:
                raise not_found(f"{prefix}/{sid}")
            return page_meta(request, ctx, host, key, [], route_path(request.scope))

        return list_linked

    for collection, prefix, host, children in LINKED_COLLECTIONS:
        for suffix, key in children:
            app.get(f"{prefix}/{{sid}}/{suffix}")(make_linked(collection, prefix, host, key))
