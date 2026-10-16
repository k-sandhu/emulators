"""Use unmodified SDK hosts through a temporary local gateway."""

from __future__ import annotations

import json
import sys

import msal
import requests
import stripe
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from slack_sdk import WebClient

from emulators.core.hub import EmulatorHub
from emulators.testing.intercept import intercept
from emulators.testing.server import EmulatorServer


def main() -> dict[str, int]:
    hub = EmulatorHub(
        providers=["microsoft", "google", "stripe", "slack"], seed="emulators", anchor="2026-10-16"
    )
    with EmulatorServer(hub) as server, intercept(server), requests.Session() as session:
        application = msal.ConfidentialClientApplication(
            "00000000-0000-0000-0000-00000000c0de",
            client_credential="synthetic-secret",
            authority="https://login.microsoftonline.com/halcyonadvisory.example",
        )
        grant = application.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
        response = session.get(
            "https://graph.microsoft.com/v1.0/users",
            headers={"Authorization": f"Bearer {grant['access_token']}"},
            params={"$top": 999},
            timeout=20,
        )
        response.raise_for_status()
        graph_count = len(response.json()["value"])
        response = session.post(
            "https://www.googleapis.com/_emulator/tokens", json={"user": "p035"}, timeout=20
        )
        response.raise_for_status()
        credentials = Credentials(token=response.json()["access_token"])
        with build("admin", "directory_v1", credentials=credentials, static_discovery=True) as directory:
            google_users = directory.users().list(customer="my_customer", maxResults=500).execute()
        stripe_client = stripe.StripeClient("sk_test_emulator")
        stripe_count = len(stripe_client.v1.customers.list({"limit": 100}).data)
        response = session.post(
            "https://slack.com/_emulator/tokens",
            json={"user": "p035", "scopes": ["users:read", "users:read.email"]},
            timeout=20,
        )
        response.raise_for_status()
        slack = WebClient(token=response.json()["access_token"])
        slack_count = len(slack.users_list(limit=200)["members"])
    result = {
        "graph_users": graph_count,
        "google_users": len(google_users["users"]),
        "stripe_customers": stripe_count,
        "slack_users": slack_count,
    }
    assert all(count > 0 for count in result.values())
    return result


if __name__ == "__main__":
    sys.stdout.write(json.dumps(main(), indent=2) + "\n")
