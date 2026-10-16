"""Regression tests from the fidelity audit: headers, CORS, input hardening, search, seed integrity."""

from __future__ import annotations

import re
from datetime import datetime

import httpx
import pytest
from fastapi import FastAPI, Request

from emulators.providers.box import db

MAYA = "34654717680"
ARJUN = "33529030912"


# -- headers, CORS and the 500 safety net ---------------------------------------------------------


async def test_every_response_carries_box_request_id_matching_the_error_body(box):
    ok = await box.get("/users/me")
    assert re.fullmatch(r"[0-9a-f]{16}", ok.headers["box-request-id"])
    missing = await box.get("/folders/123456789")
    assert missing.status_code == 404
    assert missing.headers["box-request-id"] == missing.json()["request_id"]
    unauthorized = await box.client.get("https://api.box.com/2.0/users/me")
    assert unauthorized.status_code == 401
    assert unauthorized.headers["box-request-id"] == unauthorized.json()["request_id"]
    token = await box.client.post("https://api.box.com/oauth2/token", data={"grant_type": "nope"})
    assert token.status_code == 400 and "box-request-id" in token.headers
    ids = {ok.headers["box-request-id"], missing.headers["box-request-id"]}
    assert len(ids) == 2


async def test_injected_faults_are_stamped_with_a_request_id(box):
    r = await box.client.post(
        "https://api.box.com/_emulator/faults", json={"status": 429, "path": "/2.0/users/me", "times": 1}
    )
    assert r.status_code == 200
    limited = await box.get("/users/me")
    assert limited.status_code == 429 and limited.headers["retry-after"]
    assert limited.headers["box-request-id"] == limited.json()["request_id"]
    assert limited.json()["code"] == "rate_limit_exceeded"


async def test_cors_preflight_and_origin_headers(box):
    preflight = await box.client.request(
        "OPTIONS",
        "https://api.box.com/2.0/folders/0/items",
        headers={
            "Origin": "https://app.example.test",
            "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "authorization,boxapi",
        },
    )
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == "*"
    assert preflight.headers["access-control-allow-headers"] == "authorization,boxapi"
    assert "DELETE" in preflight.headers["access-control-allow-methods"]
    plain = await box.get("/users/me", headers={"Origin": "https://app.example.test"})
    assert plain.headers["access-control-allow-origin"] == "*"
    assert "box-request-id" in plain.headers["access-control-expose-headers"].lower()
    assert "access-control-allow-origin" not in (await box.get("/users/me")).headers
    # Box's own upload pre-flight is also an OPTIONS request and must keep working.
    parent = await box.find("Technology")
    check = await box.request(
        "OPTIONS", "/files/content", json={"name": "cors-preflight.txt", "parent": {"id": parent}, "size": 1}
    )
    assert check.status_code == 200 and check.json()["upload_url"].endswith("/files/content")


