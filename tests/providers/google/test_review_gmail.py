"""Mailbox-specific review regressions and mutable settings."""

from __future__ import annotations

import base64
from email.message import EmailMessage
from typing import Any

from emulators.testing.helpers import bearer, mint

BASE = "https://gmail.googleapis.com"
ROOT = BASE + "/gmail/v1/users/me"


def raw(subject: str = "Review", text: str = "It's   ready\n for review", **headers: str) -> str:
    message = EmailMessage()
    for key, value in {
        "From": "maya.chen@halcyonadvisory.example",
        "To": "eleanor.whitfield@halcyonadvisory.example",
        "Subject": subject,
        **headers,
    }.items():
        if value:
            message[key] = value
    message.set_content(text)
    return base64.urlsafe_b64encode(message.as_bytes()).decode()


async def test_send_recipient_requirement_from_rewrite_snippet_and_thread_subject(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        missing = await client.post(ROOT + "/messages/send", headers=auth, json={"raw": raw(To="")})
        assert (
            missing.status_code == 400 and missing.json()["error"]["message"] == "Recipient address required"
        )
        sent = (
            await client.post(
                ROOT + "/messages/send",
                headers=auth,
                json={"raw": raw(From="eleanor.whitfield@halcyonadvisory.example")},
            )
        ).json()
        full = (await client.get(ROOT + "/messages/" + sent["id"], headers=auth)).json()
        assert full["snippet"] == "It&#39;s ready for review"
        assert (
            next(h["value"] for h in full["payload"]["headers"] if h["name"] == "From")
            == "maya.chen@halcyonadvisory.example"
        )
        changed_subject = (
            await client.post(
                ROOT + "/messages/send",
                headers=auth,
                json={"raw": raw(subject="Different subject"), "threadId": sent["threadId"]},
            )
        ).json()
        assert changed_subject["threadId"] != sent["threadId"]


async def test_search_me_categories_unknown_operators_and_partial_responses(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        for category, label in (
            ("primary", "PERSONAL"),
            ("social", "SOCIAL"),
            ("promotions", "PROMOTIONS"),
            ("updates", "UPDATES"),
            ("forums", "FORUMS"),
        ):
            record = (
                await client.post(
                    ROOT + "/messages",
                    headers=auth,
                    json={
                        "raw": raw(text="unknown:needle text", To="maya.chen@halcyonadvisory.example"),
                        "labelIds": ["CATEGORY_" + label],
                    },
                )
            ).json()
            for query in ("from:me to:me", "category:" + category, "unknown:needle"):
                listed = (
                    await client.get(
                        ROOT + "/messages", headers=auth, params={"q": query, "fields": "messages(id)"}
                    )
                ).json()
                assert record["id"] in {r["id"] for r in listed["messages"]}
                assert set(listed) == {"messages"} and all(set(r) == {"id"} for r in listed["messages"])
        await client.post(ROOT + "/labels", headers=auth, json={"name": "Duplicate"})
        duplicate = await client.post(ROOT + "/labels", headers=auth, json={"name": "duplicate"})
        assert (
            duplicate.status_code == 409 and duplicate.json()["error"]["errors"][0]["reason"] == "duplicate"
        )
        app = bearer(await mint(client, BASE, app_only=True))
        denied = await client.get(ROOT + "/profile", headers=app)
        assert denied.status_code == 400 and denied.json()["error"]["status"] == "FAILED_PRECONDITION"


async def test_settings_read_write_filter_forwarding_and_send_as(make_client: Any) -> None:
    async with make_client("google") as client:
        auth = bearer(await mint(client, BASE, user="p018"))
        for setting, body in (
            ("vacation", {"enableAutoReply": True, "responseSubject": "Away"}),
            ("imap", {"enabled": False}),
            ("pop", {"accessWindow": "allMail", "disposition": "archive"}),
            ("autoForwarding", {"enabled": False}),
        ):
            saved = await client.put(ROOT + "/settings/" + setting, headers=auth, json=body)
            assert saved.status_code == 200, saved.text
            read = (await client.get(ROOT + "/settings/" + setting, headers=auth)).json()
            assert all(read[k] == v for k, v in body.items())
        rule = (
            await client.post(
                ROOT + "/settings/filters",
                headers=auth,
                json={
                    "criteria": {"from": "eleanor.whitfield@halcyonadvisory.example"},
                    "action": {"addLabelIds": ["STARRED"]},
                },
            )
        ).json()
        path = ROOT + "/settings/filters/" + rule["id"]
        assert (await client.get(path, headers=auth)).json() == rule
        assert (await client.delete(path, headers=auth)).status_code == 204
        assert (await client.get(path, headers=auth)).status_code == 404
        forwarding = ROOT + "/settings/forwardingAddresses"
        email = "eleanor.whitfield@halcyonadvisory.example"
        assert (await client.post(forwarding, headers=auth, json={"forwardingEmail": email})).json()[
            "verificationStatus"
        ] == "pending"
        assert (await client.get(forwarding, headers=auth)).json()["forwardingAddresses"][0][
            "forwardingEmail"
        ] == email
        assert (await client.get(forwarding + "/" + email, headers=auth)).status_code == 200
        assert (
            await client.put(
                ROOT + "/settings/autoForwarding", headers=auth, json={"enabled": True, "emailAddress": email}
            )
        ).status_code == 400
        client.hub.context("google").store["gmail_forwarding:p018"].get(email)["verificationStatus"] = (
            "accepted"
        )
        assert (
            await client.put(
                ROOT + "/settings/autoForwarding", headers=auth, json={"enabled": True, "emailAddress": email}
            )
        ).json()["enabled"]
        assert (await client.delete(forwarding + "/" + email, headers=auth)).status_code == 204
        primary = ROOT + "/settings/sendAs/maya.chen@halcyonadvisory.example"
        assert (await client.patch(primary, headers=auth, json={"signature": "Halcyon Advisory"})).json()[
            "signature"
        ] == "Halcyon Advisory"
        assert (await client.get(primary, headers=auth)).json()["isPrimary"]
        other = bearer(await mint(client, BASE, user="p001"))
        assert (await client.get(primary, headers=other)).status_code == 404


async def test_verified_send_as_alias_is_preserved(make_client: Any) -> None:
    async with make_client("google") as client:
        admin = bearer(await mint(client, BASE, user="p035"))
        email = "maya.chen@halcyonadvisory.example"
        alias = "maya.alias@halcyonadvisory.example"
        assert (
            await client.post(
                "https://admin.googleapis.com/admin/directory/v1/users/" + email + "/aliases",
                headers=admin,
                json={"alias": alias},
            )
        ).status_code == 200
        auth = bearer(await mint(client, BASE, user="p018"))
        saved = await client.post(ROOT + "/settings/sendAs", headers=auth, json={"sendAsEmail": alias})
        assert saved.json()["verificationStatus"] == "accepted"
        sent = (
            await client.post(ROOT + "/messages/send", headers=auth, json={"raw": raw(From=alias)})
        ).json()
        full = (await client.get(ROOT + "/messages/" + sent["id"], headers=auth)).json()
        assert next(h["value"] for h in full["payload"]["headers"] if h["name"] == "From") == alias
        assert (await client.delete(ROOT + "/settings/sendAs/" + alias, headers=auth)).status_code == 204
        assert (await client.get(ROOT + "/settings/sendAs/" + alias, headers=auth)).status_code == 404
