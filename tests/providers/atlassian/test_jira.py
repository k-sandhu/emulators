from __future__ import annotations

from typing import Any

import httpx
import pytest

from emulators.testing.helpers import bearer, mint

JIRA = "/rest/api/3"
SITE = "https://halcyonadvisory.atlassian.net"
DESCRIPTION = {
    "type": "doc",
    "version": 1,
    "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Validate access controls."}]}],
}


async def create_issue(client: httpx.AsyncClient, auth: dict[str, str], **fields: Any) -> dict[str, Any]:
    response = await client.post(
        JIRA + "/issue",
        headers=auth,
        json={
            "fields": {
                "project": {"key": "OPS"},
                "summary": "Integration test request",
                "issuetype": {"name": "Task"},
                "description": DESCRIPTION,
                "priority": {"name": "High"},
                "labels": ["integration"],
                **fields,
            }
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


async def test_myself_projects_users_and_issue_wire_shapes(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    myself = (await client.get(JIRA + "/myself", headers=auth)).json()
    assert myself["emailAddress"] == "eleanor.whitfield@halcyonadvisory.example" and myself["active"] is True
    projects = await client.get(JIRA + "/project/search", headers=auth, params={"maxResults": 1})
    assert projects.json()["isLast"] is False and projects.json()["total"] == 2
    next_page = await client.get(projects.json()["nextPage"], headers=auth)
    assert next_page.json()["isLast"] is True
    assert {projects.json()["values"][0]["key"], next_page.json()["values"][0]["key"]} == {"OPS", "ENG"}
    users = (await client.get(JIRA + "/user/search", headers=auth, params={"query": "maya"})).json()
    assert len(users) == 1 and users[0]["displayName"] == "Maya Chen"
    issue = (await client.get(JIRA + "/issue/ENG-2", headers=auth)).json()
    fields = issue["fields"]
    assert fields["summary"] and fields["description"]["type"] == "doc"
    assert fields["parent"]["key"] == "ENG-1" and fields["parent"]["fields"]["issuetype"]["name"] == "Epic"
    assert fields["status"]["name"] in ("To Do", "In Progress", "Done")
    assert isinstance(fields["labels"], list) and fields["created"].endswith("+0000")
    assert (await client.get(issue["self"], headers=auth)).json()["key"] == "ENG-2"
    projected = (
        await client.get(JIRA + "/issue/ENG-2", headers=auth, params={"fields": "summary,status"})
    ).json()
    assert set(projected["fields"]) == {"summary", "status"}


@pytest.mark.parametrize("method", ["get", "post"])
async def test_jql_paging_projection_current_user_and_updates(
    client: httpx.AsyncClient, auth: dict[str, str], method: str
) -> None:
    jira_user = (await client.get(JIRA + "/myself", headers=auth)).json()["accountId"]
    await create_issue(client, auth, assignee={"accountId": jira_user})
    jql = "project = OPS AND status != Done AND assignee = currentUser() ORDER BY updated DESC"
    payload = {"jql": jql, "maxResults": 1, "fields": ["summary", "assignee", "status", "updated"]}
    if method == "get":
        response = await client.get(
            JIRA + "/search/jql", headers=auth, params={**payload, "fields": ",".join(payload["fields"])}
        )
    else:
        response = await client.post(JIRA + "/search/jql", headers=auth, json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["issues"] and body["issues"][0]["fields"]["assignee"]["accountId"] == jira_user
    assert body["issues"][0]["fields"]["status"]["name"] != "Done"
    all_keys: list[str] = []
    data: dict[str, Any] = {
        "jql": "project = OPS ORDER BY updated DESC",
        "maxResults": 3,
        "fields": ["summary", "updated"],
    }
    timestamps = []
    while True:
        response = await client.post(JIRA + "/search/jql", headers=auth, json=data)
        assert response.status_code == 200, response.text
        body = response.json()
        all_keys.extend(i["key"] for i in body["issues"])
        timestamps.extend(i["fields"]["updated"] for i in body["issues"])
        if body["isLast"]:
            assert "nextPageToken" not in body
            break
        data["nextPageToken"] = body["nextPageToken"]
    assert len(all_keys) == 16 and len(set(all_keys)) == 16
    assert timestamps == sorted(timestamps, reverse=True)
    defaults = (await client.get(JIRA + "/search/jql", headers=auth, params={"jql": "project=ENG"})).json()
    assert "fields" not in defaults["issues"][0]
    negative_fields = (
        await client.get(
            JIRA + "/search/jql", headers=auth, params={"jql": "project=ENG", "fields": "*all,-description"}
        )
    ).json()
    assert (
        "summary" in negative_fields["issues"][0]["fields"]
        and "description" not in negative_fields["issues"][0]["fields"]
    )


async def test_issue_crud_transitions_comments_and_field_updates(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    created = await create_issue(client, auth)
    key = created["key"]
    issue = (await client.get(JIRA + "/issue/" + key, headers=auth)).json()
    assert (
        issue["fields"]["summary"] == "Integration test request"
        and issue["fields"]["priority"]["name"] == "High"
    )
    updated = await client.put(
        JIRA + "/issue/" + key,
        headers=auth,
        json={
            "fields": {"summary": "Updated request", "duedate": "2026-11-01"},
            "update": {"labels": [{"add": "triaged"}, {"remove": "integration"}]},
        },
    )
    assert updated.status_code == 204 and updated.content == b""
    issue = (await client.get(JIRA + "/issue/" + key, headers=auth)).json()
    assert issue["fields"]["labels"] == ["triaged"] and issue["fields"]["duedate"] == "2026-11-01"
    transitions = (await client.get(JIRA + f"/issue/{key}/transitions", headers=auth)).json()["transitions"]
    selected = next(t for t in transitions if t["to"]["name"] == "In Progress")
    assert (
        await client.post(
            JIRA + f"/issue/{key}/transitions", headers=auth, json={"transition": {"id": selected["id"]}}
        )
    ).status_code == 204
    assert (await client.get(JIRA + "/issue/" + key, headers=auth)).json()["fields"]["status"][
        "name"
    ] == "In Progress"
    assert (
        await client.post(
            JIRA + f"/issue/{key}/transitions", headers=auth, json={"transition": {"id": selected["id"]}}
        )
    ).status_code == 400
    comment = await client.post(JIRA + f"/issue/{key}/comment", headers=auth, json={"body": DESCRIPTION})
    assert comment.status_code == 201 and comment.json()["author"]["emailAddress"].endswith(".example")
    comments = (
        await client.get(JIRA + f"/issue/{key}/comment", headers=auth, params={"maxResults": 1})
    ).json()
    assert comments["total"] == 1 and comments["comments"][0]["id"] == comment.json()["id"]
    # A malformed later field must not partially change a valid earlier field.
    denied = await client.put(
        JIRA + "/issue/" + key,
        headers=auth,
        json={"fields": {"summary": "Must not persist", "duedate": "not-a-date"}},
    )
    assert denied.status_code == 400
    assert (await client.get(JIRA + "/issue/" + key, headers=auth)).json()["fields"][
        "summary"
    ] == "Updated request"
    assert (
        await client.put(
            JIRA + "/issue/" + key + "?returnIssue=true", headers=auth, json={"fields": {"assignee": None}}
        )
    ).status_code == 200


async def test_jira_project_and_issue_security_filters_every_surface(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    staff = bearer(await mint(client, SITE, user="p039"))
    assert (await client.get(JIRA + "/issue/ENG-1", headers=staff)).status_code == 404
    project_list = (await client.get(JIRA + "/project/search", headers=staff)).json()["values"]
    assert [p["key"] for p in project_list] == ["OPS"]
    limited = bearer(await mint(client, SITE, user="p026"))
    assert (await client.get(JIRA + "/issue/ENG-1", headers=limited)).status_code == 200
    for path in ("/issue/ENG-3", "/issue/ENG-3/comment", "/issue/ENG-3/transitions"):
        response = await client.get(JIRA + path, headers=limited)
        assert response.status_code == 404 and response.json()["errors"] == {}
    visible = await client.get(
        JIRA + "/search/jql", headers=limited, params={"jql": "key=ENG-3", "fields": "*all"}
    )
    assert visible.json()["issues"] == []
    assert (await client.get(JIRA + "/issue/ENG-3", headers=auth)).status_code == 200
    assert (
        await client.put(JIRA + "/issue/ENG-1", headers=limited, json={"fields": {"summary": "Denied"}})
    ).status_code == 403
    assert (
        await client.post(
            JIRA + "/issue",
            headers=limited,
            json={"fields": {"project": {"key": "ENG"}, "summary": "Denied", "issuetype": {"name": "Task"}}},
        )
    ).status_code == 403


@pytest.mark.parametrize(
    "jql",
    [
        "",
        "ORDER BY updated DESC",
        "project=OPS AND",
        "unknown=OPS",
        "project=OPS OR madeup=anything",
        'summary ~ "unterminated',
        "assignee=badFunction()",
        "updated > not-a-date",
    ],
)
async def test_invalid_jql_returns_jira_envelope(
    client: httpx.AsyncClient, auth: dict[str, str], jql: str
) -> None:
    response = await client.get(JIRA + "/search/jql", headers=auth, params={"jql": jql})
    assert (
        response.status_code == 400 and response.json()["errorMessages"] and response.json()["errors"] == {}
    )


@pytest.mark.parametrize(
    "fields",
    [
        {"description": "not-adf"},
        {"labels": "not-an-array"},
        {"assignee": {"accountId": "missing"}},
        {"priority": {"name": "Urgent"}},
        {"status": {"name": "Done"}},
        {"parent": {"key": "OPS-1"}},
    ],
)
async def test_issue_invalid_fields_are_rejected(
    client: httpx.AsyncClient, auth: dict[str, str], fields: dict[str, Any]
) -> None:
    response = await client.put(JIRA + "/issue/OPS-1", headers=auth, json={"fields": fields})
    assert response.status_code == 400 and "errorMessages" in response.json()


async def test_jql_cursor_binding_and_paging_validation(
    client: httpx.AsyncClient, auth: dict[str, str]
) -> None:
    first = (
        await client.get(JIRA + "/search/jql", headers=auth, params={"jql": "project=OPS", "maxResults": 1})
    ).json()
    token = first["nextPageToken"]
    changed = await client.get(
        JIRA + "/search/jql",
        headers=auth,
        params={"jql": "project=ENG", "maxResults": 1, "nextPageToken": token},
    )
    assert changed.status_code == 400
    for params in ({"maxResults": "invalid"}, {"maxResults": 0}, {"nextPageToken": "bad"}):
        response = await client.get(
            JIRA + "/search/jql", headers=auth, params={"jql": "project=OPS", **params}
        )
        assert response.status_code == 400
