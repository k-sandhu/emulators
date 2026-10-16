from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from emulators.providers.microsoft import common
from emulators.providers.microsoft import drive_model as dm
from emulators.testing.helpers import bearer, mint

BASE = "https://graph.microsoft.com"


@pytest.mark.parametrize("version", ["v1.0", "beta"])
async def test_sites_discovery_libraries_columns_and_library_items(
    make_client: Callable[..., Any], version: str
) -> None:
    async with make_client("microsoft") as client:
        auth = bearer(await mint(client, BASE, app_only=True))
        base = BASE + "/" + version
        root = (await client.get(base + "/sites/root", headers=auth)).json()
        assert root["siteCollection"]["hostname"] == "halcyonadvisory.sharepoint.com"
        assert root["root"] == {}
        resolved = await client.get(
            base + "/sites/halcyonadvisory.sharepoint.com:/sites/ClientEngagements", headers=auth
        )
        assert resolved.status_code == 200, resolved.text
        site = resolved.json()
        assert len(site["id"].split(",")) == 3
        by_id = (await client.get(base + "/sites/" + site["id"], headers=auth)).json()
        assert by_id["id"] == site["id"]
        search = (await client.get(base + "/sites?search=Client", headers=auth)).json()["value"]
        assert site["id"] in [s["id"] for s in search]
        assert (await client.get(base + f"/sites/{site['id']}/sites", headers=auth)).json()["value"] == []
        drive = (await client.get(base + f"/sites/{site['id']}/drive", headers=auth)).json()
        drives = (await client.get(base + f"/sites/{site['id']}/drives", headers=auth)).json()["value"]
        assert drives[0]["id"] == drive["id"] and drive["driveType"] == "documentLibrary"
        group = common.group_id(client.hub.world, "g-audit")
        assert (await client.get(base + f"/groups/{group}/drive", headers=auth)).status_code == 200
        assert (await client.get(base + f"/groups/{group}/sites/root", headers=auth)).status_code == 200
        lists = (await client.get(base + f"/sites/{site['id']}/lists", headers=auth)).json()["value"]
        library = next(r for r in lists if r["name"] == "Documents")
        list_url = base + f"/sites/{site['id']}/lists/{library['id']}"
        assert (await client.get(list_url, headers=auth)).json()["list"]["template"] == "documentLibrary"
        columns = (await client.get(list_url + "/columns", headers=auth)).json()["value"]
        assert "Title" in [c["name"] for c in columns]
        assert (await client.get(list_url + "/columns/" + columns[0]["id"], headers=auth)).status_code == 200
        items = (
            await client.get(
                list_url + "/items",
                headers=auth,
                params={"expand": "fields(select=Title,FileLeafRef)", "$top": 3},
            )
        ).json()
        assert len(items["value"]) == 3 and "@odata.nextLink" in items
        item = items["value"][0]
        assert set(item["fields"]) == {"id", "Title", "FileLeafRef"}
        assert (await client.get(list_url + f"/items/{item['id']}/fields", headers=auth)).json()[
            "FileLeafRef"
        ] == item["fields"]["FileLeafRef"]
        drive_item = (await client.get(list_url + f"/items/{item['id']}/driveItem", headers=auth)).json()
        assert drive_item["name"] == item["fields"]["FileLeafRef"]
        changed = await client.patch(
            list_url + f"/items/{item['id']}/fields", headers=auth, json={"Title": "Library metadata"}
        )
        assert changed.status_code == 200
        assert changed.json()["Title"] == "Library metadata"
        assert (await client.get(base + f"/sites/{site['id']}/permissions", headers=auth)).json()["value"]


async def test_engagement_tracker_mutations_and_site_permissions(make_client: Callable[..., Any]) -> None:
    async with make_client("microsoft") as client:
        ctx = client.hub.context("microsoft")
        sid = dm.site_id(ctx, "clients")
        writer = bearer(await mint(client, BASE, user="p001"))
        reader = bearer(await mint(client, BASE, user="p025"))
        site_url = BASE + f"/v1.0/sites/{sid}"
        lists = (await client.get(site_url + "/lists", headers=writer)).json()["value"]
        listing = next(r for r in lists if r["displayName"] == "Engagement Tracker")
        url = site_url + f"/lists/{listing['id']}/items"
        seeded = (await client.get(url, headers=writer, params={"$expand": "fields"})).json()["value"]
        assert len(seeded) == len(client.hub.world.engagements)
        first_title = seeded[0]["fields"]["Title"].replace("'", "''")
        filtered = (
            await client.get(url, headers=writer, params={"$filter": f"fields/Title eq '{first_title}'"})
        ).json()["value"]
        assert filtered and filtered[0]["id"] == seeded[0]["id"] and "fields" not in filtered[0]
        assert set(seeded[0]["fields"]) >= {
            "Title",
            "Client",
            "Lead",
            "BudgetHours",
            "Service",
            "Status",
            "StartDate",
            "EndDate",
        }
        created = await client.post(
            url,
            headers=writer,
            json={
                "fields": {
                    "Title": "Follow-up engagement",
                    "Client": client.hub.world.clients[0].name,
                    "BudgetHours": 120,
                }
            },
        )
        assert created.status_code == 201, created.text
        item = created.json()
        assert (
            await client.patch(url + f"/{item['id']}/fields", headers=writer, json={"Status": "In Progress"})
        ).json()["Status"] == "In Progress"
        assert (
            await client.patch(url + f"/{item['id']}", headers=writer, json={"fields": {"BudgetHours": 140}})
        ).json()["fields"]["BudgetHours"] == 140
        assert (
            await client.post(url, headers=reader, json={"fields": {"Title": "Denied"}})
        ).status_code == 403
        assert (await client.get(site_url + "/permissions", headers=writer)).status_code == 403
        assert (
            await client.patch(
                url + f"/{item['id']}/fields", headers=writer, json={"BudgetHours": "not a number"}
            )
        ).status_code == 400
        assert (await client.delete(url + f"/{item['id']}", headers=writer)).status_code == 204
        assert (await client.get(url + f"/{item['id']}", headers=writer)).status_code == 404
        next_item = await client.post(url, headers=writer, json={"fields": {"Title": "Next engagement"}})
        assert int(next_item.json()["id"]) > int(item["id"])
        external = bearer(await mint(client, BASE, user=client.hub.world.external_people[0].id))
        assert (await client.get(site_url, headers=external)).status_code == 404
        assert (await client.get(BASE + "/v1.0/sites/root", headers=external)).status_code == 404
