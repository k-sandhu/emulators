"""Cross-resource changes and malformed requests must preserve Trello invariants."""

from __future__ import annotations

import copy
from typing import Any

import httpx
import pytest

from .conftest import download_auth, mint


async def test_crossboard_move_keeps_documents_and_maps_labels(
    client: httpx.AsyncClient, board: dict[str, Any], card: dict[str, Any]
) -> None:
    target = (await client.post("/1/boards", json={"name": "Destination"})).json()
    target_list = (await client.get(f"/1/boards/{target['id']}/lists")).json()[0]
    who = (await client.get("/1/members/me")).json()
    await client.post(f"/1/cards/{card['id']}/idMembers", json={"value": who["id"]})
    label = (
        await client.post("/1/labels", json={"idBoard": board["id"], "name": "Delivery", "color": "blue"})
    ).json()
    await client.post(f"/1/cards/{card['id']}/idLabels", json={"value": label["id"]})
    checklist = (await client.post(f"/1/cards/{card['id']}/checklists", json={"name": "Scope"})).json()
    attachment = (
        await client.post(
            f"/1/cards/{card['id']}/attachments", files={"file": ("scope.txt", b"scope", "text/plain")}
        )
    ).json()
    response = await client.put(f"/1/cards/{card['id']}", json={"idBoard": target["id"]})
    assert response.status_code == 200, response.text
    moved = response.json()
    assert moved["idList"] == target_list["id"] and moved["idShort"] == 1
    assert moved["idMembers"] == [who["id"]]
    assert moved["labels"][0]["name"] == "Delivery" and moved["labels"][0]["idBoard"] == target["id"]
    assert moved["labels"][0]["id"] != label["id"]
    assert (await client.get(f"/1/checklists/{checklist['id']}")).json()["idBoard"] == target["id"]
    assert (await client.get(attachment["url"], headers=download_auth(client))).content == b"scope"
    source_actions = (
        await client.get(f"/1/boards/{board['id']}/actions", params={"filter": "moveCardFromBoard"})
    ).json()
    target_actions = (
        await client.get(f"/1/boards/{target['id']}/actions", params={"filter": "moveCardToBoard"})
    ).json()
    assert len(source_actions) == len(target_actions) == 1


async def test_action_card_expansion_respects_current_board_acl(
    client: httpx.AsyncClient, board: dict[str, Any], card: dict[str, Any]
) -> None:
    other = await mint(client, "p026")
    await client.put(f"/1/boards/{board['id']}/members/{other['idMember']}", json={"type": "normal"})
    target = (await client.post("/1/boards", json={"name": "Secret destination"})).json()
    await client.put(f"/1/cards/{card['id']}", json={"idBoard": target["id"]})
    actions = (
        await client.get(
            f"/1/boards/{board['id']}/actions",
            params={"filter": "moveCardFromBoard", "token": other["token"]},
        )
    ).json()
    response = await client.get(f"/1/actions/{actions[0]['id']}/card", params={"token": other["token"]})
    assert response.status_code == 401 and "desc" not in response.text


async def test_destination_permissions_and_crossboard_references_are_atomic(
    client: httpx.AsyncClient, board: dict[str, Any], card: dict[str, Any]
) -> None:
    other = await mint(client, "p026")
    await client.put(f"/1/boards/{board['id']}/members/{other['idMember']}", json={"type": "normal"})
    target = (await client.post("/1/boards", json={"name": "Private destination"})).json()
    target_list = (await client.get(f"/1/boards/{target['id']}/lists")).json()[0]
    before = (await client.get(f"/1/cards/{card['id']}")).json()
    denied = await client.put(
        f"/1/cards/{card['id']}",
        params={"token": other["token"]},
        json={"name": "No", "idList": target_list["id"]},
    )
    assert denied.status_code == 401
    assert (await client.get(f"/1/cards/{card['id']}")).json() == before
    label = (await client.get(f"/1/boards/{target['id']}/labels")).json()[0]
    invalid = await client.put(f"/1/cards/{card['id']}", json={"name": "No", "idLabels": [label["id"]]})
    assert invalid.status_code == 400
    assert (await client.get(f"/1/cards/{card['id']}")).json() == before


