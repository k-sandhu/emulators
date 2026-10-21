"""Official ``box-sdk-gen`` against the Box emulator.

Run with ``EMULATORS_PROVIDERS=box uv run pytest tests/sdk/test_box_sdk.py``: the ``emulator_intercept``
fixture reroutes ``api.box.com``, ``upload.box.com``, ``account.box.com``, ``dl.boxcloud.com`` and friends
to the in-process gateway, so the SDK runs unchanged.
"""

from __future__ import annotations

import hashlib
import io
import json
import threading
import time
import uuid
import zipfile
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import requests
from fastapi import FastAPI, Request

pytestmark = pytest.mark.sdk

box_sdk_gen = pytest.importorskip("box_sdk_gen")

from box_sdk_gen import (  # noqa: E402
    AddShareLinkToFileSharedLink,
    AddShareLinkToFileSharedLinkAccessField,
    AddShareLinkToFileSharedLinkPermissionsField,
    BoxAPIError,
    BoxCCGAuth,
    BoxClient,
    BoxDeveloperTokenAuth,
    BoxJWTAuth,
    BoxOAuth,
    CCGConfig,
    CopyFileParent,
    CreateCollaborationAccessibleBy,
    CreateCollaborationAccessibleByTypeField,
    CreateCollaborationItem,
    CreateCollaborationItemTypeField,
    CreateCollaborationRole,
    CreateCommentItem,
    CreateCommentItemTypeField,
    CreateFolderFolderUploadEmail,
    CreateFolderFolderUploadEmailAccessField,
    CreateFolderParent,
    CreateFolderSyncState,
    CreateGroupMembershipGroup,
    CreateGroupMembershipUser,
    CreateTaskItem,
    CreateTaskItemTypeField,
    CreateWebhookTarget,
    CreateWebhookTargetTypeField,
    CreateWebhookTriggers,
    CreateZipDownloadItems,
    CreateZipDownloadItemsTypeField,
    JWTConfig,
    OAuthConfig,
    UpdateCollaborationByIdRole,
    UpdateFileByIdCollections,
    UpdateFileByIdLock,
    UploadFileAttributes,
    UploadFileAttributesParentField,
    UploadFileVersionAttributes,
    WebhooksManager,
)
from box_sdk_gen.internal.null_value import NullValue  # noqa: E402

ENTERPRISE_ID = "336533359"
CLIENT_ID, CLIENT_SECRET = "halcyon-box-app", "box-client-secret"


@pytest.fixture
def admin(emulator_intercept) -> BoxClient:
    return BoxClient(auth=BoxDeveloperTokenAuth(token="box-dev-p035"))


@pytest.fixture
def maya(emulator_intercept) -> BoxClient:
    return BoxClient(auth=BoxDeveloperTokenAuth(token="box-dev-p018"))


def token_status(token: str) -> int:
    """HTTP status of ``GET /users/me`` with a raw bearer token."""
    response = requests.get(
        "https://api.box.com/2.0/users/me", headers={"Authorization": f"Bearer {token}"}, timeout=10
    )
    return response.status_code


def test_folder_creation_options_decode_and_persist_in_sdk(admin: BoxClient) -> None:
    folder = admin.folders.create_folder(
        "SDK synced folder",
        CreateFolderParent(id="0"),
        sync_state=CreateFolderSyncState.SYNCED,
        folder_upload_email=CreateFolderFolderUploadEmail(
            access=CreateFolderFolderUploadEmailAccessField.COLLABORATORS
        ),
        fields=["sync_state", "folder_upload_email"],
    )
    assert folder.sync_state.value == "synced"
    assert folder.folder_upload_email.access.value == "collaborators"
    stored = admin.folders.get_folder_by_id(folder.id, fields=["sync_state"])
    assert stored.sync_state.value == "synced"


def folder_named(client: BoxClient, parent: str, name: str) -> str:
    items = client.folders.get_folder_items(parent, limit=1000)
    return next(e.id for e in items.entries if e.name == name)


def upload(client: BoxClient, parent: str, name: str, data: bytes):
    attributes = UploadFileAttributes(name=name, parent=UploadFileAttributesParentField(id=parent))
    return client.uploads.upload_file(attributes, io.BytesIO(data)).entries[0]


def sandbox(client: BoxClient) -> str:
    technology = folder_named(client, "0", "Technology")
    return client.folders.create_folder(f"sdk-{uuid.uuid4().hex[:8]}", CreateFolderParent(id=technology)).id


