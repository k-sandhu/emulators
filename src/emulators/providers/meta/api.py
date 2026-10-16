"""Graph API dispatch: versioned object/edge routing, OAuth, ``/me``, debug_token.

One catch-all handler serves ``graph.facebook.com`` (and ``www.facebook.com`` for
the OAuth dialog, ``lookaside.fbsbx.com`` for media downloads and
``*.fbcdn.net`` for profile pictures) because Meta's surface is a generic
object graph: ``/{object-id}/{edge}`` where the object kind decides the edges.
"""

from __future__ import annotations

import html
import json
from datetime import timedelta
from typing import Any, NoReturn
from urllib.parse import parse_qs, urlsplit

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from emulators.core.auth import OAuthError, redirect_with_params, render_account_picker
from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext
from emulators.providers.meta import shapes
from emulators.providers.meta.common import (
    APP_ID,
    APP_NAME,
    USER_SCOPES,
    Auth,
    apply_fields,
    fail,
    invalid_param,
    issue_meta_token,
    not_found,
    paged,
    permission_fail,
    picture_url,
    require_auth,
    resolve_auth,
)
from emulators.providers.meta.messenger import MessengerMixin
from emulators.providers.meta.seed import fb_user_id
from emulators.providers.meta.webhooks_out import configure_webhook_verified
from emulators.providers.meta.whatsapp import WhatsAppMixin


async def read_body(ctx: ProviderContext, request: Request) -> dict[str, Any]:
    """JSON or form body as a dict (multipart bodies are read by their handlers)."""
    content_type = request.headers.get("content-type", "")
    if "multipart/form-data" in content_type:
        return {}
    raw = await request.body()
    if not raw:
        return {}
    if "application/json" in content_type or raw[:1] in (b"{", b"["):
        try:
            parsed = json.loads(raw)
        except ValueError:
            invalid_param(ctx, "Invalid JSON in request body.")
        return parsed if isinstance(parsed, dict) else {"_body": parsed}  # type: ignore[return-value]
    if "application/x-www-form-urlencoded" in content_type:
        form = await request.form()
        return {k: v for k, v in form.items()}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


def unsupported(ctx: ProviderContext, object_id: str) -> NoReturn:
    fail(
        ctx,
        400,
        100,
        f"(#100) Object with ID '{object_id}' does not exist, cannot be loaded due to missing "
        "permissions, or does not support this operation.",
        subcode=33,
        err_type="GraphMethodException",
    )


