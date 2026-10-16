"""Accounts, auth modes, phone numbers, available numbers, balance, usage, keys."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx

from emulators.core.clock import Clock
from emulators.core.hub import EmulatorHub
from tests.providers.twilio.conftest import API, CONTACT, FROM_NUMBER, NY_NUMBER, account_base


async def test_account_fetch_and_update(twilio: httpx.AsyncClient) -> None:
    r = await twilio.get(f"{account_base(twilio)}.json")
    assert r.status_code == 200, r.text
    account = r.json()
    assert account["sid"] == twilio.account_sid  # type: ignore[attr-defined]
    assert account["friendly_name"] == "Halcyon Advisory Group"
    assert account["status"] == "active"
    assert account["type"] == "Full"
    assert account["owner_account_sid"] == account["sid"]
    assert account["auth_token"] == twilio.creds["auth_token"]  # type: ignore[attr-defined]
    assert account["subresource_uris"]["messages"].endswith("/Messages.json")
    assert account["uri"] == f"/2010-04-01/Accounts/{account['sid']}.json"

    r = await twilio.post(f"{account_base(twilio)}.json", data={"FriendlyName": "Halcyon (renamed)"})
    assert r.status_code == 200 and r.json()["friendly_name"] == "Halcyon (renamed)"

    listing = (await twilio.get(f"{API}/2010-04-01/Accounts.json")).json()
    assert [a["sid"] for a in listing["accounts"]] == [account["sid"]]


async def test_balance_and_usage(twilio: httpx.AsyncClient) -> None:
    r = await twilio.get(f"{account_base(twilio)}/Balance.json")
    assert r.json() == {
        "account_sid": twilio.account_sid,  # type: ignore[attr-defined]
        "balance": "152.34",
        "currency": "USD",
    }
    r = await twilio.get(f"{account_base(twilio)}/Usage/Records.json")
    assert r.status_code == 200, r.text
    categories = {rec["category"]: rec for rec in r.json()["usage_records"]}
    assert int(categories["sms"]["count"]) > 100  # seeded history
    assert categories["sms"]["usage_unit"] == "segments"
    assert categories["totalprice"]["price_unit"] == "usd"


async def test_auth_required_and_wrong_credentials(twilio: httpx.AsyncClient) -> None:
    r = await twilio.get(f"{account_base(twilio)}.json", auth=None)
    assert r.status_code == 401
    assert r.json() == {
        "code": 20003,
        "detail": "Your AccountSid or AuthToken was incorrect.",
        "message": "Authenticate",
        "more_info": "https://www.twilio.com/docs/errors/20003",
        "status": 401,
    }
    assert r.headers.get("www-authenticate", "").startswith("Basic")
    r = await twilio.get(f"{account_base(twilio)}.json", auth=("AC" + "f" * 32, "whatever"))
    assert r.status_code == 401
    # Lenient mode (default): any password for the right SID works.
    r = await twilio.get(f"{account_base(twilio)}.json", auth=(twilio.account_sid, "not-the-token"))  # type: ignore[attr-defined]
    assert r.status_code == 200, r.text
    # Unknown account in the path is a 20404.
    r = await twilio.get(f"{API}/2010-04-01/Accounts/{'AC' + 'f' * 32}.json")
    assert r.status_code == 404 and r.json()["code"] == 20404


async def test_strict_auth_setting() -> None:
    hub = EmulatorHub(
        providers=["twilio"],
        anchor="2026-10-16",
        clock=Clock(datetime(2026, 10, 16, tzinfo=UTC)),
        webhook_mode="sync",
        settings={"twilio": {"STRICT_AUTH": "1"}},
    )
    creds = hub.context("twilio").store.meta["credentials"]
    url = f"{API}/2010-04-01/Accounts/{creds['account_sid']}.json"
    async with httpx.AsyncClient(transport=hub.async_transport(), timeout=30) as client:
        assert (await client.get(url, auth=(creds["account_sid"], creds["auth_token"]))).status_code == 200
        assert (await client.get(url, auth=(creds["account_sid"], "wrong"))).status_code == 401
        assert (
            await client.get(url, auth=(creds["api_key_sid"], creds["api_key_secret"]))
        ).status_code == 200
        assert (await client.get(url, auth=(creds["api_key_sid"], "wrong"))).status_code == 401


async def test_api_key_auth_and_key_crud(twilio: httpx.AsyncClient) -> None:
    creds = twilio.creds  # type: ignore[attr-defined]
    r = await twilio.get(f"{account_base(twilio)}.json", auth=(creds["api_key_sid"], creds["api_key_secret"]))
    assert r.status_code == 200, r.text
    created = await twilio.post(f"{account_base(twilio)}/Keys.json", data={"FriendlyName": " CI key"})
    assert created.status_code == 201, created.text
    key = created.json()
    assert key["sid"].startswith("SK") and key["secret"]
    r = await twilio.get(f"{account_base(twilio)}.json", auth=(key["sid"], key["secret"]))
    assert r.status_code == 200, r.text
    listing = (await twilio.get(f"{account_base(twilio)}/Keys.json")).json()
    assert key["sid"] in {k["sid"] for k in listing["keys"]}
    assert all("secret" not in k for k in listing["keys"])
    assert (await twilio.delete(f"{account_base(twilio)}/Keys/{key['sid']}.json")).status_code == 204
    r = await twilio.get(f"{account_base(twilio)}.json", auth=(key["sid"], key["secret"]))
    assert r.status_code == 401


async def test_test_credentials(twilio: httpx.AsyncClient) -> None:
    creds = twilio.creds  # type: ignore[attr-defined]
    test_auth = (creds["test_account_sid"], creds["test_auth_token"])
    base = f"{API}/2010-04-01/Accounts/{creds['test_account_sid']}"
    account = (await twilio.get(f"{base}.json", auth=test_auth)).json()
    assert account["sid"] == creds["test_account_sid"]
    assert account["auth_token"] == creds["test_auth_token"]

    r = await twilio.post(
        f"{base}/Messages.json",
        auth=test_auth,
        data={"To": CONTACT, "From": "+15005550006", "Body": "test-mode send"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["account_sid"] == creds["test_account_sid"]
    r = await twilio.post(
        f"{base}/Messages.json",
        auth=test_auth,
        data={"To": "+15005550009", "From": "+15005550006", "Body": "not mobile"},
    )
    assert r.status_code == 400 and r.json()["code"] == 21614


async def test_mint_token_action(twilio: httpx.AsyncClient) -> None:
    r = await twilio.post(f"{API}/_emulator/tokens", json={})
    assert r.status_code == 200, r.text
    assert r.json()["auth_token"] == twilio.creds["auth_token"]  # type: ignore[attr-defined]
    assert r.json()["account_sid"] == twilio.account_sid  # type: ignore[attr-defined]
    info = (await twilio.get(f"{API}/_emulator/info")).json()
    assert info["credentials"]["account_sid"] == twilio.account_sid  # type: ignore[attr-defined]
    assert "inbound_sms" in info["actions"]


async def test_subaccount_isolation(twilio: httpx.AsyncClient) -> None:
    created = await twilio.post(
        f"{API}/2010-04-01/Accounts.json", data={"FriendlyName": "Sandbox subaccount"}
    )
    assert created.status_code == 201, created.text
    sub = created.json()
    assert sub["owner_account_sid"] == twilio.account_sid  # type: ignore[attr-defined]
    sub_base = f"{API}/2010-04-01/Accounts/{sub['sid']}"
    # The subaccount sees none of the parent's history or numbers.
    assert (await twilio.get(f"{sub_base}/Messages.json")).json()["messages"] == []
    assert (await twilio.get(f"{sub_base}/IncomingPhoneNumbers.json")).json()["incoming_phone_numbers"] == []
    # Sending from the parent's number inside the subaccount is denied.
    r = await twilio.post(
        f"{sub_base}/Messages.json", data={"To": CONTACT, "From": FROM_NUMBER, "Body": "not mine"}
    )
    assert r.status_code == 400 and r.json()["code"] == 21606
    # The magic test sender works and stays inside the subaccount.
    r = await twilio.post(
        f"{sub_base}/Messages.json", data={"To": CONTACT, "From": "+15005550006", "Body": "sub send"}
    )
    assert r.status_code == 201, r.text
    sub_messages = (await twilio.get(f"{sub_base}/Messages.json")).json()["messages"]
    assert [m["sid"] for m in sub_messages] == [r.json()["sid"]]
    parent_sids = {
        m["sid"]
        for m in (await twilio.get(f"{account_base(twilio)}/Messages.json", params={"PageSize": 200})).json()[
            "messages"
        ]
    }
    assert r.json()["sid"] not in parent_sids


async def test_prefix_routing_mode(twilio: httpx.AsyncClient) -> None:
    hub = twilio.hub  # type: ignore[attr-defined]
    creds = twilio.creds  # type: ignore[attr-defined]
    transport = httpx.ASGITransport(app=hub.gateway)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost:44080") as gateway_client:
        gateway_client.auth = httpx.BasicAuth(creds["account_sid"], creds["auth_token"])
        created = await gateway_client.post(
            f"/twilio/2010-04-01/Accounts/{creds['account_sid']}/Messages.json",
            data={"To": CONTACT, "From": FROM_NUMBER, "Body": "via gateway prefix"},
        )
        assert created.status_code == 201, created.text
        services = await gateway_client.get("/twilio/v1/Services")
        assert services.status_code == 200, services.text
        # v1 absolute URLs stay on the gateway base in prefix mode.
        assert services.json()["services"][0]["url"].startswith("http://localhost:44080/twilio/v1/Services/")


async def test_incoming_phone_numbers_crud(twilio: httpx.AsyncClient) -> None:
    listing = (await twilio.get(f"{account_base(twilio)}/IncomingPhoneNumbers.json")).json()
    numbers = listing["incoming_phone_numbers"]
    assert [n["phone_number"] for n in numbers] == [FROM_NUMBER, NY_NUMBER, "+14165550177"]
    toronto = numbers[0]
    assert toronto["sid"].startswith("PN")
    assert toronto["friendly_name"] == "Halcyon Client Line"
    assert toronto["capabilities"] == {"voice": True, "sms": True, "mms": True, "fax": False}
    assert toronto["sms_url"] is None
    assert toronto["address_requirements"] == "none"
    assert toronto["beta"] is False
    assert toronto["origin"] == "twilio"

    filtered = (
        await twilio.get(
            f"{account_base(twilio)}/IncomingPhoneNumbers.json", params={"PhoneNumber": NY_NUMBER}
        )
    ).json()
    assert len(filtered["incoming_phone_numbers"]) == 1

    url = f"{account_base(twilio)}/IncomingPhoneNumbers/{toronto['sid']}.json"
    updated = (
        await twilio.post(
            url,
            data={
                "FriendlyName": "Client line (new)",
                "SmsUrl": "http://consumer.test/sms",
                "SmsMethod": "POST",
                "VoiceUrl": "http://consumer.test/voice",
            },
        )
    ).json()
    assert updated["friendly_name"] == "Client line (new)"
    assert updated["sms_url"] == "http://consumer.test/sms"
    assert updated["voice_url"] == "http://consumer.test/voice"
    assert (await twilio.get(url)).json()["sms_url"] == "http://consumer.test/sms"

    assert (await twilio.delete(url)).status_code == 204
    assert (await twilio.get(url)).status_code == 404
    # After release the number is no longer a valid sender.
    r = await twilio.post(
        f"{account_base(twilio)}/Messages.json", data={"To": CONTACT, "From": FROM_NUMBER, "Body": "gone"}
    )
    assert r.status_code == 400 and r.json()["code"] == 21606


async def test_available_numbers_and_purchase(twilio: httpx.AsyncClient) -> None:
    countries = (await twilio.get(f"{account_base(twilio)}/AvailablePhoneNumbers.json")).json()
    assert {c["country_code"] for c in countries["countries"]} >= {"US", "CA", "GB"}

    local = (
        await twilio.get(
            f"{account_base(twilio)}/AvailablePhoneNumbers/CA/Local.json",
            params={"AreaCode": "416", "SmsEnabled": "true"},
        )
    ).json()
    candidates = local["available_phone_numbers"]
    assert candidates
    candidate = candidates[0]
    assert candidate["phone_number"].startswith("+141655501")
    assert candidate["locality"] == "Toronto" and candidate["iso_country"] == "CA"
    assert candidate["capabilities"]["SMS"] is True

    contains = (
        await twilio.get(
            f"{account_base(twilio)}/AvailablePhoneNumbers/US/Local.json", params={"Contains": "5550115"}
        )
    ).json()["available_phone_numbers"]
    assert contains and all("5550115" in n["phone_number"] for n in contains)

    purchased = await twilio.post(
        f"{account_base(twilio)}/IncomingPhoneNumbers.json",
        data={"PhoneNumber": candidate["phone_number"], "FriendlyName": "New Toronto line"},
    )
    assert purchased.status_code == 201, purchased.text
    numbers = (await twilio.get(f"{account_base(twilio)}/IncomingPhoneNumbers.json")).json()[
        "incoming_phone_numbers"
    ]
    assert candidate["phone_number"] in {n["phone_number"] for n in numbers}
    # Buying the same number twice fails.
    again = await twilio.post(
        f"{account_base(twilio)}/IncomingPhoneNumbers.json", data={"PhoneNumber": candidate["phone_number"]}
    )
    assert again.status_code == 400
    # Purchase by area code picks an unowned available number.
    by_area = await twilio.post(f"{account_base(twilio)}/IncomingPhoneNumbers.json", data={"AreaCode": "604"})
    assert by_area.status_code == 201, by_area.text
    assert by_area.json()["phone_number"].startswith("+160455501")