# -- authentication ------------------------------------------------------------------------------


def test_developer_token_user_and_root(admin: BoxClient) -> None:
    me = admin.users.get_user_me()
    assert me.login == "arjun.mehta@halcyonadvisory.example"
    assert me.name == "Arjun Mehta" and me.role.value == "admin"
    assert me.enterprise.id == ENTERPRISE_ID
    items = admin.folders.get_folder_items("0")
    assert any(e.name == "Client Engagements" for e in items.entries)
    assert items.total_count == len(items.entries)


def test_client_credentials_grant_enterprise_and_user_subjects(emulator_intercept) -> None:
    config = CCGConfig(client_id=CLIENT_ID, client_secret=CLIENT_SECRET, enterprise_id=ENTERPRISE_ID)
    auth = BoxCCGAuth(config)
    client = BoxClient(auth=auth)
    service = client.users.get_user_me()
    assert service.name == "Halcyon Box Automation" and service.login.endswith("@boxdevedition.com")
    assert client.users.get_users(limit=2).total_count > 40  # the service account may manage users
    maya = client.users.get_users(filter_term="Maya").entries[0]
    as_user = BoxClient(auth=auth.with_user_subject(maya.id))
    assert as_user.users.get_user_me().login == "maya.chen@halcyonadvisory.example"
    acting = client.with_as_user_header(maya.id)
    assert acting.users.get_user_me().id == maya.id
    first_token = auth.retrieve_token().access_token
    assert token_status(first_token) == 200
    auth.revoke_token()
    assert token_status(first_token) == 401
    assert client.users.get_user_me().name == "Halcyon Box Automation"  # a new token is fetched transparently


def test_ccg_with_a_wrong_enterprise_is_rejected(emulator_intercept) -> None:
    client = BoxClient(
        auth=BoxCCGAuth(CCGConfig(client_id=CLIENT_ID, client_secret="s", enterprise_id="1234"))
    )
    with pytest.raises(BoxAPIError) as raised:
        client.users.get_user_me()
    assert raised.value.response_info.status_code == 400
    assert raised.value.response_info.body["error"] == "invalid_grant"


def test_authorization_code_flow_with_refresh_rotation(emulator_intercept) -> None:
    auth = BoxOAuth(OAuthConfig(client_id=CLIENT_ID, client_secret=CLIENT_SECRET))
    url = auth.get_authorize_url()
    assert url.startswith("https://account.box.com/api/oauth2/authorize")
    response = requests.get(
        url,
        params={
            "login_hint": "maya.chen@halcyonadvisory.example",
            "redirect_uri": "https://app.example.test/cb",
        },
        allow_redirects=False,
        timeout=10,
    )
    assert response.status_code == 302, response.text
    code = parse_qs(urlsplit(response.headers["location"]).query)["code"][0]
    token = auth.get_tokens_authorization_code_grant(code)
    assert token.refresh_token and token.access_token
    client = BoxClient(auth=auth)
    assert client.users.get_user_me().login == "maya.chen@halcyonadvisory.example"
    refreshed = auth.refresh_token()
    assert refreshed.refresh_token != token.refresh_token and refreshed.access_token != token.access_token
    assert client.users.get_user_me().name == "Maya Chen"
    assert token_status(token.access_token) == 401  # the old access token died with the rotation


def test_jwt_auth_for_enterprise_and_user_subjects(emulator_intercept) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.BestAvailableEncryption(b"passphrase"),
    ).decode()
    config = JWTConfig(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        jwt_key_id="kid-1",
        private_key=pem,
        private_key_passphrase="passphrase",
        enterprise_id=ENTERPRISE_ID,
    )
    auth = BoxJWTAuth(config)
    assert BoxClient(auth=auth).users.get_user_me().name == "Halcyon Box Automation"
    maya = BoxClient(auth=auth).users.get_users(filter_term="Maya").entries[0]
    user_client = BoxClient(auth=auth.with_user_subject(maya.id))
    assert user_client.users.get_user_me().id == maya.id


