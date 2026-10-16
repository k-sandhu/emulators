"""Inbound SMS simulation: signed webhooks, TwiML replies, opt-out keywords, status callbacks."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

from tests.providers.twilio.conftest import API, CONTACT, FROM_NUMBER, account_base, messages_url

SMS_URL = "http://consumer.test/sms"
STATUS_URL = "http://consumer.test/status"


class Receiver:
    """An in-process consumer app capturing Twilio webhooks."""

    def __init__(self, twiml: str = "<Response/>") -> None:
        self.twiml = twiml
        self.calls: list[dict[str, Any]] = []
        app = FastAPI()

        @app.post("/sms")
        async def sms(request: Request) -> Response:
            return await self._capture(request, "sms", self.twiml)

        @app.post("/status")
        async def status(request: Request) -> Response:
            return await self._capture(request, "status", "")

        self.app = app

    async def _capture(self, request: Request, kind: str, reply: str) -> Response:
        raw = (await request.body()).decode()
        self.calls.append(
            {
                "kind": kind,
                "params": dict(parse_qsl(raw, keep_blank_values=True)),
                "signature": request.headers.get("x-twilio-signature"),
                "url": SMS_URL if kind == "sms" else STATUS_URL,
            }
        )
        return Response(reply, media_type="text/xml")

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["kind"] == kind]


def attach(client: httpx.AsyncClient, receiver: Receiver) -> Any:
    hub = client.hub  # type: ignore[attr-defined]
    hub.webhook_transport = httpx.ASGITransport(app=receiver.app)
    return hub.context("twilio")


async def set_sms_url(client: httpx.AsyncClient, url: str | None) -> None:
    numbers = (await client.get(f"{account_base(client)}/IncomingPhoneNumbers.json")).json()[
        "incoming_phone_numbers"
    ]
    sid = next(n["sid"] for n in numbers if n["phone_number"] == FROM_NUMBER)
    r = await client.post(
        f"{account_base(client)}/IncomingPhoneNumbers/{sid}.json", data={"SmsUrl": url or ""}
    )
    assert r.status_code == 200, r.text


async def inbound(client: httpx.AsyncClient, **payload: Any) -> httpx.Response:
    body = {"from": CONTACT, "to": FROM_NUMBER, "body": "Hello Halcyon"}
    body.update(payload)
    return await client.post(f"{API}/_emulator/actions/inbound_sms", json=body)


def assert_signed(call: dict[str, Any], token: str) -> None:
    from twilio.request_validator import RequestValidator

    validator = RequestValidator(token)
    assert call["signature"], "missing X-Twilio-Signature"
    assert validator.validate(call["url"], call["params"], call["signature"]), call


async def test_inbound_sms_webhook_params_and_signature(twilio: httpx.AsyncClient) -> None:
    receiver = Receiver()
    ctx = attach(twilio, receiver)
    await set_sms_url(twilio, SMS_URL)
    r = await inbound(twilio, body="Can we move Thursday's call?")
    assert r.status_code == 200, r.text
    message = r.json()["message"]
    assert message["direction"] == "inbound"
    assert message["status"] == "received"
    assert message["from"] == CONTACT and message["to"] == FROM_NUMBER

    (call,) = receiver.of_kind("sms")
    params = call["params"]
    assert params["Body"] == "Can we move Thursday's call?"
    assert params["From"] == CONTACT and params["To"] == FROM_NUMBER
    assert params["MessageSid"] == message["sid"] == params["SmsSid"] == params["SmsMessageSid"]
    assert params["AccountSid"] == twilio.account_sid  # type: ignore[attr-defined]
    assert params["SmsStatus"] == "received"
    assert params["NumMedia"] == "0" and params["NumSegments"] == "1"
    assert params["ApiVersion"] == "2010-04-01"
    assert params["FromCountry"] == "CA" and params["FromCity"] == "Toronto"
    assert params["ToCity"] == "Toronto" and params["ToCountry"] == "CA"
    for key in ("ToState", "FromZip", "FromState", "ToZip"):
        assert key in params
    assert_signed(call, ctx.store.meta["credentials"]["auth_token"])

    # The inbound message is fetchable through the regular API.
    fetched = (await twilio.get(messages_url(twilio).replace(".json", f"/{message['sid']}.json"))).json()
    assert fetched["body"] == "Can we move Thursday's call?"


async def test_inbound_mms_media_params(twilio: httpx.AsyncClient) -> None:
    receiver = Receiver()
    attach(twilio, receiver)
    await set_sms_url(twilio, SMS_URL)
    r = await inbound(twilio, body="photo attached", media_urls=["https://example.com/receipt.png"])
    assert r.status_code == 200, r.text
    (call,) = receiver.of_kind("sms")
    params = call["params"]
    assert params["NumMedia"] == "1"
    assert params["MediaContentType0"] == "image/png"
    assert params["MediaUrl0"].startswith("https://api.twilio.com/2010-04-01/Accounts/")
    # The media bytes are downloadable through the API.
    media = await twilio.get(params["MediaUrl0"])
    assert media.status_code == 200
    assert media.content[:8] == b"\x89PNG\r\n\x1a\n"


async def test_twiml_message_replies_are_created(twilio: httpx.AsyncClient) -> None:
    receiver = Receiver(
        twiml="<Response><Message>Thanks, we will be in touch.</Message>"
        "<Message><Body>And here is that form.</Body><Media>https://example.com/form.pdf</Media></Message></Response>"
    )
    attach(twilio, receiver)
    await set_sms_url(twilio, SMS_URL)
    r = await inbound(twilio)
    assert r.status_code == 200, r.text
    replies = r.json()["replies"]
    assert len(replies) == 2
    assert replies[0]["direction"] == "outbound-reply"
    assert replies[0]["body"] == "Thanks, we will be in touch."
    assert replies[0]["from"] == FROM_NUMBER and replies[0]["to"] == CONTACT
    assert replies[0]["status"] in ("queued", "delivered")
    assert replies[1]["num_media"] == "1"
    fetched = (await twilio.get(messages_url(twilio).replace(".json", f"/{replies[0]['sid']}.json"))).json()
    assert fetched["status"] == "delivered"


async def test_no_sms_url_means_no_webhook(twilio: httpx.AsyncClient) -> None:
    receiver = Receiver()
    attach(twilio, receiver)
    r = await inbound(twilio)
    assert r.status_code == 200, r.text
    assert r.json()["webhook"] is None
    assert receiver.calls == []


async def test_opt_out_and_opt_in(twilio: httpx.AsyncClient) -> None:
    receiver = Receiver()
    attach(twilio, receiver)
    await set_sms_url(twilio, SMS_URL)
    r = await inbound(twilio, body="STOP")
    assert r.status_code == 200, r.text
    assert r.json()["opted_out"] is True
    (reply,) = r.json()["replies"]
    assert "unsubscribed" in reply["body"]
    assert receiver.of_kind("sms") == []  # carrier keywords are not forwarded to the app

    blocked = await twilio.post(
        messages_url(twilio), data={"To": CONTACT, "From": FROM_NUMBER, "Body": "still there?"}
    )
    assert blocked.status_code == 400 and blocked.json()["code"] == 21610

    r = await inbound(twilio, body="START")
    assert r.json()["opted_in"] is True
    ok = await twilio.post(
        messages_url(twilio), data={"To": CONTACT, "From": FROM_NUMBER, "Body": "welcome back"}
    )
    assert ok.status_code == 201, ok.text


async def test_help_keyword(twilio: httpx.AsyncClient) -> None:
    attach(twilio, Receiver())
    r = await inbound(twilio, body="HELP")
    assert r.status_code == 200, r.text
    (reply,) = r.json()["replies"]
    assert "STOP" in reply["body"]


async def test_status_callbacks_are_signed_and_ordered(twilio: httpx.AsyncClient) -> None:
    receiver = Receiver()
    ctx = attach(twilio, receiver)
    r = await twilio.post(
        messages_url(twilio),
        data={"To": CONTACT, "From": FROM_NUMBER, "Body": "track me", "StatusCallback": STATUS_URL},
    )
    assert r.status_code == 201, r.text
    sid = r.json()["sid"]
    statuses = [c["params"]["MessageStatus"] for c in receiver.of_kind("status")]
    assert statuses == ["sending", "sent", "delivered"]
    for call in receiver.of_kind("status"):
        params = call["params"]
        assert params["MessageSid"] == sid == params["SmsSid"]
        assert params["SmsStatus"] == params["MessageStatus"]
        assert params["From"] == FROM_NUMBER and params["To"] == CONTACT
        assert params["ApiVersion"] == "2010-04-01"
        assert_signed(call, ctx.store.meta["credentials"]["auth_token"])
    delivered = receiver.of_kind("status")[-1]["params"]
    assert "RawDlrDoneDate" in delivered
    assert "ErrorCode" not in delivered


async def test_messaging_service_inbound_routing(twilio: httpx.AsyncClient) -> None:
    from tests.providers.twilio.conftest import MESSAGING, seeded_messaging_service

    receiver = Receiver()
    attach(twilio, receiver)
    service = await seeded_messaging_service(twilio)
    # Service inbound URL takes precedence over the number's (unset) SmsUrl.
    r = await twilio.post(f"{MESSAGING}/v1/Services/{service['sid']}", data={"InboundRequestUrl": SMS_URL})
    assert r.status_code == 200, r.text
    r = await inbound(twilio, body="routed via the service")
    assert r.status_code == 200, r.text
    (call,) = receiver.of_kind("sms")
    assert call["params"]["MessagingServiceSid"] == service["sid"]
    assert call["params"]["Body"] == "routed via the service"

    # With use_inbound_webhook_on_number, the number's SmsUrl wins again.
    receiver.calls.clear()
    await twilio.post(f"{MESSAGING}/v1/Services/{service['sid']}", data={"UseInboundWebhookOnNumber": "true"})
    r = await inbound(twilio, body="no number webhook configured")
    assert r.json()["webhook"] is None
    assert receiver.calls == []


async def test_undelivered_status_callback_carries_error(twilio: httpx.AsyncClient) -> None:
    receiver = Receiver()
    attach(twilio, receiver)
    r = await twilio.post(
        messages_url(twilio),
        data={"To": "+14165550199", "From": FROM_NUMBER, "Body": "bounce", "StatusCallback": STATUS_URL},
    )
    assert r.status_code == 201, r.text
    last = receiver.of_kind("status")[-1]["params"]
    assert last["MessageStatus"] == "undelivered"
    assert last["ErrorCode"] == "30003"
