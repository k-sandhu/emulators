from __future__ import annotations

from collections.abc import Callable
from typing import Any

from emulators.testing.helpers import bearer, mint

BASE = "https://graph.microsoft.com"


async def test_resumable_upload_fragments_status_cancel_and_validation(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft") as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        root = BASE + "/v1.0/me/drive"
        response = await client.post(
            root + "/root:/chunked.txt:/createUploadSession",
            headers=auth,
            json={"item": {"@microsoft.graph.conflictBehavior": "fail"}},
        )
        assert response.status_code == 200, response.text
        session = response.json()
        url = session["uploadUrl"]
        assert "halcyonadvisory.sharepoint.com" in url
        assert (await client.get(url)).json()["nextExpectedRanges"] == ["0-"]
        assert (await client.put(url, content=b"x")).status_code == 400
        data = b"a" * 327680 + b"ending"
        fragment = await client.put(
            url, headers={"Content-Range": f"bytes 0-327679/{len(data)}"}, content=data[:327680]
        )
        assert fragment.status_code == 202
        assert fragment.json()["nextExpectedRanges"] == ["327680-"]
        assert (
            await client.put(
                url, headers={"Content-Range": f"bytes 0-327679/{len(data)}"}, content=data[:327680]
            )
        ).status_code == 416
        final = await client.put(
            url, headers={"Content-Range": f"bytes 327680-{len(data) - 1}/{len(data)}"}, content=data[327680:]
        )
        assert final.status_code == 201, final.text
        downloaded = await client.get(final.json()["@microsoft.graph.downloadUrl"])
        assert downloaded.content == data
        assert (await client.get(url)).status_code == 404
        cancel = (
            await client.post(root + "/root:/canceled.txt:/createUploadSession", headers=auth, json={})
        ).json()
        assert (await client.delete(cancel["uploadUrl"])).status_code == 204
        assert (await client.get(cancel["uploadUrl"])).status_code == 404
        bad = await client.post(
            root + "/root:/bad.txt:/createUploadSession",
            headers=auth,
            json={"item": {"@microsoft.graph.conflictBehavior": "invented"}},
        )
        assert bad.status_code == 400


async def test_upload_session_replaces_existing_and_simple_upload_rename(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft") as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        root = BASE + "/beta/me/drive"
        item = (await client.put(root + "/root:/same.txt:/content", headers=auth, content=b"old")).json()
        session = (
            await client.post(root + f"/items/{item['id']}/createUploadSession", headers=auth, json={})
        ).json()
        final = await client.put(
            session["uploadUrl"], headers={"Content-Range": "bytes 0-2/3"}, content=b"new"
        )
        assert final.status_code == 200
        assert final.json()["id"] == item["id"]
        rename = await client.put(
            root + "/root:/same.txt:/content?@microsoft.graph.conflictBehavior=rename",
            headers=auth,
            content=b"other",
        )
        assert rename.status_code == 201 and rename.json()["name"] == "same (1).txt"