def test_downscoped_token_is_limited_to_its_resource(emulator_intercept, admin: BoxClient) -> None:
    root = sandbox(admin)
    target = upload(admin, root, "downscope.txt", b"scoped")
    other = upload(admin, root, "other.txt", b"other")
    owner_id = admin.users.get_user_me().id
    auth = BoxCCGAuth(
        CCGConfig(client_id=CLIENT_ID, client_secret="s", enterprise_id=ENTERPRISE_ID, user_id=owner_id)
    )
    scoped = auth.downscope_token(["item_preview"], resource=f"https://api.box.com/2.0/files/{target.id}")
    assert scoped.access_token and scoped.restricted_to[0].scope.value == "item_preview"
    client = BoxClient(auth=BoxDeveloperTokenAuth(token=scoped.access_token))
    assert client.files.get_file_by_id(target.id).name == "downscope.txt"
    with pytest.raises(BoxAPIError) as no_edit:
        client.files.update_file_by_id(target.id, description="nope")
    assert no_edit.value.response_info.status_code == 403
    with pytest.raises(BoxAPIError) as no_other:
        client.files.get_file_by_id(other.id)
    assert no_other.value.response_info.status_code == 404


def test_errors_surface_as_box_api_errors(admin: BoxClient) -> None:
    with pytest.raises(BoxAPIError) as raised:
        admin.files.get_file_by_id("999")
    error = raised.value
    assert error.response_info.status_code == 404 and error.response_info.code == "not_found"
    assert error.response_info.request_id and "404" in error.message
    technology = folder_named(admin, "0", "Technology")
    with pytest.raises(BoxAPIError) as conflict:
        admin.folders.create_folder("Technology", CreateFolderParent(id="0"))
    assert conflict.value.response_info.status_code == 409
    assert conflict.value.response_info.code == "item_name_in_use"
    assert conflict.value.response_info.context_info["conflicts"][0]["id"] == technology
    with pytest.raises(BoxAPIError) as stale:
        admin.folders.update_folder_by_id(technology, name="Renamed", if_match="99")
    assert stale.value.response_info.status_code == 412


# -- folders and files ---------------------------------------------------------------------------


def test_folder_lifecycle_and_trash(admin: BoxClient) -> None:
    root = sandbox(admin)
    child = admin.folders.create_folder("Child", CreateFolderParent(id=root))
    assert child.name == "Child" and child.parent.id == root
    renamed = admin.folders.update_folder_by_id(
        child.id, name="Renamed", description="d", fields=["name", "description"]
    )
    assert renamed.name == "Renamed" and renamed.description == "d"
    copy = admin.folders.copy_folder(child.id, CreateFolderParent(id=root), name="Child copy")
    assert copy.id != child.id
    page = admin.folders.get_folder_items(root, usemarker=True, limit=1)
    assert len(page.entries) == 1 and page.next_marker
    more = admin.folders.get_folder_items(root, usemarker=True, limit=1, marker=page.next_marker)
    assert more.entries[0].id != page.entries[0].id
    admin.folders.delete_folder_by_id(root, recursive=True)
    with pytest.raises(BoxAPIError) as gone:
        admin.folders.get_folder_by_id(root)
    assert gone.value.response_info.code == "trashed"
    assert admin.trashed_folders.get_trashed_folder_by_id(root).item_status.value == "trashed"
    assert admin.trashed_folders.restore_folder_from_trash(root).id == root
    admin.folders.delete_folder_by_id(root, recursive=True)
    admin.trashed_folders.delete_trashed_folder_by_id(root)
    with pytest.raises(BoxAPIError):
        admin.trashed_folders.get_trashed_folder_by_id(root)


