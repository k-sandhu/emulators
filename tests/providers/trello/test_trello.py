"""Wire-level Trello contracts, mutable collaboration and permission boundaries."""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from emulators.core.hub import EmulatorHub

from .conftest import BASE, download_auth, mint


async def test_seed_world_determinism_and_reset(client: httpx.AsyncClient) -> None:
    info = (await client.get("/_emulator/info")).json()
    assert info["default_port"] == 44118
    assert info["collections"] == {
        "actions": 30,
        "attachments": 60,
        "boards": 38,
        "cards": 54,
        "checklists": 30,
        "labels": 114,
        "lists": 114,
        "members": 131,
        "organizations": 8,
    }
    me = (await client.get("/1/members/me")).json()
    assert me["email"] == "eleanor.whitfield@halcyonadvisory.example"
    assert all(re.fullmatch("[0-9a-f]{24}", rid) for rid in me["idBoards"])
    orgs = (await client.get("/1/members/me/organizations")).json()
    assert any(o["displayName"] == "Halcyon Advisory Group" for o in orgs)
    before = (
        await client.get(
            "/1/members/me/boards", params={"cards": "all", "lists": "all", "memberships": "all"}
        )
    ).json()
    await client.post("/1/boards", json={"name": "Temporary"})
    await client.post("/_emulator/reset")
    assert (
        await client.get(
            "/1/members/me/boards", params={"cards": "all", "lists": "all", "memberships": "all"}
        )
    ).json() == before
    other = EmulatorHub(providers=["trello"], anchor="2026-10-16")
    async with httpx.AsyncClient(transport=other.async_transport()) as second:
        response = await second.get(BASE + "/1/members/me", params=dict(client.params))
        assert response.json() == me


@pytest.mark.parametrize("auth", [{}, {"key": "bad"}, {"key": "bad", "token": "bad"}])
async def test_auth_failures_are_plain_text(client: httpx.AsyncClient, auth: dict[str, str]) -> None:
    client.params = httpx.QueryParams(auth)
    response = await client.get("/1/members/me")
    assert response.status_code == 401 and response.text == "invalid key"
    assert response.headers["content-type"].startswith("text/plain")


async def test_invalid_expired_revoked_and_readonly_tokens(
    client: httpx.AsyncClient, card: dict[str, Any]
) -> None:
    invalid = await client.get("/1/members/me", params={"token": "bad"})
    assert invalid.status_code == 401 and invalid.text == "invalid token"
    expired = await mint(client, expires_in=0)
    assert (await client.get("/1/members/me", params={"token": expired["token"]})).status_code == 401
    readonly = await mint(client, scopes=["read"])
    assert (await client.get("/1/members/me", params={"token": readonly["token"]})).status_code == 200
    denied = await client.put(
        f"/1/cards/{card['id']}", params={"token": readonly["token"]}, json={"name": "Forbidden"}
    )
    assert denied.status_code == 401 and denied.text == "unauthorized permission requested"
    revoked = await mint(client)
    assert (
        await client.delete(f"/1/tokens/{revoked['token']}", params={"token": revoked["token"]})
    ).json() == {}
    assert (await client.get("/1/members/me", params={"token": revoked["token"]})).status_code == 401


async def test_oauth_header_bearer_binding_and_personal_authorize(client: httpx.AsyncClient) -> None:
    credentials = dict(client.params)
    client.params = httpx.QueryParams()
    header = f'OAuth oauth_consumer_key="{credentials["key"]}", oauth_token="{credentials["token"]}"'
    assert (await client.get("/1/members/me", headers={"Authorization": header})).status_code == 200
    assert (
        await client.get("/1/members/me", headers={"Authorization": "Bearer " + credentials["token"]})
    ).status_code == 200
    another = await mint(client, client_id="separate-client")
    denied = await client.get("/1/members/me", params={"key": credentials["key"], "token": another["token"]})
    assert denied.status_code == 401
    response = await client.get(
        "https://trello.com/1/authorize",
        params={
            "key": credentials["key"],
            "response_type": "token",
            "scope": "read,write",
            "expiration": "1day",
            "return_url": "https://consumer.example/callback",
            "login_hint": "maya.chen@halcyonadvisory.example",
        },
    )
    assert response.status_code == 302
    token = parse_qs(urlsplit(response.headers["location"]).fragment)["token"][0]
    me = (await client.get("/1/members/me", params={"key": credentials["key"], "token": token})).json()
    assert me["fullName"] == "Maya Chen"


