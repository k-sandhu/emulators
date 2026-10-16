"""Regression tests from the fidelity audit: event names, audit streams, e-mail aliases."""

from __future__ import annotations

import json
from datetime import timedelta

import httpx
from fastapi import FastAPI, Request

MAYA = "34654717680"
ARJUN = "33529030912"

REAL_EVENT_TYPES = {
    "ITEM_CREATE", "ITEM_UPLOAD", "ITEM_DOWNLOAD", "ITEM_COPY", "ITEM_MOVE", "ITEM_RENAME", "ITEM_TRASH",
    "ITEM_UNDELETE_VIA_TRASH", "ITEM_MAKE_CURRENT_VERSION", "ITEM_SHARED_CREATE", "ITEM_SHARED_UPDATE",
    "ITEM_SHARED_UNSHARE", "DELETE", "LOCK", "UNLOCK", "LOCK_CREATE", "LOCK_DESTROY", "COLLAB_ADD_COLLABORATOR",
    "COLLAB_INVITE_COLLABORATOR", "COLLAB_REMOVE_COLLABORATOR", "COLLAB_ROLE_CHANGE", "COLLABORATION_ACCEPT",
    "COMMENT_CREATE", "COMMENT_DELETE", "TASK_CREATE", "TASK_UPDATE", "TASK_ASSIGNMENT_CREATE",
    "TASK_ASSIGNMENT_UPDATE", "TASK_ASSIGNMENT_DELETE", "METADATA_INSTANCE_CREATE", "METADATA_INSTANCE_UPDATE",
    "METADATA_INSTANCE_DELETE", "METADATA_TEMPLATE_CREATE", "METADATA_TEMPLATE_UPDATE",
    "METADATA_TEMPLATE_DELETE", "WATERMARK_LABEL_CREATE", "WATERMARK_LABEL_DELETE", "NEW_USER", "EDIT_USER",
    "DELETE_USER", "GROUP_CREATION", "GROUP_DELETION", "GROUP_EDITED", "GROUP_ADD_USER", "GROUP_REMOVE_USER",
    "LOGIN", "USER_AUTHENTICATE_OAUTH2_ACCESS_TOKEN_CREATE",
}  # fmt: skip
#: The ``admin_logs`` stream names file and sharing activity the enterprise way.
ADMIN_FAMILY = {
    "UPLOAD", "DOWNLOAD", "COPY", "MOVE", "RENAME", "UNDELETE", "SHARE", "UNSHARE", "COLLABORATION_INVITE",
    "COLLABORATION_REMOVE", "COLLABORATION_ROLE_CHANGE",
}  # fmt: skip


async def stream_now(box, who="p035", **params) -> str:
    r = await box.get("/events", who, params={"stream_position": "now", **params})
    return r.json()["next_stream_position"]


async def test_event_types_are_the_names_box_documents(box):
    start = await stream_now(box, stream_type="admin_logs")
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Event names")
    fid = (await box.upload(folder, "e.txt", b"e")).json()["entries"][0]["id"]
    item = {"type": "file", "id": fid}
    await box.put(f"/files/{fid}", json={"lock": {"access": "lock"}})
    await box.put(f"/files/{fid}", json={"lock": None})
    comment = (await box.post("/comments", json={"item": item, "message": "c"})).json()["id"]
    await box.put(f"/comments/{comment}", json={"message": "edited"})
    await box.delete(f"/comments/{comment}")
    task = (await box.post("/tasks", json={"item": item, "message": "t"})).json()["id"]
    await box.put(f"/tasks/{task}", json={"message": "t2"})
    assignment = (
        await box.post(
            "/task_assignments", json={"task": {"id": task, "type": "task"}, "assign_to": {"id": MAYA}}
        )
    ).json()["id"]
    await box.delete(f"/task_assignments/{assignment}")
    lock = (await box.post("/folder_locks", json={"folder": {"id": folder, "type": "folder"}})).json()["id"]
    await box.delete(f"/folder_locks/{lock}")
    await box.put(f"/files/{fid}/watermark", json={"watermark": {"imprint": "default"}})
    await box.delete(f"/files/{fid}/watermark")
    collab = (
        await box.post(
            "/collaborations",
            json={
                "item": item,
                "accessible_by": {"type": "user", "login": "reject.me@example.test"},
                "role": "viewer",
            },
        )
    ).json()
    group = (await box.post("/groups", json={"name": "Renamed soon"})).json()["id"]
    await box.put(f"/groups/{group}", json={"name": "Renamed"})
    tpl = (await box.post("/metadata_templates/schema", json={"displayName": "Names"})).json()["templateKey"]
    await box.put(
        f"/metadata_templates/enterprise/{tpl}/schema",
        json=[{"op": "addField", "data": {"type": "string", "key": "f", "displayName": "F"}}],
    )
    await box.delete(f"/metadata_templates/enterprise/{tpl}/schema")
    await box.delete(f"/collaborations/{collab['id']}")
    await box.delete(f"/files/{fid}")
    await box.delete(f"/files/{fid}/trash")
    logs = (
        await box.get("/events", params={"stream_type": "admin_logs", "stream_position": start, "limit": 500})
    ).json()["entries"]
    kinds = {e["event_type"] for e in logs}
    assert {"UPLOAD", "DELETE", "COLLABORATION_INVITE", "COLLABORATION_REMOVE"} <= kinds
    unknown = kinds - REAL_EVENT_TYPES - ADMIN_FAMILY
    assert not unknown, unknown
    assert not {"COMMENT_UPDATE", "COLLAB_ACCEPT", "COLLAB_REJECT", "OAUTH2_ACCESS_TOKEN_REVOKE"} & kinds
    assert {"TASK_CREATE", "TASK_UPDATE", "TASK_ASSIGNMENT_DELETE", "LOCK_CREATE", "LOCK_DESTROY"} <= kinds
    assert {"WATERMARK_LABEL_CREATE", "WATERMARK_LABEL_DELETE", "GROUP_EDITED"} <= kinds
    assert {"METADATA_TEMPLATE_CREATE", "METADATA_TEMPLATE_UPDATE", "METADATA_TEMPLATE_DELETE"} <= kinds


async def test_enterprise_only_events_stay_out_of_user_streams(box):
    start = await stream_now(box)
    await box.post("/users", json={"name": "Quiet User", "login": "quiet.user@example.test"})
    await box.post("/groups", json={"name": "Quiet Group"})
    await box.post("/metadata_templates/schema", json={"displayName": "Quiet"})
    mine = (await box.get("/events", params={"stream_position": start, "limit": 500})).json()
    assert mine["entries"] == []
    admin = (
        await box.get("/events", params={"stream_type": "admin_logs", "stream_position": start, "limit": 500})
    ).json()
    assert {"NEW_USER", "GROUP_CREATION", "METADATA_TEMPLATE_CREATE"} <= {
        e["event_type"] for e in admin["entries"]
    }


