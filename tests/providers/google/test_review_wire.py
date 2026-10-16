"""Cross-API transport, error and wrong-type regression contracts."""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest

from emulators.providers.google.api_schema import SCHEMAS
from emulators.providers.google.wire import route_schema
from emulators.testing.helpers import bearer, mint

BASE = "https://www.googleapis.com"


@pytest.mark.parametrize(
    "path,body",
    [
        ("/drive/v3/files", {"id": 5}),
        ("/gmail/v1/users/me/labels", {"name": 5}),
        ("/gmail/v1/users/me/watch", {"topicName": 5}),
        (
            "/gmail/v1/users/me/settings/filters",
            {"criteria": {"from": "maya.chen@halcyonadvisory.example"}, "action": 5},
        ),
        ("/calendar/v3/calendars/primary/events", {"id": 5}),
        (
            "/admin/directory/v1/users",
            {"name": "str", "primaryEmail": "test@halcyonadvisory.example", "password": "fixture"},
        ),
    ],
)
async def test_review_wrong_types_return_400(make_client: Any, path: str, body: dict[str, Any]) -> None:
    async with make_client("google") as client:
        result = await client.post(
            BASE + path, headers=bearer(await mint(client, BASE, user="p035")), json=body
        )
        assert result.status_code == 400 and result.json()["error"]["status"] == "INVALID_ARGUMENT"


async def test_fuzz_every_registered_discovery_post_patch_route(make_client: Any) -> None:
    """Discovery types cover every implemented mutation, including nested values."""
    async with make_client("google") as client:
        auth = bearer(await mint(client, BASE, user="p035"))
        checked = set()
        for route in client.hub.apps["google"].routes:
            template = getattr(route, "path", "")
            path = re.sub(r"\{[^}]+\}", "fixture", template)
            for method in ("POST", "PATCH"):
                if method not in getattr(route, "methods", set()):
                    continue
                spec = route_schema(method, path)
                if not spec or not spec[2] or (method, path) in checked:
                    continue
                checked.add((method, path))
                properties = SCHEMAS[spec[2]].get("properties", {})
                for name, schema in properties.items():
                    resolved = SCHEMAS[schema["$ref"]] if "$ref" in schema else schema
                    kind = resolved.get("type")
                    if kind not in ("string", "object", "array", "boolean", "integer", "number"):
                        continue
                    wrong = (
                        "wrong"
                        if kind in ("object", "array", "boolean", "integer", "number")
                        else {"wrong": True}
                    )
                    response = await client.request(method, BASE + path, headers=auth, json={name: wrong})
                    assert response.status_code == 400, (method, path, name, response.text)
                    assert response.json()["error"]["status"] == "INVALID_ARGUMENT"
                    if kind == "object":
                        for child_name, child in resolved.get("properties", {}).items():
                            child = SCHEMAS[child["$ref"]] if "$ref" in child else child
                            if child.get("type") not in (
                                "string",
                                "object",
                                "array",
                                "boolean",
                                "integer",
                                "number",
                            ):
                                continue
                            bad = {"wrong": True} if child.get("type") == "string" else "wrong"
                            response = await client.request(
                                method, BASE + path, headers=auth, json={name: {child_name: bad}}
                            )
                            assert response.status_code == 400, (
                                method,
                                path,
                                name,
                                child_name,
                                response.text,
                            )
        assert len(checked) >= 30
        # Meet's config validator covers the provider's non-discovery mutations.
        space = (await client.post("https://meet.googleapis.com/v2/spaces", headers=auth, json={})).json()
        for value in (5, "wrong", [], {"accessType": 5}, {"entryPointAccess": []}):
            for method, path in (
                ("POST", "/v2/spaces"),
                ("PATCH", "/v2/" + space["name"] + "?updateMask=config"),
            ):
                response = await client.request(
                    method, "https://meet.googleapis.com" + path, headers=auth, json={"config": value}
                )
                assert response.status_code == 400 and "errors" not in response.json()["error"]


