"""Absolute URLs are right in host mode, path-prefix mode and per-port mode."""

from __future__ import annotations

import httpx
import pytest

from emulators.core.hub import EmulatorHub

from .conftest import PASSWORD, TOKEN, soap_login_xml

USER = "arjun.mehta@halcyonadvisory.example.crm"


async def login(client: httpx.AsyncClient, prefix: str) -> dict[str, str]:
    r = await client.post(
        f"{prefix}/services/oauth2/token",
        data={
            "grant_type": "password",
            "client_id": "k",
            "client_secret": "s",
            "username": USER,
            "password": PASSWORD + TOKEN,
        },
    )
    assert r.status_code == 200, r.text
    return r.json()


@pytest.mark.parametrize(
    ("base", "prefix"), [("http://localhost:44080", "/salesforce"), ("http://localhost:44105", "")]
)
async def test_prefix_and_per_port_modes_keep_urls_on_the_emulator(
    hub: EmulatorHub, base: str, prefix: str
) -> None:
    app = hub.gateway if prefix else hub.apps["salesforce"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=base) as client:
        body = await login(client, prefix)
        root = base + prefix
        assert body["instance_url"] == root
        assert body["id"] == f"{root}/id/00D5g0000049673EAA/{body['id'].rsplit('/', 1)[1]}"
        headers = {"Authorization": f"Bearer {body['access_token']}"}
        ident = await client.get(body["id"].replace(base, ""), headers=headers)
        assert (
            ident.status_code == 200 and ident.json()["urls"]["rest"] == f"{root}/services/data/v{{version}}/"
        )
        resources = (await client.get(f"{prefix}/services/data/v62.0/", headers=headers)).json()
        assert resources["identity"] == body["id"]
        soap = await client.post(
            f"{prefix}/services/Soap/u/62.0",
            content=soap_login_xml(USER, PASSWORD + TOKEN),
            headers={"content-type": "text/xml"},
        )
        assert f"<serverUrl>{root}/services/Soap/u/62.0/00D5g0000049673EAA</serverUrl>" in soap.text
        query = await client.get(
            f"{prefix}/services/data/v62.0/query",
            params={"q": "SELECT COUNT() FROM Account"},
            headers=headers,
        )
        assert query.json()["totalSize"] == 41
        info = (await client.get(f"{prefix}/.well-known/openid-configuration")).json()
        assert info["token_endpoint"] == f"{root}/services/oauth2/token"
        # the id URL minted in prefix mode is on the emulator, so following it works
        assert (await client.get(f"{prefix}/services/oauth2/userinfo", headers=headers)).json()[
            "sub"
        ] == body["id"]
        assert (
            (
                await client.get(
                    f"{prefix}/services/data/v62.0/query",
                    params={"q": "SELECT Id FROM Task LIMIT 1"},
                    headers=headers,
                )
            )
            .json()["records"][0]["attributes"]["url"]
            .startswith("/services/data/v62.0/sobjects/Task/")
        )


async def test_host_mode_includes_the_port_the_client_used(hub: EmulatorHub) -> None:
    async with httpx.AsyncClient(transport=hub.async_transport()) as client:
        r = await client.post(
            "https://login.salesforce.com:8443/services/oauth2/token",
            data={"grant_type": "password", "client_id": "k", "username": USER, "password": PASSWORD},
        )
        assert r.json()["instance_url"] == "https://halcyonadvisory.my.salesforce.com:8443"
        assert r.json()["id"].startswith("https://login.salesforce.com:8443/id/")
