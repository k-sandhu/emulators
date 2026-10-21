"""Unmodified requests/httpx integration clients through real Trello hosts and live sockets."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
import requests

pytestmark = pytest.mark.sdk
BASE = "https://api.trello.com/1"


@pytest.fixture
def credentials(emulator_intercept: object) -> dict[str, str]:
    return requests.get("https://api.trello.com/_emulator/info", timeout=15).json()["credentials"]


@pytest.fixture
def client(credentials: dict[str, str]) -> Any:
    with httpx.Client(
        base_url=BASE, params={"key": credentials["key"], "token": credentials["token"]}, timeout=15
    ) as client:
        yield client


def create_board(client: httpx.Client) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    response = client.post("/boards/", params={"name": "Socket workflow"})
    assert response.status_code == 200, response.text
    board = response.json()
    lists = client.get(f"/boards/{board['id']}/lists").json()
    return board, lists


def test_client_sync_nested_resources_and_fields(client: httpx.Client) -> None:
    me = client.get("/members/me").json()
    assert me["fullName"] == "Eleanor Whitfield"
    organizations = client.get("/members/me/organizations").json()
    firm = next(o for o in organizations if o["displayName"] == "Halcyon Advisory Group")
    boards = client.get(
        f"/organizations/{firm['id']}/boards",
        params={"lists": "open", "cards": "all", "fields": "name,idOrganization"},
    ).json()
    assert boards and all("lists" in b and "cards" in b for b in boards)
    assert all(set(b) == {"id", "name", "idOrganization", "lists", "cards"} for b in boards)


def test_requests_form_card_workflow_and_comments(credentials: dict[str, str], client: httpx.Client) -> None:
    board, lists = create_board(client)
    with requests.Session() as session:
        session.params.update(key=credentials["key"], token=credentials["token"])
        response = session.post(
            BASE + "/cards", data={"name": "Approve engagement", "idList": lists[0]["id"]}, timeout=15
        )
        assert response.status_code == 200
        card = response.json()
        moved = session.put(
            BASE + f"/cards/{card['id']}", json={"idList": lists[1]["id"], "dueComplete": True}, timeout=15
        )
        assert moved.status_code == 200 and moved.json()["idList"] == lists[1]["id"]
        comment = session.post(
            BASE + f"/cards/{card['id']}/actions/comments", data={"text": "Approved by partner"}, timeout=15
        )
        assert comment.json()["type"] == "commentCard"
        actions = session.get(
            BASE + f"/boards/{board['id']}/actions", params={"filter": "commentCard"}, timeout=15
        ).json()
        assert actions[0]["data"]["text"] == "Approved by partner"
        assert session.delete(BASE + f"/cards/{card['id']}", timeout=15).json() == {}


def test_checklists_labels_attachments_over_live_gateway(client: httpx.Client) -> None:
    board, lists = create_board(client)
    card = client.post("/cards", json={"name": "Review", "idList": lists[0]["id"]}).json()
    checklist = client.post("/checklists", json={"idCard": card["id"], "name": "Delivery"}).json()
    item = client.post(
        f"/checklists/{checklist['id']}/checkItems", data={"name": "Review", "checked": "true"}
    ).json()
    assert item["state"] == "complete"
    label = client.post(f"/boards/{board['id']}/labels", json={"name": "Done", "color": "green"}).json()
    assert client.post(f"/cards/{card['id']}/idLabels", data={"value": label["id"]}).status_code == 200
    uploaded = client.post(
        f"/cards/{card['id']}/attachments", files={"file": ("approval.txt", b"Approved\n", "text/plain")}
    )
    assert uploaded.status_code == 200
    header = f'OAuth oauth_consumer_key="{client.params["key"]}", oauth_token="{client.params["token"]}"'
    content = client.get(uploaded.json()["url"], headers={"Authorization": header})
    assert content.content == b"Approved\n"
    result = client.get(
        f"/cards/{card['shortLink']}", params={"attachments": "true", "checklists": "all"}
    ).json()
    assert result["badges"]["checkItemsChecked"] == 1 and result["labels"][0]["name"] == "Done"


def test_batch_search_and_plain_errors(client: httpx.Client) -> None:
    board, lists = create_board(client)
    card = client.post("/cards", params={"idList": lists[0]["id"], "name": "Socket approval"}).json()
    results = client.get(
        "/batch",
        params={"urls": f"/cards/{card['id']}?fields=name,/boards/{board['id']}?fields=name,/unknown"},
    ).json()
    assert results[0]["200"]["name"] == "Socket approval" and results[-1] == {"404": "not found"}
    search = client.get(
        "/search", params={"query": 'Socket board:"Socket workflow"', "modelTypes": "cards"}
    ).json()
    assert card["id"] in {c["id"] for c in search["cards"]}
    denied = client.get("/members/me", params={"token": "invalid"})
    assert denied.status_code == 401 and denied.text == "invalid token"
    assert denied.headers["content-type"].startswith("text/plain")


def test_oauth_header_and_readonly_control_token(credentials: dict[str, str], client: httpx.Client) -> None:
    token = requests.post(
        "https://api.trello.com/_emulator/tokens", json={"user": "p018", "scopes": ["read"]}, timeout=15
    ).json()
    header = f'OAuth oauth_consumer_key="{credentials["key"]}", oauth_token="{token["token"]}"'
    me = requests.get(BASE + "/members/me", headers={"Authorization": header}, timeout=15)
    assert me.status_code == 200 and me.json()["fullName"] == "Maya Chen"
    denied = requests.post(
        BASE + "/boards", headers={"Authorization": header}, json={"name": "No"}, timeout=15
    )
    assert denied.status_code == 401 and denied.text == "unauthorized permission requested"


def test_socket_action_paging_and_atomic_validation(client: httpx.Client) -> None:
    _, lists = create_board(client)
    card = client.post("/cards", json={"idList": lists[0]["id"], "name": "Paging"}).json()
    for i in range(4):
        assert client.post(f"/cards/{card['id']}/actions/comments", json={"text": str(i)}).status_code == 200
    first = client.get(f"/cards/{card['id']}/actions", params={"filter": "commentCard", "limit": 2}).json()
    second = client.get(
        f"/cards/{card['id']}/actions",
        params={"filter": "commentCard", "limit": 2, "before": first[-1]["id"]},
    ).json()
    assert [a["data"]["text"] for a in first + second] == ["3", "2", "1", "0"]
    before = client.get(f"/cards/{card['id']}").json()
    invalid = client.put(f"/cards/{card['id']}", json={"name": "Must not change", "pos": True})
    assert invalid.status_code == 400
    assert client.get(f"/cards/{card['id']}").json() == before
