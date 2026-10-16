"""Adversarial HTTP regressions for authorization, opaque state and write semantics."""

from __future__ import annotations

import base64
import json
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response

from tests.providers.dropbox.conftest import sdk_route
from tests.providers.dropbox.test_schema_sweep import make


async def test_refresh_scope_cannot_escalate_and_refresh_remains_reusable(dbx: Any) -> None:
    body = (
        await dbx.client.post(
            f"{dbx.API}/_emulator/tokens",
            json={
                "user": dbx.MAYA,
                "client_id": "audit-app",
                "scopes": ["files.metadata.read", "account_info.read"],
                "refresh_token": True,
            },
        )
    ).json()
    form = {"grant_type": "refresh_token", "refresh_token": body["refresh_token"], "client_id": "audit-app"}
    denied = await dbx.client.post(f"{dbx.API}/oauth2/token", data={**form, "scope": "files.content.write"})
    assert denied.status_code == 400 and denied.json()["error"] == "invalid_scope"
    for _ in range(2):
        refreshed = await dbx.client.post(
            f"{dbx.API}/oauth2/token", data={**form, "scope": "files.metadata.read"}
        )
        assert refreshed.status_code == 200 and "refresh_token" not in refreshed.json()
        token = refreshed.json()["access_token"]
        await dbx.ok(token, "files/list_folder", {"path": ""})
        await dbx.err(token, "users/get_current_account", status=401)
    assert "team_id" not in body and body["account_id"].startswith("dbid:")


async def test_authorization_code_requires_the_original_redirect(dbx: Any) -> None:
    redirect = "https://consumer.example/callback"
    response = await dbx.client.get(
        f"{dbx.WWW}/oauth2/authorize",
        params={
            "client_id": "audit-app",
            "response_type": "code",
            "login_hint": dbx.MAYA,
            "redirect_uri": redirect,
        },
    )
    code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    form = {"grant_type": "authorization_code", "code": code, "client_id": "audit-app"}
    missing = await dbx.client.post(f"{dbx.API}/oauth2/token", data=form)
    assert missing.status_code == 400 and missing.json()["error"] == "invalid_grant"
    wrong = await dbx.client.post(
        f"{dbx.API}/oauth2/token", data={**form, "redirect_uri": "https://other.example/"}
    )
    assert wrong.status_code == 400
    good = await dbx.client.post(f"{dbx.API}/oauth2/token", data={**form, "redirect_uri": redirect})
    assert good.status_code == 200


async def test_folder_id_relative_paths_resolve_below_that_folder(dbx: Any) -> None:
    token = await dbx.token()
    parent = (await dbx.ok(token, "files/create_folder_v2", {"path": "/audit/parent"}))["metadata"]
    child = await dbx.put(token, "/audit/parent/nested/file.txt", b"expected")
    await dbx.put(token, "/nested/file.txt", b"wrong root")
    path = parent["id"] + "/nested/file.txt"
    meta = await dbx.ok(token, "files/get_metadata", {"path": path})
    assert meta["id"] == child["id"] and meta["path_display"] == "/audit/parent/nested/file.txt"
    response, _ = await dbx.download(token, path)
    assert response.content == b"expected"
    created = await dbx.put(token, parent["id"] + "/another/new.txt", b"id upload")
    assert created["path_display"] == "/audit/parent/another/new.txt"
    moved = await dbx.ok(
        token, "files/move_v2", {"from_path": path, "to_path": parent["id"] + "/another/moved.txt"}
    )
    assert moved["metadata"]["id"] == child["id"]


async def test_namespace_root_writes_return_conflicts_without_crashing(dbx: Any) -> None:
    token = await dbx.token()
    acct = await dbx.ok(token, "users/get_current_account")
    root = "ns:" + acct["root_info"]["home_namespace_id"]
    folder = await dbx.err(token, "files/create_folder_v2", {"path": root})
    assert folder["error_summary"].startswith("path/conflict/folder")
    upload = await dbx.upload(token, "files/upload", {"path": root}, b"x")
    assert upload.status_code == 409 and upload.json()["error_summary"].startswith("path/conflict/folder")


