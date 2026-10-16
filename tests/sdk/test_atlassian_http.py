"""A connector's unchanged httpx requests through the live hostname interceptor."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

pytestmark = pytest.mark.sdk

SITE = "https://halcyonadvisory.atlassian.net"
API = "https://api.atlassian.com"
AUTH = "https://auth.atlassian.com"


def test_site_basic_connector_tree_and_incremental_search(emulator_intercept: object) -> None:
    credentials = httpx.get(SITE + "/_emulator/info").json()["credentials"]
    with httpx.Client(auth=(credentials["email"], credentials["api_token"]), timeout=30) as client:
        response = client.get(SITE + "/wiki/api/v2/spaces", params={"keys": "CLIENTS"})
        response.raise_for_status()
        space = response.json()["results"][0]
        root = space["homepageId"]
        children = client.get(SITE + f"/wiki/api/v2/pages/{root}/children", params={"limit": 2})
        children.raise_for_status()
        assert children.json()["results"] and 'rel="next"' in children.headers["link"]
        client.get(children.json()["_links"]["next"]).raise_for_status()
        search = client.get(
            SITE + "/wiki/rest/api/content/search",
            params={
                "cql": 'type=page AND space=CLIENTS AND text ~ "revenue" AND lastmodified > "2025/01/01"',
                "expand": "body.storage,version,ancestors,history.lastUpdated",
                "limit": 3,
            },
        )
        search.raise_for_status()
        assert search.json()["results"]
        page = search.json()["results"][0]
        assert (
            page["body"]["storage"]["value"]
            and page["history"]["lastUpdated"]["when"] == page["version"]["when"]
        )


def test_oauth_cloud_gateway_confluence_and_jira(emulator_intercept: object) -> None:
    redirect = "https://consumer.example/atlassian/callback"
    with httpx.Client(timeout=30, follow_redirects=False) as client:
        consent = client.get(
            AUTH + "/authorize",
            params={
                "audience": "api.atlassian.com",
                "client_id": "lumen",
                "scope": "read:confluence-content.all read:confluence-space.summary read:jira-work offline_access",
                "redirect_uri": redirect,
                "response_type": "code",
                "state": "contract",
                "login_hint": "maya.chen@halcyonadvisory.example",
            },
        )
        assert consent.status_code == 302
        code = parse_qs(urlsplit(consent.headers["location"]).query)["code"][0]
        token = client.post(
            AUTH + "/oauth/token",
            json={
                "grant_type": "authorization_code",
                "client_id": "lumen",
                "client_secret": "test-secret",
                "redirect_uri": redirect,
                "code": code,
            },
        )
        token.raise_for_status()
        client.headers["Authorization"] = "Bearer " + token.json()["access_token"]
        resources = client.get(API + "/oauth/token/accessible-resources")
        resources.raise_for_status()
        cloud = resources.json()[0]["id"]
        pages = client.get(
            API + f"/ex/confluence/{cloud}/wiki/api/v2/pages", params={"limit": 2, "body-format": "storage"}
        )
        pages.raise_for_status()
        assert pages.json()["results"][0]["body"]["storage"]["value"]
        client.get(pages.json()["_links"]["next"]).raise_for_status()
        search = client.post(
            API + f"/ex/jira/{cloud}/rest/api/3/search/jql",
            json={
                "jql": "project = ENG AND status != Done ORDER BY updated DESC",
                "fields": ["summary", "description", "status"],
                "maxResults": 2,
            },
        )
        search.raise_for_status()
        assert search.json()["issues"][0]["fields"]["description"]["type"] == "doc"
        assert search.json()["isLast"] is False
        one = search.json()["issues"][0]
        client.get(one["self"]).raise_for_status()


def test_live_private_content_and_space_property_admin_contract(emulator_intercept: object) -> None:
    with httpx.Client(timeout=30) as client:
        credentials = client.get(SITE + "/_emulator/info").json()["credentials"]
        client.auth = (credentials["email"], credentials["api_token"])
        kb = client.get(SITE + "/wiki/api/v2/spaces?keys=KB").json()["results"][0]
        page = client.post(
            SITE + "/wiki/api/v2/pages?private=true",
            json={
                "spaceId": kb["id"],
                "title": "Private HTTP audit",
                "body": {
                    "representation": "storage",
                    "value": "<p>Confidential guidance.</p>",
                },
            },
        )
        page.raise_for_status()
        client.auth = ("maya.chen@halcyonadvisory.example", credentials["api_token"])
        assert client.get(SITE + "/wiki/api/v2/pages/" + page.json()["id"]).status_code == 404
        # Discover the administrator by the seeded space's author, rather than assuming roles.
        administrator = client.post(SITE + "/_emulator/tokens", json={"user": kb["authorId"]})
        administrator.raise_for_status()
        client.auth = None
        client.headers["Authorization"] = "Bearer " + administrator.json()["access_token"]
        cloud = credentials["cloud_id"]
        path = API + f"/ex/confluence/{cloud}/wiki/api/v2/spaces/{kb['id']}/properties"
        prop = client.post(path, json={"key": "http.audit", "value": {"ok": True}})
        assert prop.status_code == 201
        assert prop.json()["createdBy"] == kb["authorId"] and "authorId" not in prop.json()["version"]
        assert prop.headers["location"].startswith(path + "/")
        assert client.get(prop.headers["location"]).json()["value"] == {"ok": True}
