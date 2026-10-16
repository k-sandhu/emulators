"""Official discovery clients exercising Gmail v1 and Calendar v3 unchanged."""

from __future__ import annotations

import base64
import io
from email.message import EmailMessage
from typing import Any

import pytest
import requests
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as ServiceCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

pytestmark = pytest.mark.sdk
BASE = "https://www.googleapis.com"


def credentials(user: str = "p018") -> Credentials:
    response = requests.post(BASE + "/_emulator/tokens", json={"user": user}, timeout=10)
    response.raise_for_status()
    return Credentials(token=response.json()["access_token"])


def mime() -> bytes:
    message = EmailMessage()
    message["From"] = "maya.chen@halcyonadvisory.example"
    message["To"] = "eleanor.whitfield@halcyonadvisory.example"
    message["Subject"] = "SDK integration contract"
    message.set_content("Testing the official Google SDK against the local emulator.")
    return message.as_bytes()


def test_gmail_discovery_read_formats_search_and_pagination(emulator_intercept: Any) -> None:
    service = build("gmail", "v1", credentials=credentials(), static_discovery=True)
    users = service.users()
    profile = users.getProfile(userId="me").execute()
    assert profile["emailAddress"] == "maya.chen@halcyonadvisory.example"
    first = users.messages().list(userId="me", maxResults=1).execute()
    second = users.messages().list_next(users.messages().list(userId="me", maxResults=1), first).execute()
    assert first["messages"][0]["id"] != second["messages"][0]["id"]
    message_id = first["messages"][0]["id"]
    assert users.messages().get(userId="me", id=message_id, format="full").execute()["payload"]["headers"]
    metadata = (
        users.messages()
        .get(userId="me", id=message_id, format="metadata", metadataHeaders=["Subject"])
        .execute()
    )
    assert [h["name"] for h in metadata["payload"]["headers"]] == ["Subject"]
    assert users.messages().get(userId="me", id=message_id, format="raw").execute()["raw"]
    assert users.messages().list(userId="me", q="from:maya.chen OR is:unread").execute()["messages"]
    assert users.labels().list(userId="me").execute()["labels"]
    with pytest.raises(HttpError) as denied:
        users.getProfile(userId="eleanor.whitfield@halcyonadvisory.example").execute()
    assert denied.value.resp.status == 403


def test_gmail_discovery_send_draft_labels_threads_history(emulator_intercept: Any) -> None:
    users = build("gmail", "v1", credentials=credentials(), static_discovery=True).users()
    start = users.getProfile(userId="me").execute()["historyId"]
    body = {"raw": base64.urlsafe_b64encode(mime()).decode()}
    sent = users.messages().send(userId="me", body=body).execute()
    assert sent["labelIds"] == ["SENT"]
    label = users.labels().create(userId="me", body={"name": "SDK/Integration"}).execute()
    users.messages().modify(userId="me", id=sent["id"], body={"addLabelIds": [label["id"]]}).execute()
    assert users.labels().get(userId="me", id=label["id"]).execute()["messagesTotal"] == 1
    assert users.threads().get(userId="me", id=sent["threadId"]).execute()["messages"]
    assert (
        users.history()
        .list(userId="me", startHistoryId=start, historyTypes=["messageAdded"])
        .execute()["history"]
    )
    draft = users.drafts().create(userId="me", body={"message": body}).execute()
    assert users.drafts().get(userId="me", id=draft["id"], format="raw").execute()["message"]["raw"]
    sent_draft = users.drafts().send(userId="me", body={"id": draft["id"]}).execute()
    users.messages().batchModify(
        userId="me", body={"ids": [sent_draft["id"]], "addLabelIds": ["STARRED"]}
    ).execute()
    users.messages().batchDelete(userId="me", body={"ids": [sent_draft["id"]]}).execute()
    users.labels().delete(userId="me", id=label["id"]).execute()


def test_gmail_discovery_multipart_media_upload(emulator_intercept: Any) -> None:
    users = build("gmail", "v1", credentials=credentials(), static_discovery=True).users()
    media = MediaIoBaseUpload(io.BytesIO(mime()), mimetype="message/rfc822", resumable=False)
    sent = users.messages().send(userId="me", body={}, media_body=media).execute()
    assert sent["labelIds"] == ["SENT"]
    media = MediaIoBaseUpload(io.BytesIO(mime()), mimetype="message/rfc822", resumable=False)
    imported = users.messages().insert(userId="me", body={"labelIds": ["INBOX"]}, media_body=media).execute()
    assert imported["labelIds"] == ["INBOX"]


def event() -> dict[str, Any]:
    return {
        "summary": "SDK integration",
        "start": {"dateTime": "2026-10-20T09:00:00-04:00", "timeZone": "America/Toronto"},
        "end": {"dateTime": "2026-10-20T10:00:00-04:00", "timeZone": "America/Toronto"},
    }


