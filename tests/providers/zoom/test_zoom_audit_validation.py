"""Adversarial write inputs must fail in Zoom's envelope without corrupting state."""

from __future__ import annotations

import copy
from typing import Any

import httpx
import pytest

from tests.providers.zoom.conftest import API, BLUEFIN_ID, V2, double_encode


def targets(ctx: Any) -> list[tuple[str, str]]:
    group = ctx.store.collection("groups").values()[0]["id"]
    rec = ctx.store.collection("recordings").values()[0]
    base = f"/meetings/{double_encode(rec['id'])}/recordings"
    return [
        ("POST", "/users"),
        ("PATCH", "/users/me"),
        ("PUT", "/users/me/email"),
        ("PUT", "/users/me/status"),
        ("PATCH", "/users/me/settings"),
        ("POST", "/groups"),
        ("PATCH", f"/groups/{group}"),
        ("POST", f"/groups/{group}/members"),
        ("POST", "/users/me/meetings"),
        ("PATCH", f"/meetings/{BLUEFIN_ID}"),
        ("PUT", f"/meetings/{BLUEFIN_ID}/status"),
        ("POST", f"/meetings/{BLUEFIN_ID}/registrants"),
        ("PUT", f"/meetings/{BLUEFIN_ID}/registrants/status"),
        ("PATCH", base + "/settings"),
        ("PUT", base + "/status"),
        ("PUT", base + f"/{rec['recording_files'][0]['id']}/status"),
    ]


@pytest.mark.parametrize("body", [b"[]", b"null", b"true", b"42", b'"text"', b"not-json", b"\xff", b'{"x":'])
async def test_every_rest_write_rejects_non_object_bodies(
    zoom: httpx.AsyncClient, s2s: dict[str, str], ctx: Any, body: bytes
) -> None:
    ctx.store.collection("meetings").get(str(BLUEFIN_ID))["settings"]["approval_type"] = 0
    before = ctx.store.snapshot()
    for method, path in targets(ctx):
        response = await zoom.request(method, V2 + path, headers=s2s, content=body)
        assert response.status_code == 400, (method, path, response.text)
        assert response.json()["code"] == 300
        assert response.headers["x-zm-trackingid"]
    assert ctx.store.snapshot() == before


@pytest.mark.parametrize("value", [None, True, 12, 1.5, [], {}, ["x"], "x"])
async def test_nested_write_fields_have_strict_types(
    zoom: httpx.AsyncClient, s2s: dict[str, str], ctx: Any, value: Any
) -> None:
    group = ctx.store.collection("groups").values()[0]["id"]
    rec = ctx.store.collection("recordings").values()[0]
    ctx.store.collection("meetings").get(str(BLUEFIN_ID))["settings"]["approval_type"] = 0
    bodies = [
        ("POST", "/users", {"action": "create", "user_info": value}),
        ("POST", f"/groups/{group}/members", {"members": value}),
        ("POST", "/users/me/meetings", {"type": 8, "recurrence": {"type": value}}),
        ("PATCH", f"/meetings/{BLUEFIN_ID}", {"recurrence": {"type": value}}),
    ]
    if value != []:
        bodies.append(
            ("PUT", f"/meetings/{BLUEFIN_ID}/registrants/status", {"action": "approve", "registrants": value})
        )
    if type(value) is not str:
        bodies += [
            ("PATCH", "/users/me", {"first_name": value}),
            ("POST", "/groups", {"name": value}),
            (
                "POST",
                f"/meetings/{BLUEFIN_ID}/registrants",
                {"email": "audit@client.example", "first_name": value},
            ),
        ]
    if type(value) is not bool:
        bodies += [
            ("POST", "/users/me/meetings", {"settings": {"waiting_room": value}}),
            ("PATCH", "/users/me/settings", {"in_meeting": {"chat": value}}),
            (
                "PATCH",
                f"/meetings/{double_encode(rec['id'])}/recordings/settings",
                {"viewer_download": value},
            ),
        ]
    if value != {} and value != []:
        # Empty arrays/objects have no malformed element; scalar and non-object elements do.
        bodies += [("POST", f"/groups/{group}/members", {"members": [value]})]
    for method, path, body in bodies:
        before = ctx.store.snapshot()
        response = await zoom.request(method, V2 + path, headers=s2s, json=body)
        assert response.status_code == 400, (method, path, body, response.text)
        assert response.json()["code"] == 300
        assert ctx.store.snapshot() == before


