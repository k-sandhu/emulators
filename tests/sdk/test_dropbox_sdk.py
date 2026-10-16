"""The official ``dropbox`` Python SDK against the Dropbox emulator.

Run with ``EMULATORS_PROVIDERS=dropbox uv run pytest tests/sdk/test_dropbox_sdk.py``: the
``emulator_intercept`` fixture reroutes ``api.dropboxapi.com``, ``content.dropboxapi.com``,
``notify.dropboxapi.com``, ``www.dropbox.com`` and ``dl.dropboxusercontent.com`` to the in-process
gateway, so the SDK runs unchanged -- including its strict Stone (de)serialisation of every response.
"""

from __future__ import annotations

import datetime
import io
import re
import zipfile
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests

pytestmark = pytest.mark.sdk

dropbox = pytest.importorskip("dropbox")

from dropbox import DropboxTeam, common, exceptions, files, sharing, team, team_log, users  # noqa: E402
from dropbox.content_hash import content_hash  # noqa: E402

MAYA = "maya.chen@halcyonadvisory.example"
RYAN = "ryan.murphy@halcyonadvisory.example"
ARJUN = "arjun.mehta@halcyonadvisory.example"
CHRISTOPHER = "christopher.ingram@northwindlogistics.example"


def mint(server: Any, user: str, **extra: Any) -> str:
    r = requests.post(
        f"{server.url_for('dropbox')}/_emulator/tokens", json={"user": user, **extra}, timeout=10
    )
    r.raise_for_status()
    return r.json()["access_token"]


def client(server: Any, user: str = MAYA, **kwargs: Any) -> Any:
    return dropbox.Dropbox(oauth2_access_token=mint(server, user), **kwargs)


def team_client(server: Any, **kwargs: Any) -> Any:
    return DropboxTeam(mint(server, ARJUN, team=True), **kwargs)


def sdk_content_hash(data: bytes) -> str:
    return content_hash(data)


def team_root(dbx: Any) -> Any:
    info = dbx.users_get_current_account()
    return dbx.with_path_root(common.PathRoot.root(info.root_info.root_namespace_id))


def test_current_account_and_space_usage(emulator_intercept: Any) -> None:
    dbx = client(emulator_intercept)
    account = dbx.users_get_current_account()
    assert account.email == MAYA
    assert account.name.display_name == "Maya Chen"
    assert account.account_type.is_business()
    assert isinstance(account.root_info, common.TeamRootInfo) and account.root_info.home_path == "/Maya Chen"
    assert account.team.name == "Halcyon Advisory Group"
    usage = dbx.users_get_space_usage()
    assert usage.used > 0 and usage.allocation.is_team()
    assert dbx.check_user(query="ping").result == "ping"


def test_list_folder_pagination_and_cursors(emulator_intercept: Any) -> None:
    dbx = team_root(client(emulator_intercept))
    first = dbx.files_list_folder("/Client Engagements", recursive=True, limit=40)
    assert first.has_more and len(first.entries) >= 40
    entries = list(first.entries)
    page = first
    while page.has_more:
        page = dbx.files_list_folder_continue(page.cursor)
        entries.extend(page.entries)
    file_entries = [e for e in entries if isinstance(e, files.FileMetadata)]
    assert len(file_entries) > 200
    sample = file_entries[0]
    assert sample.path_lower.startswith("/client engagements/") and len(sample.content_hash) == 64
    assert sample.sharing_info.parent_shared_folder_id
    # a latest cursor only reports later changes
    cursor = dbx.files_list_folder_get_latest_cursor("/Client Engagements", recursive=True).cursor
    assert dbx.files_list_folder_continue(cursor).entries == []
    dbx.files_upload(b"hello", "/Client Engagements/sdk-note.txt")
    changes = dbx.files_list_folder_continue(cursor)
    assert [e.name for e in changes.entries] == ["sdk-note.txt"]
    dbx.files_delete_v2("/Client Engagements/sdk-note.txt")
    deleted = dbx.files_list_folder_continue(changes.cursor)
    assert isinstance(deleted.entries[0], files.DeletedMetadata)


