"""Audit regressions for Messaging Services, Verify, Lookup, Voice and Conversations."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from urllib.parse import parse_qsl

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import Response

from tests.providers.twilio.conftest import (
    CONTACT,
    CONVERSATIONS,
    FROM_NUMBER,
    LOOKUPS,
    MESSAGING,
    NY_NUMBER,
    VERIFY,
    account_base,
    messages_url,
    seeded_messaging_service,
    seeded_verify_service,
)

# -- Messaging Services ---------------------------------------------------------------------------------


async def test_messaging_service_defaults_and_wire_shape(twilio: httpx.AsyncClient) -> None:
    created = (await twilio.post(f"{MESSAGING}/v1/Services", data={"FriendlyName": "Defaults"})).json()
    assert created["validity_period"] == 14400
    assert created["us_app_to_person_registered"] is False
    assert "us_app_to_person" in created["links"]
    seeded = await seeded_messaging_service(twilio)
    pool = (await twilio.get(f"{MESSAGING}/v1/Services/{seeded['sid']}/PhoneNumbers")).json()["phone_numbers"]
    assert pool and all(p["capabilities"] == ["MMS", "SMS", "Voice"] for p in pool)


async def test_messaging_service_parameters_are_validated(twilio: httpx.AsyncClient) -> None:
    missing = await twilio.post(f"{MESSAGING}/v1/Services", data={})
    assert missing.status_code == 400 and missing.json()["code"] == 20001
    assert "FriendlyName" in missing.json()["message"]
    for field, value in (
        ("InboundRequestUrl", "ftp://nope"),
        ("InboundMethod", "PUT"),
        ("ValidityPeriod", "99999"),
        ("ValidityPeriod", "0"),
        ("ScanMessageContent", "sometimes"),
    ):
        r = await twilio.post(f"{MESSAGING}/v1/Services", data={"FriendlyName": "x", field: value})
        assert r.status_code == 400, (field, value, r.text)


async def test_a_number_belongs_to_one_sender_pool(twilio: httpx.AsyncClient) -> None:
    seeded = await seeded_messaging_service(twilio)
    other = (await twilio.post(f"{MESSAGING}/v1/Services", data={"FriendlyName": "Second"})).json()
    numbers = (await twilio.get(f"{account_base(twilio)}/IncomingPhoneNumbers.json")).json()[
        "incoming_phone_numbers"
    ]
    ny = next(n["sid"] for n in numbers if n["phone_number"] == NY_NUMBER)
    same = await twilio.post(
        f"{MESSAGING}/v1/Services/{seeded['sid']}/PhoneNumbers", data={"PhoneNumberSid": ny}
    )
    assert same.status_code == 409 and same.json()["code"] == 21710
    elsewhere = await twilio.post(
        f"{MESSAGING}/v1/Services/{other['sid']}/PhoneNumbers", data={"PhoneNumberSid": ny}
    )
    assert elsewhere.status_code == 409 and elsewhere.json()["code"] == 21712
    no_param = await twilio.post(f"{MESSAGING}/v1/Services/{other['sid']}/PhoneNumbers", data={})
    assert no_param.status_code == 400 and no_param.json()["code"] == 20001


# -- Verify ------------------------------------------------------------------------------------------------


async def start(twilio: httpx.AsyncClient, service_sid: str, **data: str) -> httpx.Response:
    payload = {"To": CONTACT, "Channel": "sms", **data}
    return await twilio.post(
        f"{VERIFY}/v2/Services/{service_sid}/Verifications", data={k: v for k, v in payload.items() if v}
    )


async def check(twilio: httpx.AsyncClient, service_sid: str, code: str, **data: str) -> httpx.Response:
    payload = {"To": CONTACT, "Code": code, **data}
    return await twilio.post(f"{VERIFY}/v2/Services/{service_sid}/VerificationCheck", data=payload)


async def test_verify_requires_and_validates_parameters(twilio: httpx.AsyncClient) -> None:
    sid = (await seeded_verify_service(twilio))["sid"]
    no_channel = await start(twilio, sid, Channel="")
    assert no_channel.status_code == 400 and no_channel.json()["code"] == 20001
    no_to = await start(twilio, sid, To="")
    assert no_to.status_code == 400 and no_to.json()["code"] == 20001
    bad_to = await start(twilio, sid, To="garbage")
    assert bad_to.status_code == 400 and bad_to.json()["code"] == 60200
    assert "To" in bad_to.json()["message"]
    bad_channel = await start(twilio, sid, Channel="pigeon")
    assert bad_channel.status_code == 400 and bad_channel.json()["code"] == 60200
    bad_email = await start(twilio, sid, To="nope", Channel="email")
    assert bad_email.status_code == 400 and bad_email.json()["code"] == 60200
    bad_custom = await start(twilio, sid, CustomCode="12ab")
    assert bad_custom.status_code == 400 and bad_custom.json()["code"] == 60200
    bad_code = await check(twilio, sid, "abc")
    assert bad_code.status_code == 400 and bad_code.json()["code"] == 60200
    bad_length = await twilio.post(f"{VERIFY}/v2/Services", data={"FriendlyName": "x", "CodeLength": "3"})
    assert bad_length.status_code == 400 and bad_length.json()["code"] == 60200
    # Nothing was created or sent for any of those.
    messages = (await twilio.get(messages_url(twilio), params={"To": CONTACT, "PageSize": 100})).json()[
        "messages"
    ]
    assert not any("verification code" in (m["body"] or "") for m in messages)


async def test_verification_lookup_is_a_carrier_object(twilio: httpx.AsyncClient) -> None:
    sid = (await seeded_verify_service(twilio))["sid"]
    body = (await start(twilio, sid)).json()
    assert body["lookup"] == {
        "carrier": {
            "error_code": None,
            "name": "Rogers Communications Canada Inc.",
            "mobile_country_code": "302",
            "mobile_network_code": "720",
            "type": "mobile",
        }
    }
    email = (await start(twilio, sid, To="chris@northwindlogistics.example", Channel="email")).json()
    assert email["lookup"] == {} and email["to"] == "chris@northwindlogistics.example"


async def test_starting_again_resends_on_the_same_verification_up_to_five_times(
    twilio: httpx.AsyncClient,
) -> None:
    sid = (await seeded_verify_service(twilio))["sid"]
    ctx = twilio.hub.context("twilio")  # type: ignore[attr-defined]
    first = (await start(twilio, sid)).json()
    codes = []
    for expected_attempts in (2, 3, 4, 5):
        again = await start(twilio, sid)
        assert again.status_code == 201 and again.json()["sid"] == first["sid"]
        assert len(again.json()["send_code_attempts"]) == expected_attempts
        codes.append(ctx.store.collection("verifications").get(first["sid"])["code"])
    limit = await start(twilio, sid)
    assert limit.status_code == 429 and limit.json()["code"] == 60203
    # A different recipient is unaffected, and the latest code is the one that works.
    other = await start(twilio, sid, To="+14165550142")
    assert other.status_code == 201 and other.json()["sid"] != first["sid"]
    approved = await check(twilio, sid, codes[-1])
    assert approved.json()["status"] == "approved" and approved.json()["valid"] is True


async def test_check_limits_and_terminal_states(twilio: httpx.AsyncClient) -> None:
    sid = (await seeded_verify_service(twilio))["sid"]
    created = (await start(twilio, sid)).json()
    for _ in range(5):
        wrong = await check(twilio, sid, "000000")
        assert wrong.status_code == 200 and wrong.json()["status"] == "pending"
    blocked = await check(twilio, sid, "000000")
    assert blocked.status_code == 429 and blocked.json()["code"] == 60202
    fetched = (await twilio.get(f"{VERIFY}/v2/Services/{sid}/Verifications/{created['sid']}")).json()
    assert fetched["status"] == "max_attempts_reached"
    # It is final: neither the code nor an update revives it, and a new start makes a new verification.
    assert (await check(twilio, sid, "123456", VerificationSid=created["sid"])).status_code == 404
    fresh = (await start(twilio, sid)).json()
    assert fresh["sid"] != created["sid"]

    approved = await check(twilio, sid, "123456")
    assert approved.json()["status"] == "approved"
    # An approved verification can neither be checked again nor changed.
    assert (await check(twilio, sid, "123456")).status_code == 404
    update = await twilio.post(
        f"{VERIFY}/v2/Services/{sid}/Verifications/{fresh['sid']}", data={"Status": "canceled"}
    )
    assert update.status_code == 404
    missing = await twilio.post(f"{VERIFY}/v2/Services/{sid}/Verifications/{fresh['sid']}", data={})
    assert missing.status_code == 400


async def test_verifications_expire_after_ten_minutes(twilio: httpx.AsyncClient) -> None:
    hub = twilio.hub  # type: ignore[attr-defined]
    sid = (await seeded_verify_service(twilio))["sid"]
    created = (await start(twilio, sid)).json()
    hub.clock.freeze()
    hub.clock.advance(timedelta(minutes=11))
    fetched = (await twilio.get(f"{VERIFY}/v2/Services/{sid}/Verifications/{created['sid']}")).json()
    assert fetched["status"] == "expired"
    gone = await check(twilio, sid, "123456")
    assert gone.status_code == 404 and gone.json()["code"] == 20404


async def test_verify_service_defaults(twilio: httpx.AsyncClient) -> None:
    created = (await twilio.post(f"{VERIFY}/v2/Services", data={"FriendlyName": "Plain"})).json()
    assert created["lookup_enabled"] is False  # real default; the seeded service turns it on
    assert (await seeded_verify_service(twilio))["lookup_enabled"] is True


# -- Lookup -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("number", "valid", "errors"),
    [
        ("+14165550141", True, None),
        ("+1555", False, ["TOO_SHORT"]),
        ("+1416555014199", False, ["TOO_LONG"]),
        ("+11234567890", False, ["INVALID_BUT_POSSIBLE"]),
        ("+9990000", False, ["INVALID_COUNTRY_CODE"]),
        ("abc", False, ["NOT_A_NUMBER"]),
        ("+15005550001", False, ["INVALID_BUT_POSSIBLE"]),
    ],
)
async def test_lookup_v2_validation_vocabulary(
    twilio: httpx.AsyncClient, number: str, valid: bool, errors: list[str] | None
) -> None:
    r = await twilio.get(f"{LOOKUPS}/v2/PhoneNumbers/{number}")
    assert r.status_code == 200
    assert r.json()["valid"] is valid and r.json()["validation_errors"] == errors
    if not valid:
        assert r.json()["national_format"] is None


async def test_lookup_national_format_and_country_code(twilio: httpx.AsyncClient) -> None:
    us_default = (await twilio.get(f"{LOOKUPS}/v2/PhoneNumbers/4165550141")).json()
    assert us_default["phone_number"] == "+14165550141" and us_default["valid"] is True
    ca = (await twilio.get(f"{LOOKUPS}/v2/PhoneNumbers/(416) 555-0141", params={"CountryCode": "CA"})).json()
    assert ca["phone_number"] == "+14165550141" and ca["country_code"] == "CA"
    uk = (await twilio.get(f"{LOOKUPS}/v2/PhoneNumbers/020 7946 0100", params={"CountryCode": "GB"})).json()
    assert uk["phone_number"] == "+442079460100" and uk["national_format"] == "020 7946 0100"
    unknown = (await twilio.get(f"{LOOKUPS}/v2/PhoneNumbers/4165550141", params={"CountryCode": "ZZ"})).json()
    assert unknown["valid"] is False and unknown["validation_errors"] == ["INVALID_COUNTRY_CODE"]


async def test_lookup_v1_for_the_sdks_deprecated_shortcut(twilio: httpx.AsyncClient) -> None:
    r = await twilio.get(
        f"{LOOKUPS}/v1/PhoneNumbers/{CONTACT}", params=[("Type", "carrier"), ("Type", "caller-name")]
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["phone_number"] == CONTACT and body["country_code"] == "CA"
    assert body["carrier"]["type"] == "mobile" and body["caller_name"]["caller_name"] == "CHRISTOPHER INGRAM"
    assert body["add_ons"] is None and body["url"].endswith(f"/v1/PhoneNumbers/{CONTACT}")
    bare = (await twilio.get(f"{LOOKUPS}/v1/PhoneNumbers/{CONTACT}")).json()
    assert bare["carrier"] is None and bare["caller_name"] is None
    invalid = await twilio.get(f"{LOOKUPS}/v1/PhoneNumbers/+1555")
    assert invalid.status_code == 404 and invalid.json()["code"] == 20404


# -- Voice -----------------------------------------------------------------------------------------------------


class CallbackReceiver:
    def __init__(self) -> None:
        self.statuses: list[dict[str, str]] = []
        app = FastAPI()

        @app.post("/call-status")
        async def status(request: Request) -> Response:
            self.statuses.append(dict(parse_qsl((await request.body()).decode(), keep_blank_values=True)))
            return Response("")

        @app.post("/voice")
        async def voice(request: Request) -> Response:
            return Response("<Response><Say>hi</Say></Response>", media_type="text/xml")

        self.app = app


async def test_call_callbacks_report_initiated_and_the_caller_parameters(twilio: httpx.AsyncClient) -> None:
    receiver = CallbackReceiver()
    twilio.hub.webhook_transport = httpx.ASGITransport(app=receiver.app)  # type: ignore[attr-defined]
    r = await twilio.post(
        f"{account_base(twilio)}/Calls.json",
        data={
            "To": CONTACT,
            "From": FROM_NUMBER,
            "Url": "http://consumer.test/voice",
            "StatusCallback": "http://consumer.test/call-status",
            "StatusCallbackEvent": "initiated ringing answered completed",  # space separated, as Twilio allows
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["annotation"] is None
    assert [s["CallStatus"] for s in receiver.statuses] == [
        "initiated",
        "ringing",
        "in-progress",
        "completed",
    ]
    first = receiver.statuses[0]
    assert first["Called"] == CONTACT and first["Caller"] == FROM_NUMBER
    assert first["CalledCountry"] == "CA" and first["CallerCity"] == "Toronto"
    assert [s["SequenceNumber"] for s in receiver.statuses] == ["1", "2", "3", "4"]


async def test_call_validation_codes(twilio: httpx.AsyncClient) -> None:
    url = f"{account_base(twilio)}/Calls.json"
    no_to = await twilio.post(url, data={"From": FROM_NUMBER, "Url": "http://x.test/v"})
    assert no_to.status_code == 400 and no_to.json()["code"] == 21201
    no_from = await twilio.post(url, data={"To": CONTACT, "Url": "http://x.test/v"})
    assert no_from.status_code == 400 and no_from.json()["code"] == 21213
    bad_url = await twilio.post(url, data={"To": CONTACT, "From": FROM_NUMBER, "Url": "gopher://x"})
    assert bad_url.status_code == 400 and bad_url.json()["code"] == 21205
    bad_callback = await twilio.post(
        url, data={"To": CONTACT, "From": FROM_NUMBER, "Url": "http://x.test/v", "StatusCallback": "nope"}
    )
    assert bad_callback.status_code == 400 and bad_callback.json()["code"] == 21609


async def test_call_recording_by_call_and_real_mp3(twilio: httpx.AsyncClient) -> None:
    base = account_base(twilio)
    (recording,) = (await twilio.get(f"{base}/Recordings.json")).json()["recordings"]
    sid, call_sid = recording["sid"], recording["call_sid"]
    one = await twilio.get(f"{base}/Calls/{call_sid}/Recordings/{sid}.json")
    assert one.status_code == 200 and one.json()["call_sid"] == call_sid
    other = next(c for c in (await twilio.get(f"{base}/Calls.json")).json()["calls"] if c["sid"] != call_sid)
    assert (await twilio.get(f"{base}/Calls/{other['sid']}/Recordings/{sid}.json")).status_code == 404

    mp3 = await twilio.get(f"{base}/Recordings/{sid}.mp3")
    assert mp3.headers["content-type"].startswith("audio/mpeg")
    assert mp3.content[:2] == bytes([0xFF, 0xFB])  # an MPEG audio frame sync, not WAV bytes
    assert not mp3.content.startswith(b"RIFF") and len(mp3.content) % 417 == 0
    assert (await twilio.delete(f"{base}/Calls/{call_sid}/Recordings/{sid}.json")).status_code == 204
    assert (await twilio.get(f"{base}/Recordings/{sid}.json")).status_code == 404


# -- Conversations --------------------------------------------------------------------------------------


async def test_conversation_conflicts_and_validation(twilio: httpx.AsyncClient) -> None:
    created = (
        await twilio.post(
            f"{CONVERSATIONS}/v1/Conversations", data={"UniqueName": "case-1", "FriendlyName": "Case"}
        )
    ).json()
    dupe = await twilio.post(f"{CONVERSATIONS}/v1/Conversations", data={"UniqueName": "case-1"})
    assert dupe.status_code == 409 and dupe.json()["code"] == 50353
    other = (await twilio.post(f"{CONVERSATIONS}/v1/Conversations", data={"UniqueName": "case-2"})).json()
    clash = await twilio.post(
        f"{CONVERSATIONS}/v1/Conversations/{other['sid']}", data={"UniqueName": "case-1"}
    )
    assert clash.status_code == 409
    same = await twilio.post(
        f"{CONVERSATIONS}/v1/Conversations/{created['sid']}", data={"UniqueName": "case-1"}
    )
    assert same.status_code == 200
    bad_state = await twilio.post(
        f"{CONVERSATIONS}/v1/Conversations/{created['sid']}", data={"State": "frozen"}
    )
    assert bad_state.status_code == 400
    closed = await twilio.post(f"{CONVERSATIONS}/v1/Conversations/{created['sid']}", data={"State": "closed"})
    assert closed.json()["state"] == "closed"
    bad_attrs = await twilio.post(f"{CONVERSATIONS}/v1/Conversations", data={"Attributes": "{not json"})
    assert bad_attrs.status_code == 400
    not_object = await twilio.post(f"{CONVERSATIONS}/v1/Conversations", data={"Attributes": "[1, 2]"})
    assert not_object.status_code == 400
    ok_attrs = await twilio.post(f"{CONVERSATIONS}/v1/Conversations", data={"Attributes": '{"tier": "gold"}'})
    assert ok_attrs.json()["attributes"] == '{"tier": "gold"}'


async def test_participant_conflicts(twilio: httpx.AsyncClient) -> None:
    conv = (await twilio.post(f"{CONVERSATIONS}/v1/Conversations", data={})).json()
    url = f"{CONVERSATIONS}/v1/Conversations/{conv['sid']}/Participants"
    assert (await twilio.post(url, data={"Identity": "agent"})).status_code == 201
    dup_identity = await twilio.post(url, data={"Identity": "agent"})
    assert dup_identity.status_code == 409 and dup_identity.json()["code"] == 50433
    binding = {"MessagingBinding.Address": CONTACT, "MessagingBinding.ProxyAddress": FROM_NUMBER}
    assert (await twilio.post(url, data=binding)).status_code == 201
    dup_binding = await twilio.post(url, data=binding)
    assert dup_binding.status_code == 409 and dup_binding.json()["code"] == 50416


async def test_conversation_messages_keep_indexes_and_summarise_delivery(twilio: httpx.AsyncClient) -> None:
    conv = (await twilio.post(f"{CONVERSATIONS}/v1/Conversations", data={})).json()
    base = f"{CONVERSATIONS}/v1/Conversations/{conv['sid']}"
    await twilio.post(f"{base}/Participants", data={"Identity": "agent"})
    chat_only = (await twilio.post(f"{base}/Messages", data={"Author": "agent", "Body": "internal"})).json()
    assert chat_only["delivery"] is None  # no SMS/WhatsApp recipients, nothing to deliver
    await twilio.post(
        f"{base}/Participants",
        data={"MessagingBinding.Address": CONTACT, "MessagingBinding.ProxyAddress": FROM_NUMBER},
    )
    sms = (await twilio.post(f"{base}/Messages", data={"Author": "agent", "Body": "to the client"})).json()
    assert sms["delivery"]["total"] == 1 and sms["delivery"]["sent"] == "all"
    assert sms["delivery"]["read"] == "none"  # Twilio's delivery summary is all/some/none, not counts
    assert [chat_only["index"], sms["index"]] == [0, 1]
    # Deleting the newest message never frees its index.
    assert (await twilio.delete(f"{base}/Messages/{sms['sid']}")).status_code == 204
    after = (await twilio.post(f"{base}/Messages", data={"Author": "agent", "Body": "next"})).json()
    assert after["index"] == 2
    newest_first = (await twilio.get(f"{base}/Messages", params={"Order": "desc"})).json()["messages"]
    assert [m["index"] for m in newest_first] == [2, 0]


async def test_seeded_conversation_delivery_summaries(twilio: httpx.AsyncClient) -> None:
    ctx: Any = twilio.hub.context("twilio")  # type: ignore[attr-defined]
    outbound = [m for m in ctx.store.collection("conv_messages").values() if m["delivery"] is not None]
    assert outbound and all(m["delivery"]["total"] == 1 and m["delivery"]["sent"] == "all" for m in outbound)
    whatsapp_read = [m for m in outbound if m["delivery"]["read"] == "all"]
    assert whatsapp_read  # WhatsApp messages the contact read
    inbound = [m for m in ctx.store.collection("conv_messages").values() if m["delivery"] is None]
    assert inbound


async def test_participants_and_messages_carry_the_chat_service_sid(twilio: httpx.AsyncClient) -> None:
    listing = (await twilio.get(f"{CONVERSATIONS}/v1/Conversations", params={"PageSize": 1})).json()
    conv = listing["conversations"][0]
    base = f"{CONVERSATIONS}/v1/Conversations/{conv['sid']}"
    for resource, key in (("Participants", "participants"), ("Messages", "messages")):
        rows = (await twilio.get(f"{base}/{resource}")).json()[key]
        assert rows and all(r["chat_service_sid"] == conv["chat_service_sid"] for r in rows)
    created = (await twilio.post(f"{base}/Participants", data={"Identity": "extra"})).json()
    posted = (await twilio.post(f"{base}/Messages", data={"Author": "extra", "Body": "hello"})).json()
    assert created["chat_service_sid"] == posted["chat_service_sid"] == conv["chat_service_sid"]


async def test_advertised_account_subresources_exist(twilio: httpx.AsyncClient) -> None:
    account = (await twilio.get(f"{account_base(twilio)}.json")).json()
    missing = {}
    for name, uri in account["subresource_uris"].items():
        status = (await twilio.get(f"https://api.twilio.com{uri}")).status_code
        if status != 200:
            missing[name] = status
    # Only containers Twilio itself does not list, and the POST-only token endpoint, remain.
    assert set(missing) == {"sip", "usage", "tokens"}, missing
    call = (await twilio.get(f"{account_base(twilio)}/Calls.json")).json()["calls"][0]
    for name in ("notifications", "recordings", "transcriptions", "payments", "events"):
        assert (
            await twilio.get(f"https://api.twilio.com{call['subresource_uris'][name]}")
        ).status_code == 200
    signing = await twilio.get(f"{account_base(twilio)}/SigningKeys.json")
    assert signing.status_code == 200 and signing.json()["signing_keys"]


async def test_advertised_links_of_v1_v2_resources_resolve(twilio: httpx.AsyncClient) -> None:
    unresolved = []
    for host, path in (
        (MESSAGING, "/v1/Services"),
        (VERIFY, "/v2/Services"),
        (CONVERSATIONS, "/v1/Conversations"),
    ):
        body = (await twilio.get(f"{host}{path}")).json()
        resource = body[next(k for k in body if k != "meta")][0]
        for name, url in resource["links"].items():
            status = (await twilio.get(url)).status_code
            if status != 200:
                unresolved.append((path, name, status))
    # POST-only children (create-only endpoints) are the only links a GET cannot list.
    assert {name for _, name, _ in unresolved} == {"verifications", "verification_checks", "access_tokens"}
    empty = (
        await twilio.get(
            f"{MESSAGING}/v1/Services/{(await seeded_messaging_service(twilio))['sid']}/ShortCodes"
        )
    ).json()
    assert empty["short_codes"] == [] and empty["meta"]["key"] == "short_codes"


async def test_call_start_time_filters(twilio: httpx.AsyncClient) -> None:
    url = f"{account_base(twilio)}/Calls.json"
    seeded = (await twilio.get(url)).json()["calls"]
    assert [c["start_time"][5:16] for c in seeded] == ["12 Oct 2026", "11 Oct 2026", "10 Oct 2026"]
    # Equality is a UTC day filter, even when the SDK sends a full timestamp.
    one_day = (await twilio.get(url, params={"StartTime": "2026-10-11T03:00:00Z"})).json()["calls"]
    assert [c["sid"] for c in one_day] == [seeded[1]["sid"]]
    after = (await twilio.get(url, params={"StartTime>": "2026-10-11T00:00:00Z"})).json()["calls"]
    before = (await twilio.get(url, params={"StartTime<": "2026-10-11T00:00:00Z"})).json()["calls"]
    assert [c["sid"] for c in after] == [seeded[0]["sid"], seeded[1]["sid"]]
    assert [c["sid"] for c in before] == [seeded[2]["sid"]]
    assert (await twilio.get(url, params={"StartTime": "later"})).status_code == 400
