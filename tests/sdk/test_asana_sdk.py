"""The official urllib3 SDK uses its supported configurable host in prefix mode."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

asana = pytest.importorskip("asana")
pytestmark = pytest.mark.sdk


@pytest.fixture
def api(emulators: Any, emulator_server: Any) -> Iterator[Any]:
    configuration = asana.Configuration()
    configuration.host = emulator_server.url_for("asana") + "/api/1.0"
    configuration.access_token = "asana-dev-p018"
    configuration.page_limit = 2
    client = asana.ApiClient(configuration)
    try:
        yield client
    finally:
        client.rest_client.pool_manager.clear()
        if getattr(client, "pool", None) is not None:
            client.pool.close()
            client.pool.join()


def workspace(api: Any) -> str:
    return next(asana.WorkspacesApi(api).get_workspaces({}))["gid"]


def test_official_sdk_users_projection_and_automatic_pagination(api: Any, emulators: Any) -> None:
    users = asana.UsersApi(api)
    me = users.get_user("me", {"opt_fields": "name,email"})
    assert set(me) == {"gid", "name", "email"} and me["email"] == "maya.chen@halcyonadvisory.example"
    rows = list(users.get_users({"workspace": workspace(api), "limit": 2, "opt_fields": "name,email"}))
    assert {row["email"] for row in rows} == {person.email for person in emulators.world.active_employees}
    assert len(rows) == 45 and len({row["gid"] for row in rows}) == 45


def test_official_sdk_task_project_subtask_dependency_and_approval_lifecycle(api: Any) -> None:
    projects, tasks = asana.ProjectsApi(api), asana.TasksApi(api)
    project = projects.create_project({"data": {"name": "SDK contract", "workspace": workspace(api)}}, {})
    parent = tasks.create_task(
        {
            "data": {
                "name": "Review approval",
                "projects": [project["gid"]],
                "resource_subtype": "approval",
                "assignee": "me",
            }
        },
        {},
    )
    assert parent["approval_status"] == "pending" and parent["completed"] is False
    child = tasks.create_subtask_for_task(
        {"data": {"name": "Evidence", "due_on": "2026-12-01"}}, parent["gid"], {}
    )
    assert child["parent"]["gid"] == parent["gid"] and child["projects"] == []
    blocker = tasks.create_task({"data": {"name": "Blocker", "projects": [project["gid"]]}}, {})
    tasks.add_dependencies_for_task({"data": {"dependencies": [blocker["gid"]]}}, parent["gid"])
    assert [row["gid"] for row in tasks.get_dependencies_for_task(parent["gid"], {})] == [blocker["gid"]]
    updated = tasks.update_task(
        {"data": {"approval_status": "approved"}},
        parent["gid"],
        {"opt_fields": "name,completed,approval_status,assignee.email"},
    )
    assert (
        updated["completed"] is True and updated["assignee"]["email"] == "maya.chen@halcyonadvisory.example"
    )
    rows = list(tasks.get_tasks({"project": project["gid"], "limit": 1}))
    assert {row["gid"] for row in rows} == {parent["gid"], blocker["gid"]}
    tasks.delete_task(child["gid"])
    with pytest.raises(asana.rest.ApiException) as deleted:
        tasks.get_task(child["gid"], {})
    assert deleted.value.status == 404 and "errors" in json.loads(deleted.value.body)


def test_official_sdk_comments_and_multipart_real_attachment(api: Any, tmp_path: Path) -> None:
    tasks, stories, attachments = asana.TasksApi(api), asana.StoriesApi(api), asana.AttachmentsApi(api)
    row = tasks.create_task({"data": {"name": "SDK file task", "workspace": workspace(api)}}, {})
    story = stories.create_story_for_task({"data": {"text": "SDK comment ✓"}}, row["gid"], {})
    assert story["text"] == "SDK comment ✓"
    assert len(list(stories.get_stories_for_task(row["gid"], {}))) == 1
    path = tmp_path / "sdk-evidence.bin"
    content = b"\x00SDK real file\xff\n" * 100
    path.write_bytes(content)
    uploaded = attachments.create_attachment_for_object({"parent": row["gid"], "file": str(path)})
    assert uploaded["name"] == path.name
    metadata = attachments.get_attachment(uploaded["gid"], {})
    assert metadata["size"] == len(content) and metadata["download_url"].startswith(api.configuration.host)
    assert httpx.get(metadata["download_url"]).content == content


def test_official_sdk_auth_acl_and_atomic_validation_errors(api: Any) -> None:
    tasks = asana.TasksApi(api)
    row = tasks.create_task({"data": {"name": "Original", "workspace": workspace(api)}}, {})
    with pytest.raises(asana.rest.ApiException) as invalid:
        tasks.update_task({"data": {"name": "No partial update", "completed": 1}}, row["gid"], {})
    assert invalid.value.status == 400 and "errors" in json.loads(invalid.value.body)
    assert tasks.get_task(row["gid"], {})["name"] == "Original"
    api.configuration.access_token = "asana-dev-p035"
    with pytest.raises(asana.rest.ApiException) as denied:
        tasks.get_task(row["gid"], {})
    assert denied.value.status == 403
    api.configuration.access_token = "invalid"
    with pytest.raises(asana.rest.ApiException) as unauthorized:
        asana.UsersApi(api).get_user("me", {})
    assert unauthorized.value.status == 401