async def test_workspace_members_board_visibility_and_admin_rights(client: httpx.AsyncClient) -> None:
    workspace = (
        await client.post(
            "/1/organizations", json={"displayName": "Halcyon Delivery", "name": "halcyon_delivery"}
        )
    ).json()
    colleague = await mint(client, "p026")
    member_url = f"/1/organizations/{workspace['id']}/members/{colleague['idMember']}"
    await client.put(member_url, json={"type": "normal"})
    board = (
        await client.post(
            "/1/boards",
            json={
                "name": "Workspace visible",
                "idOrganization": workspace["id"],
                "prefs_permissionLevel": "org",
            },
        )
    ).json()
    private = (
        await client.post("/1/boards", json={"name": "Private", "idOrganization": workspace["id"]})
    ).json()
    params = {"token": colleague["token"]}
    result = (await client.get(f"/1/organizations/{workspace['id']}", params=params)).json()
    assert board["id"] in result["idBoards"] and private["id"] not in result["idBoards"]
    assert (await client.get(f"/1/boards/{board['id']}", params=params)).status_code == 200
    assert (
        await client.put(f"/1/organizations/{workspace['id']}", params=params, json={"displayName": "No"})
    ).status_code == 401
    await client.put(f"/1/organizations/{workspace['id']}", json={"displayName": "Updated delivery"})
    assert (await client.get(f"/1/organizations/{workspace['name']}")).json()[
        "displayName"
    ] == "Updated delivery"
    await client.delete(f"/1/organizations/{workspace['id']}")
    assert (await client.get(f"/1/boards/{board['id']}")).json()["idOrganization"] is None


@pytest.mark.parametrize("field,value", [("name", []), ("desc", {}), ("closed", 1), ("pos", True)])
async def test_list_and_board_invalid_updates_preserve_state(
    client: httpx.AsyncClient, board: dict[str, Any], field: str, value: Any
) -> None:
    list_ = (await client.get(f"/1/boards/{board['id']}/lists")).json()[0]
    before = copy.deepcopy(list_)
    response = await client.put(f"/1/lists/{list_['id']}", json={"name": "No", field: value})
    if field == "desc":
        # desc is not a supported list field; use the board's documented desc parameter.
        before_board = (await client.get(f"/1/boards/{board['id']}")).json()
        response = await client.put(f"/1/boards/{board['id']}", json={"name": "No", "desc": value})
        assert response.status_code == 400
        assert (await client.get(f"/1/boards/{board['id']}")).json() == before_board
    else:
        assert response.status_code == 400
        assert (await client.get(f"/1/lists/{list_['id']}")).json() == before


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE", "PATCH"])
@pytest.mark.parametrize("resource", ["boards", "organizations", "checklists", "webhooks"])
async def test_unsupported_method_shapes_do_not_crash(
    client: httpx.AsyncClient, board: dict[str, Any], method: str, resource: str
) -> None:
    target = f"/1/{resource}/{board['id']}" if resource != "webhooks" else "/1/webhooks"
    response = await client.request(method, target, json={} if method in ("POST", "PUT", "PATCH") else None)
    assert response.status_code < 500, response.text


@pytest.mark.parametrize("parameter", ["cards", "lists", "members", "memberships"])
async def test_invalid_nested_expansion_filters_are_rejected(
    client: httpx.AsyncClient, board: dict[str, Any], parameter: str
) -> None:
    response = await client.get(f"/1/boards/{board['id']}", params={parameter: "bad"})
    assert response.status_code == 400