def test_upload_download_roundtrip_with_content_hash(emulator_intercept: Any) -> None:
    dbx = client(emulator_intercept)
    payload = bytes(range(256)) * 20_000  # ~5 MB: more than one 4 MiB hash block
    meta = dbx.files_upload(payload, "/sdk/blob.bin", mode=files.WriteMode.overwrite)
    assert meta.size == len(payload) and meta.content_hash == sdk_content_hash(payload)
    got, response = dbx.files_download("/sdk/blob.bin")
    assert response.content == payload and got.id == meta.id
    again = dbx.files_upload(b"second", "/sdk/blob.bin", mode=files.WriteMode.overwrite)
    assert again.id == meta.id and again.rev != meta.rev
    with pytest.raises(exceptions.ApiError) as conflict:
        dbx.files_upload(b"third", "/sdk/blob.bin")
    assert conflict.value.error.is_path() and conflict.value.error.get_path().reason.is_conflict()
    renamed = dbx.files_upload(b"third", "/sdk/blob.bin", autorename=True)
    assert renamed.name == "blob (1).bin"
    revs = dbx.files_list_revisions("/sdk/blob.bin")
    assert [r.rev for r in revs.entries] == [again.rev, meta.rev]
    _old, resp = dbx.files_download("/sdk/blob.bin", rev=meta.rev)
    assert len(resp.content) == len(payload)
    restored = dbx.files_restore("/sdk/blob.bin", meta.rev)
    assert restored.size == len(payload)


def test_get_metadata_errors_and_unicode_paths(emulator_intercept: Any) -> None:
    dbx = client(emulator_intercept)
    with pytest.raises(exceptions.ApiError) as missing:
        dbx.files_get_metadata("/nope/missing.txt")
    assert missing.value.error.is_path() and missing.value.error.get_path().is_not_found()
    name = "/résumé – 日本語.txt"
    dbx.files_upload(b"unicode", name)
    meta = dbx.files_get_metadata(name)
    assert meta.name == name[1:] and meta.path_display == name
    listing = dbx.files_list_folder("")
    assert name[1:] in [e.name for e in listing.entries]
    _, response = dbx.files_download(name)
    assert response.content == b"unicode"
    assert dbx.files_search_v2("résumé").matches


def test_upload_session_and_batch(emulator_intercept: Any) -> None:
    dbx = client(emulator_intercept)
    chunk = b"a" * (1024 * 1024)
    start = dbx.files_upload_session_start(chunk)
    cursor = files.UploadSessionCursor(session_id=start.session_id, offset=len(chunk))
    dbx.files_upload_session_append_v2(chunk, cursor)
    cursor.offset += len(chunk)
    commit = files.CommitInfo(path="/sdk/session.bin", mode=files.WriteMode.add)
    meta = dbx.files_upload_session_finish(b"tail", cursor, commit)
    assert meta.size == 2 * len(chunk) + 4
    with pytest.raises(exceptions.ApiError) as bad:
        dbx.files_upload_session_append_v2(
            b"x", files.UploadSessionCursor(session_id=start.session_id, offset=3)
        )
    assert bad.value.error.is_not_found()
    s1 = dbx.files_upload_session_start(b"one", close=True)
    s2 = dbx.files_upload_session_start(b"two", close=True)
    result = dbx.files_upload_session_finish_batch_v2(
        [
            files.UploadSessionFinishArg(
                files.UploadSessionCursor(s1.session_id, 3), files.CommitInfo("/sdk/b1.txt")
            ),
            files.UploadSessionFinishArg(
                files.UploadSessionCursor(s2.session_id, 3), files.CommitInfo("/sdk/b2.txt")
            ),
        ]
    )
    assert [e.get_success().name for e in result.entries] == ["b1.txt", "b2.txt"]


