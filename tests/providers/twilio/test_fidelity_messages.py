"""Audit regressions for Programmable Messaging: validation, scheduling, filters, webhooks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response
from twilio.request_validator import RequestValidator

from tests.providers.twilio.conftest import (
    API,
    CONTACT,
    FROM_NUMBER,
    WHATSAPP_SENDER,
    account_base,
    messages_url,
    seeded_messaging_service,
)


async def send(client: httpx.AsyncClient, **overrides: object) -> httpx.Response:
    data: dict[str, object] = {"To": CONTACT, "From": FROM_NUMBER, "Body": "Hello from Halcyon"}
    data.update(overrides)
    data = {k: v for k, v in data.items() if v is not None}
    return await client.post(messages_url(client), data=data)


# -- create-time validation -----------------------------------------------------------------


async def test_missing_to_is_21604_and_other_required_params(twilio: httpx.AsyncClient) -> None:
    r = await send(twilio, To=None)
    assert r.status_code == 400 and r.json()["code"] == 21604
    r = await send(twilio, Body=None)
    assert r.status_code == 400 and r.json()["code"] == 21602
    r = await send(twilio, From=None)
    assert r.status_code == 400 and r.json()["code"] == 21603


async def test_content_sid_replaces_body(twilio: httpx.AsyncClient) -> None:
    r = await send(twilio, Body=None, ContentSid="HX" + "a" * 32)
    assert r.status_code == 201, r.text
    assert r.json()["num_segments"] == "1" and r.json()["body"] == ""
    bad = await send(twilio, Body=None, ContentSid="template-1")
    assert bad.status_code == 400 and bad.json()["code"] == 21655


async def test_url_parameters_are_validated(twilio: httpx.AsyncClient) -> None:
    media = await send(twilio, MediaUrl="ftp://example.com/x.png")
    assert media.status_code == 400 and media.json()["code"] == 21620
    callback = await send(twilio, StatusCallback="not a url")
    assert callback.status_code == 400 and callback.json()["code"] == 21609


async def test_channel_pairs_must_match(twilio: httpx.AsyncClient) -> None:
    sms_to_wa = await send(twilio, To=f"whatsapp:{CONTACT}")
    assert sms_to_wa.status_code == 400 and sms_to_wa.json()["code"] == 21910
    wa_to_sms = await send(twilio, From=WHATSAPP_SENDER)
    assert wa_to_sms.status_code == 400 and wa_to_sms.json()["code"] == 21910
    # A WhatsApp From must be the registered sender, not just any number the account owns.
    other = await send(twilio, To=f"whatsapp:{CONTACT}", From=f"whatsapp:{FROM_NUMBER}")
    assert other.status_code == 400 and other.json()["code"] == 63007
    ok = await send(twilio, To=f"whatsapp:{CONTACT}", From=WHATSAPP_SENDER)
    assert ok.status_code == 201, ok.text
    # Twilio's published WhatsApp sandbox sender works out of the box.
    sandbox = await send(twilio, To=f"whatsapp:{CONTACT}", From="whatsapp:+14155238886")
    assert sandbox.status_code == 201, sandbox.text
    # Anything that is not sms/whatsapp (or not a number) is a 21211.
    junk = await send(twilio, To=f"carrier-pigeon:{CONTACT}")
    assert junk.status_code == 400 and junk.json()["code"] == 21211
    # NANP numbers need exactly ten digits after +1 and a real area code.
    short = await send(twilio, To="+1416555")
    assert short.status_code == 400 and short.json()["code"] == 21211
    area = await send(twilio, To="+11165550141")
    assert area.status_code == 400 and area.json()["code"] == 21211


async def test_message_wire_details(twilio: httpx.AsyncClient) -> None:
    media_only = await send(twilio, Body=None, MediaUrl="https://example.com/photo.png")
    assert media_only.status_code == 201, media_only.text
    body = media_only.json()
    assert body["body"] == "" and body["num_media"] == "1" and body["num_segments"] == "1"
    assert body["sid"].startswith("MM")


async def test_failed_and_undelivered_states(twilio: httpx.AsyncClient) -> None:
    hub = twilio.hub  # type: ignore[attr-defined]
    ctx = hub.context("twilio")
    ctx.settings["AUTO_ADVANCE"] = "0"
    try:
        sid = (await send(twilio)).json()["sid"]
        failed = await twilio.post(
            f"{API}/_emulator/actions/advance_message", json={"sid": sid, "status": "failed"}
        )
        message = failed.json()["message"]
        assert message["status"] == "failed"
        assert message["error_code"] == 30008 and message["error_message"] == "Unknown error"
        assert message["price"] is None  # failed messages are not billed
        undelivered = await send(twilio, To="+14165550199")
        done = await twilio.post(
            f"{API}/_emulator/actions/advance_message", json={"sid": undelivered.json()["sid"]}
        )
        assert done.json()["message"]["status"] == "undelivered"
        assert done.json()["message"]["error_code"] == 30003
        assert done.json()["message"]["price"] == "-0.00790"
    finally:
        ctx.settings.pop("AUTO_ADVANCE", None)


async def test_feedback_outcome_is_validated(twilio: httpx.AsyncClient) -> None:
    sid = (await send(twilio)).json()["sid"]
    url = messages_url(twilio).replace(".json", f"/{sid}/Feedback.json")
    bad = await twilio.post(url, data={"Outcome": "maybe"})
    assert bad.status_code == 400
    ok = await twilio.post(url, data={"Outcome": "confirmed"})
    assert ok.status_code == 201 and ok.json()["outcome"] == "confirmed"


# -- scheduling -----------------------------------------------------------------------------------


async def test_scheduling_window_and_release_on_the_clock(twilio: httpx.AsyncClient) -> None:
    hub = twilio.hub  # type: ignore[attr-defined]
    hub.clock.freeze(datetime(2026, 10, 16, 9, 0, tzinfo=UTC))
    service = await seeded_messaging_service(twilio)

    async def schedule(send_at: str, **extra: str) -> httpx.Response:
        return await twilio.post(
            messages_url(twilio),
            data={
                "To": CONTACT,
                "MessagingServiceSid": service["sid"],
                "Body": "later",
                "ScheduleType": "fixed",
                "SendAt": send_at,
                **extra,
            },
        )

    soon = await schedule("2026-10-16T09:10:00Z")
    assert soon.status_code == 400 and soon.json()["code"] == 35114
    far = await schedule("2026-12-01T09:00:00Z")
    assert far.status_code == 400 and far.json()["code"] == 35114
    garbage = await schedule("next tuesday")
    assert garbage.status_code == 400 and garbage.json()["code"] == 35114
    wrong_type = await schedule("2026-10-16T10:00:00Z", ScheduleType="recurring")
    assert wrong_type.status_code == 400 and wrong_type.json()["code"] == 35115

    created = await schedule("2026-10-16T10:00:00Z")
    assert created.status_code == 201, created.text
    cancelled = await schedule("2026-10-16T10:30:00Z")
    sid, cancel_sid = created.json()["sid"], cancelled.json()["sid"]
    url = messages_url(twilio).replace(".json", f"/{sid}.json")
    assert (await twilio.get(url)).json()["status"] == "scheduled"
    assert (await twilio.get(url)).json()["date_sent"] is None

    await twilio.post(
        messages_url(twilio).replace(".json", f"/{cancel_sid}.json"), data={"Status": "canceled"}
    )
    hub.clock.advance(timedelta(minutes=61))  # past 10:00, before 10:30
    released = (await twilio.get(url)).json()
    assert released["status"] == "delivered" and released["date_sent"] is not None
    assert (await twilio.get(messages_url(twilio).replace(".json", f"/{cancel_sid}.json"))).json()[
        "status"
    ] == "canceled"
    # A message that already left cannot be canceled.
    again = await twilio.post(url, data={"Status": "canceled"})
    assert again.status_code == 400


# -- list filters ------------------------------------------------------------------------------------


async def test_date_sent_filters_are_inclusive_and_validated(twilio: httpx.AsyncClient) -> None:
    url = messages_url(twilio)
    everything = (await twilio.get(url, params={"PageSize": 1000})).json()["messages"]
    stamps = sorted(
        {m["date_sent"] for m in everything}, key=lambda s: datetime.strptime(s, "%a, %d %b %Y %H:%M:%S %z")
    )
    boundary = datetime.strptime(stamps[len(stamps) // 2], "%a, %d %b %Y %H:%M:%S %z")
    iso = boundary.strftime("%Y-%m-%dT%H:%M:%SZ")
    at_or_after = (await twilio.get(url, params={"DateSent>": iso, "PageSize": 1000})).json()["messages"]
    at_or_before = (await twilio.get(url, params={"DateSent<": iso, "PageSize": 1000})).json()["messages"]
    assert any(m["date_sent"] == stamps[len(stamps) // 2] for m in at_or_after)  # inclusive bound
    assert any(m["date_sent"] == stamps[len(stamps) // 2] for m in at_or_before)
    bad = await twilio.get(url, params={"DateSent": "garbage"})
    assert bad.status_code == 400
    # Newest first, by date_sent.
    sent = [datetime.strptime(m["date_sent"], "%a, %d %b %Y %H:%M:%S %z") for m in everything]
    assert sent == sorted(sent, reverse=True)


async def test_seed_timestamps_and_statuses_are_realistic(twilio: httpx.AsyncClient) -> None:
    messages = (await twilio.get(messages_url(twilio), params={"PageSize": 1000})).json()["messages"]
    fmt = "%a, %d %b %Y %H:%M:%S %z"
    anchor_end = datetime(2026, 10, 17, tzinfo=UTC)
    for m in messages:
        created, sent, updated = (
            datetime.strptime(m[k], fmt) for k in ("date_created", "date_sent", "date_updated")
        )
        assert created <= sent <= updated <= anchor_end, m["sid"]
        if m["direction"] == "outbound-api":
            assert sent > created  # handed to the carrier after creation
            assert m["price"] and m["price"].startswith("-")
    whatsapp = [m for m in messages if m["from"].startswith("whatsapp:") and m["direction"] == "outbound-api"]
    assert whatsapp and {m["status"] for m in whatsapp} == {"read"}
    assert all(m["error_code"] is None for m in messages if m["status"] in ("delivered", "read", "received"))


# -- webhooks ---------------------------------------------------------------------------------------------


class Capture:
    """Receiver that records method, URL, query, form and headers of every webhook."""

    def __init__(self, reply: str = "", status: int = 200) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reply = reply
        self.status = status
        app = FastAPI()

        @app.api_route("/{path:path}", methods=["GET", "POST"])
        async def hook(request: Request, path: str) -> Response:
            raw = (await request.body()).decode()
            self.calls.append(
                {
                    "path": path,
                    "method": request.method,
                    "url": str(request.url),
                    "query": dict(request.query_params),
                    "form": dict(parse_qsl(raw, keep_blank_values=True)),
                    "headers": dict(request.headers),
                }
            )
            return Response(self.reply, status_code=self.status, media_type="text/xml")

        self.app = app


async def configure_number(client: httpx.AsyncClient, phone: str, **data: str) -> None:
    numbers = (await client.get(f"{account_base(client)}/IncomingPhoneNumbers.json")).json()[
        "incoming_phone_numbers"
    ]
    sid = next(n["sid"] for n in numbers if n["phone_number"] == phone)
    r = await client.post(f"{account_base(client)}/IncomingPhoneNumbers/{sid}.json", data=data)
    assert r.status_code == 200, r.text


async def test_get_webhooks_carry_params_in_the_query_and_sign_the_full_url(
    twilio: httpx.AsyncClient,
) -> None:
    hub = twilio.hub  # type: ignore[attr-defined]
    capture = Capture()
    hub.webhook_transport = httpx.ASGITransport(app=capture.app)
    await configure_number(
        twilio, FROM_NUMBER, SmsUrl="https://consumer.test/sms?tenant=halcyon", SmsMethod="GET"
    )
    r = await twilio.post(
        f"{API}/_emulator/actions/inbound_sms", json={"from": CONTACT, "to": FROM_NUMBER, "body": "hi"}
    )
    assert r.status_code == 200, r.text
    (call,) = capture.calls
    assert call["method"] == "GET"
    assert call["query"]["Body"] == "hi" and call["query"]["tenant"] == "halcyon"
    assert call["form"] == {}  # nothing in the body of a GET
    assert call["headers"]["user-agent"] == "TwilioProxy/1.1"
    assert call["headers"]["i-twilio-idempotency-token"]
    token = twilio.creds["auth_token"]  # type: ignore[attr-defined]
    signature = call["headers"]["x-twilio-signature"]
    assert RequestValidator(token).validate(call["url"], {}, signature)  # full URL, no POST params


async def test_post_webhooks_use_form_charset_and_validate_unicode(twilio: httpx.AsyncClient) -> None:
    hub = twilio.hub  # type: ignore[attr-defined]
    capture = Capture()
    hub.webhook_transport = httpx.ASGITransport(app=capture.app)
    await configure_number(twilio, FROM_NUMBER, SmsUrl="http://consumer.test:8080/sms")
    body = "Café ☕ \U0001f600 100% & more=1"
    await twilio.post(
        f"{API}/_emulator/actions/inbound_sms", json={"from": CONTACT, "to": FROM_NUMBER, "body": body}
    )
    (call,) = capture.calls
    assert call["headers"]["content-type"] == "application/x-www-form-urlencoded; charset=UTF-8"
    assert call["form"]["Body"] == body
    validator = RequestValidator(twilio.creds["auth_token"])  # type: ignore[attr-defined]
    assert validator.validate(call["url"], call["form"], call["headers"]["x-twilio-signature"])


async def test_sms_fallback_url_is_used_when_the_primary_fails(twilio: httpx.AsyncClient) -> None:
    hub = twilio.hub  # type: ignore[attr-defined]
    primary = Capture(status=500)
    hub.webhook_transport = httpx.ASGITransport(app=primary.app)
    await configure_number(
        twilio, FROM_NUMBER, SmsUrl="https://primary.test/sms", SmsFallbackUrl="https://fallback.test/sms"
    )
    r = await twilio.post(
        f"{API}/_emulator/actions/inbound_sms", json={"from": CONTACT, "to": FROM_NUMBER, "body": "x"}
    )
    assert r.status_code == 200, r.text
    # One attempt at the primary (Twilio does not retry HTTP errors), then the fallback.
    hosts = [urlsplit(c["url"]).netloc for c in primary.calls]
    assert hosts == ["primary.test", "fallback.test"]
    fallback = primary.calls[1]["form"]
    assert fallback["ErrorCode"] == "11200" and fallback["ErrorUrl"] == "https://primary.test/sms"
    assert r.json()["webhook"]["url"] == "https://fallback.test/sms"


async def test_whatsapp_inbound_uses_the_whatsapp_parameter_set(twilio: httpx.AsyncClient) -> None:
    hub = twilio.hub  # type: ignore[attr-defined]
    capture = Capture()
    hub.webhook_transport = httpx.ASGITransport(app=capture.app)
    await configure_number(twilio, "+14165550177", SmsUrl="https://consumer.test/whatsapp")
    r = await twilio.post(
        f"{API}/_emulator/actions/inbound_sms",
        json={"from": f"whatsapp:{CONTACT}", "to": WHATSAPP_SENDER, "body": "Hello on WhatsApp"},
    )
    assert r.status_code == 200, r.text
    (call,) = capture.calls
    form = call["form"]
    assert form["WaId"] == CONTACT.lstrip("+")
    assert form["ProfileName"] == "Christopher Ingram"
    assert form["MessageType"] == "text" and form["ReferralNumMedia"] == "0"
    assert form["From"] == f"whatsapp:{CONTACT}" and form["To"] == WHATSAPP_SENDER
    assert "FromCountry" not in form and "ToCity" not in form  # SMS-only geography parameters


async def test_inbound_action_tolerates_wrongly_typed_payloads(twilio: httpx.AsyncClient) -> None:
    for payload in (
        {"from": 5, "to": ["x"], "body": {"a": 1}},
        {"from": CONTACT, "to": FROM_NUMBER, "body": 12345, "media_urls": "https://example.com/a.png"},
        {"from": CONTACT, "to": FROM_NUMBER, "body": None},
    ):
        r = await twilio.post(f"{API}/_emulator/actions/inbound_sms", json=payload)
        assert r.status_code < 500, (payload, r.text)


async def test_messages_created_in_the_same_second_list_newest_first(twilio: httpx.AsyncClient) -> None:
    twilio.hub.clock.freeze(datetime(2026, 10, 16, 9, 0, tzinfo=UTC))  # type: ignore[attr-defined]
    first = (await send(twilio, Body="first")).json()["sid"]
    second = (await send(twilio, Body="second")).json()["sid"]
    listing = (await twilio.get(messages_url(twilio), params={"PageSize": 2})).json()["messages"]
    assert [m["sid"] for m in listing] == [second, first]


async def test_media_bytes_match_the_advertised_content_type(twilio: httpx.AsyncClient) -> None:
    magic = {
        "image/png": bytes([0x89, 0x50, 0x4E, 0x47]),
        "image/jpeg": bytes([0xFF, 0xD8, 0xFF]),
        "image/gif": b"GIF8",
        "application/pdf": b"%PDF-",
        "audio/mpeg": bytes([0xFF, 0xFB]),
        "audio/wav": b"RIFF",
        "video/mp4": bytes([0, 0, 0]),
    }
    urls = {
        "image/png": "https://example.com/a.png",
        "image/jpeg": "https://example.com/b.jpg",
        "image/gif": "https://example.com/c.gif",
        "application/pdf": "https://example.com/d.pdf",
        "audio/mpeg": "https://example.com/e.mp3",
        "audio/wav": "https://example.com/f.wav",
        "video/mp4": "https://example.com/g.mp4",
    }
    for content_type, url in urls.items():
        sent = (await send(twilio, MediaUrl=url)).json()
        listing = (await twilio.get(f"{account_base(twilio)}/Messages/{sent['sid']}/Media.json")).json()
        (media,) = listing["media_list"]
        assert media["content_type"] == content_type
        raw = await twilio.get(f"{API}{media['uri'].removesuffix('.json')}")
        assert raw.headers["content-type"].startswith(content_type)
        assert raw.content.startswith(magic[content_type]), (content_type, raw.content[:8])


async def test_seeded_inbound_mms_are_downloadable(twilio: httpx.AsyncClient) -> None:
    messages = (await twilio.get(messages_url(twilio), params={"PageSize": 1000})).json()["messages"]
    mms = [m for m in messages if m["sid"].startswith("MM") and m["direction"] == "inbound"]
    assert len(mms) == 2 and all(m["num_media"] == "1" and m["num_segments"] == "1" for m in mms)
    types = []
    for message in mms:
        (media,) = (await twilio.get(f"{API}{message['subresource_uris']['media']}")).json()["media_list"]
        types.append(media["content_type"])
        raw = await twilio.get(f"{API}{media['uri'].removesuffix('.json')}")
        assert raw.status_code == 200 and len(raw.content) > 100
    assert sorted(types) == ["application/pdf", "image/png"]


async def test_date_sent_equality_is_a_day_filter_even_with_a_timestamp(twilio: httpx.AsyncClient) -> None:
    url = messages_url(twilio)
    by_day = (await twilio.get(url, params={"DateSent": "2026-10-14", "PageSize": 1000})).json()["messages"]
    by_stamp = (await twilio.get(url, params={"DateSent": "2026-10-14T17:45:00Z", "PageSize": 1000})).json()[
        "messages"
    ]  # what the SDKs send for a datetime
    assert by_day and [m["sid"] for m in by_day] == [m["sid"] for m in by_stamp]
    assert all("14 Oct 2026" in m["date_sent"] for m in by_day)


async def test_whatsapp_status_callbacks_carry_channel_fields(twilio: httpx.AsyncClient) -> None:
    capture = Capture()
    twilio.hub.webhook_transport = httpx.ASGITransport(app=capture.app)  # type: ignore[attr-defined]
    sent = await send(
        twilio,
        To=f"whatsapp:{CONTACT}",
        From=WHATSAPP_SENDER,
        StatusCallback="https://consumer.test/status",
    )
    assert sent.status_code == 201, sent.text
    assert [c["form"]["MessageStatus"] for c in capture.calls] == ["sending", "sent", "delivered"]
    for call in capture.calls:
        assert call["form"]["ChannelPrefix"] == "whatsapp" and call["form"]["ChannelToAddress"] == CONTACT
    plain = await send(twilio, StatusCallback="https://consumer.test/status")
    assert plain.status_code == 201
    assert "ChannelPrefix" not in capture.calls[-1]["form"]


async def test_inbound_media_urls_follow_the_way_the_emulator_was_reached(twilio: httpx.AsyncClient) -> None:
    hub = twilio.hub  # type: ignore[attr-defined]
    capture = Capture()
    hub.webhook_transport = httpx.ASGITransport(app=capture.app)
    await configure_number(twilio, FROM_NUMBER, SmsUrl="https://consumer.test/sms")
    gateway = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=hub.gateway), base_url="http://localhost:44080"
    )
    async with gateway:
        r = await gateway.post(
            "/twilio/_emulator/actions/inbound_sms",
            json={
                "from": CONTACT,
                "to": FROM_NUMBER,
                "body": "photo",
                "media_urls": ["https://example.com/p.png"],
            },
        )
        assert r.status_code == 200, r.text
        media_url = capture.calls[-1]["form"]["MediaUrl0"]
        assert media_url.startswith("http://localhost:44080/twilio/2010-04-01/Accounts/")
        # ... and it is downloadable there, without touching the vendor hostname.
        creds = twilio.creds  # type: ignore[attr-defined]
        downloaded = await gateway.get(media_url, auth=(creds["account_sid"], creds["auth_token"]))
        assert downloaded.status_code == 200 and downloaded.content[:4] == bytes([0x89, 0x50, 0x4E, 0x47])
