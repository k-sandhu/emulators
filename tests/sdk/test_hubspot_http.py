"""Connector-style HTTP contracts through real vendor URLs and the live gateway."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

pytestmark = pytest.mark.sdk
BASE = "https://api.hubapi.com"


def test_connector_contacts_companies_deals_sync(emulator_intercept: Any) -> None:
    with httpx.Client() as client:
        credentials = client.get(BASE + "/_emulator/info").json()["credentials"]
        client.headers["Authorization"] = "Bearer " + credentials["private_app_token"]
        for kind, props in [
            ("contacts", ["email", "firstname", "lastname", "hubspot_owner_id"]),
            ("companies", ["name", "domain", "hubspot_owner_id"]),
            ("deals", ["dealname", "amount", "dealstage", "pipeline", "hubspot_owner_id"]),
        ]:
            url = BASE + f"/crm/v3/objects/{kind}"
            response = client.get(
                url,
                params={
                    "limit": 2,
                    "properties": ",".join(props),
                    "associations": "companies" if kind != "companies" else "contacts",
                },
            )
            response.raise_for_status()
            page = response.json()
            assert len(page["results"]) == 2
            for record in page["results"]:
                assert record["id"].isdigit() and record["properties"]["hs_object_id"] == record["id"]
                assert all(p in record["properties"] for p in props)
            next_page = client.get(page["paging"]["next"]["link"])
            next_page.raise_for_status()
            assert not {r["id"] for r in page["results"]} & {r["id"] for r in next_page.json()["results"]}
            batch = client.post(
                url + "/batch/read",
                json={"properties": props, "inputs": [{"id": r["id"]} for r in page["results"]]},
            )
            batch.raise_for_status()
            assert batch.json()["status"] == "COMPLETE"
            assert len(batch.json()["results"]) == 2
        contacts = client.post(
            BASE + "/crm/v3/objects/contacts/search",
            json={
                "filterGroups": [
                    {
                        "filters": [
                            {
                                "propertyName": "email",
                                "operator": "EQ",
                                "value": "christopher.ingram@northwindlogistics.example",
                            }
                        ]
                    }
                ],
                "properties": ["email", "firstname"],
            },
        )
        contacts.raise_for_status()
        assert contacts.json()["total"] == 1
        contact_id = contacts.json()["results"][0]["id"]
        associations = client.get(BASE + f"/crm/v4/objects/contacts/{contact_id}/associations/companies")
        associations.raise_for_status()
        assert len(associations.json()["results"]) == 1
        assert isinstance(associations.json()["results"][0]["toObjectId"], int)


def test_connector_oauth_refresh_and_schema_discovery(emulator_intercept: Any) -> None:
    with httpx.Client() as client:
        redirect = "https://connector.example/callback"
        authorized = client.get(
            "https://app.hubspot.com/oauth/authorize",
            params={
                "client_id": "connector",
                "redirect_uri": redirect,
                "login_hint": "maya.chen@halcyonadvisory.example",
                "scope": "oauth crm.objects.contacts.read crm.objects.companies.read crm.objects.deals.read crm.objects.owners.read",
            },
        )
        assert authorized.status_code == 302
        code = parse_qs(urlparse(authorized.headers["location"]).query)["code"][0]
        tokens = client.post(
            BASE + "/oauth/v1/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": "connector",
                "client_secret": "secret",
                "redirect_uri": redirect,
            },
        )
        tokens.raise_for_status()
        body = tokens.json()
        client.headers["Authorization"] = "Bearer " + body["access_token"]
        for path in (
            "/crm/v3/properties/contacts",
            "/crm/v3/properties/companies",
            "/crm/v3/properties/deals",
            "/crm/v3/pipelines/deals",
            "/crm/v3/owners",
        ):
            metadata = client.get(BASE + path)
            metadata.raise_for_status()
            assert metadata.json()["results"]
        refreshed = client.post(
            BASE + "/oauth/v1/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": body["refresh_token"],
                "client_id": "connector",
                "client_secret": "secret",
            },
        )
        refreshed.raise_for_status()
        assert refreshed.json()["access_token"] != body["access_token"]
        info = client.get(BASE + "/oauth/v1/access-tokens/" + refreshed.json()["access_token"])
        info.raise_for_status()
        assert info.json()["hub_id"] == 24681357