async def test_copy_onto_itself_conflicts_or_autorenames_and_case_only_move_works(dbx: Any) -> None:
    token = await dbx.token()
    original = await dbx.put(token, "/audit-case.txt", b"contents")
    conflict = await dbx.err(
        token, "files/copy_v2", {"from_path": "/audit-case.txt", "to_path": "/audit-case.txt"}
    )
    assert conflict["error_summary"].startswith("to/conflict/file")
    copy = await dbx.ok(
        token,
        "files/copy_v2",
        {"from_path": original["id"], "to_path": "/audit-case.txt", "autorename": True},
    )
    assert copy["metadata"]["name"] == "audit-case (1).txt" and copy["metadata"]["id"] != original["id"]
    moved = await dbx.ok(
        token, "files/move_v2", {"from_path": "/audit-case.txt", "to_path": "/AUDIT-CASE.txt"}
    )
    assert moved["metadata"]["name"] == "AUDIT-CASE.txt" and moved["metadata"]["id"] == original["id"]
    assert (await dbx.ok(token, "files/get_metadata", {"path": "/audit-case.txt"}))[
        "path_display"
    ] == "/AUDIT-CASE.txt"


async def test_forged_cursors_cannot_crash_or_choose_an_admin_actor(dbx: Any) -> None:
    token = await dbx.token()
    forged = "AAH" + base64.urlsafe_b64encode(
        json.dumps({"v": 1, "ns": "x", "adm": True}).encode()
    ).decode().rstrip("=")
    await dbx.err(token, "files/list_folder/continue", {"cursor": forged})
    poll = await dbx.client.post(f"{dbx.NOTIFY}/2/files/list_folder/longpoll", json={"cursor": forged})
    assert poll.status_code == 409 and poll.json()["error"] == {".tag": "reset"}


async def test_cursors_are_owned_reusable_and_invalidated_by_reset(dbx: Any, hub: Any) -> None:
    maya = await dbx.token()
    arjun = await dbx.team_token()
    root = await dbx.root(maya)
    page = await dbx.ok(maya, "files/list_folder", {"path": "", "limit": 1}, path_root=root)
    assert page["has_more"]
    ryan = await dbx.token("ryan.murphy@halcyonadvisory.example")
    await dbx.err(ryan, "files/list_folder/continue", {"cursor": page["cursor"]})
    member = await dbx.member_id(dbx.MAYA)
    admin = await dbx.member_id(dbx.ARJUN)
    admin_page = await dbx.ok(
        arjun, "files/list_folder", {"path": "", "limit": 1}, select_admin=admin, path_root=root
    )
    await dbx.err(arjun, "files/list_folder/continue", {"cursor": admin_page["cursor"]}, select_user=member)
    first = await dbx.ok(maya, "files/list_folder/continue", {"cursor": page["cursor"]}, path_root=root)
    repeat = await dbx.ok(maya, "files/list_folder/continue", {"cursor": page["cursor"]}, path_root=root)
    assert first["entries"] == repeat["entries"]
    hub.reset("dropbox")
    dbx.tokens.clear()
    maya = await dbx.token()
    await dbx.err(maya, "files/list_folder/continue", {"cursor": page["cursor"]})


async def test_batch_job_results_cannot_be_read_by_a_different_user(dbx: Any) -> None:
    maya, ryan = await dbx.token(), await dbx.token("ryan.murphy@halcyonadvisory.example")
    job = await dbx.ok(maya, "files/create_folder_batch", {"paths": ["/audit-job"], "force_async": True})
    err = await dbx.err(ryan, "files/create_folder_batch/check", {"async_job_id": job["async_job_id"]})
    assert err["error"] == {".tag": "invalid_async_job_id"}
    done = await dbx.ok(maya, "files/create_folder_batch/check", {"async_job_id": job["async_job_id"]})
    assert done[".tag"] == "complete"


async def test_upload_modes_identical_content_and_strict_conflicts(dbx: Any) -> None:
    token = await dbx.token()
    original = await dbx.put(token, "/audit-mode.txt", b"one")
    again = await dbx.put(token, "/audit-mode.txt", b"one")
    assert again["id"] == original["id"] and again["rev"] == original["rev"]
    updated = await dbx.put(token, "/audit-mode.txt", b"two", mode="overwrite")
    stale = {"path": "/audit-mode.txt", "mode": {".tag": "update", "update": original["rev"]}}
    bad = await dbx.upload(token, "files/upload", stale, b"three")
    assert bad.status_code == 409
    unchanged = await dbx.put(token, "/audit-mode.txt", b"two", mode=stale["mode"])
    assert unchanged["rev"] == updated["rev"]
    strict = await dbx.upload(token, "files/upload", {**stale, "strict_conflict": True}, b"two")
    assert strict.status_code == 409
    missing = await dbx.upload(
        token, "files/upload", {**stale, "path": "/missing-update.txt", "strict_conflict": True}, b"x"
    )
    assert missing.status_code == 409
    await dbx.put(token, "/missing-update.txt", b"x", mode=stale["mode"])