def test_upload_download_versions_copy_and_trash(admin: BoxClient) -> None:
    root = sandbox(admin)
    content = b"first version"
    file = upload(admin, root, "doc.txt", content)
    assert file.size == len(content) and file.sha_1 == hashlib.sha1(content).hexdigest()
    assert admin.downloads.download_file(file.id).read() == content
    assert admin.downloads.get_download_file_url(file.id).startswith("https://dl.boxcloud.com/d/1/")
    second = admin.uploads.upload_file_version(
        file.id, UploadFileVersionAttributes(name="doc.txt"), io.BytesIO(b"second")
    )
    assert second.entries[0].size == 6
    versions = admin.file_versions.get_file_versions(file.id)
    assert [v.version_number for v in versions.entries] == ["1"]
    assert admin.downloads.download_file(file.id, version=versions.entries[0].id).read() == content
    promoted = admin.file_versions.promote_file_version(file.id, id=versions.entries[0].id)
    assert promoted.version_number == "3"
    assert admin.downloads.download_file(file.id).read() == content
    copy = admin.files.copy_file(file.id, CopyFileParent(id=root), name="copy.txt")
    assert copy.name == "copy.txt" and copy.sha_1 == file.sha_1
    updated = admin.files.update_file_by_id(file.id, name="renamed.txt", description="desc")
    assert updated.name == "renamed.txt" and updated.description == "desc"
    full = admin.files.get_file_by_id(
        file.id, fields=["extension", "permissions", "version_number", "is_externally_owned"]
    )
    assert full.extension == "txt" and full.permissions.can_download is True and full.version_number == "3"
    with pytest.raises(BoxAPIError) as conflict:
        upload(admin, root, "renamed.txt", b"again")
    assert conflict.value.response_info.status_code == 409
    admin.files.delete_file_by_id(file.id)
    admin.trashed_files.restore_file_from_trash(file.id)
    assert admin.files.get_file_by_id(file.id).name == "renamed.txt"
    trash = admin.trashed_items.get_trashed_items()
    assert trash.entries == [] or file.id not in [e.id for e in trash.entries]


def test_seeded_documents_download_thumbnail_and_metadata(maya: BoxClient) -> None:
    clients = folder_named(maya, "0", "Client Engagements")
    northwind = folder_named(maya, clients, "Northwind Logistics")
    deliverables = folder_named(maya, folder_named(maya, northwind, "FY2025 Review"), "Deliverables")
    items = maya.folders.get_folder_items(deliverables, fields=["name", "size", "sha1"])
    letter = next(e for e in items.entries if e.name == "Management Letter.docx")
    data = maya.downloads.download_file(letter.id).read()
    assert hashlib.sha1(data).hexdigest() == letter.sha_1 and len(data) == letter.size
    from docx import Document

    assert Document(io.BytesIO(data)).paragraphs
    thumbnail = maya.files.get_file_thumbnail_by_id(letter.id, "png", min_height=128, min_width=128)
    assert thumbnail.read().startswith(b"\x89PNG")
    meta = maya.file_metadata.get_file_metadata_by_id(letter.id, "enterprise", "clientInfo")
    assert meta.extra_data["clientName"] == "Northwind Logistics"
    listing = maya.file_metadata.get_file_metadata(letter.id)
    assert [m.template for m in listing.entries] == ["clientInfo"]


def test_chunked_upload_with_multiple_parts(admin: BoxClient) -> None:
    requests.post(
        "https://api.box.com/_emulator/actions/configure_uploads", json={"part_size": 1000}, timeout=10
    ).raise_for_status()
    root = sandbox(admin)
    payload = bytes(range(256)) * 12 + b"tail"  # 3076 bytes -> four parts of at most 1000
    uploaded = admin.chunked_uploads.upload_big_file(io.BytesIO(payload), "chunked.bin", len(payload), root)
    assert uploaded.name == "chunked.bin" and uploaded.size == len(payload)
    assert uploaded.sha_1 == hashlib.sha1(payload).hexdigest()
    assert admin.downloads.download_file(uploaded.id).read() == payload


def test_locks_and_shared_links(admin: BoxClient, maya: BoxClient) -> None:
    root = sandbox(admin)
    file = upload(admin, root, "linked.txt", b"link me")
    locked = admin.files.update_file_by_id(file.id, lock=UpdateFileByIdLock(access="lock"), fields=["lock"])
    assert locked.lock.type.value == "lock"
    admin.files.update_file_by_id(file.id, lock=NullValue())
    assert admin.files.get_file_by_id(file.id, fields=["lock"]).lock is None
    linked = admin.shared_links_files.add_share_link_to_file(
        file.id,
        "shared_link",
        shared_link=AddShareLinkToFileSharedLink(
            access=AddShareLinkToFileSharedLinkAccessField.OPEN,
            password="link-password",
            permissions=AddShareLinkToFileSharedLinkPermissionsField(can_download=True),
        ),
    )
    assert linked.shared_link.access.value == "open" and linked.shared_link.is_password_enabled is True
    url = linked.shared_link.url
    found = maya.shared_links_files.find_file_for_shared_link(
        f"shared_link={url}&shared_link_password=link-password"
    )
    assert found.id == file.id
    with pytest.raises(BoxAPIError) as wrong:
        maya.shared_links_files.find_file_for_shared_link(f"shared_link={url}&shared_link_password=bad")
    assert wrong.value.response_info.status_code == 403
    removed = admin.shared_links_files.remove_shared_link_from_file(
        file.id, "shared_link", shared_link=NullValue()
    )
    assert removed.shared_link is None


