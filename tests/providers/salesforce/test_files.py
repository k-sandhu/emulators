"""Salesforce Files (ContentDocument / ContentVersion / ContentDocumentLink) and legacy Attachments."""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from .conftest import NATALIE, RAJESH, Actor

Make = Callable[..., Awaitable[Actor]]


def err(r: Any, status: int, code: str) -> dict[str, Any]:
    assert r.status_code == status, r.text
    assert r.json()[0]["errorCode"] == code, r.json()
    return r.json()[0]


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


async def test_every_client_document_is_a_file_linked_to_its_account(
    admin: Actor, client: httpx.AsyncClient
) -> None:
    world = client.hub.world  # type: ignore[attr-defined]
    docs = [d for d in world.documents if d.client_id and not d.trashed]
    assert (await admin.query("SELECT COUNT() FROM ContentDocument"))["totalSize"] == len(docs) == 242
    assert (await admin.query("SELECT COUNT() FROM ContentVersion"))["totalSize"] == 242
    first = (
        await admin.rows(
            "SELECT Id, Title, FileExtension, FileType, ContentSize, PathOnClient, IsLatest, VersionNumber, Checksum, ContentDocument.Title, Owner.Email, FirstPublishLocation.Name FROM ContentVersion ORDER BY Id LIMIT 1"
        )
    )[0]
    d = docs[0]
    assert (
        first["PathOnClient"] == d.name and first["FileExtension"] == "docx" and first["FileType"] == "WORD_X"
    )
    assert first["Title"] == d.name.rsplit(".", 1)[0] == first["ContentDocument"]["Title"]
    assert first["IsLatest"] is True and first["VersionNumber"] == "1" and first["ContentSize"] > 1000
    assert first["Owner"]["Email"] == world.person(d.owner_id).email
    assert first["FirstPublishLocation"]["Name"] == world.client(d.client_id).name
    account = (await admin.ids(f"SELECT Id FROM Account WHERE Name = '{world.client(d.client_id).name}'"))[0]
    links = await admin.rows(
        f"SELECT ContentDocumentId, ContentDocument.Title, ShareType, Visibility FROM ContentDocumentLink WHERE LinkedEntityId = '{account}'"
    )
    assert len(links) >= 5 and all(
        link["ShareType"] == "I" and link["Visibility"] == "AllUsers" for link in links
    )
    assert "Engagement Letter" in {link["ContentDocument"]["Title"].split(" - ")[0] for link in links}
    nested = (
        await admin.rows(
            f"SELECT Id, (SELECT ContentDocumentId, ShareType FROM ContentDocumentLinks) FROM Account WHERE Id = '{account}'"
        )
    )[0]
    assert nested["ContentDocumentLinks"]["totalSize"] == len(links)
    versions = await admin.rows("SELECT ContentDocumentId, IsLatest FROM ContentVersion")
    docs_ = {
        r["Id"]: r
        for r in await admin.rows(
            "SELECT Id, LatestPublishedVersionId, LatestPublishedVersion.Title, Title FROM ContentDocument"
        )
    }
    assert len(versions) == len(docs_) and all(v["ContentDocumentId"] in docs_ for v in versions)
    assert all(doc["LatestPublishedVersion"]["Title"] == doc["Title"] for doc in docs_.values())


async def test_downloading_version_data_returns_the_real_file_bytes(admin: Actor) -> None:
    rows = await admin.rows(
        "SELECT Id, ContentSize, Checksum, FileExtension, PathOnClient FROM ContentVersion ORDER BY Id"
    )
    by_ext = {r["FileExtension"]: r for r in rows}
    assert {"docx", "xlsx", "pdf"} <= set(by_ext)
    magic = {"docx": b"PK", "xlsx": b"PK", "pdf": b"%PDF", "pptx": b"PK"}
    for ext, row in by_ext.items():
        record = (await admin.get(f"/sobjects/ContentVersion/{row['Id']}")).json()
        assert (
            record["VersionData"] == f"/services/data/v62.0/sobjects/ContentVersion/{row['Id']}/VersionData"
        )
        r = await admin.get(record["VersionData"].replace("/services/data/v62.0", ""))
        assert r.status_code == 200, ext
        assert len(r.content) == row["ContentSize"] and hashlib.md5(r.content).hexdigest() == row["Checksum"]
        if ext in magic:
            assert r.content.startswith(magic[ext])
        assert row["PathOnClient"] in r.headers["content-disposition"]
    docx = (await admin.get(f"/sobjects/ContentVersion/{by_ext['docx']['Id']}/VersionData")).headers[
        "content-type"
    ]
    assert docx == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    err(await admin.get(f"/sobjects/ContentVersion/{by_ext['pdf']['Id']}/Nothing"), 404, "NOT_FOUND")
    err(await admin.get("/sobjects/ContentVersion/068000000000000AAA/VersionData"), 404, "NOT_FOUND")