async def test_private_boards_search_expansions_and_public_access(
    client: httpx.AsyncClient, board: dict[str, Any], card: dict[str, Any]
) -> None:
    other = await mint(client, "p026")
    params = {"token": other["token"]}
    for path in (
        f"boards/{board['id']}",
        f"boards/{board['shortLink']}",
        f"cards/{card['id']}",
        f"lists/{card['idList']}",
    ):
        response = await client.get("/1/" + path, params=params)
        assert response.status_code == 401 and response.text == "unauthorized permission requested"
    visible = (
        await client.get("/1/search", params={**params, "query": "Review", "modelTypes": "cards"})
    ).json()
    assert all(c["id"] != card["id"] for c in visible["cards"])
    me = (await client.get("/1/members/me", params=params)).json()
    assert board["id"] not in me["idBoards"]
    public = await client.put(f"/1/boards/{board['id']}", json={"prefs_permissionLevel": "public"})
    assert public.status_code == 200
    assert (await client.get(f"/1/cards/{card['shortLink']}", params={"token": ""})).status_code == 200
    denied = await client.put(f"/1/cards/{card['id']}", params=params, json={"name": "Hijacked"})
    assert denied.status_code == 401


async def test_board_membership_observer_and_last_admin(
    client: httpx.AsyncClient, board: dict[str, Any], card: dict[str, Any]
) -> None:
    other = await mint(client, "p026")
    me = (await client.get("/1/members/me")).json()
    response = await client.put(
        f"/1/boards/{board['id']}/members/{other['idMember']}", data={"type": "observer"}
    )
    assert response.status_code == 200
    assert (await client.get(f"/1/cards/{card['id']}", params={"token": other["token"]})).status_code == 200
    assert (
        await client.put(f"/1/cards/{card['id']}", params={"token": other["token"]}, json={"name": "No"})
    ).status_code == 401
    assert (await client.delete(f"/1/boards/{board['id']}/members/{me['id']}")).status_code == 400
    await client.put(f"/1/boards/{board['id']}/members/{other['idMember']}", data={"type": "normal"})
    allowed = await client.put(
        f"/1/cards/{card['id']}", params={"token": other["token"]}, data={"name": "Allowed"}
    )
    assert allowed.status_code == 200
    await client.post(f"/1/cards/{card['id']}/idMembers", data={"value": other["idMember"]})
    await client.delete(f"/1/boards/{board['id']}/members/{other['idMember']}")
    assert other["idMember"] not in (await client.get(f"/1/cards/{card['id']}")).json()["idMembers"]


async def test_card_crud_move_shortids_fields_and_nested_resources(
    client: httpx.AsyncClient, board: dict[str, Any], card: dict[str, Any]
) -> None:
    lists = (await client.get(f"/1/boards/{board['id']}/lists")).json()
    another = (
        await client.post("/1/cards", data={"idList": lists[0]["id"], "name": "Second", "pos": "top"})
    ).json()
    assert another["idShort"] == card["idShort"] + 1
    assert another["pos"] < card["pos"]
    response = await client.put(
        f"/1/cards/{card['id']}",
        json={"idList": lists[1]["id"], "due": "2026-12-01T12:00:00-05:00", "dueComplete": True},
    )
    assert response.status_code == 200 and response.json()["due"] == "2026-12-01T17:00:00.000Z"
    projected = (
        await client.get(
            f"/1/cards/{card['shortLink']}",
            params={
                "fields": "name,idList",
                "list": "true",
                "board": "true",
                "members": "true",
                "checklists": "all",
            },
        )
    ).json()
    assert set(projected) == {"id", "name", "idList", "list", "board", "members", "checklists"}
    assert projected["list"]["id"] == lists[1]["id"]
    assert (await client.get(f"/1/cards/{card['id']}/name")).json() == {"_value": "Review work"}
    await client.put(f"/1/cards/{card['id']}", data={"closed": "true", "due": "null"})
    assert card["id"] not in {c["id"] for c in (await client.get(f"/1/boards/{board['id']}/cards")).json()}
    closed = (await client.get(f"/1/boards/{board['id']}/cards/closed")).json()
    assert [c["id"] for c in closed] == [card["id"]]
    await client.put(f"/1/cards/{card['id']}", json={"closed": False})
    assert (await client.delete(f"/1/cards/{card['id']}")).json() == {}
    assert (await client.get(f"/1/cards/{card['id']}")).status_code == 404