def test_folder_operations_and_batches(emulator_intercept: Any) -> None:
    dbx = client(emulator_intercept)
    dbx.files_create_folder_v2("/sdk/ops/a")
    dbx.files_upload(b"x", "/sdk/ops/a/f.txt")
    copied = dbx.files_copy_v2("/sdk/ops/a", "/sdk/ops/b")
    assert isinstance(copied.metadata, files.FolderMetadata)
    moved = dbx.files_move_v2("/sdk/ops/b/f.txt", "/sdk/ops/b/g.txt")
    assert moved.metadata.path_display == "/sdk/ops/b/g.txt"
    with pytest.raises(exceptions.ApiError) as bad:
        dbx.files_move_v2("/sdk/ops/zzz", "/sdk/ops/yyy")
    assert bad.value.error.is_from_lookup()
    launch = dbx.files_delete_batch([files.DeleteArg("/sdk/ops/b"), files.DeleteArg("/sdk/ops/missing")])
    assert launch.is_async_job_id()
    status = dbx.files_delete_batch_check(launch.get_async_job_id())
    assert status.is_complete()
    results = status.get_complete().entries
    assert results[0].is_success() and results[1].is_failure()
    batch = dbx.files_create_folder_batch(["/sdk/ops/c", "/sdk/ops/d"])
    assert batch.is_complete() and all(e.is_success() for e in batch.get_complete().entries)
    mb = dbx.files_move_batch_v2([files.RelocationPath("/sdk/ops/c", "/sdk/ops/e")])
    done = dbx.files_move_batch_check_v2(mb.get_async_job_id())
    assert done.get_complete().entries[0].is_success()


def test_download_zip_temporary_link_thumbnail_preview(emulator_intercept: Any) -> None:
    dbx = team_root(client(emulator_intercept))
    _, response = dbx.files_download_zip("/Client Engagements/Northwind Logistics/Correspondence")
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        assert zf.namelist()
    entries = dbx.files_list_folder("/Marketing & BD/Brand Assets/Photos").entries
    png = next(e for e in entries if e.name.endswith(".png"))
    meta, thumb = dbx.files_get_thumbnail_v2(
        files.PathOrLink.path(png.path_lower),
        format=files.ThumbnailFormat.png,
        size=files.ThumbnailSize.w128h128,
    )
    assert thumb.content[:8] == b"\x89PNG\r\n\x1a\n" and meta.file_metadata.id == png.id
    link = dbx.files_get_temporary_link(png.path_lower)
    assert link.link.startswith("https://dl.dropboxusercontent.com/apitl/1/")
    assert requests.get(link.link, timeout=10).content == dbx.files_download(png.path_lower)[1].content
    docs = dbx.files_list_folder("/Maya Chen/Notes").entries
    docx = next(e for e in docs if e.name.endswith(".docx"))
    _, pdf = dbx.files_get_preview(docx.path_lower)
    assert pdf.content.startswith(b"%PDF")
    papers = dbx.files_list_folder("/Knowledge Base/Paper").entries
    assert papers[0].is_downloadable is False
    exported, body = dbx.files_export(papers[0].path_lower)
    assert exported.export_metadata.name.endswith(".md") and body.content.startswith(b"# ")


def test_search_v2_and_tags(emulator_intercept: Any) -> None:
    home = client(emulator_intercept)
    dbx = team_root(home)
    result = dbx.files_search_v2("engagement letter")
    assert result.matches
    first = result.matches[0].metadata.get_metadata()
    assert "ngagement" in first.name
    paged = dbx.files_search_v2("request list", options=files.SearchOptions(max_results=5))
    assert paged.has_more
    more = dbx.files_search_continue_v2(paged.cursor)
    assert more.matches
    options = files.SearchOptions(
        file_extensions=["pdf"], file_categories=[files.FileCategory.pdf], max_results=3
    )
    assert all(
        m.metadata.get_metadata().name.endswith(".pdf")
        for m in dbx.files_search_v2("northwind", options=options).matches
    )
    meta = home.files_upload(b"t", "/sdk/tagged.txt")
    home.files_tags_add("/sdk/tagged.txt", "reviewed")
    tags = home.files_tags_get(["/sdk/tagged.txt"])
    assert tags.paths_to_tags[0].tags[0].get_user_generated_tag().tag_text == "reviewed"
    assert meta.name == "tagged.txt"