async def test_session_expiry_and_batch_requires_closed_session(dbx: Any, hub: Any) -> None:
    token = await dbx.token()
    start = await dbx.upload(token, "files/upload_session/start", {}, b"data")
    sid = start.json()["session_id"]
    entry = {"cursor": {"session_id": sid, "offset": 4}, "commit": {"path": "/audit-session.txt"}}
    failed = await dbx.ok(token, "files/upload_session/finish_batch_v2", {"entries": [entry]})
    assert failed["entries"][0]["failure"] == {
        ".tag": "lookup_failed",
        "lookup_failed": {".tag": "not_closed"},
    }
    await dbx.err(token, "files/get_metadata", {"path": "/audit-session.txt"})
    close = await dbx.upload(
        token, "files/upload_session/append_v2", {"cursor": entry["cursor"], "close": True}
    )
    assert close.status_code == 200
    result = await dbx.ok(token, "files/upload_session/finish_batch_v2", {"entries": [entry]})
    assert result["entries"][0][".tag"] == "success"
    start = await dbx.upload(token, "files/upload_session/start", {}, b"data")
    hub.clock.advance(timedelta(days=7))
    try:
        fresh = await dbx.token(expires_in=14400, client_id="after-expiry")
        expired = await dbx.upload(
            fresh,
            "files/upload_session/append_v2",
            {"cursor": {"session_id": start.json()["session_id"], "offset": 4}},
        )
        assert expired.status_code == 409 and expired.json()["error"] == {".tag": "not_found"}
    finally:
        hub.clock.advance(timedelta(days=-7))


async def test_concurrent_session_alignment_close_and_finish_rules(dbx: Any) -> None:
    token = await dbx.token()
    bad = await dbx.upload(token, "files/upload_session/start", {"session_type": "concurrent", "close": True})
    assert bad.status_code == 409 and bad.json()["error"] == {".tag": "concurrent_session_close_not_allowed"}
    sid = (await dbx.upload(token, "files/upload_session/start", {"session_type": "concurrent"})).json()[
        "session_id"
    ]
    entry = {"cursor": {"session_id": sid, "offset": 0}, "commit": {"path": "/audit-concurrent.txt"}}
    unclosed = await dbx.upload(token, "files/upload_session/finish", entry)
    assert unclosed.status_code == 409 and unclosed.json()["error"][".tag"] == "concurrent_session_not_closed"
    bad_size = await dbx.upload(token, "files/upload_session/append_v2", {"cursor": entry["cursor"]}, b"tiny")
    assert (
        bad_size.status_code == 409
        and bad_size.json()["error"][".tag"] == "concurrent_session_invalid_data_size"
    )
    bad_offset = await dbx.upload(
        token,
        "files/upload_session/append_v2",
        {"cursor": {"session_id": sid, "offset": 1}, "close": True},
        b"tail",
    )
    assert (
        bad_offset.status_code == 409
        and bad_offset.json()["error"][".tag"] == "concurrent_session_invalid_offset"
    )
    closed = await dbx.upload(
        token, "files/upload_session/append_v2", {"cursor": entry["cursor"], "close": True}, b"tail"
    )
    assert closed.status_code == 200
    entry["cursor"]["offset"] = 4
    data = await dbx.upload(token, "files/upload_session/finish", entry, b"forbidden")
    assert data.status_code == 409 and data.json()["error"][".tag"] == "concurrent_session_data_not_allowed"
    finished = await dbx.upload(token, "files/upload_session/finish", entry)
    assert finished.status_code == 200 and finished.json()["size"] == 4


