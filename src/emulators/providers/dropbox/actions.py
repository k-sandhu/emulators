"""``POST /_emulator/actions/*`` -- test-setup helpers beyond the plain control plane."""

from __future__ import annotations

from typing import Any

from emulators.core.errors import EmulatorError
from emulators.core.provider import Provider, ProviderContext
from emulators.providers.dropbox import model as m
from emulators.providers.dropbox import oauth


def find_account(ctx: ProviderContext, who: Any) -> dict[str, Any] | None:
    """A person id (``p018``), email, display name, account id or team member id."""
    if not who:
        return None
    text = str(who)
    return (
        m.account(ctx, text)
        or m.account_by_person(ctx, text)
        or m.account_by_email(ctx, text)
        or m.account_by_member_id(ctx, text)
        or next(
            (a for a in ctx.store.collection(m.ACCOUNTS) if a["display_name"].lower() == text.lower()), None
        )
    )


def mint_token(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Mint an access token without an OAuth dance.

    ``{"user": "<person id|email>", "team": false, "scopes": [...], "expires_in": 14400,
    "refresh_token": false, "client_id": "..."}``. ``team: true`` (or no ``user``) gives a team-level
    token bound to that admin (default Arjun Mehta); use it with ``Dropbox-API-Select-User``.
    """
    user = payload.get("user")
    team = bool(payload.get("team")) or bool(payload.get("app_only")) or not user
    acct = find_account(ctx, user)
    if acct is None and user:
        raise EmulatorError(404, {"error": f"unknown user '{user}'"})
    if acct is None:
        acct = next(
            a for a in ctx.store.collection(m.ACCOUNTS) if any(r["name"] == "Team admin" for r in a["roles"])
        )
    if team and not acct.get("is_team"):
        raise EmulatorError(400, {"error": "team tokens need a team member"})
    scopes = payload.get("scopes") or []
    if isinstance(scopes, str):
        scopes = scopes.split()
    body = oauth.mint(
        ctx,
        acct,
        client_id=payload.get("client_id", "emulator-client"),
        scopes=list(scopes),
        team=team,
        offline=bool(payload.get("refresh_token")),
        ttl=int(payload.get("expires_in", oauth.ACCESS_TTL)),
    )
    return body


def set_quota(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Cap one person's storage so uploads fail with ``path/insufficient_space``:
    ``{"user": "<email|id>", "allocated": 1048576}``."""
    acct = find_account(ctx, payload.get("user"))
    if acct is None:
        raise EmulatorError(404, {"error": f"unknown user '{payload.get('user')}'"})
    acct["space_allocated"] = int(payload["allocated"])
    return {"account_id": acct["id"], "allocated": acct["space_allocated"]}


def register(provider: Provider, ctx: ProviderContext) -> None:
    from emulators.providers.dropbox import file_requests, webhooks

    provider.actions["mint_token"] = mint_token
    provider.actions["set_quota"] = set_quota
    provider.actions["simulate_file_request_upload"] = file_requests.simulate_upload
    provider.actions["configure_webhook"] = webhooks.configure_webhook
    provider.actions["remove_webhook"] = webhooks.remove_webhook
    provider.actions["list_webhooks"] = webhooks.list_webhooks
    provider.actions["trigger_webhook"] = webhooks.trigger_webhook
