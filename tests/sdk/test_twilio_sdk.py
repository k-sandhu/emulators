"""Official `twilio` SDK driven unchanged against the emulator via emulator_intercept."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response
from twilio.base.exceptions import TwilioException, TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from emulators.core.hub import EmulatorHub

pytestmark = pytest.mark.sdk

FROM = "+14165550188"
CONTACT = "+14165550141"  # Christopher Ingram


def _creds(hub: EmulatorHub) -> dict[str, str]:
    return hub.context("twilio").store.meta["credentials"]


@pytest.fixture
def client(emulator_intercept: object, emulators: EmulatorHub) -> Client:
    creds = _creds(emulators)
    return Client(creds["account_sid"], creds["auth_token"])


def test_account_balance_and_usage(client: Client, emulators: EmulatorHub) -> None:
    creds = _creds(emulators)
    account = client.api.accounts(creds["account_sid"]).fetch()
    assert account.friendly_name == "Halcyon Advisory Group"
    assert account.status == "active"
    assert account.type == "Full"
    assert account.date_created is not None
    assert str(client.balance.fetch().balance) == "152.34"
    records = client.usage.records.list()
    assert {r.category for r in records} >= {"sms", "calls", "totalprice"}


def test_messages_create_fetch_list_redact_delete(client: Client) -> None:
    msg = client.messages.create(to=CONTACT, from_=FROM, body="Sent with the official SDK")
    assert msg.sid.startswith("SM")
    assert msg.status == "queued"
    assert msg.direction == "outbound-api"
    assert msg.from_ == FROM
    assert msg.to == CONTACT
    assert msg.num_segments == "1"
    assert msg.date_created is not None
    assert msg.date_sent is None

    fetched = client.messages(msg.sid).fetch()
    assert fetched.status == "delivered"
    assert fetched.date_sent is not None
    assert fetched.price == "-0.00790"

    recent = client.messages.list(to=CONTACT, limit=5)
    assert recent and all(m.to == CONTACT for m in recent)

    page = client.messages.page(page_size=3)
    first = {m.sid for m in page}
    second_page = page.next_page()
    assert second_page is not None
    assert first.isdisjoint(m.sid for m in second_page)

    after = client.messages.list(date_sent_after=datetime(2026, 10, 1, tzinfo=UTC), limit=10)
    assert after

    redacted = client.messages(msg.sid).update(body="")
    assert redacted.body == ""
    assert client.messages(msg.sid).delete() is True
    with pytest.raises(TwilioRestException) as gone:
        client.messages(msg.sid).fetch()
    assert gone.value.status == 404
    assert gone.value.code == 20404


def test_messages_media_and_whatsapp(client: Client) -> None:
    msg = client.messages.create(
        to=CONTACT,
        from_=FROM,
        body="with media",
        media_url=["https://halcyonadvisory.example/photo.png", "https://halcyonadvisory.example/report.pdf"],
    )
    assert msg.sid.startswith("MM")
    assert msg.num_media == "2"
    media = client.messages(msg.sid).media.list()
    assert {m.content_type for m in media} == {"image/png", "application/pdf"}
    assert all(m.parent_sid == msg.sid for m in media)

    wa = client.messages.create(to=f"whatsapp:{CONTACT}", from_="whatsapp:+14165550177", body="WhatsApp hi")
    assert wa.from_ == "whatsapp:+14165550177"


def test_sdk_error_mapping(client: Client, emulators: EmulatorHub) -> None:
    with pytest.raises(TwilioRestException) as invalid_to:
        client.messages.create(to="+15005550001", from_=FROM, body="x")
    assert invalid_to.value.status == 400
    assert invalid_to.value.code == 21211
    assert "+15005550001" in (invalid_to.value.msg or "")

    with pytest.raises(TwilioRestException) as too_long:
        client.messages.create(to=CONTACT, from_=FROM, body="x" * 1601)
    assert too_long.value.code == 21617

    bad_client = Client("AC" + "f" * 32, "whatever")
    with pytest.raises(TwilioRestException) as auth_error:
        bad_client.balance.fetch()
    assert auth_error.value.status == 401
    assert auth_error.value.code == 20003

    creds = _creds(emulators)
    key_client = Client(creds["api_key_sid"], creds["api_key_secret"], creds["account_sid"])
    assert key_client.messages.list(limit=1)


def test_test_credentials_magic_numbers(emulator_intercept: object, emulators: EmulatorHub) -> None:
    creds = _creds(emulators)
    test_client = Client(creds["test_account_sid"], creds["test_auth_token"])
    msg = test_client.messages.create(to=CONTACT, from_="+15005550006", body="test credentials send")
    assert msg.account_sid == creds["test_account_sid"]
    with pytest.raises(TwilioRestException) as blocked:
        test_client.messages.create(to="+15005550004", from_="+15005550006", body="x")
    assert blocked.value.code == 21610


def test_phone_numbers_and_available(client: Client) -> None:
    numbers = client.incoming_phone_numbers.list()
    assert [n.phone_number for n in numbers] == [FROM, "+12125550188", "+14165550177"]
    toronto = numbers[0]
    assert toronto.friendly_name == "Halcyon Client Line"
    assert toronto.capabilities["sms"] is True

    updated = client.incoming_phone_numbers(toronto.sid).update(sms_url="http://consumer.test/sms")
    assert updated.sms_url == "http://consumer.test/sms"

    available = client.available_phone_numbers("CA").local.list(area_code=416, sms_enabled=True)
    assert available
    assert available[0].phone_number.startswith("+141655501")
    assert available[0].locality == "Toronto"

    purchased = client.incoming_phone_numbers.create(
        phone_number=available[0].phone_number, friendly_name="SDK line"
    )
    assert purchased.phone_number == available[0].phone_number
    assert client.incoming_phone_numbers(purchased.sid).delete() is True


def test_messaging_services(client: Client) -> None:
    services = client.messaging.v1.services.list()
    seeded = next(s for s in services if s.friendly_name == "Halcyon Client Notifications")
    assert seeded.sid.startswith("MG")
    pool = client.messaging.v1.services(seeded.sid).phone_numbers.list()
    assert {p.phone_number for p in pool} == {FROM, "+12125550188"}

    msg = client.messages.create(to=CONTACT, messaging_service_sid=seeded.sid, body="via the service")
    assert msg.status == "accepted"
    assert msg.messaging_service_sid == seeded.sid

    created = client.messaging.v1.services.create(
        friendly_name="SDK service", status_callback="http://consumer.test/mg-status"
    )
    numbers = client.incoming_phone_numbers.list(phone_number=FROM)
    # A number lives in one sender pool at a time (409 otherwise): move it out of the seeded service.
    with pytest.raises(TwilioRestException) as taken:
        client.messaging.v1.services(created.sid).phone_numbers.create(phone_number_sid=numbers[0].sid)
    assert taken.value.status == 409
    assert client.messaging.v1.services(seeded.sid).phone_numbers(numbers[0].sid).delete() is True
    link = client.messaging.v1.services(created.sid).phone_numbers.create(phone_number_sid=numbers[0].sid)
    assert link.phone_number == FROM
    renamed = client.messaging.v1.services(created.sid).update(friendly_name="SDK service v2")
    assert renamed.friendly_name == "SDK service v2"
    assert client.messaging.v1.services(created.sid).phone_numbers(link.sid).delete() is True
    assert client.messaging.v1.services(created.sid).delete() is True


def test_verify(client: Client, emulators: EmulatorHub) -> None:
    services = client.verify.v2.services.list()
    seeded = next(s for s in services if s.friendly_name == "Halcyon Client Portal")
    assert seeded.code_length == 6

    verification = client.verify.v2.services(seeded.sid).verifications.create(to=CONTACT, channel="sms")
    assert verification.sid.startswith("VE")
    assert verification.status == "pending"
    assert verification.channel == "sms"

    ctx = emulators.context("twilio")
    record = next(r for r in ctx.store.collection("verifications").values() if r["sid"] == verification.sid)
    check = client.verify.v2.services(seeded.sid).verification_checks.create(to=CONTACT, code="000000")
    assert check.status == "pending" and check.valid is False
    check = client.verify.v2.services(seeded.sid).verification_checks.create(to=CONTACT, code=record["code"])
    assert check.status == "approved" and check.valid is True

    # The universal code works for a fresh verification too.
    client.verify.v2.services(seeded.sid).verifications.create(to=CONTACT, channel="sms")
    check = client.verify.v2.services(seeded.sid).verification_checks.create(to=CONTACT, code="123456")
    assert check.status == "approved"

    created = client.verify.v2.services.create(friendly_name="SDK verify", code_length=4)
    assert created.code_length == 4
    assert client.verify.v2.services(created.sid).delete() is True


def test_lookup(client: Client) -> None:
    result = client.lookups.v2.phone_numbers(CONTACT).fetch(fields="line_type_intelligence,caller_name")
    assert result.valid is True
    assert result.country_code == "CA"
    assert result.national_format == "(416) 555-0141"
    assert result.caller_name["caller_name"] == "CHRISTOPHER INGRAM"  # type: ignore[index]
    assert result.line_type_intelligence["type"] == "mobile"  # type: ignore[index]
    invalid = client.lookups.v2.phone_numbers("+15005550001").fetch()
    assert invalid.valid is False


def test_calls_and_recordings(client: Client) -> None:
    call = client.calls.create(
        to=CONTACT,
        from_=FROM,
        twiml="<Response><Say>Hello from the SDK</Say></Response>",
        record=True,
    )
    assert call.sid.startswith("CA")
    assert call.status == "queued"
    fetched = client.calls(call.sid).fetch()
    assert fetched.status == "completed"
    assert int(fetched.duration) > 0

    recordings = client.recordings.list()
    assert recordings
    seeded_and_new = [r for r in recordings if r.call_sid == call.sid]
    assert len(seeded_and_new) == 1
    assert seeded_and_new[0].media_url.endswith(f"/Recordings/{seeded_and_new[0].sid}")

    completed = client.calls.list(status="completed", to=CONTACT)
    assert completed and all(c.status == "completed" for c in completed)


def test_conversations(client: Client) -> None:
    convs = client.conversations.v1.conversations.list()
    assert len(convs) == 18
    seeded = next(c for c in convs if c.unique_name == "conv-sms-001")
    messages = client.conversations.v1.conversations(seeded.sid).messages.list()
    assert len(messages) == 6
    assert messages[0].author == "halcyon-advisory"

    created = client.conversations.v1.conversations.create(friendly_name="SDK thread")
    participant = client.conversations.v1.conversations(created.sid).participants.create(identity="agent-9")
    assert participant.identity == "agent-9"
    message = client.conversations.v1.conversations(created.sid).messages.create(
        author="agent-9", body="hello"
    )
    assert message.index == 0
    assert message.participant_sid == participant.sid
    assert client.conversations.v1.conversations(created.sid).delete() is True


def test_status_callback_signature_validates(client: Client, emulators: EmulatorHub) -> None:
    received: list[dict[str, Any]] = []
    receiver = FastAPI()

    @receiver.post("/status")
    async def status(request: Request) -> Response:
        raw = (await request.body()).decode()
        received.append(
            {
                "params": dict(parse_qsl(raw, keep_blank_values=True)),
                "sig": request.headers.get("x-twilio-signature"),
            }
        )
        return Response("")

    emulators.webhook_transport = httpx.ASGITransport(app=receiver)
    try:
        msg = client.messages.create(
            to=CONTACT, from_=FROM, body="signed callbacks", status_callback="http://consumer.test/status"
        )
    finally:
        emulators.webhook_transport = None
    assert [r["params"]["MessageStatus"] for r in received] == ["sending", "sent", "delivered"]
    validator = RequestValidator(_creds(emulators)["auth_token"])
    for entry in received:
        assert entry["params"]["MessageSid"] == msg.sid
        assert validator.validate("http://consumer.test/status", entry["params"], entry["sig"])


# -- audit regressions: things a real SDK client trips on ---------------------------------------------


def test_incoming_number_type_lists_and_partial_filter(client: Client) -> None:
    assert [n.phone_number for n in client.incoming_phone_numbers.local.list()] == [
        FROM,
        "+12125550188",
        "+14165550177",
    ]
    assert client.incoming_phone_numbers.toll_free.list() == []
    assert {n.phone_number for n in client.incoming_phone_numbers.list(phone_number="+1212")} == {
        "+12125550188"
    }


def test_paging_keeps_the_filters(client: Client) -> None:
    everything = client.messages.list(to=CONTACT)
    paged = client.messages.list(to=CONTACT, page_size=2)
    assert {m.sid for m in paged} == {m.sid for m in everything}
    assert len(paged) > 2 and all(m.to == CONTACT for m in paged)
    inbound_page = client.messages.page(from_=CONTACT, page_size=1)
    assert all(m.from_ == CONTACT for m in inbound_page.next_page())  # type: ignore[union-attr]


def test_usage_period_subresources_and_unknown_country(client: Client) -> None:
    today = client.usage.records.today.list()
    assert {r.category for r in today} >= {"sms", "calls", "totalprice"}
    assert client.usage.records.all_time.list(category="sms")[0].count == "105"
    assert client.usage.records.daily.list(category="sms")
    with pytest.raises(TwilioException) as unknown:
        client.available_phone_numbers("XX").local.list()
    assert "404" in str(unknown.value)


def test_lookup_v1_shortcut(client: Client) -> None:
    with pytest.warns(DeprecationWarning):
        result = client.lookups.phone_numbers(CONTACT).fetch(type=["carrier", "caller-name"])
    assert result.country_code == "CA" and result.national_format == "(416) 555-0141"
    assert result.carrier["type"] == "mobile"  # type: ignore[index]
    assert result.caller_name["caller_name"] == "CHRISTOPHER INGRAM"  # type: ignore[index]
    with pytest.raises(TwilioRestException) as invalid, pytest.warns(DeprecationWarning):
        client.lookups.phone_numbers("+1555").fetch()
    assert invalid.value.status == 404


def test_verify_resend_and_check_limits(client: Client, emulators: EmulatorHub) -> None:
    seeded = next(s for s in client.verify.v2.services.list() if s.friendly_name == "Halcyon Client Portal")
    verifications = client.verify.v2.services(seeded.sid).verifications
    first = verifications.create(to=CONTACT, channel="sms")
    for _ in range(4):
        assert verifications.create(to=CONTACT, channel="sms").sid == first.sid
    with pytest.raises(TwilioRestException) as limit:
        verifications.create(to=CONTACT, channel="sms")
    assert limit.value.status == 429 and limit.value.code == 60203
    with pytest.raises(TwilioRestException) as invalid:
        verifications.create(to="not-a-number", channel="sms")
    assert invalid.value.code == 60200

    checks = client.verify.v2.services(seeded.sid).verification_checks
    for _ in range(5):
        assert checks.create(to=CONTACT, code="000000").status == "pending"
    with pytest.raises(TwilioRestException) as blocked:
        checks.create(to=CONTACT, code="000000")
    assert blocked.value.status == 429 and blocked.value.code == 60202
    assert (
        client.verify.v2.services(seeded.sid).verifications(first.sid).fetch().status
        == "max_attempts_reached"
    )


def test_verify_resend_accepts_the_original_code(client: Client, emulators: EmulatorHub) -> None:
    service = next(s for s in client.verify.v2.services.list() if s.friendly_name == "Halcyon Client Portal")
    verification_api = client.verify.v2.services(service.sid)
    first = verification_api.verifications.create(to=CONTACT, channel="sms")
    record = emulators.context("twilio").store.collection("verifications").get(first.sid)
    original_code = record["code"]
    again = verification_api.verifications.create(to=CONTACT, channel="sms")
    assert again.sid == first.sid
    checked = verification_api.verification_checks.create(to=CONTACT, code=original_code)
    assert checked.status == "approved" and checked.valid is True


def test_recordings_through_the_call(client: Client) -> None:
    recording = client.recordings.list(limit=1)[0]
    via_call = client.calls(recording.call_sid).recordings(recording.sid).fetch()
    assert via_call.sid == recording.sid and via_call.call_sid == recording.call_sid
    assert client.calls(recording.call_sid).recordings(recording.sid).delete() is True


def test_scheduling_and_content_templates(client: Client, emulators: EmulatorHub) -> None:
    seeded = next(
        s for s in client.messaging.v1.services.list() if s.friendly_name == "Halcyon Client Notifications"
    )
    send_at = emulators.clock.now() + timedelta(hours=2)
    scheduled = client.messages.create(
        to=CONTACT,
        messaging_service_sid=seeded.sid,
        body="See you tomorrow",
        schedule_type="fixed",
        send_at=send_at,
    )
    assert scheduled.status == "scheduled"
    assert client.messages(scheduled.sid).update(status="canceled").status == "canceled"
    too_soon = emulators.clock.now() + timedelta(minutes=5)
    with pytest.raises(TwilioRestException) as soon:
        client.messages.create(
            to=CONTACT, messaging_service_sid=seeded.sid, body="x", schedule_type="fixed", send_at=too_soon
        )
    assert soon.value.status == 400 and soon.value.code == 35114
    templated = client.messages.create(to=CONTACT, from_=FROM, content_sid="HX" + "b" * 32)
    assert templated.sid.startswith("SM") and templated.body == ""


def test_subaccount_client_is_scoped(client: Client, emulators: EmulatorHub) -> None:
    sub = client.api.accounts.create(friendly_name="SDK subaccount")
    sub_client = Client(sub.sid, sub.auth_token)
    assert sub_client.messages.list(limit=5) == []
    with pytest.raises(TwilioRestException) as denied:
        sub_client.api.accounts(_creds(emulators)["account_sid"]).fetch()
    assert denied.value.status == 403
    assert client.api.accounts(sub.sid).update(status="closed").status == "closed"
    with pytest.raises(TwilioRestException) as inactive:
        sub_client.messages.create(to=CONTACT, from_="+15005550006", body="x")
    assert inactive.value.status == 403 and inactive.value.code == 20005


def test_get_call_status_callbacks_are_signed_over_the_full_url(
    client: Client, emulators: EmulatorHub
) -> None:
    received: list[dict[str, Any]] = []
    receiver = FastAPI()

    @receiver.get("/status")
    async def status(request: Request) -> Response:
        received.append({"url": str(request.url), "sig": request.headers.get("x-twilio-signature")})
        return Response("")

    emulators.webhook_transport = httpx.ASGITransport(app=receiver)
    try:
        client.calls.create(
            to=CONTACT,
            from_=FROM,
            twiml="<Response><Say>hi</Say></Response>",
            status_callback="http://consumer.test/status?tenant=halcyon",
            status_callback_method="GET",
        )
    finally:
        emulators.webhook_transport = None
    assert len(received) == 1  # the default event is "completed"
    validator = RequestValidator(_creds(emulators)["auth_token"])
    entry = received[0]
    assert "tenant=halcyon" in entry["url"] and "CallStatus=completed" in entry["url"]
    assert validator.validate(entry["url"], {}, entry["sig"])
