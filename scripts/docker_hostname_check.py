"""Smoke test for the Docker real-hostname mode (run inside a container on `emulators-vendors`).

    docker compose up -d --build
    docker run --rm --network emulators-vendors -v emulators-certs:/certs:ro       -v "$PWD/scripts/docker_hostname_check.py:/check.py:ro" --entrypoint python emulators:local /check.py

Every URL below is the vendor's real hostname over HTTPS; the only trust anchor is the emulator CA.
"""

from __future__ import annotations

import httpx

ca = "/certs/ca-bundle.pem"
with httpx.Client(verify=ca, timeout=20) as c:
    tok = c.post(
        "https://login.microsoftonline.com/organizations/oauth2/v2.0/token",
        data={
            "grant_type": "client_credentials",
            "client_id": "app",
            "client_secret": "s",
            "scope": "https://graph.microsoft.com/.default",
        },
    ).json()["access_token"]
    me = c.get(
        "https://graph.microsoft.com/v1.0/users/maya.chen@halcyonadvisory.example",
        headers={"Authorization": f"Bearer {tok}"},
    ).json()
    print("graph:", me["displayName"], me["mail"])
    drive = c.get(
        "https://graph.microsoft.com/v1.0/users/maya.chen@halcyonadvisory.example/drive/root/children",
        headers={"Authorization": f"Bearer {tok}"},
    ).json()
    print("onedrive items:", len(drive["value"]))
    z = c.post(
        "https://api.stripe.com/v1/customers",
        headers={"Authorization": "Bearer sk_test_x"},
        data={"email": "docker@example.com"},
    ).json()
    print("stripe:", z["id"], z["object"])
    account = c.get("https://api.zoom.us/_emulator/info").json()["account_id"]
    zt = c.post(
        "https://zoom.us/oauth/token",
        params={"grant_type": "account_credentials", "account_id": account},
        auth=("cid", "sec"),
    ).json()
    users = c.get(
        "https://api.zoom.us/v2/users", headers={"Authorization": f"Bearer {zt['access_token']}"}
    ).json()
    print("zoom users:", users["total_records"])
    r = c.get("https://halcyonadvisory.sharepoint.com/_emulator/health")
    print("sharepoint wildcard host:", r.status_code)
    for host, path, collection in (
        ("https://app.asana.com", "/api/1.0/workspaces", "data"),
        ("https://api.trello.com", "/1/members/me/organizations", None),
        ("https://api.airtable.com", "/v0/meta/bases", "bases"),
    ):
        response = c.post(host + "/_emulator/tokens", json={"user": "p001"})
        response.raise_for_status()
        token = response.json()["access_token"]
        response = c.get(host + path, headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        records = response.json()
        assert records[collection] if collection else records
    for host in ("https://content.airtable.com", "https://v5.airtableusercontent.com", "https://trello.com"):
        c.get(host + "/_emulator/health").raise_for_status()