# -- sharing and administration ----------------------------------------------------------------


def test_collaborations_grant_and_revoke_access(admin: BoxClient, emulator_intercept) -> None:
    root = sandbox(admin)
    rebecca_id = admin.users.get_users(filter_term="Rebecca").entries[0].id
    collab = admin.user_collaborations.create_collaboration(
        CreateCollaborationItem(type=CreateCollaborationItemTypeField.FOLDER, id=root),
        CreateCollaborationAccessibleBy(type=CreateCollaborationAccessibleByTypeField.USER, id=rebecca_id),
        CreateCollaborationRole.VIEWER,
    )
    assert collab.role.value == "viewer" and collab.status.value == "accepted"
    rebecca = BoxClient(auth=BoxDeveloperTokenAuth(token="box-dev-p044"))
    assert rebecca.folders.get_folder_by_id(root).id == root
    with pytest.raises(BoxAPIError) as forbidden:
        upload(rebecca, root, "no.txt", b"no")
    assert forbidden.value.response_info.status_code == 403
    admin.user_collaborations.update_collaboration_by_id(collab.id, role=UpdateCollaborationByIdRole.EDITOR)
    assert upload(rebecca, root, "yes.txt", b"yes").name == "yes.txt"
    assert [c.id for c in admin.list_collaborations.get_folder_collaborations(root).entries] == [collab.id]
    admin.user_collaborations.delete_collaboration_by_id(collab.id)
    with pytest.raises(BoxAPIError) as hidden:
        rebecca.folders.get_folder_by_id(root)
    assert hidden.value.response_info.status_code == 404


def test_users_groups_and_memberships(admin: BoxClient) -> None:
    users = admin.users.get_users(limit=1000)
    assert users.total_count > 45
    groups = admin.groups.get_groups(limit=1000)
    partners = next(g for g in groups.entries if g.name == "Partners")
    assert admin.memberships.get_group_memberships(partners.id).total_count == 4
    created = admin.groups.create_group("SDK Test Group", description="created by the SDK")
    maya = next(u for u in users.entries if u.name == "Maya Chen")
    membership = admin.memberships.create_group_membership(
        CreateGroupMembershipUser(id=maya.id), CreateGroupMembershipGroup(id=created.id)
    )
    assert membership.user.id == maya.id
    mine = admin.memberships.get_user_memberships(maya.id)
    assert created.id in [m.group.id for m in mine.entries]
    admin.memberships.delete_group_membership_by_id(membership.id)
    admin.groups.delete_group_by_id(created.id)
    contractor = admin.users.create_user("SDK Contractor", login="sdk.contractor@halcyonadvisory.example")
    assert contractor.status.value == "active"
    admin.users.delete_user_by_id(contractor.id)
    assert admin.avatars.get_user_avatar(maya.id).read().startswith(b"\x89PNG")


# -- search, events, webhooks ------------------------------------------------------------------------


def test_search_and_metadata_queries(admin: BoxClient) -> None:
    results = admin.search.search_for_content(query="engagement letter", limit=5)
    assert results.total_count > 5 and len(results.entries) == 5
    pdfs = admin.search.search_for_content(query="signed", file_extensions=["pdf"], limit=50)
    assert pdfs.entries and all(e.name.endswith(".pdf") for e in pdfs.entries)
    rows = admin.search.search_by_metadata_query(
        f"enterprise_{ENTERPRISE_ID}.clientInfo",
        "0",
        query="clientName = :name",
        query_params={"name": "Northwind Logistics"},
        fields=["name"],
    )
    assert rows.entries and all(e.type.value == "file" for e in rows.entries)


def test_events_and_long_poll_info(admin: BoxClient) -> None:
    start = admin.events.get_events(stream_position="now")
    assert start.entries == []
    root = sandbox(admin)
    upload(admin, root, "evented.txt", b"x")
    events = admin.events.get_events(stream_position=str(start.next_stream_position))
    kinds = [e.event_type.value for e in events.entries]
    assert kinds[-1] == "ITEM_UPLOAD" and "ITEM_CREATE" in kinds
    info = admin.events.get_events_with_long_polling()
    assert info.entries[0].type == "realtime_server" and "realtime.box.com" in info.entries[0].url