def test_calendar_discovery_event_crud_sync_and_conference(emulator_intercept: Any) -> None:
    service = build("calendar", "v3", credentials=credentials("p001"), static_discovery=True)
    assert service.calendarList().list().execute()["items"]
    assert service.calendars().get(calendarId="primary").execute()["timeZone"] == "America/Toronto"
    sync = service.events().list(calendarId="primary").execute()["nextSyncToken"]
    body = {**event(), "conferenceData": {"createRequest": {"requestId": "sdk-meet"}}}
    created = (
        service.events()
        .insert(calendarId="primary", conferenceDataVersion=1, sendUpdates="all", body=body)
        .execute()
    )
    assert created["conferenceData"]["conferenceSolution"]["key"]["type"] == "hangoutsMeet"
    updated = (
        service.events()
        .patch(calendarId="primary", eventId=created["id"], body={"summary": "Updated SDK integration"})
        .execute()
    )
    assert updated["summary"] == "Updated SDK integration"
    delta = service.events().list(calendarId="primary", syncToken=sync).execute()
    assert [r["id"] for r in delta["items"]] == [created["id"]]
    service.events().delete(calendarId="primary", eventId=created["id"]).execute()
    assert (
        service.events()
        .list(calendarId="primary", syncToken=delta["nextSyncToken"])
        .execute()["items"][0]["status"]
        == "cancelled"
    )


def test_calendar_discovery_recurrence_freebusy_and_settings(emulator_intercept: Any) -> None:
    service = build("calendar", "v3", credentials=credentials("p001"), static_discovery=True)
    recurring = (
        service.events()
        .insert(calendarId="primary", body={**event(), "recurrence": ["RRULE:FREQ=WEEKLY;COUNT=3"]})
        .execute()
    )
    assert (
        len(service.events().instances(calendarId="primary", eventId=recurring["id"]).execute()["items"]) == 3
    )
    busy = (
        service.freebusy()
        .query(
            body={
                "timeMin": "2026-10-20T00:00:00Z",
                "timeMax": "2026-10-21T00:00:00Z",
                "items": [{"id": "primary"}],
            }
        )
        .execute()
    )
    assert busy["calendars"]["primary"]["busy"]
    assert service.settings().get(setting="timezone").execute()["value"] == "America/Toronto"
    assert len(service.colors().get().execute()["event"]) == 11


def test_workspace_service_account_impersonation(emulator_intercept: Any) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    service = ServiceCredentials.from_service_account_info(
        {
            "type": "service_account",
            "client_email": "workspace-test@halcyonadvisory.example",
            "token_uri": "https://oauth2.googleapis.com/token",
            "private_key": pem,
            "private_key_id": "workspace-test",
        },
        scopes=[
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/calendar.readonly",
            "https://www.googleapis.com/auth/meetings.space.readonly",
        ],
    )
    delegated = service.with_subject("maya.chen@halcyonadvisory.example")
    delegated.refresh(GoogleRequest())
    gmail = build("gmail", "v1", credentials=delegated, static_discovery=True)
    assert (
        gmail.users().getProfile(userId="me").execute()["emailAddress"] == "maya.chen@halcyonadvisory.example"
    )
    calendar = build("calendar", "v3", credentials=delegated, static_discovery=True)
    assert (
        calendar.calendars().get(calendarId="primary").execute()["id"] == "maya.chen@halcyonadvisory.example"
    )
    meet = requests.get(
        "https://meet.googleapis.com/v2/conferenceRecords",
        headers={"Authorization": "Bearer " + delegated.token},
        timeout=10,
    )
    assert meet.status_code == 200 and meet.json()["conferenceRecords"]
    service.refresh(GoogleRequest())
    app_only = build("gmail", "v1", credentials=service, static_discovery=True)
    with pytest.raises(HttpError) as denied:
        app_only.users().getProfile(userId="me").execute()
    assert denied.value.resp.status == 400
    for api in (gmail, calendar, app_only):
        api.close()


@pytest.mark.parametrize(
    "api,version,user",
    [("calendar", "v3", "p001"), ("gmail", "v1", "p018"), ("admin", "directory_v1", "p035")],
)
def test_workspace_sdk_batch_and_partial_responses(
    emulator_intercept: Any, api: str, version: str, user: str
) -> None:
    service = build(api, version, credentials=credentials(user), static_discovery=True)
    if api == "calendar":
        request = service.calendarList().list(fields="items(id),nextSyncToken")
        key, children = "items", {"id"}
    elif api == "gmail":
        request = (
            service.users().messages().list(userId="me", maxResults=2, fields="messages(id),nextPageToken")
        )
        key, children = "messages", {"id"}
    else:
        request = service.users().list(
            customer="my_customer", maxResults=2, fields="users(id,primaryEmail),nextPageToken"
        )
        key, children = "users", {"id", "primaryEmail"}
    results = []
    batch = service.new_batch_http_request(
        callback=lambda request_id, response, error: results.append((response, error))
    )
    batch.add(request, request_id="partial")
    batch.execute()
    assert results[0][1] is None, results[0][1]
    assert results[0][0][key] and all(set(row) == children for row in results[0][0][key])
    service.close()