async def test_token_requires_key_and_unexposed_member_email(client: httpx.AsyncClient) -> None:
    token = dict(client.params)["token"]
    client.params = httpx.QueryParams({"token": token})
    assert (await client.get("/1/members/me")).status_code == 401
    response = await client.get("/1/members/me", headers={"Authorization": "Bearer " + token})
    assert response.status_code == 200
    colleague = await mint(client, "p026")
    response = await client.get(
        f"/1/members/{colleague['idMember']}", headers={"Authorization": "Bearer " + token}
    )
    assert response.status_code == 200 and "email" not in response.json()


async def test_account_scope_is_required_for_own_email(client: httpx.AsyncClient) -> None:
    readonly = await mint(client, scopes=["read"])
    me = await client.get("/1/members/me", params={"token": readonly["token"]})
    assert me.status_code == 200 and "email" not in me.json()
    email = await client.get("/1/members/me", params={"token": readonly["token"], "fields": "email"})
    assert email.status_code == 401 and email.text == "unauthorized permission requested"
    own = (await client.get("/1/members/me", params={"fields": "email"})).json()
    assert own["email"] == "eleanor.whitfield@halcyonadvisory.example"


async def test_membership_filters_include_member_details(
    client: httpx.AsyncClient, board: dict[str, Any]
) -> None:
    other = await mint(client, "p026")
    await client.put(f"/1/boards/{board['id']}/members/{other['idMember']}", json={"type": "normal"})
    memberships = (
        await client.get(
            f"/1/boards/{board['id']}/memberships", params={"filter": "normal", "member": "true"}
        )
    ).json()
    assert len(memberships) == 1 and memberships[0]["idMember"] == other["idMember"]
    assert memberships[0]["member"]["fullName"]


async def test_card_and_action_cursor_pages_nulls_and_page_parameter(
    client: httpx.AsyncClient, card: dict[str, Any]
) -> None:
    for i in range(4):
        await client.post("/1/cards", json={"idList": card["idList"], "name": f"Page {i}"})
        await client.post(f"/1/cards/{card['id']}/actions/comments", json={"text": f"Page {i}"})
    first = (await client.get(f"/1/lists/{card['idList']}/cards", params={"limit": 2})).json()
    second = (
        await client.get(f"/1/lists/{card['idList']}/cards", params={"limit": 2, "before": first[-1]["id"]})
    ).json()
    assert len(first) == len(second) == 2
    assert not {c["id"] for c in first} & {c["id"] for c in second}
    actions = (
        await client.get(
            f"/1/cards/{card['id']}/actions",
            params={"limit": 2, "filter": "commentCard", "page": 1, "before": "null"},
        )
    ).json()
    assert [a["data"]["text"] for a in actions] == ["Page 1", "Page 0"]


async def test_visible_cards_exclude_archived_lists_without_archiving_cards(
    client: httpx.AsyncClient, board: dict[str, Any], card: dict[str, Any]
) -> None:
    await client.put(f"/1/lists/{card['idList']}", json={"closed": True})
    assert (await client.get(f"/1/cards/{card['id']}")).json()["closed"] is False
    visible = (await client.get(f"/1/boards/{board['id']}/cards/visible")).json()
    assert card["id"] not in {c["id"] for c in visible}
    expanded = (await client.get(f"/1/boards/{board['id']}", params={"cards": "visible"})).json()
    assert expanded["cards"] == []
    await client.put(f"/1/lists/{card['idList']}", json={"closed": False})
    assert (await client.get(f"/1/boards/{board['id']}/cards/visible")).json()[0]["id"] == card["id"]


async def test_fault_errors_use_plain_auth_and_json_ratelimit_bodies(client: httpx.AsyncClient) -> None:
    await client.post("/_emulator/faults", json={"status": 401, "path": "/1/members/me", "times": 1})
    response = await client.get("/1/members/me")
    assert response.status_code == 401 and response.text == "invalid token"
    assert response.headers["content-type"].startswith("text/plain")
    await client.post("/_emulator/faults", json={"status": 429, "path": "/1/members/me", "times": 1})
    response = await client.get("/1/members/me")
    assert response.status_code == 429
    assert response.json() == {"error": "API_TOKEN_LIMIT_EXCEEDED", "message": "Rate limit exceeded"}
