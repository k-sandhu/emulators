"""Wire contracts exercise the official discovery client and google-auth transports."""

from __future__ import annotations

import io
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google.oauth2.id_token import verify_oauth2_token
from google.oauth2.service_account import Credentials as ServiceCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from emulators.providers.google.drive_seed import shared_drive_id

pytestmark = pytest.mark.sdk


def token(user: str = "p018", **extra: Any) -> str:
    response = requests.post(
        "https://www.googleapis.com/_emulator/tokens", json={"user": user, **extra}, timeout=10
    )
    response.raise_for_status()
    return response.json()["access_token"]


def test_docs_sdk_rejects_ambiguous_atomic_edits_and_normalizes_text(emulator_intercept: Any) -> None:
    service = build("docs", "v1", credentials=Credentials(token()), static_discovery=True)
    try:
        document = service.documents().create(body={"title": "SDK edit validation"}).execute()
        identifier = document["documentId"]
        with pytest.raises(HttpError) as rejected:
            service.documents().batchUpdate(
                documentId=identifier,
                body={
                    "requests": [
                        {"insertText": {"text": "prefix", "location": {"index": 1}}},
                        {
                            "insertText": {
                                "text": "invalid",
                                "location": {"index": 1},
                                "endOfSegmentLocation": {},
                            }
                        },
                    ]
                },
            ).execute()
        assert rejected.value.resp.status == 400
        assert service.documents().get(documentId=identifier).execute() == document
        service.documents().batchUpdate(
            documentId=identifier,
            body={"requests": [{"insertText": {"text": "SDK\u0000\ue000😀", "endOfSegmentLocation": {}}}]},
        ).execute()
        result = service.documents().get(documentId=identifier).execute()
        assert result["body"]["content"][1]["paragraph"]["elements"][0]["textRun"]["content"] == "SDK😀\n"
    finally:
        service.close()


def test_discovery_drive_list_get_download_permissions_and_changes(emulator_intercept: Any) -> None:
    service = build("drive", "v3", credentials=Credentials(token()), static_discovery=True)
    assert service.about().get(fields="user(emailAddress)").execute() == {
        "user": {"emailAddress": "maya.chen@halcyonadvisory.example"}
    }
    ctx = emulator_intercept.hub.context("google")
    did = shared_drive_id(ctx, "clients")
    request = service.files().list(
        q=f"'{did}' in parents and trashed = false",
        pageSize=3,
        corpora="drive",
        driveId=did,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
        fields="nextPageToken,files(id,name,mimeType,parents,modifiedTime,size,md5Checksum,webViewLink)",
    )
    first = request.execute()
    assert len(first["files"]) == 3 and "nextPageToken" in first
    second_request = service.files().list_next(request, first)
    assert second_request is not None
    second = second_request.execute()
    assert not {r["id"] for r in first["files"]} & {r["id"] for r in second["files"]}
    listed = (
        service.files()
        .list(
            q="mimeType = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' and trashed = false",
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            fields="files(id,name,size,md5Checksum)",
        )
        .execute()
    )
    item = listed["files"][0]
    assert (
        service.files().get(fileId=item["id"], supportsAllDrives=True, fields="id,name").execute()["id"]
        == item["id"]
    )
    target = io.BytesIO()
    download = MediaIoBaseDownload(
        target, service.files().get_media(fileId=item["id"], supportsAllDrives=True), chunksize=1024
    )
    done = False
    while not done:
        _, done = download.next_chunk()
    assert target.getvalue().startswith(b"PK") and len(target.getvalue()) == int(item["size"])
    perms = (
        service.permissions()
        .list(
            fileId=item["id"],
            supportsAllDrives=True,
            fields="permissions(id,type,role,emailAddress,permissionDetails)",
        )
        .execute()
    )
    assert perms["permissions"]
    start = (
        service.changes().getStartPageToken(driveId=did, supportsAllDrives=True).execute()["startPageToken"]
    )
    assert (
        service.changes().list(pageToken=start, includeRemoved=True).execute()["newStartPageToken"] == start
    )
    drives = service.drives().list(fields="drives(id,name)").execute()["drives"]
    assert any(d["id"] == did for d in drives)
    service.close()


