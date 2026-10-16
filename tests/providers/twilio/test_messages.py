"""Messages: creation shapes, validation, delivery progression, media, paging."""

from __future__ import annotations

import re

import httpx
import pytest

from tests.providers.twilio.conftest import (
    CONTACT,
    FROM_NUMBER,
    WHATSAPP_SENDER,
    messages_url,
    seeded_messaging_service,
)

SID_RE = re.compile(r"^(SM|MM)[0-9a-f]{32}$")
RFC2822_RE = re.compile(r"^[A-Z][a-z]{2}, \d{2} [A-Z][a-z]{2} \d{4} \d{2}:\d{2}:\d{2} \+0000$")


async def send(client: httpx.AsyncClient, **overrides: object) -> httpx.Response:
    data: dict[str, object] = {"To": CONTACT, "From": FROM_NUMBER, "Body": "Hello from Halcyon"}
    data.update(overrides)
    return await client.post(messages_url(client), data=data)


async def test_create_message_shape_and_progression(twilio: httpx.AsyncClient) -> None:
    r = await send(twilio)
    assert r.status_code == 201, r.text
    msg = r.json()
    assert SID_RE.match(msg["sid"])
    assert msg["account_sid"] == twilio.account_sid  # type: ignore[attr-defined]
    assert msg["api_version"] == "2010-04-01"
    assert msg["body"] == "Hello from Halcyon"
    assert msg["direction"] == "outbound-api"
    assert msg["status"] == "queued"  # create response shows the initial status
    assert msg["date_sent"] is None and msg["price"] is None
    assert msg["price_unit"] == "USD"
    assert msg["num_media"] == "0" and msg["num_segments"] == "1"
    assert msg["error_code"] is None and msg["error_message"] is None
    assert msg["messaging_service_sid"] is None
    assert RFC2822_RE.match(msg["date_created"]), msg["date_created"]
    assert msg["uri"].endswith(f"/Messages/{msg['sid']}.json")
    assert set(msg["subresource_uris"]) == {"media", "feedback"}

    fetched = (await twilio.get(messages_url(twilio).replace(".json", f"/{msg['sid']}.json"))).json()
    assert fetched["status"] == "delivered"  # sync webhook mode advanced it already
    assert fetched["date_sent"] is not None
    assert fetched["price"] == "-0.00790"


async def test_whatsapp_message(twilio: httpx.AsyncClient) -> None:
    r = await send(twilio, To=f"whatsapp:{CONTACT}", From=WHATSAPP_SENDER, Body="Hi on WhatsApp")
    assert r.status_code == 201, r.text
    assert r.json()["from"] == WHATSAPP_SENDER
    assert r.json()["to"] == f"whatsapp:{CONTACT}"


@pytest.mark.parametrize(
    ("body", "segments"),
    [
        ("a" * 160, "1"),  # GSM-7 single segment limit
        ("a" * 161, "2"),  # concatenated at 153/segment
        ("a" * 306, "2"),
        ("a" * 307, "3"),
        ("café " + "x" * 60, "1"),  # é is in the GSM-7 basic set
        ("Hello €" + "x" * 152, "1"),  # € counts 2 septets: exactly 160 units total
        ("Hello €" + "x" * 153, "2"),
        ("こんにちは", "1"),  # UCS-2
        ("あ" * 70, "1"),
        ("あ" * 71, "2"),
        ("😀" * 36, "2"),  # surrogate pairs: 72 UTF-16 units at 67/segment
    ],
)
async def test_num_segments(twilio: httpx.AsyncClient, body: str, segments: str) -> None:
    r = await send(twilio, Body=body)
    assert r.status_code == 201, r.text
    assert r.json()["num_segments"] == segments


@pytest.mark.parametrize(
    ("to", "code"),
    [
        ("+15005550001", 21211),
        ("+15005550002", 21612),
        ("+15005550003", 21408),
        ("+15005550004", 21610),
        ("+15005550009", 21614),
        ("not-a-number", 21211),
        ("+1", 21211),
    ],
)
async def test_magic_to_numbers(twilio: httpx.AsyncClient, to: str, code: int) -> None:
    r = await send(twilio, To=to)
    assert r.status_code == 400, r.text
    body = r.json()
    assert body["code"] == code
    assert body["status"] == 400
    assert body["more_info"] == f"https://www.twilio.com/docs/errors/{code}"


@pytest.mark.parametrize(
    ("from_", "code"),
    [
        ("+15005550001", 21212),
        ("+15005550007", 21606),
        ("+15005550008", 21611),
        ("+16475550151", 21606),  # an employee's mobile: real world number, not owned by the account
    ],
)
async def test_magic_from_numbers(twilio: httpx.AsyncClient, from_: str, code: int) -> None:
    r = await send(twilio, From=from_)
    assert r.status_code == 400, r.text
    assert r.json()["code"] == code


async def test_magic_valid_from(twilio: httpx.AsyncClient) -> None:
    r = await send(twilio, From="+15005550006")
    assert r.status_code == 201, r.text


