"""Webhooks and long-polling.

Dropbox webhooks are *notifications*, not payloads: after files change for an account, the
registered URL receives ``{"list_folder": {"accounts": [...]}, "delta": {"users": [...]}}`` signed
with ``X-Dropbox-Signature`` (hex HMAC-SHA256 of the body, keyed with the app secret) and the app
then calls ``list_folder/continue`` itself. Registration verifies the URL with a ``?challenge=``
GET that must be echoed back.

Webhooks are registered per app (``app_key`` = OAuth ``client_id``) through the
``configure_webhook`` action; changes notify the accounts that authorised that app (a team token
covers every team member).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI

from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext
from emulators.core.webhooks import hmac_sha256_hex
from emulators.providers.dropbox import model as m
from emulators.providers.dropbox.common import Args, Pager, api_error, tag, ts
from emulators.providers.dropbox.dispatch import route
from emulators.providers.dropbox.files_core import change_visible, in_scope, scope_list
from emulators.providers.dropbox.model import Actor, Call

DEFAULT_LONGPOLL_CAP = 2.0


def register(app: FastAPI, ctx: ProviderContext) -> None:  # routes live in the shared registry
    return None


# ---------------------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------------------
def authorised_accounts(ctx: ProviderContext, app_key: str) -> set[str]:
    """Account ids that granted ``app_key`` (``*`` = any app) a live token."""
    accounts: set[str] = set()
    for record in ctx.store.collection("_grants"):
        if record["kind"] not in ("access", "refresh") or record.get("revoked"):
            continue
        if app_key != "*" and record["client_id"] != app_key:
            continue
        acct = m.account_by_person(ctx, record.get("user_id") or "")
        if acct is None:
            continue
        if record.get("extra", {}).get("team"):
            accounts.update(a["id"] for a in ctx.store.collection(m.ACCOUNTS) if a.get("is_team"))
        else:
            accounts.add(acct["id"])
    return accounts


def accounts_with_access(ctx: ProviderContext, ns_ids: set[str]) -> set[str]:
    out: set[str] = set()
    namespaces = ctx.store.collection(m.NAMESPACES)
    for acct in ctx.store.collection(m.ACCOUNTS):
        actor = Actor(account=acct, root_ns=acct["home_ns"])
        if any(m.ns_role(actor, namespaces.get(ns_id)) for ns_id in ns_ids):
            out.add(acct["id"])
    return out


async def notify(c: Call) -> None:
    """Deliver change notifications for every namespace the request touched."""
    ctx = c.ctx
    hooks = ctx.store.collection(m.WEBHOOKS).values()
    if not hooks:
        return
    affected = accounts_with_access(ctx, set(c.touched))
    await notify_accounts(ctx, affected)


async def notify_accounts(
    ctx: ProviderContext, affected: set[str], *, only_app: str | None = None
) -> list[dict[str, Any]]:
    delivered: list[dict[str, Any]] = []
    for hook in ctx.store.collection(m.WEBHOOKS).values():
        if only_app is not None and hook["id"] != only_app:
            continue
        allowed = authorised_accounts(ctx, hook["id"])
        accounts = sorted(affected & allowed)
        if not accounts:
            continue
        users = [m.account(ctx, a)["uid"] for a in accounts]  # type: ignore[index]
        body = json.dumps(
            {"list_folder": {"accounts": accounts}, "delta": {"users": users}}, separators=(",", ":")
        ).encode()
        signature = hmac_sha256_hex(hook["secret"], body)
        record = await ctx.webhooks.deliver(
            hook["url"],
            body,
            headers={"X-Dropbox-Signature": signature, "User-Agent": "DropboxWebhooks/1.0"},
            event_type="file_changes",
            metadata={"app_key": hook["id"], "accounts": accounts},
        )
        delivered.append(record)
    return delivered


# ---------------------------------------------------------------------------------------
# configure_webhook & friends (control plane actions)
# ---------------------------------------------------------------------------------------
async def configure_webhook(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Register (and verify) a webhook: ``{"url", "app_secret", "app_key": "*"}``.

    Dropbox verifies the URL by sending ``GET url?challenge=<random>``; the endpoint must reply 200
    with the challenge as the body. The ``app_key`` ties the webhook to an OAuth client id (default
    ``*`` = every app).
    """
    url = payload.get("url")
    secret = payload.get("app_secret") or payload.get("secret")
    if not url or not secret:
        raise EmulatorError(400, {"error": "url and app_secret are required"})
    challenge = m.new_token(ctx, "challenge", 32)
    try:
        parts = urlsplit(url)
        query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "challenge"]
        verification_url = urlunsplit(parts._replace(query=urlencode([*query, ("challenge", challenge)])))
        response = await ctx.webhooks.request("GET", verification_url)
    except Exception as exc:  # network / transport failures surface as a verification failure
        raise EmulatorError(
            400, {"error": f"webhook verification failed: {type(exc).__name__}: {exc}"}
        ) from None
    if response.status_code != 200 or response.text != challenge:
        raise EmulatorError(
            400,
            {
                "error": "webhook verification failed: the URL must respond 200 with the challenge as the body",
                "status": response.status_code,
            },
        )
    app_key = payload.get("app_key") or "*"
    record = {"id": app_key, "url": url, "secret": secret, "created": ts(m.now(ctx))}
    ctx.store.collection(m.WEBHOOKS).put(record)
    return {"id": app_key, "url": url, "app_key": app_key, "verified": True}