@pytest.mark.parametrize(
    "recurrence",
    [
        {},
        {"type": 4},
        {"type": True},
        {"type": 1, "repeat_interval": 0},
        {"type": 1, "repeat_interval": 10**100},
        {"type": 1, "end_times": -1},
        {"type": 1, "end_times": 61},
        {"type": 1, "end_date_time": "wrong"},
        {"type": 1, "end_times": 2, "end_date_time": "2026-12-01T00:00:00Z"},
        {"type": 2, "weekly_days": "1,9"},
        {"type": 2, "weekly_days": [1]},
        {"type": 3, "monthly_day": 0},
        {"type": 3, "monthly_week": 1},
    ],
)
async def test_invalid_recurrence_is_rejected_atomically(
    zoom: httpx.AsyncClient, s2s: dict[str, str], ctx: Any, recurrence: dict[str, Any]
) -> None:
    before = copy.deepcopy(ctx.store.collection("meetings").get(str(BLUEFIN_ID)))
    response = await zoom.patch(
        f"{V2}/meetings/{BLUEFIN_ID}",
        headers=s2s,
        json={"topic": "Must not stick", "recurrence": recurrence},
    )
    assert response.status_code == 400 and response.json()["code"] == 300
    assert ctx.store.collection("meetings").get(str(BLUEFIN_ID)) == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("type", True),
        ("type", 2.0),
        ("duration", True),
        ("duration", 0),
        ("duration", 1441),
        ("duration", 10**100),
        ("start_time", []),
        ("start_time", "9999-12-31T23:59:59-12:00"),
        ("password", 123456),
        ("password", None),
        ("topic", "☃" * 201),
        ("agenda", "x" * 2001),
        ("settings", {"alternative_hosts": ["person@halcyonadvisory.example"]}),
    ],
)
async def test_meeting_field_limits_and_types(
    zoom: httpx.AsyncClient, staff: dict[str, str], field: str, value: Any
) -> None:
    response = await zoom.post(f"{V2}/users/me/meetings", headers=staff, json={field: value})
    assert response.status_code == 400 and response.json()["code"] == 300


async def test_invalid_datetime_patch_preserves_previous_fields(
    zoom: httpx.AsyncClient, s2s: dict[str, str]
) -> None:
    url = f"{V2}/meetings/{BLUEFIN_ID}"
    before = (await zoom.get(url, headers=s2s)).json()
    response = await zoom.patch(url, headers=s2s, json={"topic": "Wrong", "start_time": "invalid"})
    assert response.status_code == 400
    assert (await zoom.get(url, headers=s2s)).json() == before


@pytest.mark.parametrize(
    "action,body",
    [
        ("mint_token", {"user": []}),
        ("mint_token", {"scopes": 12}),
        ("mint_token", {"expires_in": []}),
        ("register_app", {"scopes": 3}),
        ("register_app", {"redirect_uris": "not-a-list"}),
        ("configure_webhook", {"url": []}),
        ("configure_webhook", {"url": "invalid"}),
        ("configure_webhook", {"url": "http://["}),
        ("configure_webhook", {"url": "https://consumer.example/hook", "events": [True]}),
        ("start_meeting", {"participants": [True]}),
        ("participant_join", {"user": []}),
        ("participant_leave", {"after_minutes": "invalid"}),
        ("end_meeting", {"duration_minutes": "invalid"}),
        ("end_meeting", {"chat": [{}]}),
        ("replay_event", {"event": []}),
    ],
)
async def test_malformed_actions_do_not_mutate_state(
    zoom: httpx.AsyncClient, ctx: Any, action: str, body: dict[str, Any]
) -> None:
    before = ctx.store.snapshot()
    response = await zoom.post(f"{API}/_emulator/actions/{action}", json={"meeting_id": BLUEFIN_ID, **body})
    assert response.status_code == 400
    assert ctx.store.snapshot() == before


async def test_report_extreme_year_returns_zoom_error(zoom: httpx.AsyncClient, s2s: dict[str, str]) -> None:
    response = await zoom.get(f"{V2}/report/daily?year=9999&month=12", headers=s2s)
    assert response.status_code == 400 and response.json()["code"] == 300


@pytest.mark.parametrize(
    "body",
    [
        b'{"settings":{"custom":1e10000}}',
        b'{"topic":"\\ud800"}',
        b'{"duration":NaN}',
    ],
)
async def test_non_finite_numbers_and_invalid_unicode_return_zoom_errors(
    zoom: httpx.AsyncClient, staff: dict[str, str], body: bytes
) -> None:
    response = await zoom.post(V2 + "/users/me/meetings", headers=staff, content=body)
    assert response.status_code == 400 and response.json()["code"] == 300


@pytest.mark.parametrize("user_type,expected", [(4, 201), (3, 400), (True, 400), (1.5, 400)])
async def test_user_type_matches_documented_integer_enum(
    zoom: httpx.AsyncClient, s2s: dict[str, str], user_type: Any, expected: int
) -> None:
    response = await zoom.post(
        V2 + "/users",
        headers=s2s,
        json={
            "action": "custCreate",
            "user_info": {
                "email": "audit.user@halcyonadvisory.example",
                "first_name": "Audit",
                "type": user_type,
            },
        },
    )
    assert response.status_code == expected


async def test_group_rename_length_is_validated_without_mutation(
    zoom: httpx.AsyncClient, s2s: dict[str, str]
) -> None:
    group = (await zoom.post(V2 + "/groups", headers=s2s, json={"name": "Audit ☃"})).json()
    url = V2 + "/groups/" + group["id"]
    response = await zoom.patch(url, headers=s2s, json={"name": "x" * 129})
    assert response.status_code == 400
    assert (await zoom.get(url, headers=s2s)).json()["name"] == "Audit ☃"