async def test_create_validation(twilio: httpx.AsyncClient) -> None:
    r = await twilio.post(messages_url(twilio), data={"From": FROM_NUMBER, "Body": "x"})
    assert r.status_code == 400 and r.json()["code"] == 21604
    r = await twilio.post(messages_url(twilio), data={"To": CONTACT, "Body": "x"})
    assert r.status_code == 400 and r.json()["code"] == 21603
    r = await twilio.post(messages_url(twilio), data={"To": CONTACT, "From": FROM_NUMBER})
    assert r.status_code == 400 and r.json()["code"] == 21602
    r = await send(twilio, Body="x" * 1601)
    assert r.status_code == 400 and r.json()["code"] == 21617
    r = await twilio.post(
        messages_url(twilio),
        data={
            "To": CONTACT,
            "From": FROM_NUMBER,
            "MediaUrl": [f"https://example.com/{i}.png" for i in range(11)],
        },
    )
    assert r.status_code == 400


async def test_undelivered_simulators(twilio: httpx.AsyncClient) -> None:
    r = await send(twilio, To="+14165550199")
    assert r.status_code == 201, r.text
    fetched = (await twilio.get(messages_url(twilio).replace(".json", f"/{r.json()['sid']}.json"))).json()
    assert fetched["status"] == "undelivered"
    assert fetched["error_code"] == 30003

    r = await send(twilio, To="+14165550198")
    fetched = (await twilio.get(messages_url(twilio).replace(".json", f"/{r.json()['sid']}.json"))).json()
    assert fetched["status"] == "undelivered"
    assert fetched["error_code"] == 30005


async def test_messaging_service_send_is_accepted_then_delivered(twilio: httpx.AsyncClient) -> None:
    service = await seeded_messaging_service(twilio)
    r = await twilio.post(
        messages_url(twilio),
        data={"To": CONTACT, "MessagingServiceSid": service["sid"], "Body": "via the pool"},
    )
    assert r.status_code == 201, r.text
    msg = r.json()
    assert msg["status"] == "accepted"
    assert msg["messaging_service_sid"] == service["sid"]
    assert msg["from"] in (FROM_NUMBER, "+12125550188")  # picked from the sender pool
    fetched = (await twilio.get(messages_url(twilio).replace(".json", f"/{msg['sid']}.json"))).json()
    assert fetched["status"] == "delivered"

    r = await twilio.post(
        messages_url(twilio),
        data={"To": CONTACT, "MessagingServiceSid": "MG" + "0" * 32, "Body": "nope"},
    )
    assert r.status_code == 404 and r.json()["code"] == 20404


async def test_scheduled_message_lifecycle(twilio: httpx.AsyncClient) -> None:
    service = await seeded_messaging_service(twilio)
    r = await twilio.post(
        messages_url(twilio),
        data={
            "To": CONTACT,
            "MessagingServiceSid": service["sid"],
            "Body": "send me later",
            "ScheduleType": "fixed",
            "SendAt": "2026-10-20T15:00:00Z",
        },
    )
    assert r.status_code == 201, r.text
    sid = r.json()["sid"]
    assert r.json()["status"] == "scheduled"
    url = messages_url(twilio).replace(".json", f"/{sid}.json")
    assert (await twilio.get(url)).json()["status"] == "scheduled"

    r = await twilio.post(url, data={"Status": "canceled"})
    assert r.status_code == 200 and r.json()["status"] == "canceled"

    # Scheduling without a Messaging Service is rejected like Twilio does.
    r = await send(twilio, ScheduleType="fixed", SendAt="2026-10-20T15:00:00Z")
    assert r.status_code == 400 and r.json()["code"] == 21704


async def test_redact_and_delete(twilio: httpx.AsyncClient) -> None:
    sid = (await send(twilio)).json()["sid"]
    url = messages_url(twilio).replace(".json", f"/{sid}.json")
    r = await twilio.post(url, data={"Body": "cannot edit"})
    assert r.status_code == 400
    r = await twilio.post(url, data={"Body": ""})
    assert r.status_code == 200 and r.json()["body"] == ""
    r = await twilio.delete(url)
    assert r.status_code == 204
    r = await twilio.get(url)
    assert r.status_code == 404 and r.json()["code"] == 20404