async def test_shared_folder_link_pagination_and_access_rechecked(dbx: Any) -> None:
    owner = await dbx.token()
    for i in range(5):
        await dbx.put(owner, f"/audit-linked/f{i}.txt", str(i).encode())
    link = await dbx.ok(owner, "sharing/create_shared_link_with_settings", {"path": "/audit-linked"})
    basic = {"Authorization": "Basic " + base64.b64encode(b"audit:app").decode()}
    response = await dbx.client.post(
        f"{dbx.API}/2/files/list_folder",
        headers=basic,
        json={"path": "", "limit": 2, "shared_link": {"url": link["url"]}},
    )
    page = response.json()
    assert response.status_code == 200 and page["has_more"]
    names = [e["name"] for e in page["entries"]]
    while page["has_more"]:
        response = await dbx.client.post(
            f"{dbx.API}/2/files/list_folder/continue", headers=basic, json={"cursor": page["cursor"]}
        )
        assert response.status_code == 200, response.text
        page = response.json()
        names += [e["name"] for e in page["entries"]]
    assert names == [f"f{i}.txt" for i in range(5)]
    await dbx.ok(owner, "sharing/revoke_shared_link", {"url": link["url"]})
    denied = await dbx.client.post(
        f"{dbx.API}/2/files/list_folder/continue", headers=basic, json={"cursor": page["cursor"]}
    )
    assert denied.status_code == 409


async def test_shared_links_continuation_retains_path_filter(dbx: Any, hub: Any) -> None:
    token = await dbx.token()
    hub.contexts["dropbox"].settings["SHARED_LINKS_PAGE"] = "1"
    for name in ("aaa-unrelated", "inside/a", "inside/b"):
        await dbx.put(token, "/audit-links/" + name + ".txt", b"x")
        await dbx.ok(
            token, "sharing/create_shared_link_with_settings", {"path": "/audit-links/" + name + ".txt"}
        )
    parent = await dbx.ok(token, "sharing/create_shared_link_with_settings", {"path": "/audit-links"})
    folder = await dbx.ok(token, "sharing/create_shared_link_with_settings", {"path": "/audit-links/inside"})
    first = await dbx.ok(token, "sharing/list_shared_links", {"path": "/audit-links/inside/a.txt"})
    urls = [x["url"] for x in first["links"]]
    while first["has_more"]:
        first = await dbx.ok(token, "sharing/list_shared_links", {"cursor": first["cursor"]})
        urls += [x["url"] for x in first["links"]]
    assert len(urls) == 3 and parent["url"] in urls and folder["url"] in urls


async def test_shared_link_keys_download_policy_and_prefix_urls(dbx: Any, hub: Any) -> None:
    token = await dbx.token()
    await dbx.put(token, "/audit-link-key.txt", b"private download")
    link = await dbx.ok(
        token,
        "sharing/create_shared_link_with_settings",
        {"path": "/audit-link-key.txt", "settings": {"allow_download": False}},
    )
    denied = await dbx.err(
        token, "sharing/get_shared_link_metadata", {"url": link["url"].replace("rlkey=", "wrong=")}
    )
    assert denied["error"][".tag"] == "shared_link_not_found"
    response = await dbx.client.get(link["url"].replace("rlkey=", "wrong="))
    assert response.status_code == 404
    download = await dbx.upload(token, "sharing/get_shared_link_file", {"url": link["url"]})
    assert download.status_code == 409 and download.json()["error"][".tag"] == "shared_link_access_denied"
    # Returned prefix-mode URLs must themselves be usable as API arguments.
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hub.gateway)) as prefix:
        response = await prefix.post(
            "http://localhost:44080/dropbox/2/sharing/list_shared_links",
            headers=dbx.headers(token),
            json={"path": "/audit-link-key.txt"},
        )
        url = response.json()["links"][0]["url"]
        assert url.startswith("http://localhost:44080/dropbox/")
        response = await prefix.post(
            "http://localhost:44080/dropbox/2/sharing/get_shared_link_metadata",
            headers=dbx.headers(token),
            json={"url": url},
        )
        assert response.status_code == 200