async def test_unexpected_errors_are_box_shaped_json_not_a_text_500(box, monkeypatch):
    from emulators.providers.box.shapes import Shaper

    def boom(self, *args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(Shaper, "user", boom)
    r = await box.get("/users/me")
    assert r.status_code == 500
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["type"] == "error" and body["code"] == "internal_server_error"
    assert body["request_id"] == r.headers["box-request-id"]


# -- malformed input never crashes ----------------------------------------------------------------


@pytest.mark.parametrize("value", [5, True, "x", {"a": 1}, [1, 2], [None]])
async def test_tags_must_be_a_list_of_strings(box, value):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Tags")
    uploaded = (await box.upload(folder, "t.txt", b"t")).json()["entries"][0]["id"]
    assert (await box.put(f"/files/{uploaded}", json={"tags": value})).status_code == 400
    assert (await box.put(f"/folders/{folder}", json={"tags": value})).status_code == 400
    ok = await box.put(f"/files/{uploaded}", json={"tags": ["a", "b"]}, params={"fields": "tags"})
    assert ok.json()["tags"] == ["a", "b"]
    assert (await box.put(f"/files/{uploaded}", json={"tags": None}, params={"fields": "tags"})).json()[
        "tags"
    ] == []


async def test_object_valued_fields_reject_other_json_types(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Shapes")
    fid = (await box.upload(folder, "s.txt", b"s")).json()["entries"][0]["id"]
    for bad in (5, "x", True, [1]):
        assert (await box.put(f"/files/{fid}", json={"permissions": bad})).status_code == 400
        assert (await box.put(f"/files/{fid}/watermark", json={"watermark": bad})).status_code == 400
        assert (await box.put(f"/folders/{folder}/watermark", json={"watermark": bad})).status_code == 400
        assert (
            await box.post("/invites", json={"enterprise": {"id": "336533359"}, "actionable_by": bad})
        ).status_code == 400
    assert (
        await box.put(f"/files/{fid}", json={"permissions": {"can_download": "everyone"}})
    ).status_code == 400
    assert (await box.put(f"/files/{fid}", json={"lock": {"access": "unlock"}})).status_code == 400
    assert (await box.put(f"/folders/{folder}", json={"sync_state": "maybe"})).status_code == 400
    assert (await box.put(f"/folders/{folder}", json={"can_non_owners_invite": "no"})).status_code == 400
    assert (await box.put(f"/folders/{folder}", json={"description": ["not text"]})).status_code == 400


async def test_metadata_and_template_patches_reject_bad_bodies(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Meta bad")
    fid = (await box.upload(folder, "m.txt", b"m")).json()["entries"][0]["id"]
    created = await box.post(
        f"/files/{fid}/metadata/enterprise/clientInfo",
        json={"clientName": "Acme", "engagementType": "Audit", "fiscalYear": 2026},
    )
    assert created.status_code == 201, created.text
    url = f"/files/{fid}/metadata/enterprise/clientInfo"
    for raw in (b"", b"{", b"\xff\xfe", b"[1, 2]", b"[null]", b'{"op": "add"}'):
        r = await box.put(url, content=raw, headers={"Content-Type": "application/json-patch+json"})
        assert r.status_code == 400, (raw, r.text)
    # enum and multi-select values must be hashable strings, not arbitrary JSON
    for value in ([], {}, [1], {"a": 1}, 5, None):
        r = await box.put(url, json=[{"op": "replace", "path": "/engagementType", "value": value}])
        assert r.status_code == 400, (value, r.text)
    schema = "/metadata_templates/enterprise/clientInfo/schema"
    for raw in (b"", b"{", b"\xff\xfe", b"[1, 2]", b"[null]"):
        r = await box.put(schema, content=raw, headers={"Content-Type": "application/json"})
        assert r.status_code == 400, (raw, r.text)
    for op in (
        {"op": "addField", "data": 5},
        {"op": "addField", "data": {"type": "enum", "key": "k", "options": [1]}},
        {"op": "editField", "fieldKey": "clientName", "data": {"displayName": 5}},
        {"op": "editField", "fieldKey": "clientName", "data": {"key": "not valid!"}},
        {"op": "reorderFields", "fieldKeys": 5},
        {"op": "addEnumOption", "fieldKey": "engagementType", "data": {"key": 5}},
        {"op": "addEnumOption", "fieldKey": "engagementType", "data": {"key": "Audit"}},
        {"op": "editTemplate", "data": {"displayName": []}},
    ):
        r = await box.put(schema, json=[op])
        assert r.status_code in (400, 409), (op, r.text)
    bad_template = await box.post("/metadata_templates/schema", json={"displayName": "Bad", "fields": [5]})
    assert bad_template.status_code == 400
    bad_options = await box.post(
        "/metadata_templates/schema",
        json={"displayName": "Bad", "fields": [{"type": "enum", "key": "k", "options": [None]}]},
    )
    assert bad_options.status_code == 400


async def test_search_parameters_are_validated(box):
    for params in (
        {"size_range": "abc"},
        {"size_range": "1"},
        {"size_range": "-5,10"},
        {"mdfilters": "{}"},
        {"mdfilters": "[1]"},
        {"mdfilters": "-1"},
        {"mdfilters": "true"},
        {"mdfilters": '[{"scope": "enterprise"}]'},
        {"mdfilters": '[{"templateKey": "clientInfo", "filters": 5}]'},
    ):
        r = await box.get("/search", params={"query": "letter", **params})
        assert r.status_code == 400, (params, r.text)
    ok = await box.get("/search", params={"query": "letter", "size_range": ",100000", "fields": "size"})
    assert ok.status_code == 200 and all(e["size"] <= 100_000 for e in ok.json()["entries"] if "size" in e)
    types = await box.get(
        "/search",
        params={
            "query": "letter",
            "mdfilters": '[{"scope": "enterprise", "templateKey": "clientInfo", "filters": {"fiscalYear": {"gt": "x"}}}]',
        },
    )
    assert types.status_code == 200 and types.json()["total_count"] == 0


async def test_search_ancestor_folder_zero_means_the_whole_account_and_excludes_the_folder_itself(box):
    everywhere = await box.get("/search", params={"query": "letter", "limit": 200})
    under_root = await box.get(
        "/search", params={"query": "letter", "ancestor_folder_ids": "0", "limit": 200}
    )
    assert everywhere.json()["total_count"] > 0
    assert under_root.json()["total_count"] == everywhere.json()["total_count"]
    clients = await box.find("Client Engagements")
    inside = await box.get(
        "/search", params={"query": "client engagements", "type": "folder", "ancestor_folder_ids": clients}
    )
    assert clients not in [e["id"] for e in inside.json()["entries"]]
    mixed = await box.get(
        "/search", params={"query": "letter", "ancestor_folder_ids": f"{clients},0", "limit": 200}
    )
    assert mixed.json()["total_count"] == everywhere.json()["total_count"]


async def test_search_file_extensions_ignore_names_without_a_dot(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Ext")
    assert (await box.upload(folder, "xlsx", b"no extension")).status_code == 201
    hits = await box.get(
        "/search", params={"query": "xlsx", "file_extensions": "xlsx", "fields": "name", "limit": 200}
    )
    names = [e["name"] for e in hits.json()["entries"]]
    assert names and "xlsx" not in names
    assert all(n.endswith(".xlsx") for n in names)


async def test_metadata_query_validates_its_body(box):
    clients = await box.find("Client Engagements")
    base = {"from": "enterprise_336533359.clientInfo", "ancestor_folder_id": clients}
    for extra in (
        {"query_params": []},
        {"query": 5},
        {"order_by": 5},
        {"order_by": [5]},
        {"order_by": [{"direction": "asc"}]},
        {"fields": "name"},
        {"fields": [1]},
        {"marker": 5},
        {"limit": True},
        {"limit": 101},
        {"limit": -1},
        {"order_by": [{"field_key": "fiscalYear", "direction": "sideways"}]},
        {
            "order_by": [
                {"field_key": "fiscalYear", "direction": "asc"},
                {"field_key": "clientName", "direction": "desc"},
            ]
        },
    ):
        r = await box.post("/metadata_queries/execute_read", json={**base, **extra})
        assert r.status_code == 400, (extra, r.text)
    ok = await box.post(
        "/metadata_queries/execute_read",
        json={
            **base,
            "query": "fiscalYear >= :y",
            "query_params": {"y": 2000},
            "order_by": [{"field_key": "fiscalYear", "direction": "desc"}],
            "limit": 5,
        },
    )
    assert ok.status_code == 200 and 0 < len(ok.json()["entries"]) <= 5


async def test_zip_upload_part_and_stream_parameters_are_validated(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Odd inputs")
    fid = (await box.upload(folder, "z.txt", b"z")).json()["entries"][0]["id"]
    for items in ([5], ["x"], [{}], [{"type": "user", "id": "1"}], [{"type": "file"}], "x"):
        assert (await box.post("/zip_downloads", json={"items": items})).status_code == 400
    assert (
        await box.post(
            "/zip_downloads", json={"items": [{"type": "file", "id": fid}], "download_file_name": 5}
        )
    ).status_code == 400
    session = await box.client.post(
        "https://upload.box.com/api/2.0/files/upload_sessions",
        headers=box.auth(),
        json={"folder_id": folder, "file_size": 5, "file_name": "five.bin"},
    )
    sid = session.json()["id"]
    parts_url = f"https://upload.box.com/api/2.0/files/upload_sessions/{sid}/parts"
    for params in ({"offset": "x"}, {"limit": "x"}, {"offset": -1}, {"limit": 1001}):
        assert (await box.client.get(parts_url, headers=box.auth(), params=params)).status_code == 400
    commit = await box.client.post(
        f"https://upload.box.com/api/2.0/files/upload_sessions/{sid}/commit",
        headers={**box.auth(), "Digest": "sha=AAAAAAAAAAAAAAAAAAAAAAAAAAA="},
        json={"parts": [5, None, "x"]},
    )
    assert commit.status_code == 400
    assert (await box.request("OPTIONS", "/events", params={"stream_type": "bogus"})).status_code == 400
    # long polling serves user events only, never the enterprise stream
    assert (await box.request("OPTIONS", "/events", params={"stream_type": "admin_logs"})).status_code == 400
    assert (
        await box.request("OPTIONS", "/events", "p018", params={"stream_type": "admin_logs"})
    ).status_code == 400
    assert (await box.request("OPTIONS", "/events", params={"stream_type": "changes"})).status_code == 200


# -- users, groups and collaborations -------------------------------------------------------------


async def test_user_creation_and_update_validate_types(box):
    base = {"name": "Audit User", "login": "audit.user@example.test"}
    for extra in (
        {"login": "not-an-email"},
        {"login": 5},
        {"name": ""},
        {"name": 5},
        {"job_title": []},
        {"status": "retired"},
        {"is_platform_access_only": "yes"},
        {"space_amount": "lots"},
        {"can_see_managed_users": "yes"},
        {"tracking_codes": "x"},
        {"tracking_codes": [1]},
    ):
        r = await box.post("/users", json={**base, **extra})
        assert r.status_code == 400, (extra, r.text)
    created = await box.post("/users", json={**base, "job_title": "Auditor", "tracking_codes": []})
    assert created.status_code == 201 and created.json()["job_title"] == "Auditor"
    uid = created.json()["id"]
    for update in (
        {"login": None},
        {"login": 5},
        {"login": "nope"},
        {"notification_email": 5},
        {"notification_email": "x"},
        {"notification_email": {"email": "nope"}},
        {"phone": 5},
        {"language": []},
        {"status": 5},
        {"space_amount": "x"},
    ):
        assert (await box.put(f"/users/{uid}", json=update)).status_code == 400, update
    ok = await box.put(
        f"/users/{uid}",
        json={"notification_email": {"email": "alerts@example.test"}, "phone": "+15555550100"},
    )
    assert ok.status_code == 200 and ok.json()["phone"] == "+15555550100"
    me = await box.put("/users/me", json={"login": None})
    assert me.status_code in (400, 404)


async def test_app_users_report_platform_access_only(box):
    created = await box.post("/users", json={"name": "Automation", "is_platform_access_only": True})
    assert created.status_code == 201
    assert created.json()["is_platform_access_only"] is True
    assert created.json()["login"].startswith("AppUser_")
    managed = await box.post("/users", json={"name": "Person", "login": "person.one@example.test"})
    assert managed.json()["is_platform_access_only"] is False


async def test_group_fields_are_validated(box):
    for body in (
        {"name": ""},
        {"name": 5},
        {"name": "G", "description": 5},
        {"name": "G", "description": ["not", "text"]},
        {"name": "G", "provenance": []},
        {"name": "G", "invitability_level": "everyone"},
    ):
        assert (await box.post("/groups", json=body)).status_code == 400, body
    gid = (await box.post("/groups", json={"name": "Audit Group"})).json()["id"]
    assert (await box.put(f"/groups/{gid}", json={"name": 5})).status_code == 400
    assert (await box.put(f"/groups/{gid}", json={"external_sync_identifier": 5})).status_code == 400
    groups = await box.get("/groups", params={"limit": 1000})
    assert groups.status_code == 200 and any(g["name"] == "Audit Group" for g in groups.json()["entries"])


async def test_comment_task_and_web_link_text_fields_are_strings(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Text fields")
    fid = (await box.upload(folder, "n.txt", b"n")).json()["entries"][0]["id"]
    item = {"type": "file", "id": fid}
    for message in (5, [], {"a": 1}, True):
        assert (await box.post("/comments", json={"item": item, "message": message})).status_code == 400
        assert (await box.post("/tasks", json={"item": item, "message": message})).status_code == 400
    comment = (await box.post("/comments", json={"item": item, "message": "findable words"})).json()
    assert (await box.put(f"/comments/{comment['id']}", json={"message": 5})).status_code == 400
    searched = await box.get("/search", params={"query": "findable", "content_types": "comments"})
    assert searched.status_code == 200 and fid in [e["id"] for e in searched.json()["entries"]]
    task = (await box.post("/tasks", json={"item": item, "message": "do it"})).json()
    assert (await box.put(f"/tasks/{task['id']}", json={"message": []})).status_code == 400
    assert (await box.put(f"/tasks/{task['id']}", json={"completion_rule": "everyone"})).status_code == 400
    assignment = await box.post(
        "/task_assignments", json={"task": {"id": task["id"], "type": "task"}, "assign_to": {"id": MAYA}}
    )
    assert assignment.status_code == 201
    assert (
        await box.post("/task_assignments", json={"task": {"id": task["id"]}, "assign_to": {"login": 5}})
    ).status_code == 400
    assert (
        await box.put(f"/task_assignments/{assignment.json()['id']}", "p018", json={"message": 5})
    ).status_code == 400
    link = (
        await box.post("/web_links", json={"url": "https://example.test/x", "parent": {"id": folder}})
    ).json()
    assert (await box.put(f"/web_links/{link['id']}", json={"url": "ftp://example.test"})).status_code == 400
    assert (await box.put(f"/web_links/{link['id']}", json={"url": 5})).status_code == 400
    assert (await box.put(f"/web_links/{link['id']}", json={"description": []})).status_code == 400
    assert (
        await box.post(
            "/web_links", json={"url": "https://example.test/y", "parent": {"id": folder}, "description": 5}
        )
    ).status_code == 400


async def test_collaboration_by_login_needs_an_email_and_respects_folder_policies(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Policies")
    item = {"type": "folder", "id": folder}
    for login in ("x", 5, "", "a@b", "two words@example.test"):
        r = await box.post(
            "/collaborations",
            json={"item": item, "accessible_by": {"type": "user", "login": login}, "role": "viewer"},
        )
        assert r.status_code == 400, (login, r.text)
    assert (
        await box.post(
            "/collaborations", json={"item": item, "accessible_by": {"type": "user", "id": MAYA}, "role": []}
        )
    ).status_code == 400
    # An editor may invite until the owner sets can_non_owners_invite=false.
    edit = await box.post(
        "/collaborations",
        json={"item": item, "accessible_by": {"type": "user", "id": MAYA}, "role": "editor"},
    )
    assert edit.status_code == 201
    invite = {"item": item, "accessible_by": {"type": "user", "id": "33724305150"}, "role": "viewer"}
    assert (await box.get(f"/folders/{folder}", "p018", params={"fields": "permissions"})).json()[
        "permissions"
    ]["can_invite_collaborator"] is True
    assert (await box.put(f"/folders/{folder}", json={"can_non_owners_invite": False})).status_code == 200
    seen = await box.get(f"/folders/{folder}", "p018", params={"fields": "permissions,allowed_invitee_roles"})
    assert seen.json()["permissions"]["can_invite_collaborator"] is False
    assert seen.json()["allowed_invitee_roles"] == []
    assert (await box.post("/collaborations", "p018", json=invite)).status_code == 403
    assert (await box.post("/collaborations", "p035", json=invite)).status_code == 201  # owners still can
    # Restricting collaboration to the enterprise stops external invitations, not internal ones.
    assert (
        await box.put(f"/folders/{folder}", json={"is_collaboration_restricted_to_enterprise": True})
    ).status_code == 200
    external = {
        "item": item,
        "accessible_by": {"type": "user", "login": "outsider@example.test"},
        "role": "viewer",
    }
    assert (await box.post("/collaborations", json=external)).status_code == 403
    sub = await box.mkdir(folder, "child")
    child = {**external, "item": {"type": "folder", "id": sub}}
    assert (await box.post("/collaborations", json=child)).status_code == 403
    assert (
        await box.put(f"/folders/{folder}", json={"is_collaboration_restricted_to_enterprise": False})
    ).status_code == 200
    assert (await box.post("/collaborations", json=external)).status_code == 201


# -- webhooks -------------------------------------------------------------------------------------


async def test_delivery_timestamp_uses_boxs_iso_offset_format(box):
    got: list[dict[str, str]] = []
    receiver = FastAPI()

    @receiver.post("/hook")
    async def hook(request: Request) -> dict:
        got.append(dict(request.headers))
        return {}

    box.hub.webhook_transport = httpx.ASGITransport(app=receiver)
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Stamp")
    created = await box.post(
        "/webhooks",
        json={
            "target": {"id": folder, "type": "folder"},
            "address": "https://hooks.example.test/hook",
            "triggers": ["FILE.UPLOADED"],
        },
    )
    assert created.status_code == 201
    await box.upload(folder, "stamp.txt", b"s")
    assert got, "no delivery"
    stamp = got[0]["box-delivery-timestamp"]
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", stamp), stamp
    assert datetime.fromisoformat(stamp).utcoffset() is not None


# -- seed integrity -------------------------------------------------------------------------------


def seeded(box):
    return box.hub.contexts["box"]


def test_seeded_accounts_exist_before_their_activity(box):
    ctx = seeded(box)
    users = {u["id"]: u for u in db.users(ctx)}
    for f in db.files(ctx):
        assert users[f["created_by"]]["created_at"] <= f["created_at"], f["name"]
        assert users[f["modified_by"]]["created_at"] <= f["modified_at"], f["name"]
        assert f["created_at"] <= f["modified_at"], f["name"]
    for folder in db.folders(ctx):
        if db.is_root(folder):
            continue
        assert users[folder["created_by"]]["created_at"] <= folder["created_at"], folder["name"]
        assert folder["created_at"] <= folder["modified_at"], folder["name"]
        assert users[folder["modified_by"]]["created_at"] <= folder["modified_at"], folder["name"]
    for c in db.collabs(ctx):
        item = db.item(ctx, c["item_type"], c["item_id"])
        assert item["created_at"] <= c["created_at"], (item["name"], c["id"])
        assert users[c["created_by"]]["created_at"] <= c["created_at"]
        if c["accessible_type"] == "user":
            assert users[c["accessible_id"]]["created_at"] <= c["created_at"], c["id"]
        else:
            assert db.groups(ctx).get(c["accessible_id"])["created_at"] <= c["created_at"], c["id"]
    for m in db.memberships(ctx):
        assert users[m["user_id"]]["created_at"] <= m["created_at"]
        assert db.groups(ctx).get(m["group_id"])["created_at"] <= m["created_at"]


def test_folders_are_never_newer_than_what_they_contain(box):
    ctx = seeded(box)
    folders = {f["id"]: f for f in db.folders(ctx)}
    for record in [*db.files(ctx), *(f for f in folders.values() if not db.is_root(f))]:
        parent = folders.get(record["_parent"])
        if parent is None or db.is_root(parent):
            continue
        assert parent["created_at"] <= record["created_at"], (parent["name"], record["name"])
        assert parent["modified_at"] >= min(record["modified_at"], parent["modified_at"])


def test_service_account_predates_its_collaborations(box):
    ctx = seeded(box)
    service = next(u for u in db.users(ctx) if u["_kind"] == "service")
    grants = [c for c in db.collabs(ctx) if c["accessible_id"] == service["id"]]
    assert grants and all(service["created_at"] <= c["created_at"] for c in grants)
    assert service["created_at"] > "2020"  # created when the integration was authorised, not at founding