async def test_uploading_a_file_creates_document_version_and_links(admin: Actor) -> None:
    account = (await admin.ids("SELECT Id FROM Account WHERE Name = 'Bluefin Seafood'"))[0]
    body = {
        "Title": "Board Pack",
        "PathOnClient": "Board Pack.pdf",
        "VersionData": b64(b"%PDF-1.4 hello"),
        "FirstPublishLocationId": account,
    }
    r = await admin.post("/sobjects/ContentVersion", body)
    assert r.status_code == 201, r.text
    vid = r.json()["id"]
    assert vid.startswith("068")
    version = (await admin.get(f"/sobjects/ContentVersion/{vid}")).json()
    assert (
        version["ContentDocumentId"].startswith("069")
        and version["IsLatest"] is True
        and version["VersionNumber"] == "1"
    )
    assert version["ContentSize"] == 14 and version["FileExtension"] == "pdf" and version["FileType"] == "PDF"
    assert (
        version["Checksum"] == hashlib.md5(b"%PDF-1.4 hello").hexdigest()
        and version["OwnerId"] == admin.user_id
    )
    doc = (await admin.get(f"/sobjects/ContentDocument/{version['ContentDocumentId']}")).json()
    assert (
        doc["Title"],
        doc["LatestPublishedVersionId"],
        doc["ContentSize"],
        doc["OwnerId"],
        doc["PublishStatus"],
    ) == ("Board Pack", vid, 14, admin.user_id, "R")
    links = await admin.rows(
        f"SELECT LinkedEntityId, ShareType FROM ContentDocumentLink WHERE ContentDocumentId = '{doc['Id']}' ORDER BY ShareType"
    )
    assert {(link["LinkedEntityId"], link["ShareType"]) for link in links} == {
        (admin.user_id, "I"),
        (account, "V"),
    }
    dl = await admin.get(f"/sobjects/ContentVersion/{vid}/VersionData")
    assert dl.content == b"%PDF-1.4 hello" and dl.headers["content-type"] == "application/pdf"
    # a new version of the same document
    second = await admin.post(
        "/sobjects/ContentVersion",
        {
            "ContentDocumentId": doc["Id"],
            "Title": "Board Pack v2",
            "PathOnClient": "Board Pack.pdf",
            "VersionData": b64(b"%PDF-1.4 hello again"),
        },
    )
    assert second.status_code == 201, second.text
    versions = await admin.rows(
        f"SELECT Id, VersionNumber, IsLatest FROM ContentVersion WHERE ContentDocumentId = '{doc['Id']}' ORDER BY VersionNumber"
    )
    assert [(v["VersionNumber"], v["IsLatest"]) for v in versions] == [("1", False), ("2", True)]
    doc = (await admin.get(f"/sobjects/ContentDocument/{doc['Id']}")).json()
    assert (
        doc["LatestPublishedVersionId"] == second.json()["id"]
        and doc["Title"] == "Board Pack v2"
        and doc["ContentSize"] == 20
    )
    assert (
        await admin.get(f"/sobjects/ContentVersion/{vid}/VersionData")
    ).content == b"%PDF-1.4 hello"  # old versions stay downloadable