@pytest.mark.parametrize(
    "route,body",
    [
        ("files/create_folder_batch", {"paths": [5]}),
        ("files/search_v2", {"query": "a", "options": {"file_extensions": [{}]}}),
        ("files/search_v2", {"query": "a", "options": {"file_categories": [{}]}}),
        ("files/upload", {"path": "/audit-union.txt", "mode": {".tag": "update"}}),
        ("files/upload", {"path": "/audit-union.txt", "mode": {".tag": []}}),
        ("files/upload", {"path": "/audit-unicode\ud800"}),
        ("files/upload", {"path": "/audit-date.txt", "client_modified": "2026-10-16"}),
        ("files/list_folder", {"path": "", "limit": float("inf")}),
        ("files/list_folder", {"path": "", "limit": 1.0}),
        ("files/list_folder", {"path": "", "limit": float("nan")}),
        ("files/upload_session/append_v2", {"cursor": {"session_id": "unknown", "offset": 2**64}}),
    ],
)
async def test_malformed_nested_fields_are_400_before_mutating(
    dbx: Any, hub: Any, route: str, body: Any
) -> None:
    from emulators.providers.dropbox.dispatch import ROUTES

    token = await dbx.token()
    before = hub.contexts["dropbox"].store.snapshot()
    headers = dbx.headers(token)
    if ROUTES[route].style == "rpc":
        headers["Content-Type"] = "application/json"
        response = await dbx.client.post(f"{dbx.API}/2/{route}", headers=headers, content=json.dumps(body))
    else:
        headers["Dropbox-API-Arg"] = json.dumps(body)
        response = await dbx.client.post(f"{dbx.CONTENT}/2/{route}", headers=headers, content=b"data")
    assert response.status_code == 400 and response.headers["content-type"].startswith("text/plain")
    assert response.headers["x-dropbox-request-id"]
    store = hub.contexts["dropbox"].store
    assert store.collection("entries").values() == list(before["collections"]["entries"].values())


async def test_every_write_route_rejects_wrong_typed_fields(dbx: Any) -> None:
    """Exercise real RPC and content styles with otherwise complete, SDK-derived arguments."""
    from emulators.providers.dropbox.dispatch import ROUTES

    user, team = await dbx.token(), await dbx.team_token()
    checked = 0
    for name, rd in sorted(ROUTES.items()):
        if not rd.scope or not any(s in rd.scope for s in ("write", "delete", "modify")):
            continue
        sdk = sdk_route(name)
        arg = make(sdk.arg_type, "", 0)
        if not isinstance(arg, dict):
            continue
        for field, value in arg.items():
            if isinstance(value, bool):
                junk: Any = "not-a-bool"
            elif isinstance(value, str):
                junk = {"wrong": "type"}
            elif isinstance(value, list):
                junk = [None]
            elif isinstance(value, dict):
                junk = {".tag": []} if ".tag" in value else 7
            else:
                junk = "not-a-number"
            mutated = {**arg, field: junk}
            headers = dbx.headers(team if "team" in rd.auth else user)
            if rd.style == "rpc":
                headers["Content-Type"] = "application/json"
                response = await dbx.client.post(
                    f"{dbx.API}/2/{name}", headers=headers, content=json.dumps(mutated)
                )
            else:
                headers["Dropbox-API-Arg"] = json.dumps(mutated)
                response = await dbx.client.post(f"{dbx.CONTENT}/2/{name}", headers=headers, content=b"x")
            assert response.status_code == 400, (name, field, response.text)
            checked += 1
    assert checked > 180


async def test_faults_have_request_ids_and_retry_headers(dbx: Any) -> None:
    token = await dbx.token()
    await dbx.client.post(
        f"{dbx.API}/_emulator/faults", json={"path": "/2/files/list_folder$", "status": 429}
    )
    response = await dbx.rpc(token, "files/list_folder", {"path": ""})
    assert response.status_code == 429 and response.headers["retry-after"] == "1"
    assert response.headers["x-dropbox-request-id"]


async def test_restricted_deletions_and_parent_copy_do_not_leak(dbx: Any, hub: Any) -> None:
    doc = hub.world.document("d0260")
    owner = await dbx.token(hub.world.person(doc.owner_id).email)
    owner_root = await dbx.root(owner)
    bystander = await dbx.token("tyler.brooks@halcyonadvisory.example")
    bystander_root = await dbx.root(bystander)
    folder = "/People & Culture/Compensation"
    files = await dbx.list_all(owner, folder, path_root=owner_root)
    restricted = next(item for item in files if item["name"] == doc.name)
    denied = await dbx.err(
        bystander,
        "files/copy_v2",
        {"from_path": folder, "to_path": "/Tyler Brooks/copied"},
        path_root=bystander_root,
    )
    assert denied["error_summary"].startswith("from_lookup/not_found/")
    latest = await dbx.ok(
        bystander, "files/list_folder/get_latest_cursor", {"path": folder}, path_root=bystander_root
    )
    await dbx.ok(owner, "files/delete_v2", {"path": restricted["id"]}, path_root=owner_root)
    delta = await dbx.ok(
        bystander, "files/list_folder/continue", {"cursor": latest["cursor"]}, path_root=bystander_root
    )
    assert delta["entries"] == []
    hub.contexts["dropbox"].settings["LONGPOLL_MAX_WAIT_S"] = "0"
    poll = await dbx.client.post(
        f"{dbx.NOTIFY}/2/files/list_folder/longpoll", json={"cursor": latest["cursor"]}
    )
    assert poll.json() == {"changes": False}