@pytest.mark.parametrize(
    "update",
    [
        {"closed": 1},
        {"dueComplete": "maybe"},
        {"name": []},
        {"desc": {}},
        {"name": "bad\x00name"},
        {"name": "bad\ud800"},
        {"pos": True},
        {"pos": -1},
        {"pos": "NaN"},
        {"idMembers": [True]},
        {"idLabels": {}},
        {"idList": []},
        {"due": "not-date"},
        {"start": 42},
        {"cover": {}},
    ],
)
async def test_failed_card_updates_are_atomic(
    client: httpx.AsyncClient, card: dict[str, Any], update: dict[str, Any]
) -> None:
    url = f"/1/cards/{card['id']}"
    before = (await client.get(url)).json()
    actions = (await client.get(url + "/actions")).json()
    response = await client.put(
        url,
        content=json.dumps({"desc": "Must not persist", **update}),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 400, response.text
    assert response.headers["content-type"].startswith("text/plain")
    assert (await client.get(url)).json() == before
    assert (await client.get(url + "/actions")).json() == actions


@pytest.mark.parametrize("raw", [b"[]", b"null", b'{"pos":NaN}', b'{"pos":Infinity}', b'{"name":', b'"text"'])
async def test_json_shape_constants_and_parser_failures(
    client: httpx.AsyncClient, card: dict[str, Any], raw: bytes
) -> None:
    response = await client.put(
        f"/1/cards/{card['id']}", content=raw, headers={"Content-Type": "application/json"}
    )
    assert response.status_code == 400 and response.text == "invalid JSON"


async def test_lists_labels_and_badges(
    client: httpx.AsyncClient, board: dict[str, Any], card: dict[str, Any]
) -> None:
    list_ = (
        await client.post(f"/1/boards/{board['id']}/lists", data={"name": "Review queue", "pos": "bottom"})
    ).json()
    assert (await client.put(f"/1/lists/{list_['id']}/closed", data={"value": "true"})).json()[
        "closed"
    ] is True
    await client.put(f"/1/lists/{list_['id']}/closed", json={"value": False})
    assert (await client.put(f"/1/cards/{card['id']}", json={"idList": list_["id"]})).status_code == 200
    label = (
        await client.post("/1/labels", json={"idBoard": board["id"], "name": "Urgent", "color": "red"})
    ).json()
    assert (
        await client.post(f"/1/cards/{card['id']}/idLabels", data={"value": label["id"]})
    ).status_code == 200
    assert (await client.get(f"/1/cards/{card['id']}")).json()["labels"][0]["name"] == "Urgent"
    await client.put(f"/1/labels/{label['id']}", json={"name": "Priority", "color": None})
    assert (await client.get(f"/1/cards/{card['id']}")).json()["labels"][0]["color"] is None
    await client.delete(f"/1/labels/{label['id']}")
    assert (await client.get(f"/1/cards/{card['id']}")).json()["idLabels"] == []
    await client.post(f"/1/lists/{list_['id']}/archiveAllCards")
    assert (await client.get(f"/1/cards/{card['id']}")).json()["closed"] is True


async def test_checklist_items_comments_and_action_paging(
    client: httpx.AsyncClient, card: dict[str, Any]
) -> None:
    checklist = (await client.post(f"/1/cards/{card['id']}/checklists", json={"name": "Review"})).json()
    item = (
        await client.post(
            f"/1/checklists/{checklist['id']}/checkItems", data={"name": "Check documents", "checked": "true"}
        )
    ).json()
    assert item["state"] == "complete"
    summary = (await client.get(f"/1/cards/{card['id']}")).json()
    assert summary["badges"]["checkItems"] == summary["badges"]["checkItemsChecked"] == 1
    response = await client.put(
        f"/1/cards/{card['id']}/checkItem/{item['id']}", json={"state": "incomplete", "name": "Recheck"}
    )
    assert response.status_code == 200 and response.json()["state"] == "incomplete"
    before = response.json()
    assert (
        await client.put(f"/1/cards/{card['id']}/checkItem/{item['id']}", json={"name": "No", "state": "bad"})
    ).status_code == 400
    assert (await client.get(f"/1/checklists/{checklist['id']}/checkItems/{item['id']}")).json() == before
    comments = []
    for i in range(5):
        comments.append(
            (
                await client.post(f"/1/cards/{card['id']}/actions/comments", data={"text": f"Comment {i}"})
            ).json()
        )
    page = (
        await client.get(f"/1/cards/{card['id']}/actions", params={"filter": "commentCard", "limit": 2})
    ).json()
    assert len(page) == 2 and page[0]["date"] >= page[1]["date"]
    next_page = (
        await client.get(
            f"/1/cards/{card['id']}/actions",
            params={"filter": "commentCard", "limit": 2, "before": page[-1]["id"]},
        )
    ).json()
    assert len(next_page) == 2 and not {a["id"] for a in page} & {a["id"] for a in next_page}
    action = comments[0]
    updated = await client.put(
        f"/1/cards/{card['id']}/actions/{action['id']}/comments", json={"text": "Edited"}
    )
    assert updated.json()["data"]["text"] == "Edited"
    await client.delete(f"/1/cards/{card['id']}/actions/{action['id']}/comments")
    await client.delete(f"/1/checklists/{checklist['id']}/checkItems/{item['id']}")
    assert (await client.get(f"/1/cards/{card['id']}")).json()["badges"]["checkItems"] == 0
    await client.delete(f"/1/checklists/{checklist['id']}")
    assert (await client.get(f"/1/cards/{card['id']}")).json()["idChecklists"] == []


async def test_attachments_bytes_links_acl_and_cleanup(
    client: httpx.AsyncClient, card: dict[str, Any]
) -> None:
    url = f"/1/cards/{card['id']}/attachments"
    uploaded = await client.post(
        url, files={"file": ("client résumé.txt", b"Halcyon document\n", "text/plain")}
    )
    assert uploaded.status_code == 200, uploaded.text
    attachment = uploaded.json()
    assert attachment["isUpload"] is True and attachment["bytes"] == 17
    assert (await client.get(attachment["url"])).status_code == 401
    downloaded = await client.get(attachment["url"], headers=download_auth(client))
    assert downloaded.content == b"Halcyon document\n" and downloaded.headers["content-type"].startswith(
        "text/plain"
    )
    outsider = await mint(client, "p026")
    assert (await client.get(attachment["url"], params={"token": outsider["token"]})).status_code == 401
    linked = (
        await client.post(url, json={"url": "https://halcyonadvisory.example/brief", "name": "Brief"})
    ).json()
    assert linked["isUpload"] is False and linked["url"] == "https://halcyonadvisory.example/brief"
    assert (await client.get(f"/1/cards/{card['id']}", params={"attachments": "true"})).json()["badges"][
        "attachments"
    ] == 2
    assert (await client.post(url, json={"url": "file:///secret"})).status_code == 400
    await client.delete(url + "/" + attachment["id"])
    assert (await client.get(attachment["url"])).status_code == 404
    await client.delete(f"/1/cards/{card['id']}")
    assert not client.hub.context("trello").store["attachments"].where(idCard=card["id"])  # type: ignore[attr-defined]


async def test_seed_attachment_is_real_document(client: httpx.AsyncClient) -> None:
    token = await mint(client, "p018")
    ctx = client.hub.context("trello")  # type: ignore[attr-defined]
    board = next(
        b
        for b in ctx.store["boards"]
        if b.get("_members", {}).get(token["idMember"]) == "admin"
        and ctx.store["attachments"].first(idBoard=b["id"])
    )
    card = ctx.store["cards"].first(idBoard=board["id"])
    attachments = (
        await client.get(f"/1/cards/{card['id']}/attachments", params={"token": token["token"]})
    ).json()
    response = await client.get(attachments[0]["url"], headers=download_auth(client, token["token"]))
    assert response.status_code == 200 and len(response.content) > 100
    assert response.content.startswith((b"PK", b"%PDF", b"\x89PNG")) or b"Halcyon" in response.content


async def test_batch_read_errors_and_search_filtering(
    client: httpx.AsyncClient, board: dict[str, Any], card: dict[str, Any]
) -> None:
    response = await client.get(
        "/1/batch",
        params={
            "urls": f"/cards/{card['id']}?fields=name,/boards/{board['id']}?fields=name,/no-such-resource"
        },
    )
    assert response.status_code == 200
    assert response.json()[0] == {"200": {"id": card["id"], "name": "Review work"}}
    assert response.json()[-1] == {"404": "not found"}
    for urls in (
        "/batch?urls=/members/me",
        "https://api.trello.com/1/members/me",
        "/1/members/me",
        "/members/me?token=another",
        "/../members/me",
        ",".join(["/members/me"] * 11),
    ):
        assert (await client.get("/1/batch", params={"urls": urls})).status_code == 400
    search = await client.get(
        "/1/search",
        params={
            "query": 'Review board:"Integration board" is:open',
            "modelTypes": "cards",
            "card_board": "true",
            "card_list": "true",
            "card_fields": "name",
        },
    )
    assert search.status_code == 200, search.text
    assert [c["id"] for c in search.json()["cards"]] == [card["id"]]
    assert search.json()["cards"][0]["board"]["id"] == board["id"]


@pytest.mark.parametrize(
    "params",
    [
        {"limit": "1.5"},
        {"limit": 0},
        {"limit": 1001},
        {"before": "bad"},
        {"since": "[]"},
        {"fields": "badField"},
    ],
)
async def test_action_pagination_and_fields_validation(
    client: httpx.AsyncClient, card: dict[str, Any], params: dict[str, Any]
) -> None:
    response = await client.get(f"/1/cards/{card['id']}/actions", params=params)
    assert response.status_code == 400, response.text


async def test_urls_prefix_and_dedicated_ports(client: httpx.AsyncClient, card: dict[str, Any]) -> None:
    hub = client.hub  # type: ignore[attr-defined]
    for root, app in (
        ("http://localhost:44080/trello", hub.gateway),
        ("http://localhost:44118", hub.apps["trello"]),
    ):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), params=client.params) as routed:
            response = await routed.get(root + f"/1/cards/{card['id']}", params={"board": "true"})
            assert response.status_code == 200, response.text
            assert response.json()["url"].startswith(root + "/c/")
            assert response.json()["board"]["url"].startswith(root + "/b/")


async def test_control_simulations_follow_rest_permissions(
    client: httpx.AsyncClient, board: dict[str, Any], card: dict[str, Any]
) -> None:
    lists = (await client.get(f"/1/boards/{board['id']}/lists")).json()
    comment = await client.post(
        "/_emulator/actions/post_comment", json={"user": "p001", "card": card["id"], "text": "Simulation"}
    )
    assert comment.status_code == 200 and comment.json()["type"] == "commentCard"
    move = await client.post(
        "/_emulator/actions/move_card", json={"user": "p001", "card": card["id"], "idList": lists[1]["id"]}
    )
    assert move.status_code == 200 and move.json()["idList"] == lists[1]["id"]
    denied = await client.post(
        "/_emulator/actions/post_comment", json={"user": "p026", "card": card["id"], "text": "No"}
    )
    assert denied.status_code == 401
