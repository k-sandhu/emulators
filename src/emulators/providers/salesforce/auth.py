"""Sessions and tokens: authenticating REST calls, minting Salesforce-shaped credentials."""

from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any

from starlette.requests import Request

from emulators.core.auth import Grant, bearer_token
from emulators.core.clock import utc
from emulators.core.provider import ProviderContext
from emulators.providers.salesforce.common import (
    PASSWORD,
    SECURITY_TOKEN,
    SfError,
    invalid_session,
)
from emulators.providers.salesforce.org import Org, Record, Session

SESSION_TTL = timedelta(hours=2)
_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _rand(n: int) -> str:
    return "".join(secrets.choice(_ALPHABET) for _ in range(n))


def new_access_token(org: Org) -> str:
    """``00D5g0000049673!AQ0AQ...`` — 15-char org id, ``!``, then an opaque session id."""
    return f"{org.org_id15}!AQ{secrets.choice('0456')}AQ{_rand(86)}"


def new_refresh_token() -> str:
    return "5Aep861" + _rand(80)


def new_code() -> str:
    return "aPrx" + _rand(80)


def integration_user(org: Org) -> Record:
    for rec in org.coll("User"):
        if rec.get("_person_id") == "integration":
            return rec
    raise SfError(500, "UNKNOWN_EXCEPTION", "integration user missing")


def issue_session(
    ctx: ProviderContext,
    org: Org,
    user: Record,
    *,
    client_id: str,
    scopes: list[str],
    ttl: timedelta | None = SESSION_TTL,
    refresh: str | None = None,
    app_only: bool = False,
) -> Grant:
    user["LastLoginDate"] = org.now_str()
    user["NumberOfFailedLogins"] = 0
    return ctx.tokens.issue_access(
        token=new_access_token(org),
        client_id=client_id,
        user_id=user["Id"],
        scopes=scopes,
        ttl=ttl,
        app_only=app_only,
        extra={"refresh_token": refresh} if refresh else {},
    )


def authenticate(ctx: ProviderContext, org: Org, request: Request) -> Session:
    """Resolve ``Authorization: Bearer|OAuth <token>`` to a live session or raise INVALID_SESSION_ID."""
    token = bearer_token(request)
    grant = ctx.tokens.resolve(token) if token else None
    if grant is None:
        raise invalid_session()
    user = org.find_user(grant.user_id) if grant.user_id else integration_user(org)
    if user is None or not user.get("IsActive"):
        raise invalid_session()
    return Session(token=token or "", grant=grant, user=user)


def check_password(org: Org, ctx: ProviderContext, user: Record, supplied: str) -> bool:
    """Passwords are ``Halcyon2026!`` (+ optional ``EMULATORTOKEN``) unless changed via the API."""
    if ctx.setting("LENIENT_PASSWORDS", "").lower() in ("1", "true", "yes"):
        return True
    password = org.meta.get("passwords", {}).get(user["Id"], PASSWORD)
    return supplied in (password, password + SECURITY_TOKEN)


def record_failed_login(user: Record | None) -> None:
    if user is not None:
        user["NumberOfFailedLogins"] = int(user.get("NumberOfFailedLogins") or 0) + 1


def user_by_login(org: Org, username: str | None) -> Record | None:
    if not username:
        return None
    lowered = username.strip().lower()
    for rec in org.coll("User"):
        if str(rec.get("Username", "")).lower() == lowered or str(rec.get("Email", "")).lower() == lowered:
            return rec
    return None


def count_call(ctx: ProviderContext, org: Org, limit: int) -> None:
    """One more REST call against ``DailyApiRequests``; 403 REQUEST_LIMIT_EXCEEDED past the limit."""
    meta = org.meta
    meta["api_calls"] = int(meta.get("api_calls", 0)) + 1
    if meta["api_calls"] > limit:
        raise SfError(403, "REQUEST_LIMIT_EXCEEDED", "TotalRequests Limit exceeded.")


def limit_info(org: Org, limit: int) -> str:
    return f"api-usage={min(int(org.meta.get('api_calls', 0)), limit)}/{limit}"


def expires_in(grant: Grant, now: Any) -> int:
    if grant.expires_at is None:
        return 0
    return max(0, int((utc(grant.expires_at) - now).total_seconds()))
