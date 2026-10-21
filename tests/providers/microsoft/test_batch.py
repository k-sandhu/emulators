from __future__ import annotations

import base64
import json
from collections.abc import Callable
from typing import Any

import pytest

from emulators.testing.helpers import bearer, mint

BASE = "https://graph.microsoft.com"


@pytest.mark.parametrize(
    "headers",
    [
        {"X-Client": "雪"},
        {"X-Client": "first\r\nInjected: true"},
        {"X-Client": "nul\u0000byte"},
        {"X-Client": "delete\u007fbyte"},
        {"X-Client": "control\u000bbyte"},
        {"bad header": "value"},
        {"bad:header": "value"},
        {"": "value"},
        {"雪": "value"},
    ],
)
async def test_batch_rejects_invalid_http_headers_before_writes(
    make_client: Callable[..., Any], headers: dict[str, str]
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        before = (await client.get("/v1.0/me", headers=auth)).json()["displayName"]
        response = await client.post(
            "/v1.0/$batch",
            headers=auth,
            json={
                "requests": [
                    {
                        "id": "write",
                        "method": "PATCH",
                        "url": "/me",
                        "headers": {"Content-Type": "application/json"},
                        "body": {"displayName": "Must not persist"},
                    },
                    {"id": "bad", "method": "GET", "url": "/me", "headers": headers},
                ]
            },
        )
        assert response.status_code == 400, response.text
        assert response.json()["error"]["code"] == "BadRequest"
        assert (await client.get("/v1.0/me", headers=auth)).json()["displayName"] == before


async def test_batch_preserves_valid_http_header_tokens_and_latin1_values(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        response = await client.post(
            "/v1.0/$batch",
            headers=auth,
            json={
                "requests": [
                    {
                        "id": "valid",
                        "method": "GET",
                        "url": "/me",
                        "headers": {"X-Client!#$%&'*+-.^_`|~": "café\tclient", "X-Empty": ""},
                    }
                ]
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["responses"][0]["status"] == 200


async def test_batch_decodes_base64url_binary_request_bodies(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE, follow_redirects=True) as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        data = b"\xff\xfe\x00binary batch bytes"
        response = await client.post(
            "/v1.0/$batch",
            headers=auth,
            json={
                "requests": [
                    {
                        "id": "upload",
                        "method": "PUT",
                        "url": "/me/drive/root:/雪😀-batch.bin:/content",
                        "headers": {"Content-Type": "application/octet-stream"},
                        "body": base64.urlsafe_b64encode(data).decode().rstrip("="),
                    }
                ]
            },
        )
        assert response.status_code == 200, response.text
        result = response.json()["responses"][0]
        assert result["status"] == 201, result
        content = await client.get(f"/v1.0/me/drive/items/{result['body']['id']}/content", headers=auth)
        assert content.content == data


async def test_batch_preserves_unicode_json_body(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        response = await client.post(
            "/v1.0/$batch",
            headers=auth,
            json={
                "requests": [
                    {
                        "id": "unicode",
                        "method": "PATCH",
                        "url": "/me",
                        "headers": {"Content-Type": "application/json"},
                        "body": {"displayName": "Maya 雪😀"},
                    }
                ]
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["responses"][0]["status"] == 204
        assert (await client.get("/v1.0/me", headers=auth)).json()["displayName"] == "Maya 雪😀"


async def test_batch_ids_and_dependencies_are_case_insensitive(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        duplicate = await client.post(
            "/v1.0/$batch",
            headers=auth,
            json={"requests": [{"id": key, "method": "GET", "url": "/me"} for key in ("A", "a")]},
        )
        assert duplicate.status_code == 400
        dependent = await client.post(
            "/v1.0/$batch",
            headers=auth,
            json={
                "requests": [
                    {"id": "B", "method": "GET", "url": "/me", "dependsOn": ["a"]},
                    {"id": "A", "method": "GET", "url": "/me"},
                ]
            },
        )
        assert dependent.status_code == 200, dependent.text
        assert [(row["id"], row["status"]) for row in dependent.json()["responses"]] == [
            ("B", 200),
            ("A", 200),
        ]


@pytest.mark.parametrize(
    "invalid",
    [
        "cycle",
        "content_type",
        "binary",
        "body_type",
        "unencodable_url",
        "unencodable_body",
        "url_control",
        "unencodable_id",
        "nonfinite_envelope",
    ],
)
async def test_malformed_batch_is_validated_before_any_mutation(
    make_client: Callable[..., Any], invalid: str
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        before = (await client.get("/v1.0/me", headers=auth)).json()["displayName"]
        entries = [
            {
                "id": "write",
                "method": "PATCH",
                "url": "/me",
                "headers": {"Content-Type": "application/json"},
                "body": {"displayName": "Must not persist"},
            }
        ]
        if invalid == "cycle":
            entries.extend(
                [
                    {"id": "a", "method": "GET", "url": "/me", "dependsOn": ["b"]},
                    {"id": "b", "method": "GET", "url": "/me", "dependsOn": ["a"]},
                ]
            )
        elif invalid == "content_type":
            entries.append({"id": "bad", "method": "PATCH", "url": "/me", "body": {}})
        elif invalid in ("unencodable_url", "url_control"):
            entries.append(
                {
                    "id": "bad",
                    "method": "GET",
                    "url": "/me/" + ("\ud800" if invalid == "unencodable_url" else "\x00"),
                }
            )
        elif invalid == "unencodable_body":
            entries.append(
                {
                    "id": "bad",
                    "method": "PATCH",
                    "url": "/me",
                    "headers": {"Content-Type": "application/json"},
                    "body": {"displayName": "\ud800"},
                }
            )
        elif invalid == "unencodable_id":
            entries.append({"id": "\ud800", "method": "GET", "url": "/me"})
        elif invalid == "nonfinite_envelope":
            entries.append({"id": "bad", "method": "GET", "url": "/me", "extra": float("nan")})
        else:
            entries.append(
                {
                    "id": "bad",
                    "method": "PUT",
                    "url": "/me/drive/root:/bad.bin:/content",
                    "headers": {"Content-Type": "application/octet-stream"},
                    "body": "not base64!" if invalid == "binary" else [],
                }
            )
        response = await client.post(
            "/v1.0/$batch",
            headers={**auth, "Content-Type": "application/json"},
            content=json.dumps({"requests": entries}),
        )
        assert response.status_code == 400
        assert (await client.get("/v1.0/me", headers=auth)).json()["displayName"] == before


async def test_batch_dependency_order_auth_and_errors(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        result = await client.post(
            "/v1.0/$batch",
            headers=auth,
            json={
                "requests": [
                    {"id": "2", "method": "GET", "url": "/me/messages?$top=1", "dependsOn": ["1"]},
                    {"id": "1", "method": "GET", "url": "/me?$select=id,mail"},
                    {"id": "3", "method": "GET", "url": "/users/absent"},
                    {"id": "4", "method": "GET", "url": "/me", "dependsOn": ["3"]},
                ]
            },
        )
        assert result.status_code == 200, result.text
        rows = result.json()["responses"]
        assert [r["id"] for r in rows] == ["2", "1", "3", "4"]
        assert [r["status"] for r in rows] == [200, 200, 404, 424]
        assert "request-id" in rows[0]["headers"]
        for requests in (
            [{"id": str(i), "url": "/me"} for i in range(21)],
            [{"id": "1", "url": "/me", "dependsOn": ["2"]}, {"id": "2", "url": "/me", "dependsOn": ["1"]}],
            [{"id": "1", "url": "https://consumer.example/private"}],
            [{"id": "1", "url": "/../_emulator/reset"}],
        ):
            assert (
                await client.post("/beta/$batch", headers=auth, json={"requests": requests})
            ).status_code == 400