def test_event_stream_iterator_yields_new_events(admin: BoxClient, emulator_intercept) -> None:
    emulator_intercept.hub.context("box").settings["LONGPOLL_TIMEOUT"] = "2"
    root = sandbox(admin)
    stream = admin.events.get_event_stream()
    seen: list[str] = []

    def consume() -> None:
        for event in stream:
            name = getattr(event.source, "name", None)
            seen.append(name or "")
            if name == "streamed.txt":
                stream.stop()
                return

    worker = threading.Thread(target=consume, daemon=True)
    worker.start()
    time.sleep(0.5)
    upload(admin, root, "streamed.txt", b"x")
    worker.join(timeout=15)
    stream.stop()
    emulator_intercept.hub.context("box").settings.pop("LONGPOLL_TIMEOUT", None)
    assert "streamed.txt" in seen


def test_webhooks_manage_and_deliver_signed_payloads(admin: BoxClient, emulator_intercept) -> None:
    root = sandbox(admin)
    hook = admin.webhooks.create_webhook(
        CreateWebhookTarget(id=root, type=CreateWebhookTargetTypeField.FOLDER),
        "https://consumer.example.test/hook",
        [CreateWebhookTriggers.FILE_UPLOADED],
    )
    assert hook.target.id == root and hook.address == "https://consumer.example.test/hook"
    assert hook.id in [w.id for w in admin.webhooks.get_webhooks().entries]
    assert admin.webhooks.get_webhook_by_id(hook.id).triggers == [CreateWebhookTriggers.FILE_UPLOADED]
    updated = admin.webhooks.update_webhook_by_id(hook.id, address="https://consumer.example.test/other")
    assert updated.address == "https://consumer.example.test/other"
    admin.webhooks.delete_webhook_by_id(hook.id)
    with pytest.raises(BoxAPIError):
        admin.webhooks.get_webhook_by_id(hook.id)

    received: list[tuple[dict[str, str], str]] = []
    consumer = FastAPI()

    @consumer.post("/hook")
    async def endpoint(request: Request) -> dict[str, Any]:
        received.append((dict(request.headers), (await request.body()).decode()))
        return {}

    hub = emulator_intercept.hub
    hub.webhook_transport = httpx.ASGITransport(app=consumer)
    try:
        requests.post(
            "https://api.box.com/_emulator/actions/configure_webhook_keys",
            json={"primary": "primary-key", "secondary": "secondary-key"},
            timeout=10,
        ).raise_for_status()
        hook = admin.webhooks.create_webhook(
            CreateWebhookTarget(id=root, type=CreateWebhookTargetTypeField.FOLDER),
            "http://consumer.test/hook",
            [CreateWebhookTriggers.FILE_UPLOADED],
        )
        upload(admin, root, "signed.txt", b"hello")
        assert received, "the webhook should have been delivered synchronously"
        headers, body = received[0]
        assert WebhooksManager.validate_message(body, headers, "primary-key", secondary_key="secondary-key")
        assert WebhooksManager.validate_message(body, headers, "wrong-key") is False
        payload = json.loads(body)
        assert payload["trigger"] == "FILE.UPLOADED" and payload["webhook"]["id"] == hook.id
        assert payload["source"]["name"] == "signed.txt"
    finally:
        hub.webhook_transport = None


# -- remaining managers ---------------------------------------------------------------------------------------


def test_comments_tasks_collections_and_recent_items(admin: BoxClient) -> None:
    root = sandbox(admin)
    file = upload(admin, root, "review.txt", b"please review")
    comment = admin.comments.create_comment(
        "Looks good", CreateCommentItem(id=file.id, type=CreateCommentItemTypeField.FILE)
    )
    assert comment.message == "Looks good"
    assert [c.id for c in admin.comments.get_file_comments(file.id).entries] == [comment.id]
    admin.comments.update_comment_by_id(comment.id, message="Looks great")
    assert admin.comments.get_comment_by_id(comment.id).message == "Looks great"
    task = admin.tasks.create_task(
        CreateTaskItem(id=file.id, type=CreateTaskItemTypeField.FILE), message="Review it"
    )
    assert task.message == "Review it" and task.is_completed is False
    assert [t.id for t in admin.tasks.get_file_tasks(file.id).entries] == [task.id]
    admin.tasks.delete_task_by_id(task.id)
    favorites = admin.collections.get_collections().entries[0]
    assert favorites.name == "Favorites"
    admin.files.update_file_by_id(file.id, collections=[UpdateFileByIdCollections(id=favorites.id)])
    assert file.id in [e.id for e in admin.collections.get_collection_items(favorites.id).entries]
    assert file.id in [r.item.id for r in admin.recent_items.get_recent_items().entries]


