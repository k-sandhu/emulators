"""The Trello /1 HTTP surface and read-only batch dispatcher."""

from __future__ import annotations

import re
import shlex
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import Response

from emulators.core.auth import Grant
from emulators.core.clock import isoformat_z
from emulators.core.errors import EmulatorError
from emulators.core.provider import ProviderContext

from .auth import authenticate, authorize
from .cards import CardsMixin
from .common import (
    Record,
    can_view,
    clean,
    fail,
    flag,
    get_board,
    http_url,
    integer,
    lookup,
    member,
    names,
    parameters,
    resource,
    text,
)
from .hooks import create_webhook, model
from .resources import ResourcesMixin
from .shapes import action_shape, board_shape, card_shape, member_shape, organization_shape


class Api(CardsMixin, ResourcesMixin):
    def __init__(
        self,
        ctx: ProviderContext,
        request: Request,
        params: Record,
        grant: Grant | None,
        method: str | None = None,
    ) -> None:
        self.ctx, self.request, self.params, self.grant = ctx, request, params, grant
        self.method = method or request.method

    async def dispatch(self, path: str) -> Any:
        parts = path.strip("/").split("/")
        handlers = {
            "members": self.members,
            "organizations": self.organizations,
            "boards": self.boards,
            "lists": self.lists,
            "cards": self.cards,
            "labels": self.labels,
            "checklists": self.checklists,
        }
        if parts[0] in handlers:
            return await handlers[parts[0]](parts[1:])
        if parts[0] in ("tokens", "webhooks"):
            return await self.webhooks(parts)
        if parts == ["search"] and self.method == "GET":
            return self.search()
        if parts == ["batch"] and self.method == "GET":
            return await self.batch()
        if parts[0] == "actions" and self.method == "GET" and len(parts) in (2, 3):
            action = lookup(self.ctx, "actions", parts[1])
            board = get_board(self.ctx, action["_board"], self.grant)
            if len(parts) == 2:
                return action_shape(
                    self.ctx, self.request, action, self.grant, self.params.get("fields", "all")
                )
            if parts[2] == "board":
                return board_shape(self.ctx, self.request, board, self.grant, self.params)
            if parts[2] == "card" and action.get("_card"):
                return card_shape(
                    self.ctx,
                    self.request,
                    resource(self.ctx, "cards", action["_card"], self.grant),
                    self.grant,
                    self.params,
                )
        fail(404, "not found")

    async def batch(self) -> list[Record]:
        urls = names(self.params.get("urls"), "urls")
        if not urls or len(urls) > 10:
            fail(400, "batch requires between 1 and 10 urls")
        parsed = []
        for url in urls:
            parts = urlsplit(url)
            path = unquote(parts.path)
            if (
                not path.startswith("/")
                or path.startswith("//")
                or parts.scheme
                or parts.netloc
                or parts.fragment
                or any(part in (".", "..") for part in path.split("/"))
                or path.startswith(("/1/", "/batch"))
                or "/download/" in path
            ):
                fail(400, "invalid batch url")
            params = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
            if "key" in params or "token" in params:
                fail(400, "batch urls cannot change authentication")
            parsed.append((path, params))
        result = []
        for path, params in parsed:
            try:
                value = await Api(self.ctx, self.request, params, self.grant, "GET").dispatch(path)
                result.append({"200": value})
            except EmulatorError as exc:
                result.append({str(exc.status): exc.body})
        return result

    def search(self) -> Record:
        ctx, p, grant = self.ctx, self.params, self.grant
        if not grant:
            fail(401, "invalid token")
        query = text(p.get("query"), "query", 16384)
        try:
            terms = shlex.split(query)
        except ValueError:
            fail(400, "invalid query")
        types = names(p.get("modelTypes", "all"), "modelTypes")
        allowed = {"boards", "cards", "members", "organizations"}
        if types == ["all"]:
            types = sorted(allowed)
        if any(t not in allowed for t in types):
            fail(400, "unsupported modelTypes")
        partial = flag(p.get("partial", False), "partial")
        boards = [b for b in ctx.store["boards"] if can_view(ctx, b, grant)]
        if "idBoards" in p:
            selected = names(p["idBoards"], "idBoards")
            boards = [
                b
                for b in boards
                if (grant.user_id in b["_members"] if selected == ["mine"] else b["id"] in selected)
            ]
        board_ids = {b["id"] for b in boards}
        filters: dict[str, list[str]] = {}
        plain = []
        for term in terms:
            if ":" in term:
                key, value = term.split(":", 1)
                if key not in ("board", "list", "label", "member", "is", "name", "description"):
                    fail(400, "unsupported search operator")
                filters.setdefault(key, []).append(value.casefold())
            else:
                plain.append(term.casefold())
        if any(value not in ("open", "archived", "closed") for value in filters.get("is", [])):
            fail(400, "invalid is filter")

        def words_match(value: str, terms_: list[str]) -> bool:
            words = re.findall(r"\w+", value.casefold())
            return all(
                (
                    term in value.casefold()
                    if " " in term
                    else any(word.startswith(term) for word in words)
                    if partial
                    else term in words
                )
                for term in terms_
            )

        result: Record = {
            "options": {
                "terms": [{"text": term} for term in terms],
                "modifiers": [],
                "modelTypes": types,
                "partial": partial,
            }
        }
        if "boards" in types:
            rows = [b for b in boards if words_match(b["name"] + " " + b["desc"], plain)]
            result["boards"] = [
                board_shape(
                    ctx,
                    self.request,
                    b,
                    grant,
                    {
                        "fields": p.get("board_fields", "name,idOrganization"),
                        "organization": p.get("board_organization", False),
                    },
                )
                for b in rows[: integer(p.get("boards_limit"), "boards_limit", 10)]
            ]
        if "cards" in types:
            cards = []
            id_cards = names(p["idCards"], "idCards") if "idCards" in p else None
            for card in ctx.store["cards"]:
                if card["idBoard"] not in board_ids or (id_cards is not None and card["id"] not in id_cards):
                    continue
                board = ctx.store["boards"].get(card["idBoard"])
                list_ = ctx.store["lists"].get(card["idList"])
                if not words_match(card["name"] + " " + card["desc"], plain):
                    continue
                if any(
                    value
                    not in (board["id"].casefold(), board["shortLink"].casefold(), board["name"].casefold())
                    for value in filters.get("board", [])
                ):
                    continue
                if any(
                    value not in (list_["id"].casefold(), list_["name"].casefold())
                    for value in filters.get("list", [])
                ):
                    continue
                if any(
                    card["closed"] != (value in ("archived", "closed")) for value in filters.get("is", [])
                ):
                    continue
                if any(
                    not words_match(card[field], filters.get(op, []))
                    for op, field in (("name", "name"), ("description", "desc"))
                ):
                    continue
                if any(
                    not any(
                        value
                        in (
                            uid.casefold(),
                            ctx.store["members"].get(uid)["username"].casefold(),
                            "me" if uid == grant.user_id else "",
                        )
                        for uid in card["idMembers"]
                    )
                    for value in filters.get("member", [])
                ):
                    continue
                if any(
                    not any(
                        value in (label.casefold(), ctx.store["labels"].get(label)["name"].casefold())
                        for label in card["idLabels"]
                    )
                    for value in filters.get("label", [])
                ):
                    continue
                cards.append(card)
            limit = integer(p.get("cards_limit"), "cards_limit", 10)
            offset = integer(p.get("cards_page"), "cards_page", 0, 100, 0) * limit
            params = {
                "fields": p.get("card_fields", "all"),
                **{
                    key: p["card_" + key]
                    for key in ("board", "list", "members", "attachments")
                    if "card_" + key in p
                },
            }
            result["cards"] = [
                card_shape(ctx, self.request, c, grant, params) for c in cards[offset : offset + limit]
            ]
        if "members" in types:
            visible = {uid for b in boards for uid in b["_members"]}
            rows = [
                m
                for m in ctx.store["members"]
                if m["id"] in visible and words_match(m["fullName"] + " " + m["username"], plain)
            ]
            result["members"] = [
                member_shape(
                    ctx,
                    self.request,
                    m,
                    grant,
                    p.get("member_fields", "avatarHash,fullName,initials,username,confirmed"),
                )
                for m in rows[: integer(p.get("members_limit"), "members_limit", 10)]
            ]
        if "organizations" in types:
            selected_orgs = names(p["idOrganizations"], "idOrganizations") if "idOrganizations" in p else None
            rows = [
                o
                for o in ctx.store["organizations"]
                if grant.user_id in o["_members"]
                and (selected_orgs is None or o["id"] in selected_orgs)
                and words_match(o["displayName"] + " " + o["desc"], plain)
            ]
            result["organizations"] = [
                organization_shape(
                    ctx, self.request, o, p.get("organization_fields", "name,displayName"), grant
                )
                for o in rows[: integer(p.get("organizations_limit"), "organizations_limit", 10)]
            ]
        return result

    async def webhooks(self, parts: list[str]) -> Any:
        ctx, p, grant, method = self.ctx, self.params, self.grant, self.method
        if not grant:
            fail(401, "invalid token")
        if parts[0] == "tokens":
            if len(parts) < 2 or parts[1] != grant.token:
                fail(401, "invalid token")
            if len(parts) == 2:
                if method == "DELETE":
                    ctx.tokens.revoke(grant.token)
                    for hook in ctx.store["webhooks"].where(_token=grant.token):
                        ctx.store["webhooks"].delete(hook["id"])
                    return {}
                if method == "GET":
                    return {
                        "id": grant.token,
                        "identifier": "Halcyon integration",
                        "idMember": grant.user_id,
                        "dateCreated": isoformat_z(grant.issued_at),
                        "dateExpires": isoformat_z(grant.expires_at) if grant.expires_at else None,
                        "permissions": [
                            {
                                "idModel": "*",
                                "modelType": "all",
                                "read": "read" in grant.scopes,
                                "write": "write" in grant.scopes,
                            }
                        ],
                    }
            if parts[2:] == ["member"] and method == "GET":
                return member_shape(ctx, self.request, member(ctx, grant.user_id), grant)
            if len(parts) < 3 or parts[2] != "webhooks":
                fail(404, "not found")
            parts = ["webhooks", *parts[3:]]
        if len(parts) == 1:
            if method == "POST":
                return await create_webhook(ctx, self.request, p, grant)
            if method == "GET":
                return [clean(hook) for hook in ctx.store["webhooks"].where(_token=grant.token)]
            fail(404, "not found")
        hook = lookup(ctx, "webhooks", parts[1])
        if hook["_token"] != grant.token:
            fail(401, "webhook does not belong to token")
        if method == "GET":
            if len(parts) == 2:
                return clean(hook)
            if len(parts) == 3 and parts[2] in clean(hook):
                return {"_value": hook[parts[2]]}
        if method == "DELETE" and len(parts) == 2:
            ctx.store["webhooks"].delete(hook["id"])
            return {}
        if method == "PUT":
            values = {parts[2]: p.get("value")} if len(parts) == 3 else p
            changes = {}
            if "description" in values:
                changes["description"] = text(values["description"], "description", empty=True)
            if "active" in values:
                changes["active"] = flag(values["active"], "active")
            if "idModel" in values:
                changes["idModel"] = text(values["idModel"], "idModel", 24)
                model(ctx, self.request, changes["idModel"], grant)
            if "callbackURL" in values:
                target = http_url(values["callbackURL"], "callbackURL")
                import httpx

                try:
                    verified = await ctx.webhooks.request("HEAD", target)
                except httpx.HTTPError:
                    fail(400, "URL did not return 200 status code")
                if verified.status_code != 200:
                    fail(400, "URL did not return 200 status code")
                changes["callbackURL"] = target
            hook.update(changes)
            return clean(hook)
        fail(404, "not found")


def build(app: FastAPI, ctx: ProviderContext) -> None:
    @app.api_route("/1/authorize", methods=["GET"])
    async def auth_page(request: Request) -> Response:
        return authorize(ctx, await parameters(request))

    @app.api_route("/1/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    async def api(path: str, request: Request) -> Any:
        params = await parameters(request)
        parts = path.strip("/").split("/")
        if parts[0] == "tokens" and len(parts) >= 2 and "token" not in params:
            params["token"] = parts[1]
        write = request.method != "GET" and parts[0] not in ("webhooks", "tokens")
        grant = authenticate(ctx, request, params, write=write)
        return await Api(ctx, request, params, grant).dispatch(path)
