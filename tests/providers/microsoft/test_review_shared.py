from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from typing import Any

import pytest

from emulators.providers.microsoft import common as c
from emulators.testing.helpers import bearer, mint

BASE = "https://graph.microsoft.com"


@pytest.mark.parametrize("content", [b"not-json", b"[]", b'"text"', b"null", b"\xff"])
async def test_json_operations_reject_malformed_or_nonobject_bodies(
    make_client: Callable[..., Any], content: bytes
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p035")))
        uid = c.user_id(client.hub.world, "p035")
        for method, path in (
            ("POST", "/users"),
            ("PATCH", f"/users/{uid}"),
            ("PATCH", "/me"),
            ("POST", "/me/messages"),
            ("POST", "/me/sendMail"),
            ("POST", "/$batch"),
            ("POST", "/subscriptions"),
            ("PATCH", "/me/mailboxSettings"),
            ("POST", "/me/events"),
            ("POST", "/me/findMeetingTimes"),
            ("POST", "/me/calendar/getSchedule"),
            ("POST", "/me/contacts"),
            ("POST", "/me/contactFolders"),
            ("POST", "/groups"),
            ("POST", "/me/mailFolders"),
            ("POST", "/me/calendars"),
            ("POST", "/me/drive/root/children"),
            ("POST", "/me/onlineMeetings"),
        ):
            response = await client.request(
                method, "/v1.0" + path, content=content, headers={"Content-Type": "application/json"}
            )
            assert response.status_code == 400, (method, path, response.text)
            assert "Unable to read JSON request payload" in response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("path", "payload"),
    [
        ("/me/sendMail", {"message": "text"}),
        ("/$batch", {"requests": [None]}),
        ("/$batch", {"requests": [{"id": "1", "method": "GET", "url": "https://["}]}),
        ("/$batch", {"requests": [{"id": "1", "method": "GET", "url": "/me", "headers": []}]}),
        ("/me/events", {"start": {}, "end": {"dateTime": "2026-10-20T12:00:00", "timeZone": "UTC"}}),
        ("/me/events", {"start": [], "end": {}}),
        ("/me/messages", {"body": {"contentType": "HTML"}}),
        (
            "/me/findMeetingTimes",
            {"timeConstraint": {"timeSlots": [{"start": {"dateTime": "2026-10-20T12:00:00"}, "end": {}}]}},
        ),
        ("/me/findMeetingTimes", {"timeConstraint": []}),
        ("/me/calendar/getSchedule", {"startTime": [], "endTime": {}}),
        ("/directoryObjects/getByIds", {"ids": [{}]}),
    ],
)
async def test_nested_bad_json_never_crashes(
    make_client: Callable[..., Any], path: str, payload: Any
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        response = await client.post("/v1.0" + path, json=payload)
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "BadRequest"


async def test_correlation_headers_match_all_error_paths_and_concurrent_requests(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        unauth = await client.get("/v1.0/me", headers={"client-request-id": "unauthenticated"})
        assert unauth.status_code == 401
        assert unauth.headers["www-authenticate"] == c.BEARER_CHALLENGE
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        responses = [unauth]
        responses += await asyncio.gather(
            *(
                client.get("/v1.0/users/absent", headers={"client-request-id": f"concurrent-{i}"})
                for i in range(8)
            )
        )
        responses.append(await client.get("/v1.0/users?$top=bad"))
        responses.append(await client.post("/v1.0/me", json={}))
        await client.post("/_emulator/faults", json={"path": "/users", "status": 429})
        responses.append(await client.get("/v1.0/users", headers={"client-request-id": "fault"}))
        for response in responses:
            inner = response.json()["error"]["innerError"]
            assert inner["request-id"] == response.headers["request-id"]
            assert inner["client-request-id"] == response.headers["client-request-id"]
        assert len({r.headers["request-id"] for r in responses}) == len(responses)
        assert [r.headers["client-request-id"] for r in responses[1:9]] == [
            f"concurrent-{i}" for i in range(8)
        ]


@pytest.mark.parametrize("resource", ["users", "groups", "me/messages", "me/events"])
@pytest.mark.parametrize(
    "query",
    [{"$select": "noSuchProperty"}, {"$filter": "noSuchProperty eq null"}, {"$orderby": "noSuchProperty"}],
)
async def test_unknown_odata_properties_are_bad_requests(
    make_client: Callable[..., Any], resource: str, query: dict[str, str]
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        response = await client.get("/v1.0/" + resource, params=query)
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "BadRequest"


async def test_batch_headers_binary_and_per_request_correlation(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        photo = await client.get("/v1.0/me/photo/$value")
        await client.post(
            "/_emulator/faults",
            json={
                "path": "/users/absent",
                "status": 401,
                "headers": {"ETag": '"test"', "OData-Version": "4.0"},
            },
        )
        result = await client.post(
            "/v1.0/$batch",
            json={
                "requests": [
                    {"id": "1", "method": "GET", "url": "/me/photo/$value"},
                    {
                        "id": "2",
                        "method": "GET",
                        "url": "/users/absent",
                        "headers": {"client-request-id": "subrequest"},
                    },
                ]
            },
        )
        first, second = result.json()["responses"]
        assert first["headers"]["Content-Type"] == "image/png"
        assert all(key.lower() != "content-length" for key in first["headers"])
        assert base64.b64decode(first["body"]) == photo.content
        inner = second["body"]["error"]["innerError"]
        assert second["headers"]["ETag"] == '"test"'
        assert second["headers"]["OData-Version"] == "4.0"
        assert second["headers"]["WWW-Authenticate"] == c.BEARER_CHALLENGE
        assert inner["request-id"] == second["headers"]["request-id"]
        assert inner["client-request-id"] == second["headers"]["client-request-id"] == "subrequest"


@pytest.mark.parametrize(
    "resource",
    [
        "users",
        "groups",
        "users/delta",
        "groups/delta",
        "me/messages",
        "me/drive/root/children",
        "me/drive/root/delta",
    ],
)
async def test_preferred_page_size_and_literal_odata_nextlinks(
    make_client: Callable[..., Any], resource: str
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        response = await client.get("/v1.0/" + resource, headers={"Prefer": "odata.maxpagesize=2"})
        assert response.status_code == 200, response.text
        assert len(response.json()["value"]) == 2
        assert response.headers["Preference-Applied"] == "odata.maxpagesize=2"
        next_link = response.json()["@odata.nextLink"]
        assert "%24" not in next_link
        assert (await client.get(next_link, headers={"Prefer": "odata.maxpagesize=2"})).status_code == 200