def test_sdk_multipart_resumable_create_update_share_copy_delete(emulator_intercept: Any) -> None:
    service = build("drive", "v3", credentials=Credentials(token()), static_discovery=True)
    uploaded = (
        service.files()
        .create(
            body={"name": "sdk-multipart.txt"},
            media_body=MediaIoBaseUpload(io.BytesIO(b"SDK multipart"), mimetype="text/plain"),
            fields="id,name,size",
        )
        .execute()
    )
    assert uploaded["size"] == "13"
    data = b"content" * 80000
    request = service.files().create(
        body={"name": "sdk-resumable.txt"},
        media_body=MediaIoBaseUpload(
            io.BytesIO(data), mimetype="text/plain", chunksize=256 * 1024, resumable=True
        ),
        fields="id,name,size",
    )
    response = None
    while response is None:
        _, response = request.next_chunk()
    assert response["size"] == str(len(data))
    fid = response["id"]
    changed = (
        service.files()
        .update(
            fileId=fid,
            body={"name": "renamed.txt"},
            media_body=MediaIoBaseUpload(io.BytesIO(b"updated"), mimetype="text/plain"),
            fields="id,name,size",
        )
        .execute()
    )
    assert changed["size"] == "7" and changed["name"] == "renamed.txt"
    permission = (
        service.permissions()
        .create(
            fileId=fid,
            body={"type": "user", "role": "reader", "emailAddress": "olivia.grant@halcyonadvisory.example"},
            sendNotificationEmail=False,
            fields="id,type,role,emailAddress",
        )
        .execute()
    )
    reader = build("drive", "v3", credentials=Credentials(token("p031")), static_discovery=True)
    assert reader.files().get(fileId=fid, fields="id").execute()["id"] == fid
    with pytest.raises(HttpError) as denied:
        reader.files().update(fileId=fid, body={"name": "denied"}).execute()
    assert denied.value.resp.status == 403
    service.permissions().delete(fileId=fid, permissionId=permission["id"]).execute()
    with pytest.raises(HttpError) as hidden:
        reader.files().get(fileId=fid).execute()
    assert hidden.value.resp.status == 404
    copied = service.files().copy(fileId=fid, body={"name": "copy.txt"}, fields="id").execute()
    assert service.files().get_media(fileId=copied["id"]).execute() == b"updated"
    service.files().delete(fileId=fid).execute()
    service.files().delete(fileId=copied["id"]).execute()
    reader.close()
    service.close()