async def test_copy_reference_is_bound_to_the_source_app_and_authorization(dbx: Any) -> None:
    maya = await dbx.token(client_id="copy-app")
    await dbx.put(maya, "/copy-source.txt", b"copy me")
    reference = await dbx.ok(maya, "files/copy_reference/get", {"path": "/copy-source.txt"})
    ryan_other = await dbx.token("ryan.murphy@halcyonadvisory.example", client_id="other-app")
    args = {"copy_reference": reference["copy_reference"], "path": "/received-copy.txt"}
    denied = await dbx.err(ryan_other, "files/copy_reference/save", args)
    assert denied["error"] == {".tag": "no_permission"}
    ryan = await dbx.token("ryan.murphy@halcyonadvisory.example", client_id="copy-app")
    saved = await dbx.ok(ryan, "files/copy_reference/save", args)
    assert saved["metadata"]["size"] == 7
    await dbx.ok(maya, "auth/token/revoke")
    denied = await dbx.err(ryan, "files/copy_reference/save", {**args, "path": "/revoked-copy.txt"})
    assert denied["error"] == {".tag": "no_permission"}


async def test_team_oauth_requires_an_administrator(dbx: Any) -> None:
    denied = await dbx.client.get(
        f"{dbx.WWW}/oauth2/authorize",
        params={
            "client_id": "team-audit",
            "response_type": "code",
            "scope": "members.read",
            "login_hint": dbx.MAYA,
        },
    )
    assert denied.status_code == 400 and "administrator" in denied.text


async def test_incremental_oauth_combines_previously_granted_scopes(dbx: Any) -> None:
    await dbx.token(client_id="incremental-app", scopes=["files.metadata.read"])
    response = await dbx.client.get(
        f"{dbx.WWW}/oauth2/authorize",
        params={
            "client_id": "incremental-app",
            "response_type": "code",
            "scope": "account_info.read",
            "include_granted_scopes": "user",
            "login_hint": dbx.MAYA,
        },
    )
    code = response.text.split("id='code'>")[1].split("<")[0]
    result = await dbx.client.post(
        f"{dbx.API}/oauth2/token",
        data={"client_id": "incremental-app", "grant_type": "authorization_code", "code": code},
    )
    assert result.status_code == 200
    assert set(result.json()["scope"].split()) == {"files.metadata.read", "account_info.read"}


async def test_strict_client_auth_redirect_allowlist_and_public_pkce(
    dbx: Any, hub: Any, monkeypatch: Any
) -> None:
    service = hub.contexts["dropbox"].tokens
    monkeypatch.setattr(service, "lenient_clients", False)
    redirect = "https://consumer.example/callback"
    service.register_client("strict-audit", "emulator-only-secret", redirect_uris=[redirect])
    params = {
        "client_id": "strict-audit",
        "response_type": "code",
        "redirect_uri": redirect,
        "login_hint": dbx.MAYA,
    }
    invalid = await dbx.client.get(
        f"{dbx.WWW}/oauth2/authorize", params={**params, "redirect_uri": "https://other.example/"}
    )
    assert invalid.status_code == 400 and "location" not in invalid.headers
    response = await dbx.client.get(f"{dbx.WWW}/oauth2/authorize", params=params)
    code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    form = {
        "client_id": "strict-audit",
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect,
    }
    denied = await dbx.client.post(f"{dbx.API}/oauth2/token", data={**form, "client_secret": "wrong"})
    assert denied.status_code == 401
    verifier = "v" * 64
    response = await dbx.client.get(
        f"{dbx.WWW}/oauth2/authorize",
        params={
            **params,
            "code_challenge": verifier,
            "code_challenge_method": "plain",
            "token_access_type": "offline",
        },
    )
    code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    good = await dbx.client.post(
        f"{dbx.API}/oauth2/token", data={**form, "code": code, "code_verifier": verifier}
    )
    assert good.status_code == 200
    refresh = await dbx.client.post(
        f"{dbx.API}/oauth2/token",
        data={
            "client_id": "strict-audit",
            "grant_type": "refresh_token",
            "refresh_token": good.json()["refresh_token"],
        },
    )
    assert refresh.status_code == 200