@pytest.mark.parametrize(
    "base,path",
    [
        (BASE, "/batch"),
        (BASE, "/batch/drive/v3"),
        (BASE, "/batch/calendar/v3"),
        (BASE, "/batch/admin/directory_v1"),
        (BASE, "/batch/gmail/v1"),
        ("https://gmail.googleapis.com", "/batch/gmail/v1"),
        ("http://localhost/google", "/batch"),
    ],
)
async def test_batch_auth_inheritance_override_and_response_content_ids(
    make_client: Any, base: str, path: str
) -> None:
    from email import policy
    from email.parser import BytesParser

    async with make_client("google") as client:
        owner = await mint(client, BASE, user="p018")
        other = await mint(client, BASE, user="p001")
        chunks = []
        for index, token in enumerate((None, other, "invalid")):
            chunks.append(
                f"--test\r\nContent-Type: application/http\r\nContent-ID: <{index}>\r\n\r\nGET /drive/v3/about?fields=user(emailAddress) HTTP/1.1\r\n"
                + (f"Authorization: Bearer {token}\r\n" if token else "")
                + "\r\n"
            )
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=client.hub.gateway)) as gateway:
            response = await gateway.post(
                base + path,
                headers={**bearer(owner), "Content-Type": "multipart/mixed; boundary=test"},
                content="".join(chunks) + "--test--\r\n",
            )
        assert response.status_code == 200, response.text
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: " + response.headers["content-type"].encode() + b"\r\n\r\n" + response.content
        )
        parts = list(message.iter_parts())
        assert [p["Content-ID"] for p in parts] == ["<response-0>", "<response-1>", "<response-2>"]
        values = [p.get_payload(decode=True) for p in parts]
        assert b"maya.chen@halcyonadvisory.example" in values[0]
        assert b"eleanor.whitfield@halcyonadvisory.example" in values[1]
        assert values[2].startswith(b"HTTP/1.1 401")


async def test_directory_lifecycle_resources_fields_and_time_precision(make_client: Any) -> None:
    async with make_client("google") as client:
        admin = bearer(await mint(client, BASE, user="p035"))
        user_auth = bearer(await mint(client, BASE, user="p018", client_id="review-client"))
        root = BASE + "/admin/directory/v1"
        email = "maya.chen@halcyonadvisory.example"
        person = (
            await client.get(
                root + "/users/" + email, headers=admin, params={"fields": "id,creationTime,lastLoginTime"}
            )
        ).json()
        assert set(person) == {"id", "creationTime", "lastLoginTime"}
        assert all(re.fullmatch(r".+\.\d{3}Z", person[f]) for f in ("creationTime", "lastLoginTime"))
        tokens = (await client.get(root + f"/users/{email}/tokens", headers=admin)).json()["items"]
        assert any(r["clientId"] == "review-client" for r in tokens)
        assert (
            await client.delete(root + f"/users/{email}/tokens/review-client", headers=admin)
        ).status_code == 204
        assert (await client.get(BASE + "/drive/v3/about?fields=user", headers=user_auth)).status_code == 401
        user_auth = bearer(await mint(client, BASE, user="p018"))
        assert (await client.post(root + f"/users/{email}/signOut", headers=admin)).status_code == 204
        assert (await client.get(BASE + "/drive/v3/about?fields=user", headers=user_auth)).status_code == 401
        assert (await client.delete(root + "/users/" + email, headers=admin)).status_code == 204
        assert (
            await client.post(
                root + f"/users/{person['id']}/undelete", headers=admin, json={"orgUnitPath": "/"}
            )
        ).status_code == 204
        assert (await client.get(root + "/users/" + email, headers=admin)).status_code == 200
        resources = root + "/customer/my_customer/resources"
        buildings = (await client.get(resources + "/buildings", headers=admin)).json()["buildings"]
        assert (
            await client.get(resources + "/buildings/" + buildings[0]["buildingId"], headers=admin)
        ).json() == buildings[0]
        rooms = (await client.get(resources + "/calendars", headers=admin)).json()["items"]
        assert (
            await client.get(resources + "/calendars/" + rooms[0]["resourceId"], headers=admin)
        ).json() == rooms[0]


async def test_docs_error_envelope_and_wording(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        response = await client.get("https://docs.googleapis.com/v1/documents/missing", headers=auth)
        assert response.json() == {
            "error": {"code": 404, "message": "Requested entity was not found.", "status": "NOT_FOUND"}
        }
        malformed = await client.post(
            "https://docs.googleapis.com/v1/documents", headers=auth, json={"title": 5}
        )
        assert malformed.status_code == 400 and "errors" not in malformed.json()["error"]
