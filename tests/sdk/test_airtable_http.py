"""HTTP consumers keep Airtable vendor URLs through the shared socket interceptor."""

from __future__ import annotations

import base64
from typing import Any

import httpx
import pytest

pytestmark = pytest.mark.sdk


def test_metadata_record_upload_and_public_download(emulator_intercept: Any) -> None:
    with httpx.Client(headers={"Authorization": "Bearer pat-airtable-owner"}) as client:
        bases = client.get("https://api.airtable.com/v0/meta/bases").json()["bases"]
        base = next(b["id"] for b in bases if b["name"] == "Halcyon Client Operations")
        schema = client.get(f"https://api.airtable.com/v0/meta/bases/{base}/tables").json()["tables"]
        documents = next(t for t in schema if t["name"] == "Documents")
        field = next(f["id"] for f in documents["fields"] if f["name"] == "Files")
        table_url = f"https://api.airtable.com/v0/{base}/{documents['id']}"
        record = client.post(table_url, json={"fields": {"Name": "Socket upload"}}).json()
        uploaded = client.post(
            f"https://content.airtable.com/v0/{base}/{record['id']}/{field}/uploadAttachment",
            json={
                "contentType": "text/plain",
                "filename": "socket.txt",
                "file": base64.b64encode(b"Socket contract").decode(),
            },
        )
        assert uploaded.status_code == 200, uploaded.text
        url = uploaded.json()["fields"][field][0]["url"]
        assert url.startswith("https://v5.airtableusercontent.com/")
        assert client.get(url, headers={"Authorization": ""}).content == b"Socket contract"
        assert client.delete(table_url + "/" + record["id"]).json()["deleted"] is True