async def test_comment_edits_fire_the_webhook_without_an_event_and_rejections_use_their_own_trigger(box):
    receiver = FastAPI()
    got: list[str] = []

    @receiver.post("/hook")
    async def hook(request: Request) -> dict:
        got.append((await request.json())["trigger"])
        return {}

    box.hub.webhook_transport = httpx.ASGITransport(app=receiver)
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Hooks again")
    fid = (await box.upload(folder, "h.txt", b"h")).json()["entries"][0]["id"]
    triggers = [
        "COMMENT.CREATED", "COMMENT.UPDATED", "COLLABORATION.CREATED", "COLLABORATION.ACCEPTED",
        "COLLABORATION.REJECTED", "COLLABORATION.REMOVED",
    ]  # fmt: skip
    hook_created = await box.post(
        "/webhooks",
        json={
            "target": {"id": folder, "type": "folder"},
            "address": "https://hooks.example.test/hook",
            "triggers": triggers,
        },
    )
    assert hook_created.status_code == 201
    start = await stream_now(box)
    comment = (
        await box.post("/comments", json={"item": {"type": "file", "id": fid}, "message": "a"})
    ).json()["id"]
    await box.put(f"/comments/{comment}", json={"message": "b"})
    events = (await box.get("/events", params={"stream_position": start, "limit": 500})).json()["entries"]
    assert [e["event_type"] for e in events] == ["COMMENT_CREATE"]
    invite = (
        await box.post(
            "/collaborations",
            json={
                "item": {"type": "file", "id": fid},
                "accessible_by": {"type": "user", "login": "decline@example.test"},
                "role": "viewer",
            },
        )
    ).json()
    minted = await box.client.post(
        "https://api.box.com/_emulator/tokens", json={"user": invite["accessible_by"]["id"]}
    )
    rejected = await box.put(
        f"/collaborations/{invite['id']}", minted.json()["access_token"], json={"status": "rejected"}
    )
    assert rejected.status_code == 204
    assert got == ["COMMENT.CREATED", "COMMENT.UPDATED", "COLLABORATION.CREATED", "COLLABORATION.REJECTED"]
    accepted = (
        await box.post(
            "/collaborations",
            json={
                "item": {"type": "file", "id": fid},
                "accessible_by": {"type": "user", "login": "accept@example.test"},
                "role": "viewer",
            },
        )
    ).json()
    await box.action("accept_collaboration", {"collaboration_id": accepted["id"]})
    assert got[-2:] == ["COLLABORATION.CREATED", "COLLABORATION.ACCEPTED"]
    stream = (await box.get("/events", params={"stream_position": start, "limit": 500})).json()["entries"]
    kinds = [e["event_type"] for e in stream]
    assert "COLLABORATION_ACCEPT" in kinds and "COLLAB_ACCEPT" not in kinds and "COLLAB_REJECT" not in kinds


async def test_email_aliases_lifecycle_and_permissions(box):
    listed = await box.get(f"/users/{MAYA}/email_aliases")
    assert listed.status_code == 200 and listed.json() == {"total_count": 0, "entries": []}
    created = await box.post(f"/users/{MAYA}/email_aliases", json={"email": "m.chen@halcyonadvisory.example"})
    assert created.status_code == 201
    alias = created.json()
    assert alias["type"] == "email_alias" and alias["is_confirmed"] is True
    assert alias["email"] == "m.chen@halcyonadvisory.example" and alias["id"]
    duplicate = await box.post(
        f"/users/{MAYA}/email_aliases", json={"email": "M.CHEN@halcyonadvisory.example"}
    )
    assert duplicate.status_code == 409
    taken = await box.post(
        f"/users/{ARJUN}/email_aliases", json={"email": "maya.chen@halcyonadvisory.example"}
    )
    assert taken.status_code == 409
    for body in ({}, {"email": 5}, {"email": "nope"}):
        assert (await box.post(f"/users/{MAYA}/email_aliases", json=body)).status_code == 400
    assert (await box.get(f"/users/{MAYA}/email_aliases", "p018")).status_code == 403
    assert (
        await box.post(f"/users/{MAYA}/email_aliases", "p018", json={"email": "x@example.test"})
    ).status_code == 403
    assert (await box.get("/users/999/email_aliases")).status_code == 404
    again = (await box.get(f"/users/{MAYA}/email_aliases")).json()
    assert again["total_count"] == 1 and again["entries"] == [alias]
    assert (await box.delete(f"/users/{MAYA}/email_aliases/{alias['id']}")).status_code == 204
    assert (await box.delete(f"/users/{MAYA}/email_aliases/{alias['id']}")).status_code == 404
    assert (await box.get(f"/users/{MAYA}/email_aliases")).json()["entries"] == []


