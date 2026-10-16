from __future__ import annotations

import base64
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

from emulators.core.clock import isoformat_z
from emulators.providers.microsoft import common as c
from emulators.testing.helpers import bearer, mint

BASE = "https://graph.microsoft.com"
START = {"dateTime": "2027-01-20T09:00:00", "timeZone": "UTC"}
END = {"dateTime": "2027-01-20T10:00:00", "timeZone": "UTC"}


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        ({}, "BadRequest"),
        ({"message": {}}, "ErrorInvalidRecipients"),
        ({"message": {"toRecipients": []}}, "ErrorInvalidRecipients"),
        ({"message": {"toRecipients": [{"emailAddress": {"address": "invalid"}}]}}, "ErrorInvalidRecipients"),
        (
            {
                "message": {
                    "importance": "urgent",
                    "toRecipients": [{"emailAddress": {"address": "maya.chen@halcyonadvisory.example"}}],
                }
            },
            "BadRequest",
        ),
    ],
)
async def test_send_mail_validates_before_mutating_state(
    make_client: Callable[..., Any], payload: Any, code: str
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        count = len(client.hub.context("microsoft").store["mail_messages"])
        response = await client.post("/v1.0/me/sendMail", json=payload)
        assert response.status_code == 400 and response.json()["error"]["code"] == code
        assert len(client.hub.context("microsoft").store["mail_messages"]) == count


@pytest.mark.parametrize("resource", ["messages", "events"])
@pytest.mark.parametrize("content_type", ["HTML", "Text"])
async def test_body_normalization_patch_projection_and_outlook_preference(
    make_client: Callable[..., Any],
    resource: str,
    content_type: str,
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        payload = {
            "subject": "Body contract",
            "body": {"contentType": content_type, "content": "<p>Hello</p>"},
        }
        if resource == "events":
            payload.update(start=START, end=END)
        response = await client.post("/v1.0/me/" + resource, json=payload)
        assert response.status_code == 201, response.text
        assert response.json()["body"]["contentType"] == content_type.lower()
        url = f"/v1.0/me/{resource}/{response.json()['id']}"
        patched = await client.patch(url, json={"body": {"contentType": "HTML", "content": "<b>Updated</b>"}})
        assert patched.status_code == 200 and patched.json()["body"]["contentType"] == "html"
        result = await client.get(
            url, params={"$select": "body"}, headers={"Prefer": 'outlook.body-content-type="text"'}
        )
        assert result.json()["body"] == {"contentType": "text", "content": "Updated"}
        assert result.headers["Preference-Applied"] == 'outlook.body-content-type="text"'
        assert set(result.json()) == {"id", "body", "@odata.context", "@odata.etag"}


async def test_mail_folder_creation_paging_delta_and_scoped_message_writes(
    make_client: Callable[..., Any],
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        initial = (await client.get("/v1.0/me/mailFolders/delta")).json()
        while "@odata.nextLink" in initial:
            initial = (await client.get(initial["@odata.nextLink"])).json()
        delta = initial["@odata.deltaLink"]
        parent = await client.post("/v1.0/me/mailFolders", json={"displayName": "Advisory QA"})
        assert parent.status_code == 201, parent.text
        fid = parent.json()["id"]
        child = await client.post(f"/v1.0/me/mailFolders/{fid}/childFolders", json={"displayName": "Review"})
        assert child.status_code == 201 and child.json()["parentFolderId"] == fid
        assert (await client.get(f"/v1.0/me/mailFolders/{fid}")).json()["childFolderCount"] == 1
        changed = (await client.get(delta)).json()["value"]
        assert {r["id"] for r in changed} == {fid, child.json()["id"]}
        draft = await client.post(f"/v1.0/me/mailFolders/{fid}/messages", json={"subject": "Folder write"})
        mid = draft.json()["id"]
        assert (
            await client.patch(f"/v1.0/me/mailFolders/Inbox/messages/{mid}", json={"isRead": True})
        ).status_code == 404
        assert (
            await client.patch(f"/v1.0/me/mailFolders/{fid}/messages/{mid}", json={"subject": "Patched"})
        ).json()["subject"] == "Patched"
        assert (await client.delete(f"/v1.0/me/mailFolders/{fid}/messages/{mid}")).status_code == 204
        assert (await client.get(f"/v1.0/me/messages/{mid}")).status_code == 404
        for i in range(5):
            assert (
                await client.post("/v1.0/me/mailFolders", json={"displayName": f"Review {i}"})
            ).status_code == 201
        page = (await client.get("/v1.0/me/mailFolders")).json()
        assert len(page["value"]) == 10 and "$skiptoken=" in page["@odata.nextLink"]
        assert (await client.get(page["@odata.nextLink"])).json()["value"]


async def test_message_default_order_and_reply_forward_drafts(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        for path in ("messages", "mailFolders/Inbox/messages"):
            rows = (await client.get("/v1.0/me/" + path)).json()["value"]
            dates = [row["receivedDateTime"] for row in rows]
            assert dates == sorted(dates, reverse=True)
        source = (await client.get("/v1.0/me/mailFolders/Inbox/messages")).json()["value"][0]
        for action, prefix in (("createReplyAll", "RE: "), ("createForward", "FW: ")):
            response = await client.post(
                f"/v1.0/me/messages/{source['id']}/{action}", json={"comment": "Review"}
            )
            assert response.status_code == 201, response.text
            assert response.json()["isDraft"] and response.json()["subject"].startswith(prefix)
            assert (await client.get(f"/v1.0/me/messages/{response.json()['id']}")).status_code == 200
            if action == "createReplyAll":
                assert response.json()["toRecipients"]


@pytest.mark.parametrize("resource", ["messages", "events"])
async def test_small_file_attachment_crud_and_mailbox_visibility(
    make_client: Callable[..., Any], resource: str
) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        payload = {"subject": "Attachment contract"}
        if resource == "events":
            payload.update(start=START, end=END)
        created = (await client.post("/v1.0/me/" + resource, json=payload)).json()
        path = f"/v1.0/me/{resource}/{created['id']}"
        content = b"\x00\xffattachment bytes\r\n"
        attachment = {
            "@odata.type": "#microsoft.graph.fileAttachment",
            "name": "Review.txt",
            "contentType": "text/plain",
            "contentBytes": base64.b64encode(content).decode(),
        }
        response = await client.post(path + "/attachments", json=attachment)
        assert response.status_code == 201, response.text
        aid = response.json()["id"]
        assert response.json()["size"] == len(content)
        assert response.json()["@odata.type"] == "#microsoft.graph.fileAttachment"
        assert (await client.get(path)).json()["hasAttachments"]
        assert (await client.get(path)).json()["changeKey"] != created["changeKey"]
        assert (await client.get(path + f"/attachments/{aid}/$value")).content == content
        assert (await client.get(path + "/attachments")).json()["value"][0]["contentBytes"] == attachment[
            "contentBytes"
        ]
        other = bearer(await mint(client, BASE, user="p019"))
        uid = c.user_id(client.hub.world, "p018")
        denied = await client.get(f"/v1.0/users/{uid}/{resource}/{created['id']}/attachments", headers=other)
        assert denied.status_code == 403
        invalid = await client.post(
            path + "/attachments", json={**attachment, "contentBytes": "!not-base64!"}
        )
        assert invalid.status_code == 400
        if resource == "events":
            assert (await client.delete(path + f"/attachments/{aid}")).status_code == 204
            assert not (await client.get(path)).json()["hasAttachments"]


async def test_calendar_creation_isolation_and_snooze_reminder(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        response = await client.post("/v1.0/me/calendars", json={"name": "Advisory Review"})
        assert response.status_code == 201, response.text
        cid = response.json()["id"]
        assert not response.json()["isDefaultCalendar"]
        assert (await client.get(f"/v1.0/me/calendars/{cid}")).json()["name"] == "Advisory Review"
        response = await client.post(
            f"/v1.0/me/calendars/{cid}/events",
            json={"subject": "Separate calendar", "start": START, "end": END},
        )
        assert response.status_code == 201, response.text
        eid = response.json()["id"]
        assert [e["id"] for e in (await client.get(f"/v1.0/me/calendars/{cid}/events")).json()["value"]] == [
            eid
        ]
        assert not (await client.get("/v1.0/me/events?$filter=subject eq 'Separate calendar'")).json()[
            "value"
        ]
        assert (
            await client.post(f"/v1.0/me/events/{eid}/snoozeReminder", json={"newReminderTime": {}})
        ).status_code == 400
        snooze = await client.post(
            f"/v1.0/me/calendars/{cid}/events/{eid}/snoozeReminder", json={"newReminderTime": START}
        )
        assert snooze.status_code == 204 and not snooze.content
        default = (await client.get("/v1.0/me/calendar")).json()["id"]
        assert (
            await client.patch(f"/v1.0/me/calendars/{default}/events/{eid}", json={"subject": "Denied"})
        ).status_code == 404


async def test_meeting_times_use_normalized_graph_datetime_timezone(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        response = await client.post(
            "/v1.0/me/findMeetingTimes",
            json={
                "meetingDuration": "PT30M",
                "timeConstraint": {"timeSlots": [{"start": START, "end": END}]},
            },
        )
        slot = response.json()["meetingTimeSuggestions"][0]["meetingTimeSlot"]
        assert slot == {
            "start": {"dateTime": "2027-01-20T09:00:00.0000000", "timeZone": "UTC"},
            "end": {"dateTime": "2027-01-20T09:30:00.0000000", "timeZone": "UTC"},
        }


async def test_schedule_status_codes_and_unresolved_recipient(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        for index, status in enumerate(("free", "tentative", "busy", "oof", "workingElsewhere")):
            start = {**START, "dateTime": f"2027-01-20T{9 + index:02}:00:00"}
            end = {**END, "dateTime": f"2027-01-20T{10 + index:02}:00:00"}
            assert (
                await client.post("/v1.0/me/events", json={"start": start, "end": end, "showAs": status})
            ).status_code == 201
        response = await client.post(
            "/v1.0/me/calendar/getSchedule",
            json={
                "schedules": ["maya.chen@halcyonadvisory.example", "missing@halcyonadvisory.example"],
                "startTime": START,
                "endTime": {**END, "dateTime": "2027-01-20T14:00:00"},
                "availabilityViewInterval": 60,
            },
        )
        rows = response.json()["value"]
        assert rows[0]["availabilityView"] == "01230"
        assert rows[1]["error"]["responseCode"] == "ErrorMailRecipientNotFound"


async def test_contact_defaults_etags_folders_and_delta(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft", base_url=BASE) as client:
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        seeded = (await client.get("/v1.0/me/contacts")).json()["value"][0]
        assert seeded["@odata.etag"] == f'W/"{seeded["changeKey"]}"'
        initial = (await client.get("/v1.0/me/contacts/delta")).json()
        while "@odata.nextLink" in initial:
            initial = (await client.get(initial["@odata.nextLink"])).json()
        response = await client.post("/v1.0/me/contacts", json={"givenName": "Maya", "surname": "Chen"})
        assert response.status_code == 201, response.text
        contact = response.json()
        assert contact["displayName"] == "Maya Chen" and contact["fileAs"] == "Chen, Maya"
        assert (
            contact["homePhones"] == [] and contact["homeAddress"] == {} and contact["assistantName"] is None
        )
        assert contact["@odata.etag"] == f'W/"{contact["changeKey"]}"'
        changed = (await client.get(initial["@odata.deltaLink"])).json()
        assert [row["id"] for row in changed["value"]] == [contact["id"]]
        patched = await client.patch(f"/v1.0/me/contacts/{contact['id']}", json={"jobTitle": "Review"})
        assert patched.json()["changeKey"] != contact["changeKey"]
        assert (await client.delete(f"/v1.0/me/contacts/{contact['id']}")).status_code == 204
        deleted = (await client.get(changed["@odata.deltaLink"])).json()["value"]
        assert deleted == [{"id": contact["id"], "@removed": {"reason": "deleted"}}]
        folder = await client.post("/v1.0/me/contactFolders", json={"displayName": "Advisory QA"})
        assert folder.status_code == 201 and folder.json()["@odata.context"].endswith(
            "contactFolders/$entity"
        )
        path = f"/v1.0/me/contactFolders/{folder.json()['id']}/contacts"
        cursor = (await client.get(path + "/delta")).json()["@odata.deltaLink"]
        created = (await client.post(path, json={"givenName": "Maya", "surname": "Chen"})).json()
        assert [row["id"] for row in (await client.get(cursor)).json()["value"]] == [created["id"]]
        assert (
            await client.get(cursor.replace(f"contactFolders/{folder.json()['id']}/", ""))
        ).status_code == 400


async def test_outlook_subscription_expiry_limit_and_notification_casing(
    make_client: Callable[..., Any],
) -> None:
    receiver = FastAPI()
    notices: list[dict[str, Any]] = []

    @receiver.post("/hook")
    async def hook(request: Request) -> Any:
        if token := request.query_params.get("validationToken"):
            return PlainTextResponse(token)
        notices.extend((await request.json())["value"])
        return {}

    async with make_client("microsoft", base_url=BASE) as client:
        client.hub.webhook_transport = httpx.ASGITransport(app=receiver)
        client.headers.update(bearer(await mint(client, BASE, user="p018")))
        for resource in ("messages", "events", "contacts"):
            body = {
                "resource": "me/" + resource,
                "changeType": "created",
                "notificationUrl": "https://consumer.example/hook",
                "expirationDateTime": isoformat_z(client.hub.clock.now() + timedelta(minutes=10071)),
            }
            assert (await client.post("/v1.0/subscriptions", json=body)).status_code == 400
            body["expirationDateTime"] = isoformat_z(client.hub.clock.now() + timedelta(minutes=10070))
            response = await client.post("/v1.0/subscriptions", json=body)
            assert response.status_code == 201, response.text
            assert (await client.post("/v1.0/subscriptions", json=body)).status_code == 409
        message = (await client.post("/v1.0/me/messages", json={"subject": "Notify"})).json()
        uid = c.user_id(client.hub.world, "p018")
        assert notices[0]["resource"] == f"Users/{uid}/Messages/{message['id']}"
        assert notices[0]["resourceData"]["@odata.id"] == notices[0]["resource"]
