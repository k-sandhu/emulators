"""Deprecated routes (search v1, create_shared_link, openid) and file property templates/groups."""

from __future__ import annotations

from typing import Any


async def test_search_v1(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    root = await dbx.root(token)
    result = await dbx.ok(
        token, "files/search", {"path": "", "query": "request list", "max_results": 3}, path_root=root
    )
    assert len(result["matches"]) == 3 and result["more"] is True and result["start"] == 3
    assert result["matches"][0]["match_type"] == {".tag": "filename"}
    nxt = await dbx.ok(
        token,
        "files/search",
        {"path": "", "query": "request list", "start": 3, "max_results": 1000},
        path_root=root,
    )
    assert nxt["more"] is False and len(nxt["matches"]) > 20
    content = await dbx.ok(
        token,
        "files/search",
        {"path": "", "query": "laptop", "mode": {".tag": "filename_and_content"}},
        path_root=root,
    )
    assert any(mt["match_type"][".tag"] in ("content", "both") for mt in content["matches"])
    deleted = await dbx.ok(
        token, "files/search", {"path": "", "query": "x", "mode": {".tag": "deleted_filename"}}
    )
    assert deleted["matches"] == []


async def test_legacy_shared_links_and_file_member_removal(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    meta = await dbx.put(token, "/legacy/doc.txt", b"d")
    link = await dbx.ok(token, "sharing/create_shared_link", {"path": "/legacy/doc.txt"})
    assert link["url"].startswith("https://www.dropbox.com/s/") and link["path"] == "/legacy/doc.txt"
    assert link["visibility"] == {".tag": "public"} and ".tag" not in link
    again = await dbx.ok(token, "sharing/create_shared_link", {"path": "/legacy/doc.txt"})
    assert again["url"] == link["url"]
    listed = await dbx.ok(token, "sharing/get_shared_links", {"path": "/legacy/doc.txt"})
    assert listed["links"][0][".tag"] == "path" and listed["links"][0]["url"] == link["url"]
    modern = await dbx.ok(token, "sharing/list_shared_links", {"path": "/legacy/doc.txt"})
    assert modern["links"][0]["url"] == link["url"]
    assert (await dbx.client.get(link["url"])).status_code == 200  # legacy /s/<id>/<name> pages resolve
    await dbx.ok(
        token,
        "sharing/add_file_member",
        {"file": meta["id"], "members": [{".tag": "email", "email": dbx.CHRISTOPHER}]},
    )
    removed = await dbx.ok(
        token,
        "sharing/remove_file_member",
        {"file": meta["id"], "member": {".tag": "email", "email": dbx.CHRISTOPHER}},
    )
    assert removed == {".tag": "success"}
    missing = await dbx.err(
        token,
        "sharing/remove_file_member",
        {"file": meta["id"], "member": {".tag": "email", "email": dbx.CHRISTOPHER}},
    )
    assert missing["error"][".tag"] == "no_explicit_access"


async def test_openid_userinfo_and_alpha_aliases(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    info = await dbx.ok(token, "openid/userinfo", {})
    assert info["email"] == dbx.MAYA and info["given_name"] == "Maya" and info["sub"].startswith("dbid:")
    narrow = await dbx.token(dbx.MAYA, scopes=["openid"])
    only = await dbx.ok(narrow, "openid/userinfo", {})
    assert "email" not in only and "given_name" not in only
    wrong = await dbx.token(dbx.MAYA, scopes=["account_info.read"], expires_in=3000)
    err = await dbx.err(wrong, "openid/userinfo", {}, status=401)
    assert err["error"]["required_scope"] == "openid"
    meta = await dbx.put(token, "/alpha.txt", b"a")
    alpha = await dbx.ok(token, "files/alpha/get_metadata", {"path": "/alpha.txt"}, check=False)
    assert alpha["id"] == meta["id"]
    r = await dbx.upload(token, "files/alpha/upload", {"path": "/alpha2.txt"}, b"x")
    assert r.status_code == 200 and r.json()["name"] == "alpha2.txt"


async def test_property_templates_and_groups(dbx: Any) -> None:
    token = await dbx.token(dbx.MAYA)
    template = await dbx.ok(
        token,
        "file_properties/templates/add_for_user",
        {
            "name": "Review",
            "description": "Review status",
            "fields": [
                {"name": "Status", "description": "state", "type": {".tag": "string"}},
                {"name": "Reviewer", "description": "who", "type": {".tag": "string"}},
            ],
        },
    )
    tid = template["template_id"]
    assert tid.startswith("ptid:")
    got = await dbx.ok(token, "file_properties/templates/get_for_user", {"template_id": tid})
    assert [f["name"] for f in got["fields"]] == ["Status", "Reviewer"]
    listed = await dbx.ok(token, "file_properties/templates/list_for_user")
    assert tid in listed["template_ids"]
    conflict = await dbx.err(
        token,
        "file_properties/templates/add_for_user",
        {
            "name": "Dup",
            "description": "",
            "fields": [{"name": "A", "description": "", "type": {".tag": "string"}}] * 2,
        },
    )
    assert conflict["error"][".tag"] == "conflicting_property_names"
    updated = await dbx.ok(
        token,
        "file_properties/templates/update_for_user",
        {"template_id": tid, "add_fields": [{"name": "Due", "description": "", "type": {".tag": "string"}}]},
    )
    assert updated["template_id"] == tid
    # another user cannot see it
    ryan = await dbx.token("ryan.murphy@halcyonadvisory.example")
    missing = await dbx.err(ryan, "file_properties/templates/get_for_user", {"template_id": tid})
    assert missing["error"][".tag"] == "template_not_found"

    await dbx.put(token, "/props/a.txt", b"a")
    await dbx.put(token, "/props/b.txt", b"b")
    groups = [
        {
            "template_id": tid,
            "fields": [{"name": "Status", "value": "Approved"}, {"name": "Reviewer", "value": "Maya"}],
        }
    ]
    await dbx.ok(token, "file_properties/properties/add", {"path": "/props/a.txt", "property_groups": groups})
    again = await dbx.err(
        token, "file_properties/properties/add", {"path": "/props/a.txt", "property_groups": groups}
    )
    assert again["error"][".tag"] == "property_group_already_exists"
    bad_field = await dbx.err(
        token,
        "file_properties/properties/overwrite",
        {
            "path": "/props/a.txt",
            "property_groups": [{"template_id": tid, "fields": [{"name": "Nope", "value": "x"}]}],
        },
    )
    assert bad_field["error"][".tag"] == "does_not_fit_template"
    await dbx.ok(
        token,
        "file_properties/properties/update",
        {
            "path": "/props/a.txt",
            "update_property_groups": [
                {
                    "template_id": tid,
                    "add_or_update_fields": [{"name": "Status", "value": "Rejected"}],
                    "remove_fields": ["Reviewer"],
                }
            ],
        },
    )
    include = {".tag": "filter_some", "filter_some": [tid]}
    meta = await dbx.ok(
        token, "files/get_metadata", {"path": "/props/a.txt", "include_property_groups": include}
    )
    assert meta["property_groups"] == [
        {"template_id": tid, "fields": [{"name": "Status", "value": "Rejected"}]}
    ]
    plain = await dbx.ok(token, "files/get_metadata", {"path": "/props/a.txt"})
    assert "property_groups" not in plain
    listing = await dbx.ok(token, "files/list_folder", {"path": "/props", "include_property_groups": include})
    assert [e.get("property_groups") for e in listing["entries"]] == [meta["property_groups"], None]
    found = await dbx.ok(
        token,
        "file_properties/properties/search",
        {
            "queries": [{"query": "reject", "mode": {".tag": "field_name", "field_name": "Status"}}],
            "template_filter": {".tag": "filter_none"},
        },
    )
    assert [m["path"] for m in found["matches"]] == ["/props/a.txt"]
    none = await dbx.ok(
        token,
        "file_properties/properties/search",
        {
            "queries": [{"query": "approved", "mode": {".tag": "field_name", "field_name": "Status"}}],
            "template_filter": {".tag": "filter_none"},
        },
    )
    assert none["matches"] == []
    await dbx.ok(
        token, "file_properties/properties/remove", {"path": "/props/a.txt", "property_template_ids": [tid]}
    )
    gone = await dbx.err(
        token, "file_properties/properties/remove", {"path": "/props/a.txt", "property_template_ids": [tid]}
    )
    assert gone["error"]["property_group_lookup"][".tag"] == "property_group_not_found"
    folder = await dbx.ok(token, "files/create_folder_v2", {"path": "/props/dir"})
    on_folder = await dbx.err(
        token, "file_properties/properties/add", {"path": "/props/dir", "property_groups": groups}
    )
    assert on_folder["error"][".tag"] == "unsupported_folder"
    assert folder
    await dbx.ok(token, "file_properties/templates/remove_for_user", {"template_id": tid})
    after = await dbx.err(token, "file_properties/templates/get_for_user", {"template_id": tid})
    assert after["error"][".tag"] == "template_not_found"


async def test_team_property_templates_are_visible_to_members(dbx: Any) -> None:
    arjun = await dbx.team_token()
    created = await dbx.ok(
        arjun,
        "file_properties/templates/add_for_team",
        {
            "name": "Retention",
            "description": "",
            "fields": [{"name": "Class", "description": "", "type": {".tag": "string"}}],
        },
    )
    maya = await dbx.token(dbx.MAYA)
    got = await dbx.ok(
        maya, "file_properties/templates/get_for_user", {"template_id": created["template_id"]}
    )
    assert got["name"] == "Retention"
    listed = await dbx.ok(arjun, "file_properties/templates/list_for_team")
    assert created["template_id"] in listed["template_ids"]
    await dbx.ok(arjun, "file_properties/templates/remove_for_team", {"template_id": created["template_id"]})