async def test_media_message_and_content(twilio: httpx.AsyncClient) -> None:
    r = await twilio.post(
        messages_url(twilio),
        data={
            "To": CONTACT,
            "From": FROM_NUMBER,
            "Body": "see attached",
            "MediaUrl": [
                "https://halcyonadvisory.example/files/photo.png",
                "https://halcyonadvisory.example/files/report.pdf",
            ],
        },
    )
    assert r.status_code == 201, r.text
    msg = r.json()
    assert msg["sid"].startswith("MM")
    assert msg["num_media"] == "2"
    media_url = messages_url(twilio).replace(".json", f"/{msg['sid']}/Media.json")
    listing = (await twilio.get(media_url)).json()
    assert len(listing["media_list"]) == 2
    by_type = {m["content_type"]: m for m in listing["media_list"]}
    assert set(by_type) == {"image/png", "application/pdf"}
    png = by_type["image/png"]
    assert png["sid"].startswith("ME") and png["parent_sid"] == msg["sid"]
    content_url = messages_url(twilio).replace(".json", f"/{msg['sid']}/Media/{png['sid']}")
    content = await twilio.get(content_url)
    assert content.status_code == 200
    assert content.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert content.headers["content-type"].startswith("image/png")
    # Second fetch serves the blob-cached copy (identical bytes).
    assert (await twilio.get(content_url)).content == content.content
    pdf = by_type["application/pdf"]
    pdf_content = await twilio.get(messages_url(twilio).replace(".json", f"/{msg['sid']}/Media/{pdf['sid']}"))
    assert pdf_content.content[:4] == b"%PDF"
    # Delete one media item.
    r = await twilio.delete(messages_url(twilio).replace(".json", f"/{msg['sid']}/Media/{pdf['sid']}.json"))
    assert r.status_code == 204
    assert (await twilio.get(messages_url(twilio).replace(".json", f"/{msg['sid']}.json"))).json()[
        "num_media"
    ] == "1"


async def test_seeded_history_and_list_filters(twilio: httpx.AsyncClient) -> None:
    r = await twilio.get(messages_url(twilio), params={"To": CONTACT, "PageSize": 100})
    assert r.status_code == 200, r.text
    outbound = r.json()["messages"]
    assert outbound, "seeded outbound history to Christopher Ingram"
    assert all(m["to"] == CONTACT for m in outbound)
    assert all(m["from"] == FROM_NUMBER for m in outbound)
    assert any("Noah Williams" in (m["body"] or "") for m in outbound)

    r = await twilio.get(messages_url(twilio), params={"From": CONTACT})
    inbound = r.json()["messages"]
    assert inbound and all(m["direction"] == "inbound" and m["status"] == "received" for m in inbound)

    # WhatsApp history is seeded with whatsapp: addresses.
    r = await twilio.get(messages_url(twilio), params={"From": WHATSAPP_SENDER, "PageSize": 5})
    assert r.json()["messages"], "seeded WhatsApp history"


async def test_list_paging_envelope(twilio: httpx.AsyncClient) -> None:
    first = (await twilio.get(messages_url(twilio), params={"PageSize": 5})).json()
    assert first["page"] == 0 and first["page_size"] == 5
    assert first["start"] == 0 and first["end"] == 4
    assert first["previous_page_uri"] is None
    assert first["next_page_uri"]
    assert first["first_page_uri"].startswith("/2010-04-01/Accounts/")
    assert first["uri"].startswith("/2010-04-01/Accounts/")
    # Follow the relative next-page URI the way the SDK does (against api.twilio.com).
    second = (await twilio.get(f"https://api.twilio.com{first['next_page_uri']}")).json()
    assert second["page"] == 1
    assert {m["sid"] for m in first["messages"]}.isdisjoint(m["sid"] for m in second["messages"])
    # Newest first.
    assert first["messages"][0]["date_created"] >= second["messages"][0]["date_created"]


async def test_list_date_filters(twilio: httpx.AsyncClient) -> None:
    r = await twilio.get(messages_url(twilio), params={"DateSent>": "2026-10-01", "PageSize": 200})
    assert r.status_code == 200, r.text
    assert r.json()["messages"]
    r = await twilio.get(messages_url(twilio), params={"DateSent<": "2020-01-01"})
    assert r.json()["messages"] == []
    r = await twilio.get(messages_url(twilio), params={"DateSent": "2026-10-03", "From": FROM_NUMBER})
    # Seeded conversation conv-sms-001 starts 2026-10-03 (UTC).
    assert any(m["date_sent"] and "03 Oct 2026" in m["date_sent"] for m in r.json()["messages"])


async def test_advance_message_action(twilio: httpx.AsyncClient) -> None:
    service = await seeded_messaging_service(twilio)
    created = await twilio.post(
        messages_url(twilio),
        data={
            "To": CONTACT,
            "MessagingServiceSid": service["sid"],
            "Body": "later",
            "ScheduleType": "fixed",
            "SendAt": "2026-10-20T15:00:00Z",
        },
    )
    sid = created.json()["sid"]
    r = await twilio.post(
        "https://api.twilio.com/_emulator/actions/advance_message", json={"sid": sid, "status": "delivered"}
    )
    assert r.status_code == 200, r.text
    r = await twilio.post(
        "https://api.twilio.com/_emulator/actions/advance_message", json={"sid": "SM" + "0" * 32}
    )
    assert r.status_code == 404


async def test_feedback(twilio: httpx.AsyncClient) -> None:
    sid = (await send(twilio)).json()["sid"]
    url = messages_url(twilio).replace(".json", f"/{sid}/Feedback.json")
    r = await twilio.post(url, data={"Outcome": "confirmed"})
    assert r.status_code == 201, r.text
    assert r.json()["outcome"] == "confirmed"
    assert r.json()["message_sid"] == sid
