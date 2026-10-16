"""Messaging Services (v1), Verify (v2) and Lookup (v2)."""

from __future__ import annotations

import re

import httpx

from tests.providers.twilio.conftest import (
    API,
    CONTACT,
    CONTACT_NAME,
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

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


async def test_messaging_service_seed_and_meta_paging(twilio: httpx.AsyncClient) -> None:
    r = await twilio.get(f"{MESSAGING}/v1/Services")
    assert r.status_code == 200, r.text
    body = r.json()
    meta = body["meta"]
    assert meta["key"] == "services"
    assert meta["page"] == 0
    assert meta["first_page_url"].startswith("https://messaging.twilio.com/v1/Services")
    assert meta["next_page_url"] is None
    service = body["services"][0]
    assert service["sid"].startswith("MG")
    assert service["friendly_name"] == "Halcyon Client Notifications"
    assert service["account_sid"] == twilio.account_sid  # type: ignore[attr-defined]
    assert ISO_RE.match(service["date_created"])
    assert service["url"].endswith(f"/v1/Services/{service['sid']}")
    assert service["links"]["phone_numbers"].endswith("/PhoneNumbers")
    assert service["inbound_request_url"] is None
    assert service["use_inbound_webhook_on_number"] is False
    assert service["validity_period"] == 14400

    numbers = (await twilio.get(f"{MESSAGING}/v1/Services/{service['sid']}/PhoneNumbers")).json()
    assert numbers["meta"]["key"] == "phone_numbers"
    assert {n["phone_number"] for n in numbers["phone_numbers"]} == {FROM_NUMBER, NY_NUMBER}
    entry = numbers["phone_numbers"][0]
    assert entry["service_sid"] == service["sid"]
    assert entry["sid"].startswith("PN")
    assert "SMS" in entry["capabilities"]


async def test_messaging_service_crud_and_pool(twilio: httpx.AsyncClient) -> None:
    created = await twilio.post(
        f"{MESSAGING}/v1/Services",
        data={
            "FriendlyName": "Reminders",
            "InboundRequestUrl": "http://consumer.test/mg-sms",
            "StatusCallback": "http://consumer.test/mg-status",
            "StickySender": "true",
            "ValidityPeriod": "7200",
        },
    )
    assert created.status_code == 201, created.text
    service = created.json()
    assert service["inbound_request_url"] == "http://consumer.test/mg-sms"
    assert service["status_callback"] == "http://consumer.test/mg-status"
    assert service["validity_period"] == 7200

    # An empty pool cannot send.
    r = await twilio.post(
        messages_url(twilio),
        data={"To": CONTACT, "MessagingServiceSid": service["sid"], "Body": "hello"},
    )
    assert r.status_code == 400

    numbers = (await twilio.get(f"{account_base(twilio)}/IncomingPhoneNumbers.json")).json()[
        "incoming_phone_numbers"
    ]
    pn_sid = next(n["sid"] for n in numbers if n["phone_number"] == NY_NUMBER)
    # A number belongs to one sender pool at a time: release it from the seeded service first.
    seeded = await seeded_messaging_service(twilio)
    assert (
        await twilio.delete(f"{MESSAGING}/v1/Services/{seeded['sid']}/PhoneNumbers/{pn_sid}")
    ).status_code == 204
    added = await twilio.post(
        f"{MESSAGING}/v1/Services/{service['sid']}/PhoneNumbers", data={"PhoneNumberSid": pn_sid}
    )
    assert added.status_code == 201, added.text
    assert added.json()["phone_number"] == NY_NUMBER
    duplicate = await twilio.post(
        f"{MESSAGING}/v1/Services/{service['sid']}/PhoneNumbers", data={"PhoneNumberSid": pn_sid}
    )
    assert duplicate.status_code == 409 and duplicate.json()["code"] == 21710
    missing = await twilio.post(
        f"{MESSAGING}/v1/Services/{service['sid']}/PhoneNumbers", data={"PhoneNumberSid": "PN" + "0" * 32}
    )
    assert missing.status_code == 404

    sent = await twilio.post(
        messages_url(twilio),
        data={"To": CONTACT, "MessagingServiceSid": service["sid"], "Body": "from the new pool"},
    )
    assert sent.status_code == 201, sent.text
    assert sent.json()["from"] == NY_NUMBER

    updated = await twilio.post(
        f"{MESSAGING}/v1/Services/{service['sid']}", data={"FriendlyName": "Reminders v2"}
    )
    assert updated.json()["friendly_name"] == "Reminders v2"
    removed = await twilio.delete(f"{MESSAGING}/v1/Services/{service['sid']}/PhoneNumbers/{pn_sid}")
    assert removed.status_code == 204
    assert (await twilio.delete(f"{MESSAGING}/v1/Services/{service['sid']}")).status_code == 204
    assert (await twilio.get(f"{MESSAGING}/v1/Services/{service['sid']}")).status_code == 404

    no_name = await twilio.post(f"{MESSAGING}/v1/Services", data={})
    assert no_name.status_code == 400 and no_name.json()["code"] == 20001


async def test_verify_service_seed_and_crud(twilio: httpx.AsyncClient) -> None:
    service = await seeded_verify_service(twilio)
    assert service["sid"].startswith("VA")
    assert service["code_length"] == 6
    assert service["lookup_enabled"] is True
    assert service["custom_code_enabled"] is True
    assert service["url"].endswith(f"/v2/Services/{service['sid']}")
    assert service["links"]["verifications"].endswith("/Verifications")

    created = await twilio.post(
        f"{VERIFY}/v2/Services",
        data={"FriendlyName": "Partner Portal", "CodeLength": "4", "LookupEnabled": "false"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["code_length"] == 4
    sid = created.json()["sid"]
    updated = await twilio.post(f"{VERIFY}/v2/Services/{sid}", data={"FriendlyName": "Partner Portal v2"})
    assert updated.json()["friendly_name"] == "Partner Portal v2"
    assert (await twilio.delete(f"{VERIFY}/v2/Services/{sid}")).status_code == 204
    assert (await twilio.get(f"{VERIFY}/v2/Services/{sid}")).status_code == 404


async def _start_verification(twilio: httpx.AsyncClient, service_sid: str, **data: object) -> httpx.Response:
    payload: dict[str, object] = {"To": CONTACT, "Channel": "sms"}
    payload.update(data)
    return await twilio.post(f"{VERIFY}/v2/Services/{service_sid}/Verifications", data=payload)


async def test_verify_flow_with_state_code(twilio: httpx.AsyncClient) -> None:
    service = await seeded_verify_service(twilio)
    r = await _start_verification(twilio, service["sid"])
    assert r.status_code == 201, r.text
    verification = r.json()
    assert verification["sid"].startswith("VE")
    assert verification["service_sid"] == service["sid"]
    assert verification["to"] == CONTACT
    assert verification["channel"] == "sms"
    assert verification["status"] == "pending"
    assert verification["valid"] is False
    assert verification["send_code_attempts"][0]["channel"] == "sms"
    assert verification["send_code_attempts"][0]["attempt_sid"].startswith("VL")
    assert ISO_RE.match(verification["date_created"])
    assert "code" not in verification  # never on the wire
    assert verification["lookup"]["carrier"]["type"] == "mobile"

    # The code is visible through the control plane and was also sent as an SMS.
    state = (await twilio.get(f"{API}/_emulator/state/verifications")).json()
    record = next(rec for rec in state["records"] if rec["sid"] == verification["sid"])
    code = record["code"]
    assert re.fullmatch(r"\d{6}", code)
    sms = (await twilio.get(messages_url(twilio), params={"To": CONTACT, "PageSize": 100})).json()["messages"]
    assert any(code in (m["body"] or "") and "verification code" in (m["body"] or "") for m in sms)

    # Wrong code stays pending; the real code approves.
    wrong = await twilio.post(
        f"{VERIFY}/v2/Services/{service['sid']}/VerificationCheck", data={"To": CONTACT, "Code": "000000"}
    )
    assert wrong.status_code == 200, wrong.text
    assert wrong.json()["status"] == "pending" and wrong.json()["valid"] is False
    ok = await twilio.post(
        f"{VERIFY}/v2/Services/{service['sid']}/VerificationCheck", data={"To": CONTACT, "Code": code}
    )
    assert ok.json()["status"] == "approved" and ok.json()["valid"] is True
    fetched = (
        await twilio.get(f"{VERIFY}/v2/Services/{service['sid']}/Verifications/{verification['sid']}")
    ).json()
    assert fetched["status"] == "approved"


async def test_verify_universal_and_custom_codes(twilio: httpx.AsyncClient) -> None:
    service = await seeded_verify_service(twilio)
    await _start_verification(twilio, service["sid"])
    ok = await twilio.post(
        f"{VERIFY}/v2/Services/{service['sid']}/VerificationCheck", data={"To": CONTACT, "Code": "123456"}
    )
    assert ok.json()["status"] == "approved"

    custom = await _start_verification(twilio, service["sid"], CustomCode="424242")
    assert custom.status_code == 201, custom.text
    ok = await twilio.post(
        f"{VERIFY}/v2/Services/{service['sid']}/VerificationCheck",
        data={"VerificationSid": custom.json()["sid"], "Code": "424242"},
    )
    assert ok.json()["status"] == "approved"


async def test_verify_cancel_and_max_attempts(twilio: httpx.AsyncClient) -> None:
    service = await seeded_verify_service(twilio)
    created = await _start_verification(twilio, service["sid"])
    vsid = created.json()["sid"]
    canceled = await twilio.post(
        f"{VERIFY}/v2/Services/{service['sid']}/Verifications/{vsid}", data={"Status": "canceled"}
    )
    assert canceled.json()["status"] == "canceled"

    await _start_verification(twilio, service["sid"])
    for _ in range(5):  # five wrong codes are allowed and stay pending ...
        wrong = await twilio.post(
            f"{VERIFY}/v2/Services/{service['sid']}/VerificationCheck", data={"To": CONTACT, "Code": "000000"}
        )
        assert wrong.status_code == 200 and wrong.json()["status"] == "pending"
    # ... the sixth attempt is refused with Twilio's 429 "Max check attempts reached" (60202).
    last = await twilio.post(
        f"{VERIFY}/v2/Services/{service['sid']}/VerificationCheck", data={"To": CONTACT, "Code": "000000"}
    )
    assert last.status_code == 429 and last.json()["code"] == 60202
    # And afterwards the verification is gone for checking purposes.
    gone = await twilio.post(
        f"{VERIFY}/v2/Services/{service['sid']}/VerificationCheck", data={"To": CONTACT, "Code": "123456"}
    )
    assert gone.status_code == 404

    unknown = await _start_verification(twilio, "VA" + "0" * 32)
    assert unknown.status_code == 404


async def test_lookup_world_numbers(twilio: httpx.AsyncClient) -> None:
    r = await twilio.get(
        f"{LOOKUPS}/v2/PhoneNumbers/{CONTACT}",
        params={"Fields": "line_type_intelligence,caller_name,validation"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["phone_number"] == CONTACT
    assert body["calling_country_code"] == "1"
    assert body["country_code"] == "CA"
    assert body["national_format"] == "(416) 555-0141"
    assert body["valid"] is True and body["validation_errors"] is None
    assert body["caller_name"] == {
        "caller_name": CONTACT_NAME.upper(),
        "caller_type": "CONSUMER",
        "error_code": None,
    }
    line = body["line_type_intelligence"]
    assert line["type"] == "mobile"
    assert line["carrier_name"]
    assert line["mobile_country_code"] == "302"
    assert body["url"].endswith(f"/v2/PhoneNumbers/{CONTACT}")

    # Without Fields the add-on packages stay null, like the real API.
    bare = (await twilio.get(f"{LOOKUPS}/v2/PhoneNumbers/{CONTACT}")).json()
    assert bare["caller_name"] is None and bare["line_type_intelligence"] is None

    # An employee mobile resolves to their name too.
    emp = (
        await twilio.get(f"{LOOKUPS}/v2/PhoneNumbers/+16475550151", params={"Fields": "caller_name"})
    ).json()
    assert emp["caller_name"]["caller_name"] == "ELEANOR WHITFIELD"

    # The firm's own Twilio number is non-fixed VoIP.
    own = (
        await twilio.get(
            f"{LOOKUPS}/v2/PhoneNumbers/{FROM_NUMBER}", params={"Fields": "line_type_intelligence"}
        )
    ).json()
    assert own["line_type_intelligence"]["type"] == "nonFixedVoip"
    assert own["line_type_intelligence"]["carrier_name"] == "Twilio Inc."


async def test_lookup_invalid_number(twilio: httpx.AsyncClient) -> None:
    body = (await twilio.get(f"{LOOKUPS}/v2/PhoneNumbers/+15005550001")).json()
    assert body["valid"] is False
    assert body["validation_errors"]
    assert (await twilio.get(f"{LOOKUPS}/v2/PhoneNumbers/+442079460100")).json()["country_code"] == "GB"