def remove_webhook(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Remove the webhook registered for ``app_key`` (default ``*``)."""
    key = payload.get("app_key") or payload.get("id") or "*"
    return {"removed": ctx.store.collection(m.WEBHOOKS).delete(key) is not None}


def list_webhooks(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """List registered webhooks (secrets omitted)."""
    return {
        "webhooks": [
            {"app_key": h["id"], "url": h["url"], "created": h.get("created")}
            for h in ctx.store.collection(m.WEBHOOKS)
        ]
    }


async def trigger_webhook(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
    """Send a notification now: ``{"accounts": ["dbid:..." | email | person id], "app_key": "*"}``
    (default: every account that authorised the app)."""
    from emulators.providers.dropbox.actions import find_account

    accounts: set[str] = set()
    for who in payload.get("accounts") or []:
        acct = find_account(ctx, who)
        if acct is not None:
            accounts.add(acct["id"])
    if not accounts:
        accounts = {a["id"] for a in ctx.store.collection(m.ACCOUNTS)}
    records = await notify_accounts(ctx, accounts, only_app=payload.get("app_key"))
    return {
        "delivered": [
            {"id": r["id"], "status": r["status"], "accounts": r["metadata"]["accounts"]} for r in records
        ]
    }


# ---------------------------------------------------------------------------------------
# Long polling
# ---------------------------------------------------------------------------------------
def has_changes(ctx: ProviderContext, state: dict[str, Any]) -> bool:
    acct = m.account(ctx, state["a"])
    if acct is None:
        raise api_error(tag("reset"))
    actor = Actor(account=acct, admin=bool(state.get("adm")), root_ns=state["ns"])
    c = Call(
        ctx=ctx,
        request=None,
        route="files/list_folder/longpoll",
        args=Args({}),
        body=b"",
        actor=actor,
        request_id="",
    )  # type: ignore[arg-type]
    if state["ph"] == "list":
        return True
    ns = c.tree.namespace(state["ns"])
    if ns is None:
        raise api_error(tag("reset"))
    base = m.Loc(state["ns"], state["rel"], "", None, state["pl"], state["pd"], reachable=state["reach"])
    scopes = scope_list(c, base, recursive=state["rec"], mounted=state["mnt"])
    for ev in ctx.store.collection(m.CHANGES):
        if (
            ev["seq"] > state["seq"]
            and in_scope(ev, scopes, state["rec"]) is not None
            and change_visible(c, ev)
        ):
            return True
    return False


@route("files/list_folder/longpoll", auth="noauth", void_arg=False)
async def list_folder_longpoll(c: m.Call) -> Any:
    state = Pager.decode(c.args.str("cursor"), c)
    timeout = c.args.int("timeout", 30, lo=30, hi=480)
    if not state or state.get("v") != 1:
        raise api_error(tag("reset"))
    if state["seq"] > m.current_seq(c.ctx):
        raise api_error(tag("reset"))
    cap = float(c.ctx.setting("LONGPOLL_MAX_WAIT_S", str(DEFAULT_LONGPOLL_CAP)) or DEFAULT_LONGPOLL_CAP)
    deadline = min(float(timeout), cap)
    waited = 0.0
    while True:
        if has_changes(c.ctx, state):
            return {"changes": True}
        if waited >= deadline:
            return {"changes": False}
        await asyncio.sleep(0.05)
        waited += 0.05