def test_google_auth_refresh_and_oidc_cert_verification(emulator_intercept: Any) -> None:
    response = requests.get(
        "https://accounts.google.com/o/oauth2/v2/auth",
        params={
            "client_id": "sdk-google-client",
            "redirect_uri": "https://consumer.example/callback",
            "response_type": "code",
            "scope": "openid email https://www.googleapis.com/auth/drive",
            "access_type": "offline",
            "login_hint": "maya.chen@halcyonadvisory.example",
        },
        allow_redirects=False,
        timeout=10,
    )
    code = parse_qs(urlparse(response.headers["Location"]).query)["code"][0]
    exchanged = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": "sdk-google-client",
            "client_secret": "secret",
            "redirect_uri": "https://consumer.example/callback",
        },
        timeout=10,
    )
    exchanged.raise_for_status()
    credentials = Credentials(
        token=None,
        refresh_token=exchanged.json()["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id="sdk-google-client",
        client_secret="secret",
        scopes=["openid", "email", "https://www.googleapis.com/auth/drive"],
    )
    credentials.refresh(GoogleRequest())
    assert credentials.token and credentials.token.startswith("ya29.")
    assert credentials.refresh_token == exchanged.json()["refresh_token"]
    assert credentials.id_token
    verified = verify_oauth2_token(credentials.id_token, GoogleRequest(), audience="sdk-google-client")
    assert verified["email"] == "maya.chen@halcyonadvisory.example"
    service = build("drive", "v3", credentials=credentials, static_discovery=True)
    assert (
        service.about().get(fields="user(emailAddress)").execute()["user"]["emailAddress"]
        == "maya.chen@halcyonadvisory.example"
    )
    service.close()


def test_official_directory_and_docs_clients_and_service_account(emulator_intercept: Any) -> None:
    admin = build("admin", "directory_v1", credentials=Credentials(token("p035")), static_discovery=True)
    users = (
        admin.users()
        .list(customer="my_customer", query="orgUnitPath='/Advisory/London'", maxResults=100)
        .execute()
    )
    assert len(users["users"]) == 2
    ous = admin.orgunits().list(customerId="my_customer", orgUnitPath="/Advisory", type="children").execute()
    assert ous["organizationUnits"][0]["orgUnitPath"] == "/Advisory/London"
    docs = build("docs", "v1", credentials=Credentials(token()), static_discovery=True)
    doc = docs.documents().create(body={"title": "SDK Doc"}).execute()
    fid = doc["documentId"]
    docs.documents().batchUpdate(
        documentId=fid,
        body={"requests": [{"insertText": {"location": {"index": 1}, "text": "SDK native text"}}]},
    ).execute()
    result = docs.documents().get(documentId=fid).execute()
    assert (
        result["body"]["content"][1]["paragraph"]["elements"][0]["textRun"]["content"] == "SDK native text\n"
    )
    drive = build("drive", "v3", credentials=Credentials(token()), static_discovery=True)
    assert drive.files().export_media(fileId=fid, mimeType="text/plain").execute() == b"SDK native text\n"
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    service = ServiceCredentials.from_service_account_info(
        {
            "type": "service_account",
            "client_email": "sdk-service@halcyonadvisory.example",
            "token_uri": "https://oauth2.googleapis.com/token",
            "private_key": pem,
            "private_key_id": "test-key",
        },
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    ).with_subject("maya.chen@halcyonadvisory.example")
    service.refresh(GoogleRequest())
    impersonated = build("drive", "v3", credentials=service, static_discovery=True)
    assert (
        impersonated.about().get(fields="user(emailAddress)").execute()["user"]["emailAddress"]
        == "maya.chen@halcyonadvisory.example"
    )
    for api in (admin, docs, drive, impersonated):
        api.close()


def test_sdk_drive_batch_mixed_success_error_and_long_get_override(emulator_intercept: Any) -> None:
    service = build("drive", "v3", credentials=Credentials(token()), static_discovery=True)
    results: dict[str, tuple[Any, Any]] = {}
    batch = service.new_batch_http_request(
        callback=lambda key, value, error: results.update({key: (value, error)})
    )
    batch.add(service.files().create(body={"name": "Batched file"}, fields="id,name"), request_id="create")
    batch.add(service.about().get(fields="user(emailAddress)"), request_id="about")
    batch.add(service.files().get(fileId="missing"), request_id="missing")
    batch.execute()
    assert results["create"][0]["name"] == "Batched file" and results["create"][1] is None
    assert results["about"][0]["user"]["emailAddress"] == "maya.chen@halcyonadvisory.example"
    assert isinstance(results["missing"][1], HttpError) and results["missing"][1].resp.status == 404
    query = " or ".join(["name = 'Batched file'"] * 150)
    request = service.files().list(q=query, fields="files(id,name)")
    assert len(request.uri) > 2048
    response = request.execute()
    assert any(row["name"] == "Batched file" for row in response["files"])
    entries = emulator_intercept.hub.context("google").journal.entries()
    assert any(r["method"] == "POST" and r["headers"].get("x-http-method-override") == "GET" for r in entries)
    service.close()


def test_google_auth_service_account_id_token_credentials(emulator_intercept: Any) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from google.oauth2.service_account import IDTokenCredentials

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    credential = IDTokenCredentials.from_service_account_info(
        {
            "type": "service_account",
            "client_email": "sdk-service@halcyonadvisory.example",
            "token_uri": "https://oauth2.googleapis.com/token",
            "private_key": pem,
            "private_key_id": "test-key",
        },
        target_audience="https://consumer.example",
    )
    credential.refresh(GoogleRequest())
    assert credential.token
    verified = verify_oauth2_token(credential.token, GoogleRequest(), audience="https://consumer.example")
    assert verified["email"] == "sdk-service@halcyonadvisory.example"
    for version in ("v1", "v2", "v3"):
        response = requests.get(
            "https://www.googleapis.com/oauth2/" + version + "/tokeninfo",
            params={"id_token": credential.token},
            timeout=10,
        )
        response.raise_for_status()
        body = response.json()
        assert (
            isinstance(body["exp"], str) and isinstance(body["iat"], str) and body["email_verified"] == "true"
        )
    profile = requests.get(
        "https://www.googleapis.com/userinfo/v2/me",
        headers={"Authorization": "Bearer " + token()},
        timeout=10,
    )
    assert profile.status_code == 200 and profile.json()["verified_email"] is True