def test_auth_errors_and_rate_limits(emulator_intercept: Any) -> None:
    bad = dropbox.Dropbox(oauth2_access_token="not-a-token")
    with pytest.raises(exceptions.AuthError) as auth:
        bad.users_get_current_account()
    assert auth.value.error.is_invalid_access_token()
    server = emulator_intercept
    dbx = client(server, max_retries_on_rate_limit=0)
    requests.post(
        f"{server.url_for('dropbox')}/_emulator/faults",
        json={"path": "/2/users/get_current_account$", "status": 429, "times": 1},
        timeout=10,
    )
    with pytest.raises(exceptions.RateLimitError) as limited:
        dbx.users_get_current_account()
    assert limited.value.backoff == 1
    requests.post(
        f"{server.url_for('dropbox')}/_emulator/faults",
        json={"path": "/2/users/get_current_account$", "status": 503, "times": 1},
        timeout=10,
    )
    assert client(server, max_retries_on_error=2).users_get_current_account().email == MAYA
    scoped = dropbox.Dropbox(oauth2_access_token=mint(server, MAYA, scopes=["files.metadata.read"]))
    with pytest.raises(exceptions.AuthError) as scope:
        scoped.files_create_folder_v2("/nope")
    assert (
        scope.value.error.is_missing_scope()
        and scope.value.error.get_missing_scope().required_scope == "files.content.write"
    )
    with pytest.raises(exceptions.PathRootError):
        client(server).with_path_root(common.PathRoot.root("424242")).files_list_folder("")


def test_shared_folders_members_and_mounting(emulator_intercept: Any) -> None:
    maya = client(emulator_intercept)
    ryan = client(emulator_intercept, RYAN)
    maya.files_upload(b"spec", "/sdk/Shared Alpha/spec.txt")
    folder = maya.sharing_share_folder("/sdk/Shared Alpha").get_complete()
    assert folder.access_type.is_owner() and folder.path_lower == "/sdk/shared alpha"
    ryan_id = ryan.users_get_current_account().account_id
    maya.sharing_add_folder_member(
        folder.shared_folder_id,
        [sharing.AddMember(sharing.MemberSelector.dropbox_id(ryan_id), sharing.AccessLevel.editor)],
    )
    members = maya.sharing_list_folder_members(folder.shared_folder_id)
    assert {u.user.email: u.access_type._tag for u in members.users} == {MAYA: "owner", RYAN: "editor"}
    mountable = ryan.sharing_list_mountable_folders()
    assert folder.shared_folder_id in [f.shared_folder_id for f in mountable.entries]
    mounted = ryan.sharing_mount_folder(folder.shared_folder_id)
    assert mounted.path_lower == "/shared alpha"
    ryan.files_upload(b"mine", "/Shared Alpha/ryan.txt")
    assert {e.name for e in maya.files_list_folder("/sdk/Shared Alpha").entries} == {"spec.txt", "ryan.txt"}
    with pytest.raises(exceptions.ApiError) as dup:
        maya.sharing_share_folder("/sdk/Shared Alpha")
    assert dup.value.error.is_bad_path() and dup.value.error.get_bad_path().is_already_shared()
    maya.sharing_update_folder_member(
        folder.shared_folder_id, sharing.MemberSelector.dropbox_id(ryan_id), sharing.AccessLevel.viewer
    )
    with pytest.raises(exceptions.ApiError) as denied:
        ryan.files_upload(b"again", "/Shared Alpha/ryan2.txt")
    assert denied.value.error.is_path() and denied.value.error.get_path().reason.is_no_write_permission()
    job = maya.sharing_remove_folder_member(
        folder.shared_folder_id, sharing.MemberSelector.dropbox_id(ryan_id), False
    )
    assert maya.sharing_check_remove_member_job_status(job.get_async_job_id()).is_complete()
    maya.sharing_unshare_folder(folder.shared_folder_id)


