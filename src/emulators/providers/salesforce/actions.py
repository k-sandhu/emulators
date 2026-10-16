"""``/_emulator/actions/*`` for Salesforce: token minting, quota control, outbound messages, lead
conversion, undelete and session expiry."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from emulators.core.errors import EmulatorError
from emulators.core.provider import Provider, ProviderContext, ctx_url_for_host
from emulators.providers.salesforce import auth as sfauth
from emulators.providers.salesforce.common import INSTANCE_HOST, LOGIN_HOST, SfError, norm_id
from emulators.providers.salesforce.dml import Dml, Txn
from emulators.providers.salesforce.opp_logic import stage_info
from emulators.providers.salesforce.routes import api_for
from emulators.providers.salesforce.sharing import Access


def _fail(status: int, message: str) -> EmulatorError:
    return EmulatorError(status, {"error": message})


def _int(payload: dict[str, Any], key: str, default: int | None = None, *, minimum: int = 0) -> int | None:
    value = payload.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise _fail(400, f"'{key}' must be an integer")
    if isinstance(value, float) and not value.is_integer():
        raise _fail(400, f"'{key}' must be an integer")
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise _fail(400, f"'{key}' must be an integer") from None
    if number < minimum:
        raise _fail(400, f"'{key}' must be at least {minimum}")
    return number


def _text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is not None and not isinstance(value, str):
        raise _fail(400, f"'{key}' must be a string")
    return value


def install(provider: Provider) -> None:
    @provider.action("mint_token")
    def mint_token(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Mint a Salesforce-format session token: ``{user, scopes, client_id, app_only, expires_in}``.

        ``user`` is a world person id (``p012``), email, Username or Salesforce user Id; without it (or with
        ``app_only``) the integration user is used.
        """
        org = api_for(ctx).org
        ident = _text(payload, "user")
        user = org.find_user(ident) if ident and not payload.get("app_only") else sfauth.integration_user(org)
        if user is None:
            raise _fail(404, f"unknown user '{ident}'")
        ttl = _int(payload, "expires_in", 7200, minimum=1) or 7200
        try:
            lifetime = timedelta(seconds=ttl)
            ctx.clock.now() + lifetime
        except OverflowError as exc:
            raise _fail(400, "'expires_in' is outside the supported range") from exc
        app_only = payload.get("app_only", False)
        if not isinstance(app_only, bool):
            raise _fail(400, "'app_only' must be a boolean")
        scopes = payload.get("scopes", ["api", "full"])
        if not isinstance(scopes, list) or not all(isinstance(s, str) and s for s in scopes):
            raise _fail(400, "'scopes' must be a list of scope names")
        grant = sfauth.issue_session(
            ctx,
            org,
            user,
            client_id=_text(payload, "client_id") or "emulator-client",
            scopes=scopes,
            ttl=lifetime,
            app_only=app_only,
        )
        return {
            "access_token": grant.token,
            "token_type": "Bearer",
            "expires_in": ttl,
            "scope": " ".join(scopes),
            "instance_url": ctx_url_for_host(ctx, INSTANCE_HOST, "").rstrip("/"),
            "id": ctx_url_for_host(ctx, LOGIN_HOST, f"/id/{org.org_id18}/{user['Id']}"),
            "user_id": user["Id"],
            "username": user["Username"],
        }

    @provider.action("set_api_usage")
    def set_api_usage(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Set DailyApiRequests ``{used, max}`` (or ``remaining``) to provoke ``REQUEST_LIMIT_EXCEEDED``."""
        meta = api_for(ctx).org.meta
        maximum = _int(payload, "max", None, minimum=1)
        remaining = _int(payload, "remaining", None)
        used = _int(payload, "used", None)
        if maximum is not None:
            meta["api_limit"] = maximum
        limit = int(meta.get("api_limit") or ctx.setting("DAILY_API_LIMIT") or 15000)
        if remaining is not None:
            meta["api_calls"] = max(0, limit - remaining)
        if used is not None:
            meta["api_calls"] = used
        return {"used": meta["api_calls"], "max": limit, "remaining": max(0, limit - meta["api_calls"])}

    @provider.action("configure_outbound_message")
    def configure_outbound_message(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Register a SOAP outbound message: ``{object, url, events: [create|update|delete], fields, name}``."""
        if not payload.get("object") or not payload.get("url"):
            raise _fail(400, "'object' and 'url' are required")
        _text(payload, "object"), _text(payload, "url"), _text(payload, "name")
        try:
            return api_for(ctx).notifier.configure(
                payload["object"],
                payload["url"],
                payload.get("events"),
                payload.get("fields"),
                payload.get("name"),
            )
        except ValueError as exc:
            raise _fail(400, str(exc)) from exc

    @provider.action("list_outbound_messages")
    def list_outbound_messages(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """The configured outbound messages."""
        return {"outboundMessages": api_for(ctx).org.coll("_sf_outbound").values()}

    @provider.action("clear_outbound_messages")
    def clear_outbound_messages(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Remove every configured outbound message."""
        coll = api_for(ctx).org.coll("_sf_outbound")
        n = len(coll)
        coll.clear()
        return {"removed": n}

    @provider.action("expire_sessions")
    def expire_sessions(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Revoke every token of a user (``{user}``) so the next call returns INVALID_SESSION_ID."""
        org = api_for(ctx).org
        ident = _text(payload, "user")
        user = org.find_user(ident) if ident else None
        if user is None:
            raise _fail(404, f"unknown user '{payload.get('user')}'")
        return {"revoked": ctx.tokens.revoke_user(user["Id"])}

    @provider.action("undelete")
    async def undelete(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Restore a record from the recycle bin: ``{id}``."""
        api = api_for(ctx)
        found = None
        rid = norm_id(payload.get("id"))
        if rid is not None:
            from emulators.providers.salesforce.schema import SCHEMA

            obj = SCHEMA.for_id(rid)
            rec = api.org.coll(obj.name).get(rid) if obj else None
            found = (obj, rec) if obj and rec else None
        if not found:
            raise _fail(404, f"no such record '{payload.get('id')}'")
        txn = Txn()
        try:
            admin = sfauth.integration_user(api.org)
            Dml(api.org, Access(api.org, admin), txn, system=True).undelete(found[0].name, rid)
        except SfError as exc:
            raise _fail(exc.status, exc.message) from exc
        await api.notifier.flush(txn.events)
        return {"id": rid, "undeleted": True}

    @provider.action("convert_lead")
    async def convert_lead(ctx: ProviderContext, payload: dict[str, Any]) -> dict[str, Any]:
        """Convert a lead (the SOAP ``convertLead`` call): ``{lead_id, do_not_create_opportunity,
        opportunity_name, account_id, contact_id, converted_status}``."""
        api = api_for(ctx)
        org = api.org
        lead_id = norm_id(payload.get("lead_id"))
        lead = org.get("Lead", lead_id) if lead_id else None
        if lead is None or lead.get("IsDeleted"):
            raise _fail(404, "lead not found")
        if lead.get("IsConverted"):
            raise _fail(400, "lead is already converted")
        for key, obj_name in (("account_id", "Account"), ("contact_id", "Contact")):
            given = payload.get(key)
            if given is not None:
                target = org.get(obj_name, norm_id(given)) if norm_id(given) else None
                if target is None or target.get("IsDeleted"):
                    raise _fail(404, f"{key} '{given}' is not an existing {obj_name}")
                payload[key] = target["Id"]
        _text(payload, "opportunity_name"), _text(payload, "converted_status")
        status = payload.get("converted_status") or "Closed - Converted"
        converted = [r for r in org.coll("LeadStatus") if r["MasterLabel"] == status and r.get("IsConverted")]
        if not converted:
            raise _fail(400, f"'{status}' is not a converted lead status")
        owner = org.user(lead["OwnerId"]) or sfauth.integration_user(org)
        txn = Txn()
        dml = Dml(org, Access(org, owner), txn, system=True)
        try:
            account_id = payload.get("account_id")
            if account_id is None:
                account_id = dml.insert(
                    "Account",
                    {
                        "Name": lead["Company"],
                        "Phone": lead.get("Phone"),
                        "Website": lead.get("Website"),
                        "Industry": lead.get("Industry"),
                        "Rating": lead.get("Rating"),
                        "AnnualRevenue": lead.get("AnnualRevenue"),
                        "NumberOfEmployees": lead.get("NumberOfEmployees"),
                        "BillingStreet": lead.get("Street"),
                        "BillingCity": lead.get("City"),
                        "BillingState": lead.get("State"),
                        "BillingPostalCode": lead.get("PostalCode"),
                        "BillingCountry": lead.get("Country"),
                        "AccountSource": lead.get("LeadSource"),
                        "Type": "Prospect",
                        "OwnerId": lead["OwnerId"],
                    },
                )["Id"]
            contact_id = payload.get("contact_id")
            if contact_id is None:
                contact_id = dml.insert(
                    "Contact",
                    {
                        "AccountId": account_id,
                        "LastName": lead["LastName"],
                        "FirstName": lead.get("FirstName"),
                        "Salutation": lead.get("Salutation"),
                        "Email": lead.get("Email"),
                        "Phone": lead.get("Phone"),
                        "MobilePhone": lead.get("MobilePhone"),
                        "Title": lead.get("Title"),
                        "LeadSource": lead.get("LeadSource"),
                        "MailingStreet": lead.get("Street"),
                        "MailingCity": lead.get("City"),
                        "MailingState": lead.get("State"),
                        "MailingPostalCode": lead.get("PostalCode"),
                        "MailingCountry": lead.get("Country"),
                        "OwnerId": lead["OwnerId"],
                    },
                )["Id"]
            opportunity_id = None
            if not payload.get("do_not_create_opportunity"):
                opportunity_id = dml.insert(
                    "Opportunity",
                    {
                        "Name": payload.get("opportunity_name") or f"{lead['Company']}-",
                        "AccountId": account_id,
                        "StageName": "Prospecting",
                        "CloseDate": (org.now() + timedelta(days=30)).date().isoformat(),
                        "LeadSource": lead.get("LeadSource"),
                        "OwnerId": lead["OwnerId"],
                    },
                )["Id"]
                dml.insert(
                    "OpportunityContactRole",
                    {
                        "OpportunityId": opportunity_id,
                        "ContactId": contact_id,
                        "Role": "Decision Maker",
                        "IsPrimary": True,
                    },
                )
        except SfError as exc:
            txn.rollback()
            raise _fail(exc.status, f"{exc.code}: {exc.message}") from exc
        txn.record("Lead", lead)
        lead.update(
            IsConverted=True,
            Status=status,
            ConvertedDate=org.now().date().isoformat(),
            ConvertedAccountId=account_id,
            ConvertedContactId=contact_id,
            ConvertedOpportunityId=opportunity_id,
            LastModifiedDate=org.now_str(),
            SystemModstamp=org.now_str(),
        )
        txn.events.append(("update", "Lead", lead["Id"]))
        await api.notifier.flush(txn.events)
        _ = stage_info
        return {
            "success": True,
            "leadId": lead["Id"],
            "accountId": account_id,
            "contactId": contact_id,
            "opportunityId": opportunity_id,
        }
