from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import quote

import httpx
import pytest

from emulators.providers.asana.common import gid

from .conftest import BASE, task, workspace


async def test_attachment_multipart_binary_ranges_acl_delete_and_expiry(
    asana_client: httpx.AsyncClient,
) -> None:
    client = asana_client
    row = await task(client)
    content = b"\x00real binary\xff\xfe\n" * 8
    response = await client.post(
        BASE + "/attachments",
        data={"parent": row["gid"]},
        files={"file": (quote("evidence 雪.bin"), content, "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    attachment = response.json()["data"]
    assert set(attachment) == {"gid", "resource_type", "name"} and attachment["name"] == "evidence 雪.bin"
    aid = attachment["gid"]
    other = {"Authorization": "Bearer asana-dev-p035"}
    assert (await client.get(BASE + f"/attachments/{aid}", headers=other)).status_code == 403
    metadata = (await client.get(BASE + f"/attachments/{aid}")).json()["data"]
    assert metadata["size"] == len(content) and metadata["parent"]["gid"] == row["gid"]
    url = metadata["download_url"]
    downloaded = await client.get(url, headers={"Authorization": ""})
    assert downloaded.content == content and downloaded.headers["content-type"] == "application/octet-stream"
    partial = await client.get(url, headers={"Range": "bytes=3-9"})
    assert partial.status_code == 206 and partial.content == content[3:10]
    assert partial.headers["content-range"] == f"bytes 3-9/{len(content)}"
    assert (await client.get(url, headers={"Range": "bytes=-4"})).content == content[-4:]
    assert (await client.get(url, headers={"Range": "bytes=999-"})).status_code == 416
    assert (await client.get(url.replace("signature=", "signature=x"))).status_code == 403
    client.hub.clock.advance(timedelta(hours=2))
    assert (await client.get(url)).status_code == 403
    new_url = (await client.get(BASE + f"/attachments/{aid}")).json()["data"]["download_url"]
    assert (await client.get(new_url)).content == content
    assert (await client.delete(BASE + f"/attachments/{aid}")).json() == {"data": {}}
    assert (await client.get(new_url)).status_code == 404
    assert (await client.get(BASE + "/attachments", params={"parent": row["gid"]})).json()["data"] == []


async def test_seeded_document_attachment_has_real_file_bytes_and_reset(
    asana_client: httpx.AsyncClient,
) -> None:
    client = asana_client
    store = client.hub.context("asana").store
    document = None
    for candidate in store["attachments"]:
        if (
            candidate["name"].endswith(".docx")
            and (await client.get(BASE + f"/attachments/{candidate['gid']}")).status_code == 200
        ):
            document = candidate
            break
    assert document is not None
    metadata = (await client.get(BASE + f"/attachments/{document['gid']}")).json()["data"]
    content = await client.get(metadata["download_url"])
    assert content.content.startswith(b"PK\x03\x04") and metadata["size"] == len(content.content)
    assert "wordprocessingml" in content.headers["content-type"]
    new = await task(client)
    assert (
        await client.post(
            BASE + f"/tasks/{new['gid']}/attachments", files={"file": ("tiny.txt", b"tiny", "text/plain")}
        )
    ).status_code == 201
    assert (await client.post("https://app.asana.com/_emulator/reset")).status_code == 200
    assert (await client.get(BASE + f"/tasks/{new['gid']}")).status_code == 404
    refreshed = (await client.get(BASE + f"/attachments/{document['gid']}")).json()["data"]
    assert (await client.get(refreshed["download_url"])).content == content.content


async def test_invalid_attachment_uploads_are_atomic(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    row = await task(client)
    count = len(client.hub.context("asana").store["attachments"])
    assert (
        await client.post(BASE + "/attachments", json={"data": {"parent": row["gid"]}})
    ).status_code == 400
    assert (
        await client.post(
            BASE + "/attachments", data={"parent": row["gid"]}, files={"other": ("a.txt", b"x")}
        )
    ).status_code == 400
    assert (
        await client.post(BASE + "/attachments", data={"parent": "missing"}, files={"file": ("a.txt", b"x")})
    ).status_code == 404
    assert (
        await client.post(
            BASE + "/attachments", data={"parent": row["gid"]}, files={"file": ("a.txt", b"x", "bad mime")}
        )
    ).status_code == 400
    assert len(client.hub.context("asana").store["attachments"]) == count


async def test_comment_lifecycle_author_acl_rich_text_and_pagination(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    row = await task(client, followers=[gid("user", "p018"), gid("user", "p035")])
    other = {"Authorization": "Bearer asana-dev-p035"}
    response = await client.post(
        BASE + f"/tasks/{row['gid']}/stories",
        json={"data": {"html_text": "<body>Review <strong>evidence</strong> ✓</body>", "is_pinned": True}},
    )
    assert response.status_code == 201, response.text
    story = response.json()["data"]
    assert story["text"] == "Review evidence ✓" and story["type"] == "comment" and story["is_pinned"] is True
    assert (await client.get(BASE + f"/stories/{story['gid']}", headers=other)).status_code == 200
    assert (
        await client.put(
            BASE + f"/stories/{story['gid']}", headers=other, json={"data": {"text": "Forbidden"}}
        )
    ).status_code == 403
    assert (await client.delete(BASE + f"/stories/{story['gid']}", headers=other)).status_code == 403
    for text in ("Second", "Third"):
        assert (
            await client.post(BASE + f"/tasks/{row['gid']}/stories", json={"data": {"text": text}})
        ).status_code == 201
    first = (await client.get(BASE + f"/tasks/{row['gid']}/stories", params={"limit": 2})).json()
    assert len(first["data"]) == 2 and first["next_page"]
    assert len((await client.get(first["next_page"]["uri"])).json()["data"]) == 1
    updated = await client.put(
        BASE + f"/stories/{story['gid']}", json={"data": {"text": "Edited", "is_pinned": False}}
    )
    assert updated.json()["data"]["html_text"] == "<body>Edited</body>"
    assert (await client.delete(BASE + f"/stories/{story['gid']}")).status_code == 200
    assert (await client.get(BASE + f"/stories/{story['gid']}")).status_code == 404


@pytest.mark.parametrize(
    "changes",
    [
        {"text": "", "is_pinned": True},
        {"text": "changed", "is_pinned": 1},
        {"text": "x", "html_text": "<body>x</body>"},
        {"html_text": "<body>bad"},
        {"text": "\ud800"},
        {"text": float("nan")},
    ],
)
async def test_invalid_comment_changes_are_atomic(
    asana_client: httpx.AsyncClient, changes: dict[str, object]
) -> None:
    client = asana_client
    row = await task(client)
    story = (
        await client.post(BASE + f"/tasks/{row['gid']}/stories", json={"data": {"text": "Original"}})
    ).json()["data"]
    response = await client.put(
        BASE + f"/stories/{story['gid']}",
        content=json.dumps({"data": changes}),
        headers={"Content-Type": "application/json"},
    )
    assert (
        response.status_code == 400
        and (await client.get(BASE + f"/stories/{story['gid']}")).json()["data"] == story
    )


async def test_tag_deletion_removes_task_relationships(asana_client: httpx.AsyncClient) -> None:
    client = asana_client
    tag = (
        await client.post(
            BASE + "/tags", json={"data": {"name": "Contract", "workspace": await workspace(client)}}
        )
    ).json()["data"]
    row = await task(client, tags=[tag["gid"]])
    assert (await client.put(BASE + f"/tags/{tag['gid']}", json={"data": {"name": "Updated"}})).json()[
        "data"
    ]["name"] == "Updated"
    assert (await client.delete(BASE + f"/tags/{tag['gid']}")).status_code == 200
    assert (await client.get(BASE + f"/tags/{tag['gid']}")).status_code == 404
    assert (await client.get(BASE + f"/tasks/{row['gid']}")).json()["data"]["tags"] == []