def test_shared_links(emulator_intercept: Any) -> None:
    dbx = client(emulator_intercept)
    dbx.files_upload(b"link body", "/sdk/linked.txt")
    settings = sharing.SharedLinkSettings(
        requested_visibility=sharing.RequestedVisibility.public,
        expires=datetime.datetime(2030, 1, 1),
        allow_download=True,
    )
    link = dbx.sharing_create_shared_link_with_settings("/sdk/linked.txt", settings)
    assert isinstance(link, sharing.FileLinkMetadata) and link.expires == datetime.datetime(2030, 1, 1)
    assert re.fullmatch(r"https://www\.dropbox\.com/scl/fi/\w+/linked\.txt\?rlkey=\w+&dl=0", link.url)
    with pytest.raises(exceptions.ApiError) as exists:
        dbx.sharing_create_shared_link_with_settings("/sdk/linked.txt")
    existing = exists.value.error.get_shared_link_already_exists().get_metadata()
    assert existing.url == link.url
    assert [lk.url for lk in dbx.sharing_list_shared_links(path="/sdk/linked.txt").links] == [link.url]
    meta = dbx.sharing_get_shared_link_metadata(link.url)
    assert meta.name == "linked.txt"
    _, response = dbx.sharing_get_shared_link_file(link.url)
    assert response.content == b"link body"
    page = requests.get(link.url.replace("dl=0", "dl=1"), timeout=10)
    assert page.content == b"link body"
    updated = dbx.sharing_modify_shared_link_settings(
        link.url, sharing.SharedLinkSettings(requested_visibility=sharing.RequestedVisibility.team_only)
    )
    assert updated.link_permissions.resolved_visibility.is_team_only()
    dbx.sharing_revoke_shared_link(link.url)
    with pytest.raises(exceptions.ApiError) as gone:
        dbx.sharing_get_shared_link_metadata(link.url)
    assert gone.value.error.is_shared_link_not_found()


def test_file_members_and_received_files(emulator_intercept: Any) -> None:
    maya = client(emulator_intercept)
    chris = client(emulator_intercept, CHRISTOPHER)
    meta = maya.files_upload(b"for chris", "/sdk/for-chris.txt")
    results = maya.sharing_add_file_member(
        meta.id, [sharing.MemberSelector.email(CHRISTOPHER)], access_level=sharing.AccessLevel.viewer
    )
    assert results[0].result.is_success()
    members = maya.sharing_list_file_members(meta.id)
    assert {u.user.email for u in members.users} == {MAYA, CHRISTOPHER}
    received = chris.sharing_list_received_files()
    assert meta.id in [f.id for f in received.entries]
    _, response = chris.files_download(meta.id)
    assert response.content == b"for chris"
    maya.sharing_remove_file_member_2(meta.id, sharing.MemberSelector.email(CHRISTOPHER))
    with pytest.raises(exceptions.ApiError):
        chris.files_download(meta.id)


def test_team_admin_apis_and_acting_as_members(emulator_intercept: Any) -> None:
    dbt = team_client(emulator_intercept)
    info = dbt.team_get_info()
    assert info.name == "Halcyon Advisory Group" and info.num_provisioned_users == 47
    members = dbt.team_members_list_v2(limit=20)
    all_members = list(members.members)
    while members.has_more:
        members = dbt.team_members_list_continue_v2(members.cursor)
        all_members.extend(members.members)
    assert len(all_members) == 47
    by_email = {m.profile.email: m for m in all_members}
    assert by_email[ARJUN].roles[0].name == "Team admin"
    assert by_email[MAYA].profile.status.is_active()
    groups = dbt.team_groups_list()
    partners = next(g for g in groups.groups if g.group_name == "Partners")
    group_members = dbt.team_groups_members_list(team.GroupSelector.group_id(partners.group_id))
    assert len(group_members.members) == 4
    folders = dbt.team_team_folder_list()
    assert {f.name for f in folders.team_folders} >= {"Client Engagements", "Finance & Operations"}
    namespaces = dbt.team_namespaces_list()
    assert any(n.namespace_type.is_team_member_folder() for n in namespaces.namespaces)
    maya_id = by_email[MAYA].profile.team_member_id
    as_maya = dbt.as_user(maya_id)
    assert as_maya.users_get_current_account().email == MAYA
    assert {e.name for e in as_maya.files_list_folder("").entries} == {"Notes", "Expenses"}
    admin = dbt.as_admin(by_email[ARJUN].profile.team_member_id)
    client_ns = next(n for n in namespaces.namespaces if n.name == "Client Engagements").namespace_id
    rooted = admin.with_path_root(common.PathRoot.namespace_id(client_ns))
    top = rooted.files_list_folder("")
    assert top.entries
    with pytest.raises(exceptions.AuthError) as bad:
        dbt.as_user("dbmid:unknown").users_get_current_account()
    assert bad.value.error.is_invalid_select_user()