async def test_shared_links_resolve_from_web_app_style_urls(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Link forms")
    fid = (await box.upload(folder, "l.txt", b"l")).json()["entries"][0]["id"]
    shared = await box.put(
        f"/files/{fid}", json={"shared_link": {"access": "open"}}, params={"fields": "shared_link"}
    )
    url = shared.json()["shared_link"]["url"]
    forms = [
        url,
        f"{url}/",
        f"{url}?utm=1",
        f"{url}/file/{fid}",
        url.replace("https://app.box.com", "https://halcyon.app.box.com"),
    ]
    for form in forms:
        r = await box.client.get(
            "https://api.box.com/2.0/shared_items", headers={"BoxApi": f"shared_link={form}"}
        )
        assert r.status_code == 200 and r.json()["id"] == fid, form


async def test_document_owners_keep_access_to_files_they_authored_outside_their_spaces(box):
    # Priya Raman wrote the partner minutes but is not in the Partner Group space; Natalie Cho wrote
    # client onboarding forms for the Client Engagements space without being a member of it.
    minutes = await box.get(
        "/search", "p003", params={"query": '"Partner Meeting Minutes"', "type": "file", "fields": "name"}
    )
    names = {e["name"] for e in minutes.json()["entries"]}
    assert "Partner Meeting Minutes - September 2026.docx" in names
    assert len(names) >= 3
    outsider = await box.get(
        "/search", "p040", params={"query": '"Partner Meeting Minutes"', "type": "file", "fields": "name"}
    )
    assert outsider.json()["total_count"] == 0
    forms = await box.get(
        "/search",
        "p040",
        params={"query": '"Client Onboarding Form"', "type": "file", "fields": "name,owned_by"},
    )
    assert forms.json()["total_count"] >= 3
    # ... but the file-level access is scoped to the file: its folder is not theirs to browse or change.
    entry = forms.json()["entries"][0]
    full = await box.get(f"/files/{entry['id']}", "p040", params={"fields": "permissions,parent"})
    assert full.json()["permissions"]["can_download"] is True
    assert full.json()["parent"] is None


async def test_user_updates_are_atomic_and_keep_restricted_statuses(box):
    created = await box.post("/users", json={"name": "Status User", "login": "status.user@example.test"})
    uid = created.json()["id"]
    refused = await box.put(f"/users/{uid}", json={"role": "coadmin", "status": "retired", "job_title": "X"})
    assert refused.status_code == 400
    unchanged = (await box.get(f"/users/{uid}")).json()
    assert unchanged["role"] == "user" and unchanged["status"] == "active" and unchanged["job_title"] == ""
    # The two restricted statuses are reported as set and still allow signing in...
    token = (await box.client.post("https://api.box.com/_emulator/tokens", json={"user": uid})).json()[
        "access_token"
    ]
    for status in ("cannot_delete_edit", "cannot_delete_edit_upload"):
        assert (await box.put(f"/users/{uid}", json={"status": status})).json()["status"] == status
        assert (await box.get("/users/me", token)).status_code == 200
    # ... but remove the matching abilities from content they own.
    assert (await box.put(f"/users/{uid}", json={"status": "active"})).status_code == 200
    mine = await box.mkdir("0", "Restricted account folder", token)
    assert (await box.put(f"/users/{uid}", json={"status": "cannot_delete_edit"})).status_code == 200
    assert (await box.put(f"/folders/{mine}", token, json={"name": "renamed"})).status_code == 403
    assert (await box.delete(f"/folders/{mine}", token)).status_code == 403
    assert (await box.upload(mine, "still-allowed.txt", b"x", token)).status_code == 201
    assert (await box.put(f"/users/{uid}", json={"status": "cannot_delete_edit_upload"})).status_code == 200
    assert (await box.upload(mine, "blocked.txt", b"x", token)).status_code == 403
    assert (await box.put(f"/users/{uid}", json={"status": "active"})).json()["status"] == "active"
    assert (await box.put(f"/folders/{mine}", token, json={"name": "renamed"})).status_code == 200
    assert (await box.put(f"/users/{uid}", json={"status": "inactive"})).json()["status"] == "inactive"
    assert (await box.get("/users/me", token)).status_code == 401


async def test_co_admins_manage_ordinary_users_but_not_admins(box):
    ordinary = (await box.post("/users", json={"name": "Ordinary", "login": "ordinary@example.test"})).json()[
        "id"
    ]
    assert (await box.put(f"/users/{ordinary}", "p036", json={"job_title": "Clerk"})).status_code == 200
    assert (await box.put(f"/users/{ARJUN}", "p036", json={"job_title": "Boss"})).status_code == 403
    assert (await box.delete(f"/users/{ARJUN}", "p036")).status_code == 403
    assert (
        await box.post(f"/users/{ARJUN}/email_aliases", "p036", json={"email": "a@example.test"})
    ).status_code == 403
    assert (
        await box.put(f"/users/{ordinary}", "p036", json={"role": "coadmin"})
    ).status_code == 403  # admins only
    assert (await box.put(f"/users/{ordinary}", "p035", json={"role": "coadmin"})).status_code == 200
    # a co-admin is not another co-admin's manager, but may edit themselves
    assert (await box.put(f"/users/{ordinary}", "p036", json={"job_title": "Peer"})).status_code == 403
    me = (await box.get("/users/me", "p036")).json()["id"]
    assert (await box.put(f"/users/{me}", "p036", json={"job_title": "Co-admin"})).status_code == 200
    assert (await box.delete(f"/users/{ordinary}", "p035")).status_code == 204


async def test_a_downscoped_token_cannot_be_exchanged_for_a_wider_one(box):
    designated = await box.find("Client Engagements/Northwind Logistics/FY2025 Review/Deliverables", "p004")
    target, other = (await box.items(designated, "p004"))[:2]
    full = (await box.action("mint_token", {"user": "p004"}))["access_token"]
    exchange = {
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
    }
    narrow = await box.client.post(
        "https://api.box.com/oauth2/token",
        data={
            **exchange,
            "subject_token": full,
            "scope": "item_preview",
            "resource": f"https://api.box.com/2.0/files/{target['id']}",
        },
    )
    assert narrow.status_code == 200
    wider = await box.client.post(
        "https://api.box.com/oauth2/token",
        data={
            **exchange,
            "subject_token": narrow.json()["access_token"],
            "scope": "item_download item_readwrite",
            "resource": f"https://api.box.com/2.0/files/{other['id']}",
        },
    )
    assert wider.status_code == 400 and wider.json()["error"] == "invalid_grant"
    assert (await box.get(f"/files/{other['id']}", narrow.json()["access_token"])).status_code == 404


async def test_folder_listings_put_web_links_after_files(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Mixed kinds")
    await box.mkdir(folder, "zz folder")
    await box.upload(folder, "mm file.txt", b"m")
    await box.upload(folder, "aa file.txt", b"a")
    link = await box.post(
        "/web_links", json={"url": "https://example.test/aa", "parent": {"id": folder}, "name": "aa link"}
    )
    assert link.status_code == 201
    for sort in (None, "name", "size", "date", "id"):
        params = {"sort": sort} if sort else {}
        entries = await box.items(folder, **params)
        assert [e["type"] for e in entries] == ["folder", "file", "file", "web_link"], (sort, entries)
    assert [e["name"] for e in await box.items(folder, direction="DESC")] == [
        "zz folder",
        "mm file.txt",
        "aa file.txt",
        "aa link",
    ]


async def test_shared_link_landing_page_escapes_the_item_name(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Escaping")
    fid = (await box.upload(folder, "<img src=x onerror=alert(1)>.txt", b"x")).json()["entries"][0]["id"]
    link = await box.put(
        f"/files/{fid}", json={"shared_link": {"access": "open"}}, params={"fields": "shared_link"}
    )
    page = await box.client.get(link.json()["shared_link"]["url"])
    assert page.status_code == 200
    assert "<img" not in page.text and "&lt;img" in page.text


async def test_item_names_may_not_contain_control_characters(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Names")
    for bad in ("a\nb", "tab\there", "nul\x00", "bell\x07", "del\x7f"):
        made = await box.post("/folders", json={"name": bad, "parent": {"id": folder}})
        assert made.status_code == 400, repr(bad)
        assert (await box.upload(folder, bad, b"x")).status_code == 400, repr(bad)
    fid = (await box.upload(folder, "fine.txt", b"x")).json()["entries"][0]["id"]
    assert (await box.put(f"/files/{fid}", json={"name": "bad\r\nX-Injected: 1"})).status_code == 400
    assert (await box.put(f"/files/{fid}", json={"name": "café ☃.txt"})).status_code == 200
    page = await box.get(f"/files/{fid}/content")
    download = await box.client.get(page.headers["location"])
    assert download.status_code == 200 and "x-injected" not in download.headers


async def test_upload_attributes_are_validated(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Attrs")
    truncated = await box.upload(folder, "long.txt", b"x", description="d" * 400)
    assert truncated.status_code == 201 and truncated.json()["entries"][0]["description"] == "d" * 255
    assert (await box.upload(folder, "list.txt", b"x", description=["not", "text"])).status_code == 400
    assert (await box.upload(folder, "stamp.txt", b"x", content_created_at="yesterday")).status_code == 400
    ok = await box.upload(
        folder,
        "ok.txt",
        b"x",
        description="a fine description",
        content_created_at="2020-01-01T00:00:00-08:00",
        content_modified_at="2020-02-01T00:00:00Z",
    )
    assert ok.status_code == 201
    entry = ok.json()["entries"][0]
    assert entry["description"] == "a fine description"
    assert entry["content_created_at"] == "2020-01-01T00:00:00-08:00"


async def test_control_plane_actions_reject_malformed_payloads(box):
    base = "https://api.box.com/_emulator"
    bad = [
        ("tokens", {"expires_in": "abc"}),
        ("tokens", {"expires_in": 1.5}),
        ("tokens", {"client_id": 5}),
        ("actions/configure_uploads", {"part_size": "x"}),
        ("actions/configure_uploads", {"part_size": 0}),
        ("actions/configure_uploads", {"part_size": True}),
        ("actions/configure_webhook_keys", {"primary": 5}),
        ("actions/configure_webhook_keys", {"secondary": []}),
        ("actions/simulate_upload", {"name": "x.txt", "content_base64": "not base64!"}),
    ]
    for path, payload in bad:
        r = await box.client.post(f"{base}/{path}", json=payload)
        assert r.status_code == 400, (path, payload, r.text)
    keys = await box.client.post(f"{base}/actions/configure_webhook_keys", json={"primary": "a-new-key"})
    assert keys.json()["primary"] == "a-new-key" and keys.json()["secondary"]
    sized = await box.client.post(f"{base}/actions/configure_uploads", json={"part_size": 4096})
    assert sized.json()["upload_part_size"] == 4096


async def test_metadata_query_like_is_not_a_regular_expression(box):
    import time

    from emulators.providers.box.query import like_match

    assert (
        like_match("Northwind Logistics", "north%")
        and like_match("Northwind", "%WIND")
        and like_match("ab", "a_")
    )
    assert not like_match("ab", "a_b") and not like_match("a" * 60, "%" * 40 + "b")
    base = {"from": "enterprise_336533359.clientInfo", "ancestor_folder_id": "0"}
    started = time.perf_counter()
    nothing = await box.post(
        "/metadata_queries/execute_read", json={**base, "query": f"clientName LIKE '{'%' * 60}zzz'"}
    )
    assert nothing.status_code == 200 and nothing.json()["entries"] == []
    assert time.perf_counter() - started < 5
    some = await box.post(
        "/metadata_queries/execute_read",
        json={**base, "query": "clientName LIKE :n", "query_params": {"n": "north%"}},
    )
    assert some.status_code == 200 and some.json()["entries"]
    literal = await box.post(
        "/metadata_queries/execute_read", json={**base, "query": "clientName LIKE 'x.*'"}
    )
    assert literal.json()["entries"] == []  # regex metacharacters are plain characters


async def test_app_item_associations_are_listed_empty_for_visible_items(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "App items")
    fid = (await box.upload(folder, "app.txt", b"x")).json()["entries"][0]["id"]
    for kind, item in (("files", fid), ("folders", folder)):
        r = await box.get(f"/{kind}/{item}/app_item_associations")
        assert r.status_code == 200 and r.json() == {"limit": 100, "next_marker": None, "entries": []}
        assert (
            await box.get(f"/{kind}/{item}/app_item_associations", params={"limit": 0})
        ).status_code == 400
        assert (await box.get(f"/{kind}/{item}/app_item_associations", "p044")).status_code == 404


async def test_search_follows_the_documented_semantics(box):
    async def search(who="p035", **params):
        r = await box.get("/search", who, params={"limit": 200, "fields": "name", **params})
        assert r.status_code == 200, r.text
        return r.json()

    # relevance ordering ignores ``direction`` (Box: "locked to descending order of relevance")
    down = [e["id"] for e in (await search(query="letter", direction="DESC"))["entries"]]
    up = [e["id"] for e in (await search(query="letter", direction="ASC"))["entries"]]
    assert down == up and down
    # ``tags`` is the documented content type; ``tag`` is tolerated
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Tagged")
    fid = (await box.upload(folder, "plain.txt", b"nothing here")).json()["entries"][0]["id"]
    await box.put(f"/files/{fid}", json={"tags": ["quarterly-close"]})
    for content_type in ("tags", "tag"):
        found = await search(query="quarterly-close", content_types=content_type)
        assert fid in [e["id"] for e in found["entries"]]
    assert fid not in [
        e["id"] for e in (await search(query="quarterly-close", content_types="name"))["entries"]
    ]
    # size_range applies to folders as well as files
    big = await search(query="Client Engagements", type="folder", size_range="1000,")
    assert any(e["name"] == "Client Engagements" for e in big["entries"])
    tiny = await search(query="Client Engagements", type="folder", size_range=",10")
    assert not any(e["name"] == "Client Engagements" for e in tiny["entries"])
    # an unreachable or unknown ancestor folder is a 404, not an empty list
    assert (
        await box.get("/search", "p044", params={"query": "a", "ancestor_folder_ids": parent})
    ).status_code == 404
    assert (
        await box.get("/search", params={"query": "a", "ancestor_folder_ids": "99999999"})
    ).status_code == 404
    # mdfilters takes exactly one template filter
    one = '{"scope": "enterprise", "templateKey": "clientInfo", "filters": {"fiscalYear": 2025}}'
    assert (await box.get("/search", params={"mdfilters": f"[{one}]"})).status_code == 200
    assert (await box.get("/search", params={"mdfilters": f"[{one},{one}]"})).status_code == 400
    assert (await box.get("/search", params={"mdfilters": "[]"})).status_code == 400


async def test_search_in_the_trash_by_who_and_when(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Trash search")
    a = (await box.upload(folder, "binned alpha.txt", b"a")).json()["entries"][0]["id"]
    b = (await box.upload(folder, "binned beta.txt", b"b")).json()["entries"][0]["id"]
    await box.delete(f"/files/{a}")
    await box.delete(f"/files/{b}", "p035")
    me = (await box.get("/users/me")).json()["id"]
    base = {"query": "binned", "trash_content": "trashed_only", "fields": "name"}
    everyone = await box.get("/search", params=base)
    assert {e["id"] for e in everyone.json()["entries"]} == {a, b}
    mine = await box.get("/search", params={**base, "deleted_user_ids": me})
    assert {e["id"] for e in mine.json()["entries"]} == {a, b}
    other = await box.get("/search", params={**base, "deleted_user_ids": "1"})
    assert other.json()["entries"] == []
    now = box.hub.clock.now().isoformat()
    recent = await box.get("/search", params={**base, "deleted_at_range": f"{now},"})
    assert recent.json()["entries"] == []  # nothing was deleted after "now"
    ranged = await box.get("/search", params={**base, "deleted_at_range": "2000-01-01T00:00:00Z,"})
    assert {e["id"] for e in ranged.json()["entries"]} == {a, b}
    # deleted-by filters only make sense when searching the trash: otherwise nothing matches
    live = await box.get("/search", params={"query": "binned", "deleted_user_ids": me})
    assert live.json()["entries"] == []


async def test_user_filter_term_matches_partially_and_outsiders_only_exactly(box):
    async def names_for(**params):
        r = await box.get("/users", params={"limit": 1000, **params})
        assert r.status_code == 200, r.text
        return [e["name"] for e in r.json()["entries"]]

    assert "Maya Chen" in await names_for(filter_term="aya")  # partial: a name or login fragment
    assert "Maya Chen" in await names_for(filter_term="maya.chen@halcyon")
    assert "Maya Chen" in await names_for(filter_term="MAYA", user_type="managed")
    assert await names_for(filter_term="zzzz-nobody") == []
    assert "Halcyon Box Automation" in await names_for(user_type="managed")  # the service account is a user
    assert await names_for(
        user_type="external", filter_term="christopher.ingram@northwindlogistics.example"
    ) == ["Christopher Ingram"]
    assert await names_for(user_type="external") == []
    everyone = await names_for()
    assert "Christopher Ingram" not in everyone and len(everyone) == 48
    # an exact external login wins over partial matches of managed users
    assert await names_for(filter_term="christopher.ingram@northwindlogistics.example") == [
        "Christopher Ingram"
    ]


async def test_documented_rules_for_locks_assignments_dispositions_and_multipart(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Rules")
    fid = (await box.upload(folder, "r.txt", b"r")).json()["entries"][0]["id"]
    # folder locks: both operations or neither
    target = {"folder": {"id": folder, "type": "folder"}}
    for operations in ({"move": True}, {"delete": True}, {"move": True, "delete": False}, {}, {"copy": True}):
        r = await box.post("/folder_locks", json={**target, "locked_operations": operations})
        assert r.status_code == 400, operations
    ok = await box.post("/folder_locks", json={**target, "locked_operations": {"move": True, "delete": True}})
    assert ok.status_code == 201 and ok.json()["locked_operations"] == {"move": True, "delete": True}
    # review tasks are approved/rejected, "complete" tasks completed
    item = {"type": "file", "id": fid}
    review = (await box.post("/tasks", json={"item": item, "action": "review"})).json()["id"]
    general = (await box.post("/tasks", json={"item": item, "action": "complete"})).json()["id"]
    a1 = (
        await box.post(
            "/task_assignments", json={"task": {"id": review, "type": "task"}, "assign_to": {"id": ARJUN}}
        )
    ).json()["id"]
    a2 = (
        await box.post(
            "/task_assignments", json={"task": {"id": general, "type": "task"}, "assign_to": {"id": ARJUN}}
        )
    ).json()["id"]
    assert (
        await box.put(f"/task_assignments/{a1}", json={"resolution_state": "completed"})
    ).status_code == 400
    assert (
        await box.put(f"/task_assignments/{a2}", json={"resolution_state": "approved"})
    ).status_code == 400
    assert (
        await box.put(f"/task_assignments/{a1}", json={"resolution_state": "approved"})
    ).status_code == 200
    assert (
        await box.put(f"/task_assignments/{a2}", json={"resolution_state": "completed"})
    ).status_code == 200
    assert (await box.get(f"/tasks/{review}")).json()["is_completed"] is True
    # retention (disposition) dates can be extended, never shortened or cleared
    assert (
        await box.put(f"/files/{fid}", json={"disposition_at": "2031-01-01T00:00:00Z"})
    ).status_code == 200
    assert (
        await box.put(f"/files/{fid}", json={"disposition_at": "2032-01-01T00:00:00Z"})
    ).status_code == 200
    assert (
        await box.put(f"/files/{fid}", json={"disposition_at": "2030-01-01T00:00:00Z"})
    ).status_code == 400
    assert (await box.put(f"/files/{fid}", json={"disposition_at": None})).status_code == 400
    # uploads: the attributes part must come before the file part
    attributes = ("attributes", (None, json.dumps({"name": "order.txt", "parent": {"id": folder}})))
    wrong = await box.client.post(
        "https://upload.box.com/api/2.0/files/content",
        headers=box.auth(),
        files=[("file", ("order.txt", b"x")), attributes],
    )
    assert wrong.status_code == 400
    right = await box.client.post(
        "https://upload.box.com/api/2.0/files/content",
        headers=box.auth(),
        files=[attributes, ("file", ("order.txt", b"x"))],
    )
    assert right.status_code == 201


async def test_group_admins_manage_their_own_group_and_webhooks_follow_access(box):
    from emulators.providers.box import db

    ctx = box.hub.context("box")
    users = {u["id"]: u for u in db.users(ctx)}
    membership = next(
        m
        for m in db.memberships(ctx)
        if m["role"] == "admin"
        and users[m["user_id"]]["role"] == "user"
        and users[m["user_id"]]["_kind"] == "managed"
    )
    head, gid = membership["user_id"], membership["group_id"]
    other_group = next(g["id"] for g in db.groups(ctx) if g["id"] != gid)
    who = users[head]["_pid"]
    # a group admin edits their group and its memberships ...
    renamed = await box.put(
        f"/groups/{gid}",
        who,
        json={"description": "edited by the group admin"},
        params={"fields": "description"},
    )
    assert renamed.status_code == 200 and renamed.json()["description"] == "edited by the group admin"
    members = (await box.get(f"/groups/{gid}/memberships", who)).json()["entries"]
    other_member = next(m for m in members if m["user"]["id"] != head)
    assert (
        await box.put(f"/group_memberships/{other_member['id']}", who, json={"role": "admin"})
    ).status_code == 200
    assert (
        await box.put(f"/group_memberships/{other_member['id']}", who, json={"role": "member"})
    ).status_code == 200
    # ... but not other groups, and adding members or deleting the group is enterprise-admin work
    assert (await box.put(f"/groups/{other_group}", who, json={"description": "nope"})).status_code == 403
    assert (await box.delete(f"/groups/{gid}", who)).status_code == 403
    add = await box.post("/group_memberships", who, json={"user": {"id": MAYA}, "group": {"id": gid}})
    assert add.status_code == 403
    assert (await box.delete(f"/group_memberships/{other_member['id']}", who)).status_code == 204
    # a rename may not collide with another group
    taken = next(g for g in db.groups(ctx) if g["id"] != gid)
    clash = await box.put(f"/groups/{gid}", json={"name": taken["name"].upper()})
    assert clash.status_code == 409
    assert (
        await box.put(f"/groups/{gid}", json={"name": db.groups(ctx).get(gid)["name"]})
    ).status_code == 200

    # webhooks: visible to whoever can reach the target, not to every admin
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Hook visibility", "enterprise")
    created = await box.post(
        "/webhooks",
        "enterprise",
        json={
            "target": {"id": folder, "type": "folder"},
            "address": "https://hooks.example.test/h",
            "triggers": ["FILE.UPLOADED"],
        },
    )
    assert created.status_code == 201
    hook = created.json()["id"]
    assert (await box.get(f"/webhooks/{hook}", "enterprise")).status_code == 200  # the creator
    assert (await box.get(f"/webhooks/{hook}", "p035")).status_code == 200  # the folder's owner reaches it
    assert (await box.get(f"/webhooks/{hook}", "p044")).status_code == 404  # no access to the folder
    mine = {w["id"] for w in (await box.get("/webhooks", "p044")).json()["entries"]}
    assert hook not in mine
    other_admin = await box.get(f"/webhooks/{hook}", "p036")  # a co-admin without access to that folder
    assert other_admin.status_code == (
        200 if (await box.get(f"/folders/{folder}", "p036")).status_code == 200 else 404
    )


async def test_rejected_updates_change_nothing(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Atomic")
    other = await box.mkdir(parent, "Atomic target")
    fid = (await box.upload(folder, "keep.txt", b"k")).json()["entries"][0]["id"]
    before = (
        await box.get(f"/files/{fid}", params={"fields": "name,parent,tags,lock,description,etag"})
    ).json()
    for body in (
        {"name": "renamed.txt", "tags": 5},
        {"name": "renamed.txt", "parent": {"id": other}, "description": ["not", "text"]},
        {"lock": {"access": "lock"}, "tags": ["ok"], "disposition_at": "not a date"},
        {"description": "fine", "permissions": {"can_download": "everyone"}},
        {"parent": {"id": other}, "shared_link": {"access": "nonsense"}},
        {"name": "renamed.txt", "collections": [{"id": "999"}]},
        {"description": "fine", "collections": "x"},
    ):
        r = await box.put(f"/files/{fid}", json=body)
        assert r.status_code in (400, 404), (body, r.text)
        after = (
            await box.get(f"/files/{fid}", params={"fields": "name,parent,tags,lock,description,etag"})
        ).json()
        assert after == before, body
        assert [e["name"] for e in await box.items(folder)] == ["keep.txt"], body
    ok = await box.put(
        f"/files/{fid}",
        json={"name": "renamed.txt", "parent": {"id": other}, "tags": ["t"], "description": "d"},
    )
    assert ok.status_code == 200 and ok.json()["name"] == "renamed.txt" and ok.json()["parent"]["id"] == other
    sub = await box.mkdir(folder, "inner")
    snap = (
        await box.get(f"/folders/{folder}", params={"fields": "name,parent,tags,description,sync_state,etag"})
    ).json()
    for body in (
        {"name": "folder renamed", "tags": 5},
        {"name": "folder renamed", "parent": {"id": other}, "sync_state": "sideways"},
        {"parent": {"id": sub}, "description": "x"},
        {"description": "fine", "can_non_owners_invite": "maybe"},
        {
            "description": "fine",
            "can_non_owners_view_collaborators": False,
        },  # needs can_non_owners_invite=false
        {"name": "folder renamed", "folder_upload_email": {"access": "everyone"}},
        {"description": "fine", "collections": [{"id": "999"}]},
    ):
        r = await box.put(f"/folders/{folder}", json=body)
        assert r.status_code in (400, 404), (body, r.text)
        after = (
            await box.get(
                f"/folders/{folder}", params={"fields": "name,parent,tags,description,sync_state,etag"}
            )
        ).json()
        assert after == snap, body


async def test_can_view_path_and_hidden_collaborator_lists(box):
    outer = await box.mkdir("0", "Path outer")  # in the admin's own root: nobody else reaches it
    inner = await box.mkdir(outer, "Path inner")
    fid = (await box.upload(inner, "deep.txt", b"d")).json()["entries"][0]["id"]
    item = {"type": "folder", "id": inner}
    plain = await box.post(
        "/collaborations",
        json={"item": item, "accessible_by": {"type": "user", "id": MAYA}, "role": "viewer"},
    )
    assert plain.status_code == 201 and plain.json()["can_view_path"] is False
    seen = (await box.get(f"/files/{fid}", "p018", params={"fields": "path_collection"})).json()[
        "path_collection"
    ]
    assert [e["name"] for e in seen["entries"]] == [
        "All Files",
        "Path inner",
    ]  # only the shared folder onwards
    # an owner may grant the full path ...
    assert (
        await box.put(f"/collaborations/{plain.json()['id']}", json={"can_view_path": True})
    ).status_code == 200
    seen = (await box.get(f"/files/{fid}", "p018", params={"fields": "path_collection"})).json()[
        "path_collection"
    ]
    assert [e["name"] for e in seen["entries"]] == ["All Files", "Path outer", "Path inner"]
    assert seen["entries"][0]["name"] == "All Files" and seen["total_count"] == len(seen["entries"])
    # ... but it is a folder-only setting that only owners and co-owners may hand out
    on_file = await box.post(
        "/collaborations",
        json={
            "item": {"type": "file", "id": fid},
            "accessible_by": {"type": "user", "id": "33724305150"},
            "role": "viewer",
            "can_view_path": True,
        },
    )
    assert on_file.status_code == 400
    await box.put(f"/collaborations/{plain.json()['id']}", json={"role": "editor"})
    by_editor = await box.post(
        "/collaborations",
        "p018",
        json={
            "item": item,
            "accessible_by": {"type": "user", "id": "33724305150"},
            "role": "viewer",
            "can_view_path": True,
        },
    )
    assert by_editor.status_code == 403
    assert (
        await box.put(f"/collaborations/{plain.json()['id']}", "p018", json={"can_view_path": True})
    ).status_code == 403
    # collaborators can be hidden from non-owners, once inviting is switched off as well
    assert (
        await box.put(f"/folders/{inner}", json={"can_non_owners_view_collaborators": False})
    ).status_code == 400
    both = {"can_non_owners_view_collaborators": False, "can_non_owners_invite": False}
    assert (await box.put(f"/folders/{inner}", json=both)).status_code == 200
    assert (await box.get(f"/folders/{inner}/collaborations", "p018")).status_code == 403
    assert (await box.get(f"/files/{fid}/collaborations", "p018")).status_code == 403
    assert (await box.get(f"/folders/{inner}/collaborations", "p035")).status_code == 200


async def test_avatar_upload_and_delete(box):
    from emulators.world import documents

    png = documents.make_png(16, 16, seed=3)
    url = f"https://api.box.com/2.0/users/{MAYA}/avatar"
    generated = await box.get(f"/users/{MAYA}/avatar")
    assert generated.status_code == 200 and generated.content != png
    posted = await box.client.post(url, headers=box.auth("p018"), files={"pic": ("me.png", png, "image/png")})
    assert posted.status_code == 200
    urls = posted.json()["pic_urls"]
    assert set(urls) == {"small", "large", "preview"}
    assert urls["large"] == f"https://app.box.com/api/avatar/large/{MAYA}"
    assert (await box.get(f"/users/{MAYA}/avatar", "p018")).content == png
    assert (await box.client.get(urls["small"])).content == png  # the public avatar URL serves it too
    # only admins and the user may change it; clients must send a small image
    assert (
        await box.client.post(url, headers=box.auth("p019"), files={"pic": ("x.png", png)})
    ).status_code == 403
    for payload in (b"not an image", b"", png + b"\x00" * (1024 * 1024)):
        bad = await box.client.post(url, headers=box.auth("p018"), files={"pic": ("x.png", payload)})
        assert bad.status_code in (400, 413), len(payload)
    assert (await box.client.post(url, headers=box.auth("p018"), data={"x": "y"})).status_code == 400
    assert (await box.client.post(url, headers=box.auth("p018"), content=b"junk")).status_code == 400
    assert (
        await box.client.post(url, headers=box.auth("p035"), files={"pic": ("a.png", png)})
    ).status_code == 200
    assert (await box.delete(f"/users/{MAYA}/avatar", "p018")).status_code == 204
    assert (await box.delete(f"/users/{MAYA}/avatar", "p018")).status_code == 404
    assert (await box.get(f"/users/{MAYA}/avatar")).content == generated.content


async def test_shared_link_password_and_expiry_rules(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Link rules")
    fid = (await box.upload(folder, "lnk.txt", b"l")).json()["entries"][0]["id"]

    async def link(**settings):
        return await box.put(
            f"/files/{fid}", json={"shared_link": settings}, params={"fields": "shared_link"}
        )

    for weak in ("short1!", "alllowercase", "12345", "abcdefgh"):  # < 8 characters, or no digit/upper/symbol
        assert (await link(access="open", password=weak)).status_code == 400, weak
    assert (await link(access="company", password="Password1")).status_code == 400  # open links only
    ok = await link(access="open", password="Longenough1")
    assert ok.status_code == 200 and ok.json()["shared_link"]["is_password_enabled"] is True
    assert (await link(password=None)).json()["shared_link"]["is_password_enabled"] is False
    assert (await link(access="open", password="lowercase!")).status_code == 200  # a symbol is enough
    past = box.hub.clock.now().isoformat()
    assert (await link(access="open", unshared_at=past)).status_code == 400
    future = (box.hub.clock.now() + timedelta(days=3)).isoformat()
    assert (await link(access="open", unshared_at=future)).status_code == 200
    assert (await link(unshared_at=None)).json()["shared_link"]["unshared_at"] is None


async def test_pending_invitees_are_redacted_for_everyone_but_themselves(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Redaction")
    item = {"type": "folder", "id": folder}
    by_login = (
        await box.post(
            "/collaborations",
            json={
                "item": item,
                "accessible_by": {"type": "user", "login": "mail.invite@example.test"},
                "role": "viewer",
            },
        )
    ).json()
    assert by_login["status"] == "pending"
    assert (
        by_login["accessible_by"]["login"] == "mail.invite@example.test"
        and by_login["accessible_by"]["name"] == ""
    )
    ingram = next(u for u in await box.externals() if u["login"].startswith("christopher.ingram"))
    by_id = (
        await box.post(
            "/collaborations",
            json={"item": item, "accessible_by": {"type": "user", "id": ingram["id"]}, "role": "viewer"},
        )
    ).json()
    assert by_id["status"] == "pending"
    assert by_id["accessible_by"]["login"] == "" and by_id["accessible_by"]["name"] == ""
    assert by_id["accessible_by"]["id"] == ingram["id"]
    listed = (await box.get(f"/folders/{folder}/collaborations")).json()["entries"]
    assert {e["id"]: e["accessible_by"]["login"] for e in listed} == {
        by_login["id"]: "mail.invite@example.test",
        by_id["id"]: "",
    }
    # the invitee sees themself in full, and accepting reveals them to everyone
    token = (await box.action("mint_token", {"user": ingram["id"]}))["access_token"]
    own = (await box.get(f"/collaborations/{by_id['id']}", token)).json()
    assert own["accessible_by"]["login"] == ingram["login"] and own["accessible_by"]["name"] == ingram["name"]
    assert (
        await box.put(f"/collaborations/{by_id['id']}", token, json={"status": "accepted"})
    ).status_code == 200
    revealed = (await box.get(f"/collaborations/{by_id['id']}")).json()
    assert (
        revealed["accessible_by"]["login"] == ingram["login"]
        and revealed["accessible_by"]["name"] == ingram["name"]
    )


async def test_copy_names_are_sanitized_rather_than_rejected(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Copy names")
    fid = (await box.upload(folder, "orig.txt", b"o")).json()["entries"][0]["id"]
    sub = await box.mkdir(folder, "orig dir")
    copied = await box.post(f"/files/{fid}/copy", json={"parent": {"id": folder}, "name": "a/b\\c\nd.txt   "})
    assert copied.status_code == 201 and copied.json()["name"] == "abcd.txt"
    dir_copy = await box.post(f"/folders/{sub}/copy", json={"parent": {"id": folder}, "name": "x/y\x07z"})
    assert dir_copy.status_code == 201 and dir_copy.json()["name"] == "xyz"
    for name in ("..", "/", "\n", "   "):
        r = await box.post(f"/files/{fid}/copy", json={"parent": {"id": folder}, "name": name})
        assert r.status_code == 400, repr(name)
    # creating and renaming still reject such names outright
    assert (await box.post("/folders", json={"name": "a/b", "parent": {"id": folder}})).status_code == 400
    assert (await box.put(f"/files/{fid}", json={"name": "a\\b"})).status_code == 400


async def test_every_trigger_box_documents_is_accepted(box):
    from box_sdk_gen import CreateWebhookTriggers

    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "All triggers")
    every = [m.value for m in CreateWebhookTriggers]
    created = await box.post(
        "/webhooks",
        json={
            "target": {"id": folder, "type": "folder"},
            "address": "https://hooks.example.test/all",
            "triggers": every,
        },
    )
    assert created.status_code == 201, created.text
    assert set(created.json()["triggers"]) == set(every)
    assert (
        await box.post(
            "/webhooks",
            json={
                "target": {"id": folder, "type": "folder"},
                "address": "https://hooks.example.test/x",
                "triggers": ["FILE.NOT_A_THING"],
            },
        )
    ).status_code == 400


async def test_roles_the_api_hands_out_are_user_and_coadmin(box):
    created = await box.post(
        "/users", json={"name": "Role Test", "login": "role.test@example.test", "role": "coadmin"}
    )
    assert created.status_code == 201 and created.json()["role"] == "coadmin"
    assert (
        await box.post("/users", json={"name": "R", "login": "r@example.test", "role": "admin"})
    ).status_code == 400
    assert (
        await box.post("/users", "p036", json={"name": "R", "login": "r2@example.test", "role": "coadmin"})
    ).status_code == 403
    uid = created.json()["id"]
    assert (await box.put(f"/users/{uid}", json={"role": "admin"})).status_code == 400
    assert (await box.put(f"/users/{uid}", json={"role": "user"})).json()["role"] == "user"
    assert (await box.put(f"/users/{uid}", "p036", json={"role": "admin"})).status_code == 403


async def test_group_invitability_and_member_viewability_levels(box):
    from emulators.providers.box import db

    ctx = box.hub.context("box")
    users = {u["id"]: u for u in db.users(ctx)}
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Invitable")
    # give a colleague who is in no group of interest edit rights so they can try to invite
    maya = users[MAYA]["_pid"]
    await box.post(
        "/collaborations",
        json={
            "item": {"type": "folder", "id": folder},
            "accessible_by": {"type": "user", "id": MAYA},
            "role": "editor",
        },
    )
    my_groups = {m["group_id"] for m in db.memberships(ctx) if m["user_id"] == MAYA}
    outside = next(g for g in db.groups(ctx) if g["id"] not in my_groups)
    inside = next(g for g in db.groups(ctx) if g["id"] in my_groups)

    async def invite(group, who):
        return await box.post(
            "/collaborations",
            who,
            json={
                "item": {"type": "folder", "id": folder},
                "accessible_by": {"type": "group", "id": group["id"]},
                "role": "viewer",
            },
        )

    async def set_level(group, level, field="invitability_level"):
        r = await box.put(f"/groups/{group['id']}", json={field: level})
        assert r.status_code == 200, r.text

    await set_level(outside, "admins_only")
    assert (await invite(outside, maya)).status_code == 403  # not an admin of that group
    await set_level(outside, "admins_and_members")
    assert (await invite(outside, maya)).status_code == 403  # not a member either
    await set_level(outside, "all_managed_users")
    assert (await invite(outside, maya)).status_code == 201  # any managed user may
    await set_level(inside, "admins_and_members")
    assert (await invite(inside, maya)).status_code == 201  # a member may
    # who sees a group's members follows member_viewability_level
    inside_gid = inside["id"]
    await set_level(inside, "admins_only", "member_viewability_level")
    member = next(
        u
        for u in db.users(ctx)
        if u["id"] != MAYA
        and any(
            m["group_id"] == inside_gid and m["user_id"] == u["id"] and m["role"] == "member"
            for m in db.memberships(ctx)
        )
    )
    assert (await box.get(f"/groups/{inside_gid}/memberships", member["_pid"])).status_code == 403
    assert (await box.get(f"/groups/{inside_gid}/memberships", "p035")).status_code == 200
    await set_level(inside, "admins_and_members", "member_viewability_level")
    assert (await box.get(f"/groups/{inside_gid}/memberships", member["_pid"])).status_code == 200
    seen = (await box.get(f"/groups/{inside_gid}", member["_pid"], params={"fields": "permissions"})).json()
    assert seen["permissions"]["can_invite_as_collaborator"] is True


async def test_shared_link_permissions_follow_the_access_level(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Link permissions")
    fid = (await box.upload(folder, "perm.txt", b"p")).json()["entries"][0]["id"]

    async def link(**settings):
        return await box.put(
            f"/files/{fid}", json={"shared_link": settings}, params={"fields": "shared_link"}
        )

    opened = (await link(access="open")).json()["shared_link"]
    assert (
        opened["permissions"]["can_download"] is True
        and opened["download_url"]
        and opened["effective_permission"] == "can_download"
    )
    collab_only = (await link(access="collaborators")).json()["shared_link"]
    assert collab_only["permissions"] == {"can_download": False, "can_preview": True, "can_edit": False}
    assert collab_only["download_url"] is None and collab_only["effective_permission"] == "can_preview"
    assert (await link(access="collaborators", permissions={"can_download": True})).status_code == 400
    assert (
        await link(access="open", permissions={"can_edit": True, "can_download": False})
    ).status_code == 400
    assert (await link(access="open", permissions={"can_download": "yes"})).status_code == 400
    editable = await link(access="open", permissions={"can_edit": True, "can_download": True})
    assert (
        editable.status_code == 200 and editable.json()["shared_link"]["effective_permission"] == "can_edit"
    )
    # the root folder cannot be shared, so it offers no access levels
    root = (await box.get("/folders/0", params={"fields": "allowed_shared_link_access_levels"})).json()
    assert root["allowed_shared_link_access_levels"] == []
    inner = (
        await box.get(f"/folders/{folder}", params={"fields": "allowed_shared_link_access_levels"})
    ).json()
    assert inner["allowed_shared_link_access_levels"] == ["open", "company", "collaborators"]


async def test_locks_protect_every_change_and_company_downloads_exclude_outsiders(box):
    parent = await box.find("Technology")
    folder = await box.mkdir(parent, "Lock scope")
    fid = (await box.upload(folder, "locked.txt", b"l")).json()["entries"][0]["id"]
    await box.post(
        "/collaborations",
        json={
            "item": {"type": "folder", "id": folder},
            "accessible_by": {"type": "user", "id": MAYA},
            "role": "editor",
        },
    )
    assert (await box.put(f"/files/{fid}", "p018", json={"lock": {"access": "lock"}})).status_code == 200
    for change in (
        {"description": "x"},
        {"tags": ["x"]},
        {"name": "other.txt"},
        {"shared_link": {"access": "collaborators"}},
        {"disposition_at": "2031-01-01T00:00:00Z"},
        {"permissions": {"can_download": "company"}},
    ):
        denied = await box.put(f"/files/{fid}", json=change)  # the admin did not take the lock
        assert denied.status_code == 403 and denied.json()["code"] == "access_denied_item_locked", change
    assert (
        await box.put(f"/files/{fid}", "p018", json={"description": "mine", "tags": ["t"]})
    ).status_code == 200
    assert (await box.put(f"/files/{fid}", "p018", json={"lock": None})).status_code == 200
    assert (await box.put(f"/files/{fid}", json={"description": "now anyone"})).status_code == 200
    # permissions.can_download = company removes downloads from external collaborators
    ingram = next(u for u in await box.externals() if u["login"].startswith("christopher.ingram"))
    invite = (
        await box.post(
            "/collaborations",
            json={
                "item": {"type": "file", "id": fid},
                "accessible_by": {"type": "user", "id": ingram["id"]},
                "role": "editor",
            },
        )
    ).json()
    token = (await box.action("mint_token", {"user": ingram["id"]}))["access_token"]
    await box.put(f"/collaborations/{invite['id']}", token, json={"status": "accepted"})
    assert (await box.get(f"/files/{fid}/content", token)).status_code == 302
    assert (
        await box.put(f"/files/{fid}", json={"permissions": {"can_download": "company"}})
    ).status_code == 200
    blocked = await box.get(f"/files/{fid}/content", token)
    assert blocked.status_code == 403
    assert (await box.get(f"/files/{fid}", token, params={"fields": "permissions"})).json()["permissions"][
        "can_download"
    ] is False
    assert (await box.get(f"/files/{fid}/content", "p018")).status_code == 302  # colleagues still can
    assert (await box.put(f"/files/{fid}", json={"permissions": {"can_download": "open"}})).status_code == 200
    assert (await box.get(f"/files/{fid}/content", token)).status_code == 302