async def test_multipart_upload_with_entity_content(admin: Actor) -> None:
    boundary = "----emulatorboundary"
    entity = json.dumps({"Title": "Multipart Memo", "Description": "uploaded as multipart"})
    payload = b"hello multipart world\n"
    parts = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="entity_content"\r\nContent-Type: application/json\r\n\r\n{entity}\r\n'
            f'--{boundary}\r\nContent-Type: application/octet-stream\r\nContent-Disposition: form-data; name="VersionData"; filename="memo.txt"\r\n\r\n'
        ).encode()
        + payload
        + f"\r\n--{boundary}--\r\n".encode()
    )
    r = await admin.post(
        "/sobjects/ContentVersion",
        content=parts,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert r.status_code == 201, r.text
    version = (await admin.get(f"/sobjects/ContentVersion/{r.json()['id']}")).json()
    assert (
        version["PathOnClient"] == "memo.txt"
        and version["Title"] == "Multipart Memo"
        and version["FileType"] == "TEXT"
    )
    assert (await admin.get(f"/sobjects/ContentVersion/{version['Id']}/VersionData")).content == payload
    assert version["Description"] == "uploaded as multipart" and version["ContentSize"] == len(payload)


async def test_content_validation_and_permissions(admin: Actor, actor: Make) -> None:
    err(
        await admin.post("/sobjects/ContentVersion", {"Title": "x", "PathOnClient": "x.txt"}),
        400,
        "REQUIRED_FIELD_MISSING",
    )
    err(
        await admin.post(
            "/sobjects/ContentVersion",
            {"Title": "x", "PathOnClient": "x.txt", "VersionData": "!!not base64!!"},
        ),
        400,
        "JSON_PARSER_ERROR",
    )
    err(
        await admin.post(
            "/sobjects/ContentVersion",
            {
                "Title": "x",
                "PathOnClient": "x.txt",
                "VersionData": b64(b"x"),
                "ContentDocumentId": "069000000000000AAA",
            },
        ),
        400,
        "INVALID_CROSS_REFERENCE_KEY",
    )
    err(
        await admin.post("/sobjects/ContentDocument", {"Title": "direct"}),
        403,
        "INSUFFICIENT_ACCESS_OR_READONLY",
    )
    version = (await admin.rows("SELECT Id, ContentDocumentId FROM ContentVersion LIMIT 1"))[0]
    err(
        await admin.delete(f"/sobjects/ContentVersion/{version['Id']}"),
        403,
        "INSUFFICIENT_ACCESS_OR_READONLY",
    )
    jasmine = await actor("p029")
    err(
        await jasmine.post(
            "/sobjects/ContentVersion", {"Title": "x", "PathOnClient": "x.txt", "VersionData": b64(b"x")}
        ),
        403,
        "INSUFFICIENT_ACCESS_OR_READONLY",
    )


async def test_files_follow_the_visibility_of_the_records_they_are_linked_to(
    admin: Actor, actor: Make
) -> None:
    natalie, rajesh = await actor(NATALIE), await actor(RAJESH)
    opp = (await natalie.ids("SELECT Id FROM Opportunity LIMIT 1"))[0]
    created = await natalie.post(
        "/sobjects/ContentVersion",
        {
            "Title": "Private proposal",
            "PathOnClient": "proposal.docx",
            "VersionData": b64(b"proposal"),
            "FirstPublishLocationId": opp,
        },
    )
    assert created.status_code == 201
    doc_id = (await natalie.get(f"/sobjects/ContentVersion/{created.json()['id']}")).json()[
        "ContentDocumentId"
    ]
    assert doc_id in await natalie.ids("SELECT Id FROM ContentDocument")
    assert doc_id in await admin.ids("SELECT Id FROM ContentDocument")
    assert doc_id not in await rajesh.ids(
        "SELECT Id FROM ContentDocument"
    )  # linked only to a private opportunity
    err(await rajesh.get(f"/sobjects/ContentDocument/{doc_id}"), 404, "NOT_FOUND")
    err(await rajesh.get(f"/sobjects/ContentVersion/{created.json()['id']}/VersionData"), 404, "NOT_FOUND")
    assert created.json()["id"] not in await rajesh.ids("SELECT Id FROM ContentVersion")
    # share it with a public account and Rajesh can now see (but not edit) it
    account = (await admin.ids("SELECT Id FROM Account LIMIT 1"))[0]
    shared = await natalie.post(
        "/sobjects/ContentDocumentLink",
        {"LinkedEntityId": account, "ContentDocumentId": doc_id, "ShareType": "V"},
    )
    assert shared.status_code == 201, shared.text
    assert shared.json()["id"].startswith("06A")
    assert doc_id in await rajesh.ids("SELECT Id FROM ContentDocument")
    err(
        await rajesh.patch(f"/sobjects/ContentDocument/{doc_id}", {"Title": "stolen"}),
        403,
        "INSUFFICIENT_ACCESS_OR_READONLY",
    )
    err(
        await natalie.post(
            "/sobjects/ContentDocumentLink",
            {"LinkedEntityId": account, "ContentDocumentId": doc_id, "ShareType": "V"},
        ),
        400,
        "DUPLICATE_VALUE",
    )
    assert (
        await natalie.patch(f"/sobjects/ContentDocument/{doc_id}", {"Title": "Final proposal"})
    ).status_code == 204
    # unlinking removes access again
    assert (await natalie.delete(f"/sobjects/ContentDocumentLink/{shared.json()['id']}")).status_code == 204
    assert doc_id not in await rajesh.ids("SELECT Id FROM ContentDocument")


async def test_content_document_link_queries_need_a_selective_filter(admin: Actor) -> None:
    for soql in (
        "SELECT Id FROM ContentDocumentLink",
        "SELECT Id FROM ContentDocumentLink WHERE ShareType = 'I'",
        "SELECT Id FROM ContentDocumentLink WHERE LinkedEntityId != '001000000000000AAA'",
        "SELECT Id FROM ContentDocumentLink WHERE LinkedEntityId = '001000000000000AAA' OR ShareType = 'V'",
    ):
        r = await admin.get("/query", params={"q": soql})
        assert r.status_code == 400, soql
        assert r.json()[0]["errorCode"] == "MALFORMED_QUERY"
        assert (
            "requires a filter by a single Id on ContentDocumentId or LinkedEntityId"
            in r.json()[0]["message"]
        )
    account = (await admin.ids("SELECT Id FROM Account LIMIT 1"))[0]
    ok = await admin.query(
        f"SELECT Id FROM ContentDocumentLink WHERE LinkedEntityId IN ('{account}') AND ShareType = 'I'"
    )
    assert ok["totalSize"] >= 0
    doc = (await admin.ids("SELECT Id FROM ContentDocument LIMIT 1"))[0]
    assert (await admin.query(f"SELECT Id FROM ContentDocumentLink WHERE ContentDocumentId = '{doc}'"))[
        "totalSize"
    ] >= 2


async def test_deleting_a_document_removes_versions_and_links(admin: Actor) -> None:
    doc = (await admin.rows("SELECT Id FROM ContentDocument LIMIT 1"))[0]["Id"]
    assert (await admin.delete(f"/sobjects/ContentDocument/{doc}")).status_code == 204
    err(await admin.get(f"/sobjects/ContentDocument/{doc}"), 404, "NOT_FOUND")
    assert (await admin.query(f"SELECT COUNT() FROM ContentVersion WHERE ContentDocumentId = '{doc}'"))[
        "totalSize"
    ] == 0
    assert (await admin.query(f"SELECT COUNT() FROM ContentDocumentLink WHERE ContentDocumentId = '{doc}'"))[
        "totalSize"
    ] == 0
    assert (await admin.query("SELECT COUNT() FROM ContentDocument"))["totalSize"] == 241


async def test_attachments(admin: Actor, actor: Make) -> None:
    account = (await admin.ids("SELECT Id FROM Account WHERE Name = 'Northwind Logistics'"))[0]
    created = await admin.post(
        "/sobjects/Attachment",
        {"Name": "notes.txt", "ParentId": account, "Body": b64(b"legacy attachment body")},
    )
    assert created.status_code == 201, created.text
    aid = created.json()["id"]
    assert aid.startswith("00P")
    got = (await admin.get(f"/sobjects/Attachment/{aid}")).json()
    assert (
        got["BodyLength"] == 22
        and got["ContentType"] == "text/plain"
        and got["Body"].endswith(f"/Attachment/{aid}/Body")
    )
    assert (await admin.get(f"/sobjects/Attachment/{aid}/Body")).content == b"legacy attachment body"
    listed = (await admin.get(f"/sobjects/Account/{account}/Attachments")).json()
    assert [a["Id"] for a in listed["records"]] == [aid]
    assert (await admin.patch(f"/sobjects/Attachment/{aid}", {"Body": b64(b"replaced")})).status_code == 204
    again = (await admin.get(f"/sobjects/Attachment/{aid}")).json()
    assert (
        again["BodyLength"] == 8
        and (await admin.get(f"/sobjects/Attachment/{aid}/Body")).content == b"replaced"
    )
    natalie = await actor(NATALIE)
    opp_attachment = (
        await natalie.post(
            "/sobjects/Attachment",
            {
                "Name": "nda.pdf",
                "ParentId": (await natalie.ids("SELECT Id FROM Opportunity LIMIT 1"))[0],
                "Body": b64(b"x"),
            },
        )
    ).json()["id"]
    rajesh = await actor(RAJESH)
    err(await rajesh.get(f"/sobjects/Attachment/{opp_attachment}"), 404, "NOT_FOUND")
    assert opp_attachment not in await rajesh.ids("SELECT Id FROM Attachment")
    assert aid in await rajesh.ids("SELECT Id FROM Attachment")
    assert (await admin.delete(f"/sobjects/Account/{account}")).status_code == 204
    err(await admin.get(f"/sobjects/Attachment/{aid}"), 404, "NOT_FOUND")  # attachments go with their parent