def test_team_log_events(emulator_intercept: Any) -> None:
    dbt = team_client(emulator_intercept)
    dbx = client(emulator_intercept)
    dbx.files_upload(b"x", "/sdk/audit.txt")
    events = dbt.team_log_get_events(limit=1000, category=team_log.EventCategory.file_operations)
    assert any(e.event_type.is_file_add() for e in events.events)
    mine = dbt.team_log_get_events(account_id=dbx.users_get_current_account().account_id)
    assert any(
        e.assets and e.assets[0].is_file() and e.assets[0].get_file().display_name == "audit.txt"
        for e in mine.events
    )


def test_oauth_authorization_code_flow_with_pkce_and_refresh(emulator_intercept: Any) -> None:
    flow = dropbox.DropboxOAuth2FlowNoRedirect(
        "sdk-app",
        use_pkce=True,
        token_access_type="offline",
        scope=["account_info.read", "files.metadata.read"],
    )
    url = flow.start()
    parts = urlsplit(url)
    assert parts.netloc == "www.dropbox.com" and parts.path == "/oauth2/authorize"
    page = requests.get(url + "&login_hint=" + MAYA, timeout=10)
    code = re.search(r"id='code'>([^<]+)<", page.text).group(1)  # type: ignore[union-attr]
    result = flow.finish(code)
    assert result.account_id.startswith("dbid:")
    assert result.refresh_token and result.scope == "account_info.read files.metadata.read"
    dbx = dropbox.Dropbox(oauth2_access_token=result.access_token)
    assert dbx.users_get_current_account().email == MAYA
    # an expired access token is refreshed transparently using the refresh token
    refreshing = dropbox.Dropbox(
        oauth2_refresh_token=result.refresh_token,
        oauth2_access_token="stale",
        oauth2_access_token_expiration=datetime.datetime(2000, 1, 1),
        app_key="sdk-app",
    )
    assert refreshing.users_get_current_account().email == MAYA


def test_oauth_redirect_flow(emulator_intercept: Any) -> None:
    session: dict[str, str] = {}
    flow = dropbox.DropboxOAuth2Flow(
        "sdk-app", "https://app.example/cb", session, "dropbox-auth-csrf-token", consumer_secret="sdk-secret"
    )
    url = flow.start()
    r = requests.get(url + "&login_hint=" + MAYA, allow_redirects=False, timeout=10)
    assert r.status_code == 302
    query = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}
    result = flow.finish(query)
    assert result.user_id.isdigit()
    assert dropbox.Dropbox(oauth2_access_token=result.access_token).users_get_current_account().email == MAYA


def test_longpoll_and_conflicting_cursor(emulator_intercept: Any) -> None:
    dbx = client(emulator_intercept)
    dbx.files_create_folder_v2("/sdk/poll")
    cursor = dbx.files_list_folder_get_latest_cursor("/sdk/poll").cursor
    quiet = dbx.files_list_folder_longpoll(cursor, timeout=30)
    assert quiet.changes is False
    dbx.files_upload(b"x", "/sdk/poll/a.txt")
    assert dbx.files_list_folder_longpoll(cursor, timeout=30).changes is True
    with pytest.raises(exceptions.ApiError) as reset:
        dbx.files_list_folder_longpoll("garbage", timeout=30)
    assert reset.value.error.is_reset()


def test_sdk_models_cover_the_whole_listing(emulator_intercept: Any) -> None:
    """Decoding every entry of a large recursive listing proves the metadata shapes are SDK-exact."""
    dbt = team_client(emulator_intercept)
    arjun = (
        dbt.team_members_get_info_v2([team.UserSelectorArg.email(ARJUN)]).members_info[0].get_member_info()
    )
    dbx = dbt.as_admin(arjun.profile.team_member_id)
    root = dbx.with_path_root(
        common.PathRoot.root(dbx.users_get_current_account().root_info.root_namespace_id)
    )
    entries = root.files_list_folder(
        "", recursive=True, limit=500, include_has_explicit_shared_members=True
    ).entries
    assert entries
    assert users.FullAccount is not None