class MetaAPI(WhatsAppMixin, MessengerMixin):
    def __init__(
        self,
        ctx: ProviderContext,
        request: Request,
        version: str,
        body: dict[str, Any],
        params_override: dict[str, Any] | None = None,
    ) -> None:
        self.ctx = ctx
        self.request = request
        self.version = version
        self.body = body
        if params_override is not None:
            self.params = dict(params_override)
        else:
            params: dict[str, Any] = {}
            for key, value in body.items():
                if isinstance(value, (str, int, float, bool)):
                    params[key] = value
            params.update(request.query_params)
            self.params = params

    # -- small helpers -------------------------------------------------------------------------------
    def shaped(self, shape: dict[str, Any]) -> Any:
        return apply_fields(shape, self.params.get("fields"))

    def person_id_for_auth(self, auth: Auth) -> str | None:
        if auth.person_id:
            return auth.person_id
        if auth.fb_user_id:
            record = self.ctx.store.collection("fb_users").get(auth.fb_user_id)
            return record.get("_person_id") if record else None
        return None

    def resolve_me_id(self, auth: Auth) -> str:
        meta = self.ctx.store.meta
        if auth.meta_type == "PAGE":
            return str(auth.page_id or meta["page_id"])
        if auth.meta_type == "SYSTEM_USER":
            return str(auth.fb_user_id or meta["system_user_id"])
        if auth.meta_type == "APP":
            return str(meta["app_id"])
        return str(auth.fb_user_id or "")

    # -- main dispatch ---------------------------------------------------------------------------------
    async def dispatch(self, method: str, rest: str) -> Any:
        segments = [s for s in rest.split("/") if s != ""]
        if segments == ["dialog", "oauth"]:
            return await self.oauth_dialog()
        if segments == ["oauth", "access_token"]:
            return await self.oauth_access_token()
        if segments == ["debug_token"]:
            if method != "GET":
                unsupported(self.ctx, "debug_token")
            return self.debug_token()
        if segments[:2] == ["whatsapp_business", "attachments"]:
            return self.media_download()
        if segments and segments[0] == "v" and segments[-1].endswith(".png"):
            return self.cdn_picture(segments[-1])
        if not segments:
            if method == "POST" and (self.body.get("batch") or self.params.get("batch")):
                return await self.batch()
            not_found(self.ctx, "")
        return await self.object_dispatch(method, segments)

    async def object_dispatch(self, method: str, segments: list[str]) -> Any:
        ctx = self.ctx
        first = segments[0]
        rest = segments[1:]
        if first == "me":
            return await self.me_dispatch(method, rest)
        # Username / vanity alias for the Page (GET /halcyonadvisory).
        page = ctx.store.collection("pages").get(ctx.store.meta["page_id"])
        if page and first == page.get("username"):
            first = page["id"]
        if rest == ["picture"]:
            return self.picture_endpoint(first)
        phone = ctx.store.collection("phone_numbers").get(first)
        if phone is not None:
            return await self.phone_dispatch(method, first, rest)
        if first in ctx.store.collection("wabas"):
            return self.waba_dispatch(method, first, rest)
        if first in ctx.store.collection("businesses"):
            return self.business_dispatch(method, first, rest)
        if first in ctx.store.collection("pages"):
            return await self.page_dispatch(method, first, rest)
        if first == ctx.store.meta["app_id"]:
            return await self.app_dispatch(method, first, rest)
        if first == ctx.store.meta["system_user_id"] or first in ctx.store.collection("fb_users"):
            return self.user_dispatch(method, first, rest)
        if first in ctx.store.collection("psids"):
            if method == "GET" and not rest:
                return self.handle_psid_profile(first)
            unsupported(ctx, first)
        if first in ctx.store.collection("ms_conversations"):
            if method == "GET" and not rest:
                return self.handle_ms_conversation(first)
            if method == "GET" and rest == ["messages"]:
                return self.handle_ms_conversation_messages(first)
            if method == "GET" and rest == ["participants"]:
                conv = self.get_ms_conversation(first)
                self.require_page(conv["_page_id"])
                return {"data": conv["participants"]["data"]}
            unsupported(ctx, first)
        if first in ctx.store.collection("ms_messages"):
            if method == "GET" and not rest:
                return self.handle_ms_message(first)
            unsupported(ctx, first)
        if first in ctx.store.collection("media"):
            if method == "GET" and not rest:
                return self.handle_media_get(first)
            if method == "DELETE" and not rest:
                return self.handle_media_delete(first)
            unsupported(ctx, first)
        if first in ctx.store.collection("templates"):
            return self.template_dispatch(method, first, rest)
        not_found(ctx, first)

    # -- per-kind dispatch -------------------------------------------------------------------------------
    async def phone_dispatch(self, method: str, phone_id: str, rest: list[str]) -> Any:
        if not rest and method == "GET":
            return self.handle_phone(phone_id)
        if rest == ["messages"] and method == "POST":
            return await self.handle_wa_send(phone_id)
        if rest == ["media"] and method == "POST":
            return await self.handle_media_upload(phone_id)
        if rest == ["whatsapp_business_profile"]:
            if method == "GET":
                return self.handle_profile_get(phone_id)
            if method == "POST":
                return self.handle_profile_post(phone_id)
        if rest == ["register"] and method == "POST":
            return self.handle_register(phone_id, deregister=False)
        if rest == ["deregister"] and method == "POST":
            return self.handle_register(phone_id, deregister=True)
        unsupported(self.ctx, phone_id)

    def waba_dispatch(self, method: str, waba_id: str, rest: list[str]) -> Any:
        if not rest and method == "GET":
            return self.handle_waba(waba_id)
        if rest == ["phone_numbers"] and method == "GET":
            return self.handle_waba_phone_numbers(waba_id)
        if rest == ["message_templates"]:
            if method == "GET":
                return self.handle_templates_get(waba_id)
            if method == "POST":
                return self.handle_templates_post(waba_id)
            if method == "DELETE":
                return self.handle_templates_delete(waba_id)
        if rest == ["subscribed_apps"]:
            if method == "GET":
                return self.handle_subscribed_apps_get(waba_id)
            if method == "POST":
                return self.handle_subscribed_apps_post(waba_id)
            if method == "DELETE":
                return self.handle_subscribed_apps_delete(waba_id)
        if not rest and method == "POST":
            self.require_whatsapp(management=True)
            waba = self.get_waba(waba_id)
            for key in ("name", "timezone_id", "currency"):
                if key in self.body:
                    waba[key] = self.body[key]
            return {"success": True}
        unsupported(self.ctx, waba_id)

    def template_dispatch(self, method: str, template_id: str, rest: list[str]) -> Any:
        if rest:
            unsupported(self.ctx, template_id)
        self.require_whatsapp(management=True)
        record = self.ctx.store.collection("templates").get(template_id)
        assert record is not None
        if method == "GET":
            return self.shaped(shapes.template_shape(record))
        if method == "DELETE":
            self.ctx.store.collection("templates").delete(template_id)
            return {"success": True}
        if method == "POST":
            if "components" in self.body:
                record["components"] = self.body["components"]
                record["status"] = "PENDING"
            if "category" in self.body:
                record["category"] = self.body["category"]
            return {"success": True}
        unsupported(self.ctx, template_id)

    def business_dispatch(self, method: str, business_id: str, rest: list[str]) -> Any:
        require_auth(
            self.ctx,
            self.request,
            self.params,
            types={"SYSTEM_USER", "USER", "APP"},
            perm_message="Permissions error: business objects require a user, system-user or app token.",
        )
        record = self.ctx.store.collection("businesses").get(business_id)
        assert record is not None
        if not rest and method == "GET":
            return self.shaped(shapes.business_shape(record))
        if method == "GET" and rest == ["owned_whatsapp_business_accounts"]:
            items = [self.shaped(shapes.waba_shape(w)) for w in self.ctx.store.collection("wabas")]
            return paged(
                self.ctx,
                self.request,
                self.version,
                f"{business_id}/owned_whatsapp_business_accounts",
                items,
                self.params,
            )
        if method == "GET" and rest == ["owned_pages"]:
            items = [
                self.shaped(shapes.page_shape(self.ctx, self.request, p))
                for p in self.ctx.store.collection("pages")
            ]
            return paged(
                self.ctx, self.request, self.version, f"{business_id}/owned_pages", items, self.params
            )
        if method == "GET" and rest == ["system_users"]:
            return {"data": [self.shaped(shapes.system_user_shape(self.ctx))]}
        unsupported(self.ctx, business_id)

    async def page_dispatch(self, method: str, page_id: str, rest: list[str]) -> Any:
        ctx = self.ctx
        page = self.get_page(page_id)
        if not rest and method == "GET":
            auth = require_auth(ctx, self.request, self.params)
            wants_token = "access_token" in str(self.params.get("fields") or "")
            manager = self.person_id_for_auth(auth) in (page.get("_roles") or {})
            include_token = wants_token and (
                auth.page_id == page_id
                or auth.meta_type == "SYSTEM_USER"
                or (auth.meta_type == "USER" and manager)
            )
            return self.shaped(shapes.page_shape(ctx, self.request, page, include_token=bool(include_token)))
        if not rest and method == "POST":
            auth = require_auth(ctx, self.request, self.params)
            manager = self.person_id_for_auth(auth) in (page.get("_roles") or {})
            if not (
                auth.page_id == page_id
                or (auth.meta_type == "USER" and manager)
                or auth.meta_type == "SYSTEM_USER"
            ):
                permission_fail(
                    ctx, "Permissions error: updating a Page requires a page or admin user token."
                )
            for key in ("about", "description", "phone", "website", "category", "bio"):
                if key in self.body:
                    page[key] = self.body[key]
            return {"success": True}
        if rest == ["messages"] and method == "POST":
            return await self.handle_ms_send(page_id)
        if rest == ["conversations"] and method == "GET":
            return self.handle_ms_conversations(page_id)
        if rest == ["message_attachments"] and method == "POST":
            return await self.handle_ms_attachments(page_id)
        if rest == ["messenger_profile"]:
            if method == "GET":
                return self.handle_ms_profile_get(page_id)
            if method == "POST":
                return self.handle_ms_profile_post(page_id)
            if method == "DELETE":
                return self.handle_ms_profile_delete(page_id)
        if rest == ["roles"] and method == "GET":
            auth = require_auth(ctx, self.request, self.params)
            manager = self.person_id_for_auth(auth) in (page.get("_roles") or {})
            if not (
                auth.page_id == page_id
                or auth.meta_type == "SYSTEM_USER"
                or (auth.meta_type == "USER" and manager)
            ):
                permission_fail(ctx, "Permissions error: listing Page roles requires an admin token.")
            data = []
            for person_id, role in (page.get("_roles") or {}).items():
                user = ctx.store.collection("fb_users").get(fb_user_id(person_id))
                if user:
                    data.append({"id": user["id"], "name": user["name"], "role": role})
            return {"data": data}
        if rest == ["subscribed_apps"]:
            self.require_page(page_id)
            if method == "GET":
                fields_list = page.get("_subscribed_fields") or []
                data = (
                    [
                        {
                            "id": ctx.store.meta["app_id"],
                            "name": ctx.store.meta["app_name"],
                            "subscribed_fields": fields_list,
                        }
                    ]
                    if fields_list
                    else []
                )
                return {"data": data}
            if method == "POST":
                raw = self.body.get("subscribed_fields") or self.params.get("subscribed_fields") or ""
                page["_subscribed_fields"] = (
                    raw if isinstance(raw, list) else [f.strip() for f in str(raw).split(",") if f.strip()]
                )
                return {"success": True}
            if method == "DELETE":
                page["_subscribed_fields"] = []
                return {"success": True}
        unsupported(ctx, page_id)

    async def app_dispatch(self, method: str, app_id: str, rest: list[str]) -> Any:
        if not rest and method == "GET":
            require_auth(self.ctx, self.request, self.params)
            return self.shaped(shapes.app_shape(self.ctx))
        if rest == ["subscriptions"]:
            return await self.app_subscriptions(method)
        unsupported(self.ctx, app_id)

    async def app_subscriptions(self, method: str) -> Any:
        ctx = self.ctx
        require_auth(
            ctx,
            self.request,
            self.params,
            types={"APP"},
            perm_message="Manage subscriptions requires an app access token.",
        )
        configs = ctx.store.collection("webhook_configs")
        if method == "GET":
            data = [
                {
                    "active": True,
                    "object": c["object"],
                    "callback_url": c["callback_url"],
                    "fields": [
                        {"name": name, "version": self.version} for name in c.get("fields") or ["messages"]
                    ],
                }
                for c in configs
            ]
            return {"data": data}
        if method == "POST":
            obj = str(self.body.get("object") or self.params.get("object") or "")
            callback = str(self.body.get("callback_url") or self.params.get("callback_url") or "")
            verify_token = str(self.body.get("verify_token") or self.params.get("verify_token") or "")
            raw_fields = self.body.get("fields") or self.params.get("fields") or "messages"
            field_list = (
                raw_fields
                if isinstance(raw_fields, list)
                else [f.strip() for f in str(raw_fields).split(",") if f.strip()]
            )
            if not obj or not callback or not verify_token:
                invalid_param(ctx, "object, callback_url and verify_token are required.")
            ok, info = await configure_webhook_verified(
                ctx,
                obj=obj,
                callback_url=callback,
                verify_token=verify_token,
                fields=field_list,
            )
            if not ok:
                fail(
                    ctx,
                    400,
                    100,
                    "(#100) Callback verification failed: the callback URL did not echo the hub.challenge.",
                    subcode=2200,
                )
            return {"success": True, **({"data": info} if info else {})}
        if method == "DELETE":
            obj = str(self.params.get("object") or self.body.get("object") or "")
            if not obj:
                invalid_param(ctx, "The parameter object is required.")
            configs.delete(obj)
            return {"success": True}
        unsupported(ctx, ctx.store.meta["app_id"])

    def user_dispatch(self, method: str, user_id: str, rest: list[str]) -> Any:
        ctx = self.ctx
        if not rest and method == "GET":
            auth = require_auth(ctx, self.request, self.params)
            if user_id == ctx.store.meta["system_user_id"]:
                return self.shaped(shapes.system_user_shape(ctx))
            record = ctx.store.collection("fb_users").get(user_id)
            assert record is not None
            with_email = auth.fb_user_id == user_id or auth.meta_type in ("APP", "SYSTEM_USER")
            return self.shaped(shapes.fb_user_shape(ctx, self.request, record, with_email=bool(with_email)))
        if rest == ["accounts"] and method == "GET":
            auth = require_auth(
                ctx,
                self.request,
                self.params,
                types={"USER"},
                perm_message="This endpoint requires a user access token.",
            )
            if auth.fb_user_id != user_id and auth.meta_type != "APP":
                permission_fail(ctx, "Permissions error: you can only list your own Pages.")
            return self.accounts_for(auth)
        if rest == ["permissions"]:
            auth = require_auth(ctx, self.request, self.params)
            return self.permissions(auth, method)
        unsupported(ctx, user_id)

    # -- /me ----------------------------------------------------------------------------------------------
    async def me_dispatch(self, method: str, rest: list[str]) -> Any:
        ctx = self.ctx
        if rest == ["messages"] and method == "POST":
            auth = require_auth(
                ctx,
                self.request,
                self.params,
                types={"PAGE"},
                perm_message="Requires pages_messaging permission and a page access token to send messages.",
            )
            return await self.handle_ms_send(str(auth.page_id or ctx.store.meta["page_id"]))
        if rest == ["message_attachments"] and method == "POST":
            auth = require_auth(ctx, self.request, self.params, types={"PAGE"})
            return await self.handle_ms_attachments(str(auth.page_id or ctx.store.meta["page_id"]))
        if rest == ["messenger_profile"]:
            auth = require_auth(ctx, self.request, self.params, types={"PAGE"})
            page_id = str(auth.page_id or ctx.store.meta["page_id"])
            if method == "GET":
                return self.handle_ms_profile_get(page_id)
            if method == "POST":
                return self.handle_ms_profile_post(page_id)
            if method == "DELETE":
                return self.handle_ms_profile_delete(page_id)
        auth = require_auth(ctx, self.request, self.params)
        if not rest and method == "GET":
            return self.me_shape(auth)
        if rest == ["picture"] and method == "GET":
            return self.picture_endpoint(self.resolve_me_id(auth))
        if rest == ["accounts"] and method == "GET":
            if auth.meta_type != "USER":
                permission_fail(ctx, "This endpoint requires a user access token.", code=200)
            return self.accounts_for(auth)
        if rest == ["permissions"]:
            return self.permissions(auth, method)
        unsupported(ctx, "me")

    def me_shape(self, auth: Auth) -> Any:
        ctx = self.ctx
        if auth.meta_type == "PAGE":
            page = self.get_page(str(auth.page_id or ctx.store.meta["page_id"]))
            wants_token = "access_token" in str(self.params.get("fields") or "")
            return self.shaped(shapes.page_shape(ctx, self.request, page, include_token=wants_token))
        if auth.meta_type == "SYSTEM_USER":
            return self.shaped(shapes.system_user_shape(ctx))
        if auth.meta_type == "APP":
            return self.shaped(shapes.app_shape(ctx))
        record = ctx.store.collection("fb_users").get(auth.fb_user_id or "")
        if record is None:
            not_found(ctx, auth.fb_user_id or "me")
        return self.shaped(shapes.fb_user_shape(ctx, self.request, record, with_email="email" in auth.scopes))

    def accounts_for(self, auth: Auth) -> Any:
        ctx = self.ctx
        person_id = self.person_id_for_auth(auth)
        items = []
        for page in ctx.store.collection("pages"):
            role = (page.get("_roles") or {}).get(person_id or "")
            if not role:
                continue
            tasks = (
                ["MANAGE", "CREATE_CONTENT", "MODERATE", "ADVERTISE", "ANALYZE"]
                if role == "ADMIN"
                else ["CREATE_CONTENT", "MODERATE", "ADVERTISE", "ANALYZE"]
            )
            items.append(
                self.shaped(shapes.page_shape(ctx, self.request, page, include_token=True, tasks=tasks))
            )
        return paged(ctx, self.request, self.version, "me/accounts", items, self.params)

    def permissions(self, auth: Auth, method: str) -> Any:
        ctx = self.ctx
        if method == "GET":
            return {"data": [{"permission": s, "status": "granted"} for s in auth.scopes]}
        if method == "DELETE":
            permission = self.params.get("permission") or self.body.get("permission")
            if not permission:
                invalid_param(ctx, "The parameter permission is required.")
            if auth.grant is not None:
                record = ctx.store.collection("_grants").get(auth.token)
                if record and permission in (record.get("scopes") or []):
                    record["scopes"] = [s for s in record["scopes"] if s != permission]
            return {"success": True}
        unsupported(ctx, "permissions")

    # -- OAuth ----------------------------------------------------------------------------------------------
    async def oauth_dialog(self) -> Response:
        ctx = self.ctx
        params = self.params
        client_id = str(params.get("client_id") or "")
        redirect_uri = str(params.get("redirect_uri") or "")
        if not client_id or not redirect_uri:
            invalid_param(ctx, "client_id and redirect_uri are required.")
        ctx.tokens.authenticate_client(client_id, None, require_secret=False)
        response_type = str(params.get("response_type") or "code")
        raw_scope = str(params.get("scope") or "")
        scopes = [s for s in raw_scope.replace(",", " ").split() if s] or ["public_profile"]
        login_hint = str(params.get("login_hint") or "")
        person = None
        if login_hint:
            person = ctx.world.person_by_email(login_hint)
            if person is None:
                for candidate in ctx.world.people:
                    if (
                        candidate.id == login_hint
                        or candidate.username == login_hint
                        or fb_user_id(candidate.id) == login_hint
                    ):
                        person = candidate
                        break
        if person is None:
            client = ctx.tokens.client(client_id) or {}
            url = str(self.request.url)
            page = render_account_picker(
                provider_title="Facebook",
                app_name=str(client.get("name") or APP_NAME),
                scopes=scopes,
                accounts=[
                    {"display_name": p.display_name, "email": p.email, "login_hint": p.email}
                    for p in ctx.world.active_employees
                ],
                action_base=url,
            )
            return HTMLResponse(page)
        parts = {p.strip() for p in response_type.replace(",", " ").split()}
        state = params.get("state")
        if "token" in parts and "code" not in parts:
            grant = issue_meta_token(
                ctx,
                meta_type="USER",
                fb_user_id=fb_user_id(person.id),
                person_id=person.id,
                scopes=scopes,
                ttl=timedelta(hours=2),
                client_id=client_id,
            )
            return RedirectResponse(
                redirect_with_params(
                    redirect_uri,
                    {"access_token": grant.token, "expires_in": 7200, "state": state},
                    fragment=True,
                ),
                status_code=302,
            )
        code = ctx.tokens.issue_code(
            client_id=client_id,
            user_id=fb_user_id(person.id),
            scopes=scopes,
            redirect_uri=redirect_uri,
            extra={"person_id": person.id},
        )
        return RedirectResponse(
            redirect_with_params(redirect_uri, {"code": code.token, "state": state}), status_code=302
        )

    async def oauth_access_token(self) -> dict[str, Any]:
        ctx = self.ctx
        p = self.params
        client_id = str(p.get("client_id") or "")
        client_secret = str(p.get("client_secret") or "") or None
        grant_type = str(p.get("grant_type") or "")
        if client_id == APP_ID and client_secret not in (None, ctx.store.meta["app_secret"]):
            fail(ctx, 400, 100, "Error validating client secret.")
        try:
            client = ctx.tokens.authenticate_client(
                client_id or None, client_secret, require_secret=client_secret is not None
            )
        except OAuthError as exc:
            fail(ctx, exc.status, 100, exc.description or "Error validating client secret.")
        if grant_type == "client_credentials":
            secret = client_secret or str(client.get("client_secret") or "")
            if not secret:
                fail(ctx, 400, 100, "Error validating client secret.")
            return {"access_token": f"{client_id}|{secret}", "token_type": "bearer"}
        if grant_type == "fb_exchange_token":
            source = ctx.tokens.resolve(str(p.get("fb_exchange_token") or ""))
            if source is None or (source.extra or {}).get("meta_type") != "USER":
                fail(ctx, 400, 190, "Error validating access token: Session is invalid.", subcode=467)
            extra = source.extra or {}
            grant = issue_meta_token(
                ctx,
                meta_type="USER",
                fb_user_id=extra.get("fb_user_id") or source.user_id,
                person_id=extra.get("person_id"),
                scopes=list(source.scopes or USER_SCOPES),
                ttl=timedelta(days=60),
                client_id=client_id or source.client_id,
            )
            return {"access_token": grant.token, "token_type": "bearer", "expires_in": 5184000}
        code = p.get("code")
        if grant_type in ("", "authorization_code") and code:
            try:
                code_grant = ctx.tokens.redeem_code(
                    str(code),
                    client_id=client_id or None,
                    redirect_uri=str(p["redirect_uri"]) if p.get("redirect_uri") else None,
                    code_verifier=str(p["code_verifier"]) if p.get("code_verifier") else None,
                )
            except OAuthError as exc:
                fail(
                    ctx,
                    400,
                    100,
                    "Error validating verification code. Please make sure your redirect_uri is "
                    f"identical to the one used in the OAuth dialog. ({exc.description})",
                )
            extra = code_grant.extra or {}
            grant = issue_meta_token(
                ctx,
                meta_type="USER",
                fb_user_id=code_grant.user_id,
                person_id=extra.get("person_id"),
                scopes=list(code_grant.scopes or ["public_profile"]),
                ttl=timedelta(hours=1),
                client_id=client_id or code_grant.client_id,
            )
            return {"access_token": grant.token, "token_type": "bearer", "expires_in": 3600}
        fail(ctx, 400, 100, "Unsupported grant_type or missing code.")

    # -- debug_token --------------------------------------------------------------------------------------
    def debug_token(self) -> dict[str, Any]:
        ctx = self.ctx
        caller = require_auth(ctx, self.request, self.params)
        input_token = str(self.params.get("input_token") or "")
        if not input_token:
            invalid_param(ctx, "The parameter input_token is required.")
        now_ts = int(ctx.clock.now().timestamp())
        meta = ctx.store.meta
        if "|" in input_token:
            sub = MetaAPI(ctx, self.request, self.version, {}, {"access_token": input_token})
            resolved = resolve_auth(ctx, self.request, sub.params)
            return {
                "data": {
                    "app_id": meta["app_id"],
                    "type": "APP",
                    "application": meta["app_name"],
                    "data_access_expires_at": 0,
                    "expires_at": 0,
                    "is_valid": resolved is not None,
                    "issued_at": now_ts,
                    "scopes": [],
                    "granular_scopes": [],
                    "user_id": meta["app_id"],
                }
            }
        grant = ctx.tokens.get(input_token, "access")
        if grant is None:
            return {
                "data": {
                    "app_id": meta["app_id"],
                    "type": "USER",
                    "application": meta["app_name"],
                    "data_access_expires_at": 0,
                    "error": {"code": 190, "message": "Invalid OAuth access token.", "subcode": 467},
                    "expires_at": 0,
                    "is_valid": False,
                    "issued_at": 0,
                    "scopes": [],
                    "granular_scopes": [],
                    "user_id": "",
                }
            }
        live = ctx.tokens.resolve(input_token) is not None
        extra = grant.extra or {}
        meta_type = str(extra.get("meta_type") or "USER")
        user_id = extra.get("fb_user_id") or grant.user_id or ""
        issued = int(grant.issued_at.timestamp()) if hasattr(grant.issued_at, "timestamp") else 0
        expires = int(grant.expires_at.timestamp()) if grant.expires_at is not None else 0
        granular: list[dict[str, Any]] = []
        person_id = extra.get("person_id")
        manages_page = False
        for page in ctx.store.collection("pages"):
            if person_id and person_id in (page.get("_roles") or {}):
                manages_page = True
        for scope in grant.scopes or []:
            if scope.startswith("pages_") and manages_page:
                granular.append({"scope": scope, "target_ids": [meta["page_id"]]})
            elif scope.startswith("whatsapp_"):
                granular.append({"scope": scope, "target_ids": [meta["waba_id"]]})
        data: dict[str, Any] = {
            "app_id": grant.client_id,
            "type": meta_type,
            "application": meta["app_name"] if grant.client_id == meta["app_id"] else grant.client_id,
            "data_access_expires_at": issued + 90 * 86400 if meta_type == "USER" and issued else 0,
            "expires_at": expires,
            "is_valid": live,
            "issued_at": issued,
            "scopes": list(grant.scopes or []),
            "granular_scopes": granular,
            "user_id": str(user_id),
        }
        if meta_type == "PAGE":
            data["profile_id"] = extra.get("page_id") or grant.user_id
        if not live:
            data["error"] = {
                "code": 190,
                "message": "Error validating access token: Session has expired."
                if ctx.tokens.is_expired(input_token)
                else "Error validating access token: Session is invalid.",
                "subcode": 463 if ctx.tokens.is_expired(input_token) else 467,
            }
        _ = caller
        return {"data": data}

    # -- media download / pictures --------------------------------------------------------------------------
    def media_download(self) -> Response:
        ctx = self.ctx
        params: dict[str, Any] = dict(self.request.query_params)
        if resolve_auth(ctx, self.request, params) is None:
            fail(ctx, 401, 190, "Invalid OAuth access token.", subcode=467)
        mid = str(params.get("mid") or "")
        record = ctx.store.collection("media").get(mid)
        if record is None or not record.get("_blob_key"):
            fail(ctx, 404, 100, "(#100) Media not found.", subcode=33)
        assert record is not None
        try:
            content = ctx.store.blobs.get(record["_blob_key"])
        except KeyError:
            fail(ctx, 404, 100, "(#100) Media not found.", subcode=33)
        filename = html.escape(str(record.get("_filename") or "download"), quote=True)
        return Response(
            content,
            media_type=str(record.get("mime_type") or "application/octet-stream"),
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "private, max-age=300",
                "Content-Length": str(len(content)),
            },
        )

    def cdn_picture(self, filename: str) -> Response:
        from emulators.world.documents import make_png

        seed = int(filename.split(".")[0][-4:] or "0", 36) if filename.split(".")[0] else 1
        return Response(make_png(200, 200, seed=seed % 1000), media_type="image/png")

    def picture_endpoint(self, object_id: str) -> Response:
        ctx = self.ctx
        known = (
            object_id in ctx.store.collection("pages")
            or object_id in ctx.store.collection("fb_users")
            or object_id in ctx.store.collection("psids")
            or object_id in ctx.store.collection("phone_numbers")
            or object_id == ctx.store.meta["app_id"]
            or object_id == ctx.store.meta["system_user_id"]
        )
        if not known:
            not_found(ctx, object_id)
        if str(self.params.get("redirect") or "true").lower() == "false":
            from fastapi.responses import JSONResponse

            width = int(self.params.get("width", 50)) if str(self.params.get("width", "")).isdigit() else 50
            height = (
                int(self.params.get("height", 50)) if str(self.params.get("height", "")).isdigit() else 50
            )
            return JSONResponse(
                {
                    "data": {
                        "url": picture_url(ctx, self.request, object_id),
                        "width": width,
                        "height": height,
                        "is_silhouette": False,
                    }
                }
            )
        return RedirectResponse(picture_url(ctx, self.request, object_id), status_code=302)

    # -- batch ----------------------------------------------------------------------------------------------
    async def batch(self) -> Any:
        ctx = self.ctx
        auth = require_auth(ctx, self.request, self.params)
        raw = self.body.get("batch") or self.params.get("batch")
        try:
            items = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            invalid_param(ctx, "batch must be a JSON-encoded list of requests.")
        if not isinstance(items, list) or len(items) > 50:
            invalid_param(ctx, "batch must be a list of at most 50 requests.")
        include_headers = str(self.params.get("include_headers", "true")).lower() != "false"
        results: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                results.append(
                    {"code": 400, "body": json.dumps({"error": {"code": 100, "message": "Invalid element"}})}
                )
                continue
            method = str(item.get("method") or "GET").upper()
            relative = str(item.get("relative_url") or "")
            split = urlsplit(relative)
            path = split.path.lstrip("/")
            version = self.version
            head, _, tail = path.partition("/")
            if head.startswith("v") and "." in head:
                version, path = head, tail
            query = {k: v[-1] for k, v in parse_qs(split.query).items()}
            sub_body: dict[str, Any] = {}
            if item.get("body"):
                sub_body = {k: v[-1] for k, v in parse_qs(str(item["body"])).items()}
            params = {**sub_body, **query}
            params.setdefault("access_token", auth.token)
            sub = MetaAPI(ctx, self.request, version, sub_body, params)
            try:
                result = await sub.dispatch(method, path)
                if isinstance(result, Response):
                    body_text = result.body.decode("utf-8", "replace") if hasattr(result, "body") else ""  # type: ignore[attr-defined]
                    code = result.status_code
                else:
                    body_text = json.dumps(result)
                    code = 200
            except EmulatorError as exc:
                body_text = exc.body if isinstance(exc.body, str) else json.dumps(exc.body)
                code = exc.status
            entry: dict[str, Any] = {"code": code, "body": body_text}
            if include_headers:
                entry["headers"] = [{"name": "Content-Type", "value": "application/json; charset=UTF-8"}]
            results.append(entry)
        return results