async def test_webhook_query_parameters_and_signed_retries(dbx: Any, hub: Any) -> None:
    import hashlib
    import hmac

    receiver = FastAPI()
    deliveries: list[tuple[bytes, str]] = []

    @receiver.get("/hook")
    async def challenge(challenge: str, tenant: str) -> PlainTextResponse:
        assert tenant == "fictional"
        return PlainTextResponse(challenge)

    @receiver.post("/hook")
    async def notify(request: Request) -> Response:
        assert request.query_params["tenant"] == "fictional"
        deliveries.append((await request.body(), request.headers["x-dropbox-signature"]))
        return Response(status_code=503 if len(deliveries) == 1 else 200)

    hub.webhook_transport = httpx.ASGITransport(app=receiver)
    configured = await dbx.client.post(
        f"{dbx.API}/_emulator/actions/configure_webhook",
        json={
            "url": "http://consumer.test/hook?tenant=fictional",
            "app_secret": "emulator-webhook-key",
            "app_key": "retry-app",
        },
    )
    assert configured.status_code == 200
    token = await dbx.token(client_id="retry-app")
    await dbx.put(token, "/webhook-retry.txt", b"notify")
    assert len(deliveries) == 2 and deliveries[0] == deliveries[1]
    body, signature = deliveries[0]
    assert signature == hmac.new(b"emulator-webhook-key", body, hashlib.sha256).hexdigest()


async def test_permanently_deleted_folder_purges_child_revisions(dbx: Any) -> None:
    token = await dbx.token()
    folder = (await dbx.ok(token, "files/create_folder_v2", {"path": "/purge-audit"}))["metadata"]
    child = await dbx.put(token, "/purge-audit/nested/child.txt", b"private history")
    await dbx.ok(token, "files/delete_v2", {"path": folder["id"]})
    deleted = await dbx.ok(token, "files/get_metadata", {"path": child["id"], "include_deleted": True})
    assert deleted[".tag"] == "deleted"
    await dbx.ok(token, "files/permanently_delete", {"path": folder["id"]})
    await dbx.err(token, "files/get_metadata", {"path": child["id"], "include_deleted": True})
    await dbx.err(token, "files/list_revisions", {"path": child["id"]})


async def test_app_basic_auth_validates_credentials_and_cursors_are_app_bound(
    dbx: Any, hub: Any, monkeypatch: Any
) -> None:
    malformed = await dbx.client.post(f"{dbx.API}/2/check/app", headers={"Authorization": "Basic invalid"})
    assert malformed.status_code == 400
    token = await dbx.token()
    await dbx.err(token, "check/app", status=401)
    service = hub.contexts["dropbox"].tokens
    service.register_client("app-a", "emulator-a")
    service.register_client("app-b", "emulator-b")
    for i in range(3):
        await dbx.put(token, f"/app-audit/f{i}.txt", b"x")
    link = await dbx.ok(token, "sharing/create_shared_link_with_settings", {"path": "/app-audit"})
    monkeypatch.setattr(service, "lenient_clients", False)

    def basic(client: str, secret: str) -> dict[str, str]:
        return {"Authorization": "Basic " + base64.b64encode(f"{client}:{secret}".encode()).decode()}

    wrong = await dbx.client.post(f"{dbx.API}/2/check/app", headers=basic("app-a", "wrong"))
    assert wrong.status_code == 401
    first = await dbx.client.post(
        f"{dbx.API}/2/files/list_folder",
        headers=basic("app-a", "emulator-a"),
        json={"path": "", "limit": 1, "shared_link": {"url": link["url"]}},
    )
    assert first.status_code == 200 and first.json()["has_more"]
    denied = await dbx.client.post(
        f"{dbx.API}/2/files/list_folder/continue",
        headers=basic("app-b", "emulator-b"),
        json={"cursor": first.json()["cursor"]},
    )
    assert denied.status_code == 409