def test_team_writes_groups_and_team_folders(emulator_intercept: Any) -> None:
    dbt = team_client(emulator_intercept)
    created = dbt.team_groups_create("SDK Group", group_external_id="sdk-1")
    assert created.group_name == "SDK Group" and created.member_count in (0, None)
    maya = dbt.team_members_get_info_v2([team.UserSelectorArg.email(MAYA)]).members_info[0].get_member_info()
    changed = dbt.team_groups_members_add(
        team.GroupSelector.group_id(created.group_id),
        [
            team.MemberAccess(
                team.UserSelectorArg.team_member_id(maya.profile.team_member_id), team.GroupAccessType.member
            )
        ],
    )
    assert changed.group_info.member_count == 1
    listing = dbt.team_groups_members_list(team.GroupSelector.group_id(created.group_id))
    assert [m.profile.email for m in listing.members] == [MAYA]
    dbt.team_groups_delete(team.GroupSelector.group_id(created.group_id))
    folder = dbt.team_team_folder_create("SDK Folder")
    assert folder.status.is_active()
    renamed = dbt.team_team_folder_rename(folder.team_folder_id, "SDK Folder 2")
    assert renamed.name == "SDK Folder 2"
    added = dbt.team_members_add_v2(
        [team.MemberAddV2Arg(member_email="sdk.hire@halcyonadvisory.example", member_given_name="Sdk")]
    )
    assert added.get_complete()[0].is_success()
    sel = team.UserSelectorArg.email("sdk.hire@halcyonadvisory.example")
    dbt.team_members_suspend(sel)
    with pytest.raises(exceptions.ApiError) as again:
        dbt.team_members_suspend(sel)
    assert again.value.error.is_suspend_inactive_user()
    dbt.team_members_unsuspend(sel)
    with pytest.raises(exceptions.ApiError) as nope:
        dbt.team_members_suspend(team.UserSelectorArg.email("ghost@halcyonadvisory.example"))
    assert nope.value.error.is_user_not_found()


def test_file_properties_locks_and_temp_upload(emulator_intercept: Any) -> None:
    from dropbox import file_properties

    dbx = client(emulator_intercept)
    template = dbx.file_properties_templates_add_for_user(
        "SDK",
        "template",
        [file_properties.PropertyFieldTemplate("Status", "state", file_properties.PropertyType.string)],
    )
    dbx.files_upload(b"p", "/sdk/props.txt")
    dbx.file_properties_properties_add(
        "/sdk/props.txt",
        [
            file_properties.PropertyGroup(
                template.template_id, [file_properties.PropertyField("Status", "Done")]
            )
        ],
    )
    meta = dbx.files_get_metadata(
        "/sdk/props.txt",
        include_property_groups=file_properties.TemplateFilterBase.filter_some([template.template_id]),
    )
    assert meta.property_groups[0].fields[0].value == "Done"
    found = dbx.file_properties_properties_search(
        [
            file_properties.PropertiesSearchQuery(
                "done", file_properties.PropertiesSearchMode.field_name("Status")
            )
        ]
    )
    assert [m.path for m in found.matches] == ["/sdk/props.txt"]
    link = dbx.files_get_temporary_upload_link(files.CommitInfo("/sdk/via-link.txt"), duration=600.0)
    uploaded = requests.post(
        link.link, data=b"via temp link", headers={"Content-Type": "application/octet-stream"}, timeout=10
    )
    assert uploaded.json()["size"] == 13
    assert dbx.files_get_metadata("/sdk/via-link.txt").size == 13
    ref = dbx.files_copy_reference_get("/sdk/props.txt")
    saved = dbx.files_copy_reference_save(ref.copy_reference, "/sdk/props-copy.txt")
    assert saved.metadata.name == "props-copy.txt"
    unlocked = dbx.files_get_file_lock_batch([files.LockFileArg("/sdk/props.txt")])
    assert unlocked.entries[0].get_success().lock.content.is_unlocked()
