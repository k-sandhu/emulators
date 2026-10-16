"""Regression tests from the adversarial fidelity audit: envelopes, paging, auth, numbers, usage."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import httpx

from emulators.core.clock import Clock
from emulators.core.hub import EmulatorHub
from tests.providers.twilio.conftest import (
    API,
    CONTACT,
    CONVERSATIONS,
    FROM_NUMBER,
    NY_NUMBER,
    account_base,
    messages_url,
)

REQUEST_ID_RE = re.compile(r"^RQ[0-9a-f]{32}$")


# -- envelopes and headers ---------------------------------------------------------------


async def test_every_response_carries_twilio_headers(twilio: httpx.AsyncClient) -> None:
    ok = await twilio.get(f"{account_base(twilio)}/Balance.json")
    assert REQUEST_ID_RE.match(ok.headers["twilio-request-id"])
    assert float(ok.headers["twilio-request-duration"]) >= 0
    assert ok.headers["twilio-concurrent-requests"] == "1"
    missing = await twilio.get(f"{account_base(twilio)}/Messages/SM{'0' * 32}.json")
    assert missing.status_code == 404 and REQUEST_ID_RE.match(missing.headers["twilio-request-id"])
    # Injected faults come with a request id too, so clients that log it keep working.
    await twilio.post(f"{API}/_emulator/faults", json={"path": "Balance", "status": 503, "times": 1})
    fault = await twilio.get(f"{account_base(twilio)}/Balance.json")
    assert fault.status_code == 503 and fault.json()["code"] == 20503
    assert REQUEST_ID_RE.match(fault.headers["twilio-request-id"])


async def test_auth_error_has_detail_and_unknown_routes_name_the_path(twilio: httpx.AsyncClient) -> None:
    unauth = await twilio.get(f"{account_base(twilio)}/Messages.json", auth=None)
    assert unauth.status_code == 401
    assert unauth.json()["detail"] == "Your AccountSid or AuthToken was incorrect."
    assert unauth.json()["message"] == "Authenticate"

    nope = await twilio.get(f"{account_base(twilio)}/Nope.json")
    assert nope.status_code == 404
    assert (
        nope.json()["message"]
        == f"The requested resource {account_base(twilio).removeprefix(API)}/Nope.json was not found"
    )

    wrong_method = await twilio.put(messages_url(twilio))
    assert wrong_method.status_code == 405
    assert wrong_method.json()["code"] == 20004


# -- paging ---------------------------------------------------------------------------------


async def test_2010_paging_links_keep_the_filters(twilio: httpx.AsyncClient) -> None:
    url = messages_url(twilio)
    page = (await twilio.get(url, params={"To": CONTACT, "PageSize": 2})).json()
    assert page["next_page_uri"] and "To=%2B14165550141" in page["next_page_uri"]
    assert "PageToken" not in page["first_page_uri"]  # only later pages carry a token
    seen = [m["to"] for m in page["messages"]]
    while page["next_page_uri"]:
        page = (await twilio.get(API + page["next_page_uri"])).json()
        seen += [m["to"] for m in page["messages"]]
    assert seen and set(seen) == {CONTACT}  # following next_page_uri never drops the filter

    first = (await twilio.get(url, params={"PageSize": 1, "Page": -4})).json()
    assert first["page"] == 0 and first["start"] == 0 and first["previous_page_uri"] is None


async def test_meta_paging_urls_keep_the_filters(twilio: httpx.AsyncClient) -> None:
    page = (
        await twilio.get(f"{CONVERSATIONS}/v1/Conversations", params={"State": "active", "PageSize": 2})
    ).json()
    assert "State=active" in page["meta"]["next_page_url"]
    assert page["meta"]["key"] == "conversations"
    assert all(c["state"] == "active" for c in page["conversations"])


# -- incoming phone numbers -------------------------------------------------------------------


async def test_incoming_number_typed_lists_and_purchase(twilio: httpx.AsyncClient) -> None:
    base = f"{account_base(twilio)}/IncomingPhoneNumbers"
    local = (await twilio.get(f"{base}/Local.json")).json()
    assert [n["phone_number"] for n in local["incoming_phone_numbers"]] == [
        FROM_NUMBER,
        NY_NUMBER,
        "+14165550177",
    ]
    assert (await twilio.get(f"{base}/TollFree.json")).json()["incoming_phone_numbers"] == []
    assert (await twilio.get(f"{base}/Mobile.json")).json()["incoming_phone_numbers"] == []

    bought = await twilio.post(f"{base}/TollFree.json", data={"PhoneNumber": "+18885550133"})
    assert bought.status_code == 201, bought.text
    toll_free = (await twilio.get(f"{base}/TollFree.json")).json()["incoming_phone_numbers"]
    assert [n["phone_number"] for n in toll_free] == ["+18885550133"]
    assert len((await twilio.get(f"{base}/Local.json")).json()["incoming_phone_numbers"]) == 3

    # An unencoded "+" arrives as a space; partial numbers and wildcards both filter.
    partial = (await twilio.get(f"{base}.json", params={"PhoneNumber": "+1416"})).json()
    assert {n["phone_number"] for n in partial["incoming_phone_numbers"]} == {FROM_NUMBER, "+14165550177"}
    wildcard = (await twilio.get(f"{base}.json", params={"PhoneNumber": "+1212*"})).json()
    assert [n["phone_number"] for n in wildcard["incoming_phone_numbers"]] == [NY_NUMBER]


async def test_incoming_number_wire_shape(twilio: httpx.AsyncClient) -> None:
    number = (await twilio.get(f"{account_base(twilio)}/IncomingPhoneNumbers.json")).json()[
        "incoming_phone_numbers"
    ][0]
    # Fields the real resource has (and the emulator used to drop) ...
    assert number["status_callback_method"] == "POST"
    assert number["voice_caller_id_lookup"] is False
    assert number["emergency_status"] == "Inactive"
    assert number["emergency_address_status"] == "unregistered"
    # ... and one it invented.
    assert "type" not in number and "id" not in number


async def test_incoming_number_config_is_validated(twilio: httpx.AsyncClient) -> None:
    numbers = (await twilio.get(f"{account_base(twilio)}/IncomingPhoneNumbers.json")).json()[
        "incoming_phone_numbers"
    ]
    url = f"{account_base(twilio)}/IncomingPhoneNumbers/{numbers[0]['sid']}.json"
    bad_url = await twilio.post(url, data={"SmsUrl": "not a url"})
    assert bad_url.status_code == 400 and bad_url.json()["code"] == 21402
    bad_method = await twilio.post(url, data={"SmsMethod": "PATCH"})
    assert bad_method.status_code == 400 and bad_method.json()["code"] == 21403
    ok = await twilio.post(url, data={"SmsUrl": "https://consumer.test/sms", "SmsMethod": "get"})
    assert ok.json()["sms_method"] == "GET"
    cleared = await twilio.post(url, data={"SmsUrl": "", "SmsMethod": ""})
    assert cleared.json()["sms_url"] is None and cleared.json()["sms_method"] == "POST"
    # PhoneNumber is purchase-only: updating it is ignored.
    same = await twilio.post(url, data={"PhoneNumber": "+14165550999"})
    assert same.json()["phone_number"] == FROM_NUMBER


async def test_available_numbers_unknown_country_and_inventory(twilio: httpx.AsyncClient) -> None:
    base = f"{account_base(twilio)}/AvailablePhoneNumbers"
    assert (await twilio.get(f"{base}/XX.json")).status_code == 404
    unknown = await twilio.get(f"{base}/XX/Local.json")
    assert unknown.status_code == 404 and unknown.json()["code"] == 20404
    stock = (await twilio.get(f"{base}/CA/Local.json", params={"AreaCode": "416"})).json()
    first = stock["available_phone_numbers"][0]["phone_number"]
    purchased = await twilio.post(
        f"{account_base(twilio)}/IncomingPhoneNumbers.json", data={"PhoneNumber": first}
    )
    assert purchased.status_code == 201
    after = (await twilio.get(f"{base}/CA/Local.json", params={"AreaCode": "416"})).json()
    assert first not in {n["phone_number"] for n in after["available_phone_numbers"]}
    again = await twilio.post(
        f"{account_base(twilio)}/IncomingPhoneNumbers.json", data={"PhoneNumber": first}
    )
    assert again.status_code == 400


# -- API keys and subaccounts ------------------------------------------------------------------


async def test_seeded_api_key_can_be_revoked_and_has_the_real_shape(twilio: httpx.AsyncClient) -> None:
    creds = twilio.creds  # type: ignore[attr-defined]
    base = account_base(twilio)
    key_auth = (creds["api_key_sid"], creds["api_key_secret"])
    assert (await twilio.get(f"{base}/Balance.json", auth=key_auth)).status_code == 200
    created = (await twilio.post(f"{base}/Keys.json", data={"FriendlyName": "CI"})).json()
    assert set(created) == {"sid", "friendly_name", "date_created", "date_updated", "secret"}
    fetched = (await twilio.get(f"{base}/Keys/{created['sid']}.json")).json()
    assert set(fetched) == {"sid", "friendly_name", "date_created", "date_updated"}
    renamed = await twilio.post(f"{base}/Keys/{created['sid']}.json", data={"FriendlyName": "CI v2"})
    assert renamed.json()["friendly_name"] == "CI v2"

    assert (await twilio.delete(f"{base}/Keys/{creds['api_key_sid']}.json")).status_code == 204
    revoked = await twilio.get(f"{base}/Balance.json", auth=key_auth)
    assert revoked.status_code == 401 and revoked.json()["code"] == 20003


async def test_subaccount_credentials_are_scoped_to_the_subaccount(twilio: httpx.AsyncClient) -> None:
    sub = (await twilio.post(f"{API}/2010-04-01/Accounts.json", data={"FriendlyName": "Scoped"})).json()
    sub_base = f"{API}/2010-04-01/Accounts/{sub['sid']}"
    sub_auth = (sub["sid"], sub["auth_token"])
    # Its own resources: fine.
    assert (await twilio.get(f"{sub_base}/Messages.json", auth=sub_auth)).status_code == 200
    # The parent's resources and account: forbidden, and it cannot mint subaccounts.
    denied = await twilio.get(messages_url(twilio), auth=sub_auth)
    assert denied.status_code == 403 and denied.json()["code"] == 20003
    assert (await twilio.get(f"{account_base(twilio)}.json", auth=sub_auth)).status_code == 403
    assert (await twilio.post(f"{API}/2010-04-01/Accounts.json", auth=sub_auth)).status_code == 403
    listing = (await twilio.get(f"{API}/2010-04-01/Accounts.json", auth=sub_auth)).json()["accounts"]
    assert [a["sid"] for a in listing] == [sub["sid"]]
    # The parent reaches into the subaccount and sees it in its own listing.
    assert (await twilio.get(f"{sub_base}/Messages.json")).status_code == 200
    parent_listing = (await twilio.get(f"{API}/2010-04-01/Accounts.json")).json()["accounts"]
    assert {a["sid"] for a in parent_listing} == {twilio.account_sid, sub["sid"]}  # type: ignore[attr-defined]
    # Default subaccount names follow Twilio's pattern.
    default_named = (await twilio.post(f"{API}/2010-04-01/Accounts.json")).json()
    assert default_named["friendly_name"].startswith("SubAccount Created at 2026-")


async def test_strict_auth_checks_subaccount_tokens() -> None:
    hub = EmulatorHub(
        providers=["twilio"],
        anchor="2026-10-16",
        clock=Clock(datetime(2026, 10, 16, tzinfo=UTC)),
        webhook_mode="sync",
        settings={"twilio": {"STRICT_AUTH": "1"}},
    )
    creds = hub.context("twilio").store.meta["credentials"]
    async with httpx.AsyncClient(transport=hub.async_transport(), timeout=30) as client:
        sub = (
            await client.post(
                f"{API}/2010-04-01/Accounts.json",
                data={"FriendlyName": "Strict sub"},
                auth=(creds["account_sid"], creds["auth_token"]),
            )
        ).json()
        url = f"{API}/2010-04-01/Accounts/{sub['sid']}/Balance.json"
        assert (await client.get(url, auth=(sub["sid"], sub["auth_token"]))).status_code == 200
        assert (await client.get(url, auth=(sub["sid"], "wrong"))).status_code == 401


async def test_account_status_transitions(twilio: httpx.AsyncClient) -> None:
    sub = (await twilio.post(f"{API}/2010-04-01/Accounts.json", data={"FriendlyName": "Lifecycle"})).json()
    url = f"{API}/2010-04-01/Accounts/{sub['sid']}.json"
    assert (await twilio.post(url, data={"Status": "bogus"})).status_code == 400
    suspended = await twilio.post(url, data={"Status": "suspended"})
    assert suspended.status_code == 200 and suspended.json()["status"] == "suspended"
    # A suspended account refuses writes (reads still work) ...
    write = await twilio.post(
        f"{API}/2010-04-01/Accounts/{sub['sid']}/Messages.json",
        data={"To": CONTACT, "From": "+15005550006", "Body": "x"},
    )
    assert write.status_code == 403 and write.json()["code"] == 20005
    assert (await twilio.get(f"{API}/2010-04-01/Accounts/{sub['sid']}/Messages.json")).status_code == 200
    # ... can be reactivated, closed, and a closed account stays closed.
    assert (await twilio.post(url, data={"Status": "active"})).json()["status"] == "active"
    assert (await twilio.post(url, data={"Status": "closed"})).json()["status"] == "closed"
    reopen = await twilio.post(url, data={"Status": "active"})
    assert reopen.status_code == 400
    # The main account cannot be suspended or closed through the API.
    assert (await twilio.post(f"{account_base(twilio)}.json", data={"Status": "closed"})).status_code == 403


# -- usage ------------------------------------------------------------------------------------------


async def test_usage_record_subresources(twilio: httpx.AsyncClient) -> None:
    base = f"{account_base(twilio)}/Usage/Records"

    def sms(records: list[dict[str, str]]) -> dict[str, str]:
        return next(r for r in records if r["category"] == "sms")

    all_time = sms((await twilio.get(f"{base}.json")).json()["usage_records"])
    assert int(all_time["count"]) == 105
    today_before = sms((await twilio.get(f"{base}/Today.json")).json()["usage_records"])
    assert today_before["start_date"] == today_before["end_date"]
    seeded_today = int(today_before["count"])  # the live test clock decides what "today" holds

    sent = await twilio.post(
        messages_url(twilio), data={"To": CONTACT, "From": FROM_NUMBER, "Body": "usage check"}
    )
    assert sent.status_code == 201
    today = sms((await twilio.get(f"{base}/Today.json")).json()["usage_records"])
    assert int(today["count"]) == seeded_today + 1
    assert int(today["usage"]) == int(today_before["usage"]) + 1
    assert round(float(today["price"]) - float(today_before["price"]), 5) == 0.0079
    assert sms((await twilio.get(f"{base}/AllTime.json")).json()["usage_records"])["count"] == "106"
    assert sms((await twilio.get(f"{base}.json")).json()["usage_records"])["count"] == "106"

    daily = (await twilio.get(f"{base}/Daily.json", params={"Category": "sms"})).json()["usage_records"]
    assert len(daily) > 5 and all(r["category"] == "sms" and r["start_date"] == r["end_date"] for r in daily)
    monthly = (await twilio.get(f"{base}/Monthly.json", params={"Category": "sms"})).json()["usage_records"]
    assert sum(int(r["count"]) for r in monthly) == 106

    assert (await twilio.get(f"{base}/Fortnightly.json")).status_code == 404
    bad = await twilio.get(f"{base}.json", params={"StartDate": "yesterday-ish"})
    assert bad.status_code == 400


# -- settings ---------------------------------------------------------------------------------------


async def test_auto_advance_can_be_switched_off_to_drive_transitions_by_hand() -> None:
    hub = EmulatorHub(
        providers=["twilio"],
        anchor="2026-10-16",
        clock=Clock(datetime(2026, 10, 16, 9, 0, tzinfo=UTC)),
        webhook_mode="sync",
        settings={"twilio": {"AUTO_ADVANCE": "0"}},
    )
    creds = hub.context("twilio").store.meta["credentials"]
    base = f"{API}/2010-04-01/Accounts/{creds['account_sid']}"
    async with httpx.AsyncClient(
        transport=hub.async_transport(), timeout=30, auth=(creds["account_sid"], creds["auth_token"])
    ) as client:
        sent = (
            await client.post(
                f"{base}/Messages.json", data={"To": CONTACT, "From": FROM_NUMBER, "Body": "hi"}
            )
        ).json()
        assert (await client.get(f"{base}/Messages/{sent['sid']}.json")).json()["status"] == "queued"
        advanced = await client.post(
            f"{API}/_emulator/actions/advance_message", json={"sid": sent["sid"], "status": "sent"}
        )
        assert advanced.json()["message"]["status"] == "sent" and advanced.json()["message"]["date_sent"]
        bad = await client.post(
            f"{API}/_emulator/actions/advance_message", json={"sid": sent["sid"], "status": "teleported"}
        )
        assert bad.status_code == 400
        delivered = await client.post(f"{API}/_emulator/actions/advance_message", json={"sid": sent["sid"]})
        assert delivered.json()["message"]["status"] == "delivered"
        assert delivered.json()["message"]["price"] == "-0.00790"

        # Calls stay queued until driven, so a test can hang up an in-progress call.
        call = (
            await client.post(
                f"{base}/Calls.json", data={"To": CONTACT, "From": FROM_NUMBER, "Url": "http://x.test/voice"}
            )
        ).json()
        assert (await client.get(f"{base}/Calls/{call['sid']}.json")).json()["status"] == "queued"
        up = await client.post(
            f"{API}/_emulator/actions/advance_call", json={"sid": call["sid"], "status": "in-progress"}
        )
        assert up.json()["call"]["status"] == "in-progress" and up.json()["call"]["start_time"]
        cancel = await client.post(f"{base}/Calls/{call['sid']}.json", data={"Status": "canceled"})
        assert cancel.status_code == 400 and cancel.json()["code"] == 21220
        hub.clock.advance(timedelta(seconds=42))  # the call stays up for 42s of emulator time
        hangup = await client.post(f"{base}/Calls/{call['sid']}.json", data={"Status": "completed"})
        assert hangup.status_code == 200 and hangup.json()["status"] == "completed"
        assert hangup.json()["duration"] == "42"
        assert hangup.json()["end_time"] == "Fri, 16 Oct 2026 09:00:42 +0000"
        again = await client.post(f"{base}/Calls/{call['sid']}.json", data={"Status": "completed"})
        assert again.status_code == 400 and again.json()["code"] == 21220

        queued = (
            await client.post(
                f"{base}/Calls.json", data={"To": CONTACT, "From": FROM_NUMBER, "Url": "http://x.test/voice"}
            )
        ).json()
        canceled = await client.post(f"{base}/Calls/{queued['sid']}.json", data={"Status": "canceled"})
        assert canceled.json()["status"] == "canceled" and canceled.json()["duration"] == "0"


async def test_async_mode_progression_is_drained_with_the_webhooks() -> None:
    hub = EmulatorHub(
        providers=["twilio"],
        anchor="2026-10-16",
        clock=Clock(datetime(2026, 10, 16, 9, 0, tzinfo=UTC)),
        webhook_mode="async",
    )
    creds = hub.context("twilio").store.meta["credentials"]
    base = f"{API}/2010-04-01/Accounts/{creds['account_sid']}"
    async with httpx.AsyncClient(
        transport=hub.async_transport(), timeout=30, auth=(creds["account_sid"], creds["auth_token"])
    ) as client:
        sent = (
            await client.post(
                f"{base}/Messages.json", data={"To": CONTACT, "From": FROM_NUMBER, "Body": "hi"}
            )
        ).json()
        assert sent["status"] == "queued"
        await hub.drain_webhooks()  # waits for the background progression, not just webhooks
        assert (await client.get(f"{base}/Messages/{sent['sid']}.json")).json()["status"] == "delivered"


async def test_a_subaccount_key_only_acts_as_the_subaccount(twilio: httpx.AsyncClient) -> None:
    sub = (await twilio.post(f"{API}/2010-04-01/Accounts.json", data={"FriendlyName": "Key owner"})).json()
    sub_base = f"{API}/2010-04-01/Accounts/{sub['sid']}"
    key = (await twilio.post(f"{sub_base}/Keys.json", auth=(sub["sid"], sub["auth_token"]))).json()
    key_auth = (key["sid"], key["secret"])
    # The key reaches its own account ...
    assert (await twilio.get(f"{sub_base}/Messages.json", auth=key_auth)).status_code == 200
    # ... and nothing of the parent's (it must not be a back door to the main account).
    parent = await twilio.get(messages_url(twilio), auth=key_auth)
    assert parent.status_code == 403 and parent.json()["code"] == 20003
    # Keys are listed per owning account.
    main_keys = {k["sid"] for k in (await twilio.get(f"{account_base(twilio)}/Keys.json")).json()["keys"]}
    sub_keys = {k["sid"] for k in (await twilio.get(f"{sub_base}/Keys.json")).json()["keys"]}
    assert key["sid"] in sub_keys and key["sid"] not in main_keys
    assert (await twilio.get(f"{account_base(twilio)}/Keys/{key['sid']}.json")).status_code == 404
    # The parent can still revoke it through the subaccount path.
    assert (await twilio.delete(f"{sub_base}/Keys/{key['sid']}.json")).status_code == 204
    assert (await twilio.get(f"{sub_base}/Messages.json", auth=key_auth)).status_code == 401


async def test_v1_v2_resources_are_scoped_to_the_credentials_account(twilio: httpx.AsyncClient) -> None:
    from tests.providers.twilio.conftest import CONVERSATIONS, MESSAGING, VERIFY

    sub = (await twilio.post(f"{API}/2010-04-01/Accounts.json", data={"FriendlyName": "Tenant"})).json()
    auth = (sub["sid"], sub["auth_token"])
    parent_service = (await twilio.get(f"{MESSAGING}/v1/Services")).json()["services"][0]
    parent_verify = (await twilio.get(f"{VERIFY}/v2/Services")).json()["services"][0]
    parent_conv = (await twilio.get(f"{CONVERSATIONS}/v1/Conversations")).json()["conversations"][0]

    for url, key in (
        (f"{MESSAGING}/v1/Services", "services"),
        (f"{VERIFY}/v2/Services", "services"),
        (f"{CONVERSATIONS}/v1/Conversations", "conversations"),
    ):
        assert (await twilio.get(url, auth=auth)).json()[key] == []
    for url in (
        f"{MESSAGING}/v1/Services/{parent_service['sid']}",
        f"{VERIFY}/v2/Services/{parent_verify['sid']}",
        f"{CONVERSATIONS}/v1/Conversations/{parent_conv['sid']}",
        f"{CONVERSATIONS}/v1/Conversations/{parent_conv['unique_name']}",
    ):
        assert (await twilio.get(url, auth=auth)).status_code == 404
        assert (await twilio.delete(url, auth=auth)).status_code == 404
    # The parent's sender pool, services and numbers cannot be borrowed either.
    sub_base = f"{API}/2010-04-01/Accounts/{sub['sid']}"
    borrowed = await twilio.post(
        f"{sub_base}/Messages.json",
        auth=auth,
        data={"To": CONTACT, "MessagingServiceSid": parent_service["sid"], "Body": "x"},
    )
    assert borrowed.status_code == 404
    started = await twilio.post(
        f"{VERIFY}/v2/Services/{parent_verify['sid']}/Verifications",
        auth=auth,
        data={"To": CONTACT, "Channel": "sms"},
    )
    assert started.status_code == 404

    # Its own resources belong to it alone.
    mine = (await twilio.post(f"{MESSAGING}/v1/Services", auth=auth, data={"FriendlyName": "Mine"})).json()
    assert mine["account_sid"] == sub["sid"]
    assert mine["sid"] not in {
        s["sid"] for s in (await twilio.get(f"{MESSAGING}/v1/Services")).json()["services"]
    }
    own_verify = (await twilio.post(f"{VERIFY}/v2/Services", auth=auth, data={"FriendlyName": "Mine"})).json()
    assert own_verify["account_sid"] == sub["sid"]
    pool_add = await twilio.post(
        f"{MESSAGING}/v1/Services/{mine['sid']}/PhoneNumbers",
        auth=auth,
        data={
            "PhoneNumberSid": (await twilio.get(f"{account_base(twilio)}/IncomingPhoneNumbers.json")).json()[
                "incoming_phone_numbers"
            ][0]["sid"]
        },
    )
    assert pool_add.status_code == 404  # the parent's number is not the subaccount's to pool
    conv = (
        await twilio.post(f"{CONVERSATIONS}/v1/Conversations", auth=auth, data={"UniqueName": "mine"})
    ).json()
    assert conv["account_sid"] == sub["sid"]
    assert (await twilio.get(f"{CONVERSATIONS}/v1/Conversations/mine")).status_code == 404  # not the parent's


async def test_number_purchase_errors_and_area_code_purchase(twilio: httpx.AsyncClient) -> None:
    url = f"{account_base(twilio)}/IncomingPhoneNumbers.json"
    neither = await twilio.post(url, data={})
    assert neither.status_code == 400 and neither.json()["code"] == 20001
    no_stock = await twilio.post(url, data={"AreaCode": "999"})
    assert no_stock.status_code == 400 and no_stock.json()["code"] == 21452
    invalid = await twilio.post(url, data={"PhoneNumber": "12345"})
    assert invalid.status_code == 400 and invalid.json()["code"] == 21421
    by_area = await twilio.post(url, data={"AreaCode": "604", "FriendlyName": "Vancouver"})
    assert by_area.status_code == 201 and by_area.json()["phone_number"].startswith("+1604")
    # The number it picked is gone from the inventory, so the next purchase gets a different one.
    again = await twilio.post(url, data={"AreaCode": "604"})
    assert again.status_code == 201 and again.json()["phone_number"] != by_area.json()["phone_number"]


async def test_actions_refuse_to_move_messages_and_calls_backwards(twilio: httpx.AsyncClient) -> None:
    sent = (
        await twilio.post(messages_url(twilio), data={"To": CONTACT, "From": FROM_NUMBER, "Body": "done"})
    ).json()
    backwards = await twilio.post(
        f"{API}/_emulator/actions/advance_message", json={"sid": sent["sid"], "status": "sending"}
    )
    assert backwards.status_code == 400 and "cannot move" in backwards.json()["message"]
    read = await twilio.post(
        f"{API}/_emulator/actions/advance_message", json={"sid": sent["sid"], "status": "read"}
    )
    assert read.status_code == 200 and read.json()["message"]["status"] == "read"
    # Only scheduled messages can be canceled.
    cancel = await twilio.post(
        f"{API}/_emulator/actions/advance_message", json={"sid": sent["sid"], "status": "canceled"}
    )
    assert cancel.status_code == 400

    call = (
        await twilio.post(
            f"{account_base(twilio)}/Calls.json",
            data={"To": CONTACT, "From": FROM_NUMBER, "Url": "http://x.test/voice"},
        )
    ).json()
    reopen = await twilio.post(
        f"{API}/_emulator/actions/advance_call", json={"sid": call["sid"], "status": "ringing"}
    )
    assert reopen.status_code == 400 and "cannot move" in reopen.json()["message"]


async def test_prefix_mode_urls_never_repeat_the_mount_prefix(twilio: httpx.AsyncClient) -> None:
    hub = twilio.hub  # type: ignore[attr-defined]
    creds = twilio.creds  # type: ignore[attr-defined]
    transport = httpx.ASGITransport(app=hub.gateway)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:44080") as gateway:
        gateway.auth = httpx.BasicAuth(creds["account_sid"], creds["auth_token"])
        base = f"/twilio/2010-04-01/Accounts/{creds['account_sid']}"
        queues = (await gateway.get(f"{base}/Queues.json")).json()
        assert queues["uri"].startswith("/2010-04-01/")  # 2010 URIs are relative to the API root
        missing = (await gateway.get(f"{base}/Nope.json")).json()
        assert (
            missing["message"]
            == f"The requested resource {base.removeprefix('/twilio')}/Nope.json was not found"
        )
        service = (await gateway.get("/twilio/v1/Services")).json()["services"][0]
        short_codes = (await gateway.get(f"/twilio/v1/Services/{service['sid']}/ShortCodes")).json()
        assert short_codes["meta"]["url"].startswith("http://localhost:44080/twilio/v1/Services/")
        assert "/twilio/twilio" not in short_codes["meta"]["url"]