def test_zip_download(admin: BoxClient) -> None:
    root = sandbox(admin)
    one = upload(admin, root, "one.txt", b"1")
    two = upload(admin, root, "two.txt", b"22")
    zipped = admin.zip_downloads.create_zip_download(
        [
            CreateZipDownloadItems(id=one.id, type=CreateZipDownloadItemsTypeField.FILE),
            CreateZipDownloadItems(id=two.id, type=CreateZipDownloadItemsTypeField.FILE),
        ],
        download_file_name="pair",
    )
    status = admin.zip_downloads.get_zip_download_status(zipped.status_url)
    assert status.state.value == "succeeded" and status.total_file_count == 2
    archive = admin.zip_downloads.get_zip_download_content(zipped.download_url).read()
    assert sorted(zipfile.ZipFile(io.BytesIO(archive)).namelist()) == ["one.txt", "two.txt"]


def test_sdk_pointed_at_the_path_prefix_base_urls(emulator_server, emulators) -> None:
    """No interception: the SDK's ``BaseUrls`` override talks to ``http://127.0.0.1:PORT/box``."""
    from box_sdk_gen import BaseUrls, NetworkSession

    base = emulator_server.url_for("box")
    session = NetworkSession(
        base_urls=BaseUrls(base_url=base, upload_url=f"{base}/api", oauth_2_url=f"{base}/api/oauth2")
    )
    client = BoxClient(auth=BoxDeveloperTokenAuth(token="box-dev-p035"), network_session=session)
    assert client.users.get_user_me().name == "Arjun Mehta"
    root = sandbox(client)
    payload = b"prefix mode"
    file = upload(client, root, "prefix.txt", payload)
    assert (
        client.downloads.download_file(file.id).read() == payload
    )  # follows the 302 to the prefixed boxcloud URL
    session_id = client.chunked_uploads.create_file_upload_session(root, 4, "tiny.bin")
    assert session_id.session_endpoints.commit.startswith(f"{base}/api/2.0/files/upload_sessions/")
    ccg = BoxCCGAuth(CCGConfig(client_id=CLIENT_ID, client_secret="s", enterprise_id=ENTERPRISE_ID))
    ccg_client = BoxClient(auth=ccg, network_session=session)
    assert ccg_client.users.get_user_me().name == "Halcyon Box Automation"


