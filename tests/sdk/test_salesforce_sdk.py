"""simple-salesforce (the official community SDK) against the emulator, unmodified.

Run with ``EMULATORS_PROVIDERS=salesforce uv run pytest tests/sdk/test_salesforce_sdk.py -q``.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from simple_salesforce import Salesforce
from simple_salesforce.exceptions import (
    SalesforceAuthenticationFailed,
    SalesforceExpiredSession,
    SalesforceMalformedRequest,
    SalesforceRefusedRequest,
    SalesforceResourceNotFound,
)

pytestmark = pytest.mark.sdk

INSTANCE = "https://halcyonadvisory.my.salesforce.com"
ADMIN = "arjun.mehta@halcyonadvisory.example.crm"
PASSWORD = "Halcyon2026!"
TOKEN = "EMULATORTOKEN"
WAIT = 0.01  # bulk 2.0 poll interval (the SDK sleeps this long before the first status check)


@pytest.fixture
def sf(emulator_intercept: object) -> Salesforce:
    """Logged in through the SOAP partner login with username + password + security token."""
    return Salesforce(username=ADMIN, password=PASSWORD, security_token=TOKEN)


def minted(person: str) -> str:
    r = requests.post(f"{INSTANCE}/_emulator/actions/mint_token", json={"user": person}, timeout=10)
    r.raise_for_status()
    return str(r.json()["access_token"])


def test_soap_login_populates_session_and_instance(sf: Salesforce) -> None:
    assert sf.session_id.startswith("00D5g0000049673!")
    assert sf.sf_instance == "halcyonadvisory.my.salesforce.com"
    assert sf.base_url == "https://halcyonadvisory.my.salesforce.com/services/data/v59.0/"
    assert sf.is_sandbox() is False


def test_soap_login_failures_raise_authentication_failed(emulator_intercept: object) -> None:
    with pytest.raises(SalesforceAuthenticationFailed) as bad_password:
        Salesforce(username=ADMIN, password="wrong", security_token=TOKEN)
    assert "INVALID_LOGIN" in str(bad_password.value)
    with pytest.raises(SalesforceAuthenticationFailed):
        Salesforce(username="nobody@halcyonadvisory.example.crm", password=PASSWORD, security_token=TOKEN)
    with pytest.raises(SalesforceAuthenticationFailed):  # departed employee
        Salesforce(
            username="paul.henderson@halcyonadvisory.example.crm", password=PASSWORD, security_token=TOKEN
        )


def test_session_id_and_instance_url_path(emulator_intercept: object) -> None:
    client = Salesforce(instance_url=INSTANCE, session_id=minted("p018"))
    me = client.query(
        "SELECT Id, Name, Profile.Name, UserRole.Name FROM User WHERE Username LIKE 'maya.chen%'"
    )
    assert me["totalSize"] == 1 and me["records"][0]["Name"] == "Maya Chen"
    assert me["records"][0]["UserRole"]["Name"] == "Advisory Partner"
    expired = Salesforce(instance_url=INSTANCE, session_id="00D5g0000049673!AQnotarealtoken")
    with pytest.raises(SalesforceExpiredSession) as err:
        expired.query("SELECT Id FROM Account")
    assert err.value.content[0]["errorCode"] == "INVALID_SESSION_ID"


def test_oauth_username_password_flow(emulator_intercept: object) -> None:
    client = Salesforce(
        username=ADMIN, password=PASSWORD + TOKEN, consumer_key="3MVG9-key", consumer_secret="secret"
    )
    assert (
        client.session_id.startswith("00D5g0000049673!")
        and client.sf_instance == "halcyonadvisory.my.salesforce.com"
    )
    assert client.query("SELECT COUNT() FROM Account")["totalSize"] == 41


def test_oauth_client_credentials_flow(emulator_intercept: object) -> None:
    client = Salesforce(consumer_key="3MVG9-key", consumer_secret="secret", domain="halcyonadvisory.my")
    assert client.sf_instance == "halcyonadvisory.my.salesforce.com"
    assert client.query("SELECT COUNT() FROM Lead")["totalSize"] == 18  # the integration user sees everything
    assert client.is_sandbox() is False


def test_oauth_jwt_bearer_flow(emulator_intercept: object) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    client = Salesforce(username=ADMIN, consumer_key="3MVG9-jwt", privatekey=pem)
    assert client.sf_instance == "halcyonadvisory.my.salesforce.com"
    assert (
        client.query("SELECT Name FROM User WHERE Username = '" + ADMIN + "'")["records"][0]["Name"]
        == "Arjun Mehta"
    )
    with pytest.raises(SalesforceAuthenticationFailed):
        Salesforce(username="ghost@halcyonadvisory.example.crm", consumer_key="3MVG9-jwt", privatekey=pem)


def test_sobject_crud_cycle(sf: Salesforce) -> None:
    created = sf.Account.create({"Name": "SDK Corp", "Industry": "Technology", "AnnualRevenue": 5_000_000})
    assert created["success"] is True and created["errors"] == [] and created["id"].startswith("001")
    rid = created["id"]
    record = sf.Account.get(rid)
    assert record["Name"] == "SDK Corp" and record["attributes"]["type"] == "Account"
    assert record["attributes"]["url"] == f"/services/data/v59.0/sobjects/Account/{rid}"
    assert sf.Account.update(rid, {"Rating": "Hot"}) == 204
    assert sf.Account.get(rid)["Rating"] == "Hot"
    assert sf.Account.delete(rid) == 204
    with pytest.raises(SalesforceResourceNotFound) as gone:
        sf.Account.get(rid)
    assert gone.value.content == [
        {"message": "The requested resource does not exist", "errorCode": "NOT_FOUND"}
    ]
    assert gone.value.status == 404


def test_error_classes_carry_salesforce_error_arrays(sf: Salesforce) -> None:
    with pytest.raises(SalesforceMalformedRequest) as missing:
        sf.Account.create({})
    assert missing.value.content == [
        {
            "message": "Required fields are missing: [Name]",
            "errorCode": "REQUIRED_FIELD_MISSING",
            "fields": ["Name"],
        }
    ]
    with pytest.raises(SalesforceMalformedRequest) as bad_query:
        sf.query("SELECT Nope FROM Account")
    assert (
        bad_query.value.content[0]["errorCode"] == "INVALID_FIELD"
        and "No such column 'Nope'" in bad_query.value.content[0]["message"]
    )
    with pytest.raises(SalesforceMalformedRequest) as bad_type:
        sf.query("SELECT Id FROM Widget__c")
    assert bad_type.value.content[0]["errorCode"] == "INVALID_TYPE"
    with pytest.raises(SalesforceResourceNotFound):
        sf.Widget.describe()
    jasmine = Salesforce(instance_url=INSTANCE, session_id=minted("p029"))  # Read Only profile
    with pytest.raises(SalesforceRefusedRequest) as refused:
        jasmine.Account.create({"Name": "Nope"})
    assert refused.value.content[0]["errorCode"] == "INSUFFICIENT_ACCESS_OR_READONLY"


def test_external_id_upsert_and_get_by_custom_id(sf: Salesforce) -> None:
    assert sf.Account.upsert("QuickBooks_Customer_Id__c/QB-SDK-1", {"Name": "Upserted via SDK"}) == 201
    assert sf.Account.upsert("QuickBooks_Customer_Id__c/QB-SDK-1", {"Rating": "Warm"}) == 200
    got = sf.Account.get_by_custom_id("QuickBooks_Customer_Id__c", "QB-SDK-1")
    assert got["Name"] == "Upserted via SDK" and got["Rating"] == "Warm"
    raw = sf.Account.upsert("QuickBooks_Customer_Id__c/QB-SDK-2", {"Name": "Second"}, raw_response=True)
    assert raw.status_code == 201 and raw.json()["success"] is True
    with pytest.raises(SalesforceResourceNotFound):
        sf.Account.get_by_custom_id("QuickBooks_Customer_Id__c", "MISSING")
    assert sf.Engagement__c.get_by_custom_id("Engagement_Code__c", "e001")["Name"].endswith("FY2025 Review")


def test_describe_global_describe_and_metadata(sf: Salesforce) -> None:
    names = {s["name"] for s in sf.describe()["sobjects"]}
    assert {"Account", "Contact", "Engagement__c"} <= names
    account = sf.Account.describe()
    assert account["keyPrefix"] == "001" and {f["name"] for f in account["fields"]} >= {
        "Id",
        "Name",
        "BillingAddress",
    }
    custom = sf.Engagement__c.describe()
    assert custom["custom"] is True and custom["keyPrefix"] == "a0X"
    meta = sf.Account.metadata()
    assert meta["objectDescribe"]["name"] == "Account" and isinstance(meta["recentItems"], list)
    layout = sf.Account.describe_layout("012000000000000AAA")
    assert layout["detailLayoutSections"]


def test_query_pagination_query_more_and_query_all(sf: Salesforce) -> None:
    headers = {"Sforce-Query-Options": "batchSize=200"}
    first = sf.query("SELECT Id FROM Task ORDER BY Id", headers=headers)
    assert first["totalSize"] == 316 and first["done"] is False and len(first["records"]) == 200
    second = sf.query_more(first["nextRecordsUrl"], identifier_is_url=True)
    assert second["done"] is True and len(second["records"]) == 116
    by_id = sf.query_more(first["nextRecordsUrl"].rsplit("/", 1)[1], identifier_is_url=False)
    assert by_id["records"][0]["Id"] == second["records"][0]["Id"]
    everything = sf.query_all("SELECT Id FROM Task", headers=headers)
    assert everything["totalSize"] == 316 and everything["done"] is True and len(everything["records"]) == 316
    streamed = list(sf.query_all_iter("SELECT Id FROM Task", headers=headers))
    assert len(streamed) == 316 and len({r["Id"] for r in streamed}) == 316


def test_deleted_records_with_include_deleted_and_changes(sf: Salesforce) -> None:
    rid = sf.Account.create({"Name": "To be deleted"})["id"]
    sf.Account.delete(rid)
    assert sf.query(f"SELECT Id FROM Account WHERE Id = '{rid}'")["totalSize"] == 0
    hit = sf.query(f"SELECT Id, IsDeleted FROM Account WHERE Id = '{rid}'", include_deleted=True)
    assert hit["records"][0]["IsDeleted"] is True
    from datetime import datetime, timedelta

    end = datetime.now(UTC)
    start = end - timedelta(hours=1)
    deleted = sf.Account.deleted(start, end)
    assert rid in [d["id"] for d in deleted["deletedRecords"]]
    updated = sf.Account.updated(start, end)
    assert isinstance(updated["ids"], list) and "latestDateCovered" in updated


def test_search_quick_search_and_limits(sf: Salesforce) -> None:
    hits = sf.search("FIND {Northwind} IN ALL FIELDS RETURNING Account(Id, Name)")["searchRecords"]
    assert [h["Name"] for h in hits] == ["Northwind Logistics"]
    quick = sf.quick_search("Northwind")["searchRecords"]
    assert quick and quick[0]["attributes"]["type"] == "Account"
    limits = sf.limits()
    assert limits["DailyApiRequests"]["Max"] == 15000
    sf.query("SELECT Id FROM Account LIMIT 1")
    assert sf.api_usage["api-usage"].total == 15000 and sf.api_usage["api-usage"].used >= 1


def test_restful_composite_and_collections(sf: Salesforce) -> None:
    result = sf.restful(
        "composite",
        method="POST",
        json={
            "allOrNone": True,
            "compositeRequest": [
                {
                    "method": "POST",
                    "url": "/services/data/v59.0/sobjects/Account",
                    "referenceId": "a",
                    "body": {"Name": "SDK composite"},
                },
                {
                    "method": "POST",
                    "url": "/services/data/v59.0/sobjects/Contact",
                    "referenceId": "c",
                    "body": {"LastName": "Tester", "AccountId": "@{a.id}"},
                },
            ],
        },
    )
    assert [r["httpStatusCode"] for r in result["compositeResponse"]] == [201, 201]
    batch = sf.restful(
        "composite/sobjects",
        method="POST",
        json={
            "allOrNone": False,
            "records": [
                {"attributes": {"type": "Account"}, "Name": "Collected 1"},
                {"attributes": {"type": "Account"}, "Name": "Collected 2"},
            ],
        },
    )
    assert all(b["success"] for b in batch)  # type: ignore[union-attr]
    tree = sf.restful(
        "composite/tree/Account",
        method="POST",
        json={"records": [{"attributes": {"type": "Account", "referenceId": "t"}, "Name": "Tree"}]},
    )
    assert tree["hasErrors"] is False  # type: ignore[index]
    assert sf.restful("sobjects/Account/describe")["name"] == "Account"  # type: ignore[index]
    assert (
        sf.restful("query", params={"q": "SELECT COUNT() FROM Contact WHERE LastName = 'Tester'"})[
            "totalSize"
        ]
        == 1
    )  # type: ignore[index]


def test_set_password_and_userinfo_oauth_endpoint(sf: Salesforce) -> None:
    user_id = sf.query("SELECT Id FROM User WHERE Username LIKE 'hannah.schmidt%'")["records"][0]["Id"]
    assert sf.set_password(user_id, "Brand-New-1") is None
    login = Salesforce(
        username="hannah.schmidt@halcyonadvisory.example.crm", password="Brand-New-1", security_token=TOKEN
    )
    assert login.query("SELECT COUNT() FROM Account")["totalSize"] == 41
    info = sf.oauth2("userinfo")
    assert info["preferred_username"] == ADMIN and info["organization_id"] == "00D5g0000049673EAA"


def test_apex_rest_and_bulk_v1_are_unavailable(sf: Salesforce) -> None:
    with pytest.raises(SalesforceResourceNotFound):
        sf.apexecute("Custom/Endpoint")
    with pytest.raises(Exception):  # noqa: B017 - the XML Bulk 1.0 API is a documented gap
        sf.bulk.Account.insert([{"Name": "x"}])


# -- Bulk API 2.0 --------------------------------------------------------------------------


def rows(*names: str) -> list[dict[str, str]]:
    return [{"Name": n, "Industry": "Retail"} for n in names]


def test_bulk2_insert_update_upsert_delete(sf: Salesforce) -> None:
    result = sf.bulk2.Account.insert(records=rows("Bulk SDK A", "Bulk SDK B", ""), wait=WAIT)[0]
    assert (
        result["numberRecordsProcessed"],
        result["numberRecordsFailed"],
        result["numberRecordsTotal"],
    ) == (3, 1, 3)
    ids = sf.query("SELECT Id, Name FROM Account WHERE Name LIKE 'Bulk SDK%' ORDER BY Name")["records"]
    assert [r["Name"] for r in ids] == ["Bulk SDK A", "Bulk SDK B"]
    update = sf.bulk2.Account.update(
        records=[{"Id": ids[0]["Id"], "Rating": "Hot"}, {"Id": ids[1]["Id"], "Rating": "Cold"}], wait=WAIT
    )[0]
    assert update["numberRecordsFailed"] == 0
    assert {
        r["Rating"] for r in sf.query("SELECT Rating FROM Account WHERE Name LIKE 'Bulk SDK%'")["records"]
    } == {"Hot", "Cold"}
    upsert = sf.bulk2.Account.upsert(
        records=[
            {"QuickBooks_Customer_Id__c": "BULK-SDK", "Name": "Bulk SDK Upserted"},
            {"QuickBooks_Customer_Id__c": "1", "Rating": "Warm"},
        ],
        external_id_field="QuickBooks_Customer_Id__c",
        wait=WAIT,
    )[0]
    assert upsert["numberRecordsFailed"] == 0
    assert sf.Account.get_by_custom_id("QuickBooks_Customer_Id__c", "BULK-SDK")["Name"] == "Bulk SDK Upserted"
    delete = sf.bulk2.Account.delete(records=[{"Id": r["Id"]} for r in ids], wait=WAIT)[0]
    assert delete["numberRecordsProcessed"] == 2 and delete["numberRecordsFailed"] == 0
    assert (
        sf.query("SELECT COUNT() FROM Account WHERE Name LIKE 'Bulk SDK%' AND Name != 'Bulk SDK Upserted'")[
            "totalSize"
        ]
        == 0
    )


def test_bulk2_job_results_and_query(sf: Salesforce) -> None:
    client = sf.bulk2.Account._client  # the low-level job client behind sf.bulk2.Account
    job = client.create_job("insert")
    client.upload_job_data(job["id"], "Name,Industry\nSDK Results A,Retail\n,Retail\n")
    client.close_job(job["id"])
    client.wait_for_job(job["id"], False, WAIT)
    ok = client.get_ingest_results(job["id"], "successfulResults")
    assert ok.splitlines()[0] == '"sf__Id","sf__Created","Name","Industry"'
    failed = client.get_ingest_results(job["id"], "failedResults")
    assert "REQUIRED_FIELD_MISSING" in failed
    chunks: list[Any] = list(
        sf.bulk2.Account.query(
            "SELECT Id, Name FROM Account WHERE Name LIKE 'SDK Results%'", max_records=10, wait=WAIT
        )
    )
    assert chunks and "SDK Results A" in chunks[0]
    everything = list(
        sf.bulk2.Account.query(
            "SELECT Id, Name, Owner.Name FROM Account ORDER BY Name", max_records=15, wait=WAIT
        )
    )
    assert len(everything) == 3  # 42 rows in chunks of at most 15
    assert everything[0].splitlines()[0] == '"Id","Name","Owner.Name"'
    total = sum(chunk.count("\n") - 1 for chunk in everything)
    assert total == 42


def test_bulk2_enforces_the_callers_sharing_and_permissions(emulator_intercept: object) -> None:
    rajesh = Salesforce(instance_url=INSTANCE, session_id=minted("p012"))
    chunks = list(rajesh.bulk2.Opportunity.query("SELECT Id FROM Opportunity", max_records=100, wait=WAIT))
    assert sum(c.count("\n") - 1 for c in chunks) == 5
    read_only = Salesforce(instance_url=INSTANCE, session_id=minted("p029"))
    result = read_only.bulk2.Account.insert(records=rows("Nope"), wait=WAIT)[0]
    assert result["numberRecordsFailed"] == 1


def test_files_upload_and_download_through_content_version(sf: Salesforce) -> None:
    import base64

    created = sf.ContentVersion.create(
        {
            "Title": "SDK file",
            "PathOnClient": "sdk.txt",
            "VersionData": base64.b64encode(b"sdk bytes").decode(),
        }
    )
    assert created["success"] is True and created["id"].startswith("068")
    assert sf.ContentVersion.get_base64(created["id"], "VersionData") == b"sdk bytes"
    row = sf.query(
        f"SELECT ContentDocumentId, ContentSize, FileType FROM ContentVersion WHERE Id = '{created['id']}'"
    )
    assert row["records"][0]["ContentSize"] == 9 and row["records"][0]["FileType"] == "TEXT"
    links = sf.query(
        f"SELECT LinkedEntityId FROM ContentDocumentLink WHERE ContentDocumentId = '{row['records'][0]['ContentDocumentId']}'"
    )
    assert links["totalSize"] == 1
    existing = sf.query("SELECT Id FROM ContentVersion WHERE FileExtension = 'pdf' LIMIT 1")["records"][0][
        "Id"
    ]
    assert sf.ContentVersion.get_base64(existing, "VersionData").startswith(b"%PDF")


def test_web_server_flow_with_a_generic_oauth2_client(emulator_intercept: object) -> None:
    """requests-oauthlib (what most Salesforce integrations use) can run the authorize + token + refresh dance."""
    pytest.importorskip("requests_oauthlib")
    from requests_oauthlib import OAuth2Session

    session = OAuth2Session(
        "3MVG9-client", redirect_uri="https://consumer.example/callback", scope=["api", "refresh_token"]
    )
    url, _state = session.authorization_url("https://login.salesforce.com/services/oauth2/authorize")
    redirect = requests.get(url, params={"login_hint": ADMIN}, allow_redirects=False, timeout=10)
    assert redirect.status_code == 302
    token = session.fetch_token(
        "https://login.salesforce.com/services/oauth2/token",
        authorization_response=redirect.headers["location"],
        client_secret="secret",
        include_client_id=True,
    )
    assert token["access_token"].startswith("00D5g0000049673!") and token["refresh_token"].startswith(
        "5Aep861"
    )
    assert token["instance_url"] == INSTANCE and token["id"].startswith("https://login.salesforce.com/id/")
    client = Salesforce(instance_url=token["instance_url"], session_id=token["access_token"])
    assert client.query("SELECT COUNT() FROM Account")["totalSize"] == 41
    refreshed = session.refresh_token(
        "https://login.salesforce.com/services/oauth2/token",
        refresh_token=token["refresh_token"],
        client_id="3MVG9-client",
        client_secret="secret",
    )
    assert refreshed["access_token"] != token["access_token"]
    identity = requests.get(
        token["id"], headers={"Authorization": f"Bearer {refreshed['access_token']}"}, timeout=10
    )
    assert identity.json()["username"] == ADMIN


def test_collection_retrieve_uses_salesforce_post_field_names(sf: Salesforce) -> None:
    rid = sf.query("SELECT Id FROM Account LIMIT 1")["records"][0]["Id"]
    result = sf.restful(
        "composite/sobjects/Account",
        method="POST",
        json={
            "recordIds": [rid] * 201,
            "fieldNames": ["Id", "Name"],
        },
    )
    assert len(result) == 201 and all(row["Id"] == rid for row in result)


def test_composite_collection_results_can_feed_sobject_requests(sf: Salesforce) -> None:
    result = sf.restful(
        "composite",
        method="POST",
        json={
            "allOrNone": True,
            "compositeRequest": [
                {
                    "method": "POST",
                    "url": "/services/data/v59.0/composite/sobjects",
                    "referenceId": "accounts",
                    "body": {
                        "records": [{"attributes": {"type": "Account"}, "Name": "SDK array reference"}],
                    },
                },
                {
                    "method": "POST",
                    "url": "/services/data/v59.0/sobjects/Contact",
                    "referenceId": "contact",
                    "body": {
                        "LastName": "Collection SDK",
                        "AccountId": "@{accounts[0].id}",
                    },
                },
            ],
        },
    )["compositeResponse"]
    assert [row["httpStatusCode"] for row in result] == [200, 201]
    assert sf.Contact.get(result[1]["body"]["id"])["AccountId"] == result[0]["body"][0]["id"]


def test_external_id_lookup_keeps_private_records_hidden(sf: Salesforce, emulator_intercept: object) -> None:
    rid = sf.Opportunity.create(
        {"Name": "SDK private alias", "StageName": "Prospecting", "CloseDate": "2026-11-01"}
    )["id"]
    staff = Salesforce(instance_url=INSTANCE, session_id=minted("p011"))
    with pytest.raises(SalesforceResourceNotFound):
        staff.Opportunity.get_by_custom_id("Id", rid)
    assert sf.Opportunity.get_by_custom_id("Id", rid)["Id"] == rid


def test_case_insensitive_18_character_ids_work_through_sdk(sf: Salesforce) -> None:
    rid = sf.Lead.create({"LastName": "Case safe SDK", "Company": "SDK Company"})["id"]
    assert sf.Lead.get(rid.lower())["Id"] == rid
    assert sf.Lead.update(rid.upper(), {"Title": "Updated"}) == 204
    assert sf.Lead.get(rid)["Title"] == "Updated"