def test_metadata_classification_watermark_locks_links_and_invites(admin: BoxClient) -> None:
    from box_sdk_gen import (
        CreateFileMetadataByIdScope,
        CreateFolderLockFolder,
        CreateFolderLockLockedOperations,
        CreateInviteActionableBy,
        CreateInviteEnterprise,
        CreateMetadataTemplateFields,
        CreateMetadataTemplateFieldsTypeField,
        CreateWebLinkParent,
        UpdateFileMetadataByIdRequestBody,
        UpdateFileMetadataByIdRequestBodyOpField,
        UpdateFileMetadataByIdScope,
        UpdateFileWatermarkWatermark,
    )

    root = sandbox(admin)
    file = upload(admin, root, "governed.txt", b"governed")
    template = admin.metadata_templates.create_metadata_template(
        "enterprise",
        "SDK Matter",
        template_key="sdkMatter",
        fields=[
            CreateMetadataTemplateFields(
                type=CreateMetadataTemplateFieldsTypeField.STRING, key="matter", display_name="Matter"
            ),
            CreateMetadataTemplateFields(
                type=CreateMetadataTemplateFieldsTypeField.FLOAT, key="year", display_name="Year"
            ),
        ],
    )
    assert template.template_key == "sdkMatter" and [f.key for f in template.fields] == ["matter", "year"]
    created = admin.file_metadata.create_file_metadata_by_id(
        file.id, CreateFileMetadataByIdScope.ENTERPRISE, "sdkMatter", {"matter": "M-7", "year": 2026}
    )
    assert created.extra_data["matter"] == "M-7"
    patched = admin.file_metadata.update_file_metadata_by_id(
        file.id,
        UpdateFileMetadataByIdScope.ENTERPRISE,
        "sdkMatter",
        [
            UpdateFileMetadataByIdRequestBody(
                op=UpdateFileMetadataByIdRequestBodyOpField.REPLACE, path="/matter", value="M-8"
            )
        ],
    )
    assert patched.extra_data["matter"] == "M-8"
    classified = admin.file_classifications.add_classification_to_file(
        file.id, box_security_classification_key="Internal"
    )
    assert classified.box_security_classification_key == "Internal"
    assert admin.files.get_file_by_id(file.id, fields=["classification"]).classification.name == "Internal"
    assert (
        admin.classifications.get_classification_template().template_key
        == "securityClassification-6VMVochwUWo"
    )
    admin.file_watermarks.update_file_watermark(file.id, UpdateFileWatermarkWatermark())
    assert admin.file_watermarks.get_file_watermark(file.id).watermark.created_at is not None
    admin.file_watermarks.delete_file_watermark(file.id)
    lock = admin.folder_locks.create_folder_lock(
        CreateFolderLockFolder(type="folder", id=root),
        locked_operations=CreateFolderLockLockedOperations(move=True, delete=True),
    )
    assert lock.folder.id == root and lock.locked_operations.delete is True
    with pytest.raises(BoxAPIError) as frozen:
        admin.folders.delete_folder_by_id(root, recursive=True)
    assert frozen.value.response_info.code == "access_denied_item_locked"
    admin.folder_locks.delete_folder_lock_by_id(lock.id)
    link = admin.web_links.create_web_link(
        "https://status.example.test", CreateWebLinkParent(id=root), name="Status"
    )
    assert link.url == "https://status.example.test"
    assert "Status" in [e.name for e in admin.folders.get_folder_items(root).entries]
    admin.web_links.delete_web_link_by_id(link.id)
    invite = admin.invites.create_invite(
        CreateInviteEnterprise(id=ENTERPRISE_ID), CreateInviteActionableBy(login="new.hire@example.test")
    )
    assert invite.status == "pending" and invite.actionable_by.login == "new.hire@example.test"
    admin.metadata_templates.delete_metadata_template("enterprise", "sdkMatter")


def test_preflight_upload_content_md5_and_thumbnail_url(admin: BoxClient) -> None:
    from box_sdk_gen import (
        GetFileThumbnailUrlExtension,
        PreflightFileUploadCheckParent,
        UploadWithPreflightCheckAttributes,
        UploadWithPreflightCheckAttributesParentField,
    )

    root = sandbox(admin)
    data = b"preflight and checksum"
    check = admin.uploads.preflight_file_upload_check(
        name="pre.txt", size=len(data), parent=PreflightFileUploadCheckParent(id=root)
    )
    assert check.upload_url == "https://upload.box.com/api/2.0/files/content"
    attributes = UploadWithPreflightCheckAttributes(
        name="pre.txt", size=len(data), parent=UploadWithPreflightCheckAttributesParentField(id=root)
    )
    uploaded = admin.uploads.upload_with_preflight_check(
        attributes, io.BytesIO(data), content_md_5=hashlib.sha1(data).hexdigest()
    ).entries[0]
    assert uploaded.name == "pre.txt"
    with pytest.raises(BoxAPIError) as taken:
        admin.uploads.preflight_file_upload_check(
            name="pre.txt", size=1, parent=PreflightFileUploadCheckParent(id=root)
        )
    assert taken.value.response_info.status_code == 409
    with pytest.raises(BoxAPIError) as bad_digest:
        admin.uploads.upload_file(
            UploadFileAttributes(name="bad.txt", parent=UploadFileAttributesParentField(id=root)),
            io.BytesIO(data),
            content_md_5="0" * 40,
        )
    assert bad_digest.value.response_info.code == "bad_digest"
    thumbnail = admin.files.get_file_thumbnail_by_id(uploaded.id, "png", min_height=64, min_width=64)
    assert thumbnail.read().startswith(b"\x89PNG")
    # File types that cannot be rendered redirect to a generic icon, which is what the *_url call returns.
    blob = upload(admin, root, "archive.bin", b"\x00\x01")
    url = admin.files.get_file_thumbnail_url(
        blob.id, GetFileThumbnailUrlExtension.PNG, min_height=64, min_width=64
    )
    assert url == "https://app.box.com/static/img/file_icons/file_default.png"
    assert requests.get(url, timeout=10).content.startswith(b"\x89PNG")
