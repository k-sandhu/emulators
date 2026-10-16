"""Recording downloads: 302 to the ssrweb host, token rules, content types, ranges, file validity."""

from __future__ import annotations

import itertools
import json
import re
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from emulators.testing.helpers import bearer
from emulators.world import media
from tests.providers.zoom.conftest import (
    BLUEFIN_ID,
    HOST,
    MAYA,
    ORCHID_ID,
    V2,
    double_encode,
    token_for,
)


def recorded(ctx: Any, meeting_id: int) -> list[dict[str, Any]]:
    return sorted(
        (r for r in ctx.store.collection("recordings") if r["meeting_id"] == meeting_id),
        key=lambda r: r["start_time"],
    )


async def files_of(
    client: httpx.AsyncClient, headers: dict[str, str], uuid: str
) -> dict[str, dict[str, Any]]:
    body = (await client.get(f"{V2}/meetings/{double_encode(uuid)}/recordings", headers=headers)).json()
    return {f["file_type"]: f for f in body["recording_files"]}


async def test_download_redirects_to_ssrweb_and_streams_the_file(
    zoom: httpx.AsyncClient, host: dict[str, str], ctx: Any
) -> None:
    uuid = recorded(ctx, BLUEFIN_ID)[0]["id"]  # a UUID starting with "/"
    files = await files_of(zoom, host, uuid)
    for kind, content_type in (
        ("MP4", "video/mp4"),
        ("M4A", "audio/mp4"),
        ("TRANSCRIPT", "text/vtt"),
        ("TIMELINE", "application/json"),
    ):
        entry = files[kind]
        hop = await zoom.get(entry["download_url"], headers=host)
        assert hop.status_code == 302, kind
        location = urlparse(hop.headers["location"])
        assert location.scheme == "https" and location.netloc == "ssrweb.zoom.us"
        assert {"Expires", "Signature", "Key-Pair-Id"} <= set(parse_qs(location.query))
        final = await zoom.get(entry["download_url"], headers=host, follow_redirects=True)
        assert final.status_code == 200, (kind, final.text[:200])
        assert final.headers["content-type"].split(";")[0] == content_type
        assert int(final.headers["content-length"]) == len(final.content) == entry["file_size"]
        assert final.headers["accept-ranges"] == "bytes"
        assert "filename=" in final.headers["content-disposition"]


async def test_downloaded_media_is_structurally_valid(
    zoom: httpx.AsyncClient, host: dict[str, str], ctx: Any
) -> None:
    uuid = recorded(ctx, ORCHID_ID)[1]["id"]
    files = await files_of(zoom, host, uuid)
    mp4 = (await zoom.get(files["MP4"]["download_url"], headers=host, follow_redirects=True)).content
    m4a = (await zoom.get(files["M4A"]["download_url"], headers=host, follow_redirects=True)).content
    assert mp4[4:8] == b"ftyp" and mp4[8:12] == b"isom" and b"moov" in mp4[:400] and b"mdat" in mp4
    assert m4a[4:8] == b"ftyp" and m4a[8:12] == b"M4A "
    vtt = (await zoom.get(files["TRANSCRIPT"]["download_url"], headers=host, follow_redirects=True)).content
    text = vtt.decode()
    assert (
        text.startswith("WEBVTT\n\n1\n00:") and "<v " not in text
    )  # Zoom style: numbered cues, "Name: text"
    cues = media.parse_vtt(vtt)
    assert len(cues) > 10 and cues[0].start_s >= 0
    assert all(b.start_s >= a.start_s for a, b in itertools.pairwise(cues))
    assert {c.speaker for c in cues} >= {"Benjamin Adler"}
    assert re.search(r"^\d+\n\d\d:\d\d:\d\d\.\d{3} --> \d\d:\d\d:\d\d\.\d{3}\nBenjamin Adler: ", text, re.M)
    timeline = json.loads(
        (await zoom.get(files["TIMELINE"]["download_url"], headers=host, follow_redirects=True)).content
    )
    assert timeline["timeline"] and {"ts", "users"} <= set(timeline["timeline"][0])
    assert re.fullmatch(r"\d\d:\d\d:\d\d\.\d{3}", timeline["timeline"][0]["ts"])


async def test_chat_file_format(zoom: httpx.AsyncClient, host: dict[str, str], ctx: Any) -> None:
    uuid = next(
        r["id"]
        for r in ctx.store.collection("recordings")
        if any(f["file_type"] == "CHAT" for f in r["recording_files"])
    )
    chat = (await files_of(zoom, host, uuid))["CHAT"]
    body = await zoom.get(chat["download_url"], headers=host, follow_redirects=True)
    assert body.headers["content-type"].startswith("text/plain")
    lines = body.text.strip().splitlines()
    assert lines and all(re.fullmatch(r"\d\d:\d\d:\d\d\t From  .+ to  Everyone:\t.+", line) for line in lines)


async def test_range_and_head_requests(zoom: httpx.AsyncClient, host: dict[str, str], ctx: Any) -> None:
    entry = (await files_of(zoom, host, recorded(ctx, ORCHID_ID)[0]["id"]))["M4A"]
    url = (await zoom.get(entry["download_url"], headers=host)).headers["location"]
    full = (await zoom.get(url)).content
    first = await zoom.get(url, headers={"Range": "bytes=0-99"})
    assert first.status_code == 206 and first.content == full[:100]
    assert (
        first.headers["content-range"] == f"bytes 0-99/{len(full)}"
        and first.headers["content-length"] == "100"
    )
    tail = await zoom.get(url, headers={"Range": "bytes=-50"})
    assert tail.status_code == 206 and tail.content == full[-50:]
    open_ended = await zoom.get(url, headers={"Range": f"bytes={len(full) - 10}-"})
    assert open_ended.content == full[-10:]
    assert (await zoom.get(url, headers={"Range": f"bytes={len(full) + 5}-"})).status_code == 416
    head = await zoom.head(url)
    assert (
        head.status_code == 200 and int(head.headers["content-length"]) == len(full) and head.content == b""
    )
    redirect_head = await zoom.head(entry["download_url"], headers=host)
    assert redirect_head.status_code == 302


async def test_token_rules_for_downloads(
    zoom: httpx.AsyncClient, host: dict[str, str], maya: dict[str, str], ctx: Any
) -> None:
    uuid = recorded(ctx, ORCHID_ID)[0]["id"]
    other = recorded(ctx, ORCHID_ID)[1]["id"]
    url = (await files_of(zoom, host, uuid))["TRANSCRIPT"]["download_url"]
    bare = await zoom.get(url)
    assert bare.status_code == 401 and bare.json() == {"code": 124, "message": "Invalid access token."}
    html = await zoom.get(url, headers={"Accept": "text/html"})
    assert html.status_code == 401 and "text/html" in html.headers["content-type"] and "401" in html.text
    assert (await zoom.get(url, headers=bearer("nonsense"))).status_code == 401
    assert (await zoom.get(url, params={"access_token": "nonsense"})).status_code == 401
    # OAuth tokens: the host works (header or query), another user does not
    oauth = host["Authorization"].split(" ", 1)[1]
    assert (await zoom.get(url, params={"access_token": oauth}, follow_redirects=True)).status_code == 200
    assert (await zoom.get(url, headers=maya)).status_code == 403
    narrow = await token_for(zoom, HOST, scopes="meeting:read:admin")
    assert (await zoom.get(url, headers=narrow)).status_code == 401
    # download_access_token: bound to the meeting it was issued for
    token = (
        await zoom.get(
            f"{V2}/meetings/{double_encode(uuid)}/recordings",
            headers=host,
            params={"include_fields": "download_access_token"},
        )
    ).json()["download_access_token"]
    assert (await zoom.get(url, params={"access_token": token}, follow_redirects=True)).status_code == 200
    assert (await zoom.get(url, headers=bearer(token), follow_redirects=True)).status_code == 200
    other_url = (await files_of(zoom, host, other))["TRANSCRIPT"]["download_url"]
    assert (await zoom.get(other_url, headers=bearer(token))).status_code == 401
    # the token is not an API token
    assert (await zoom.get(f"{V2}/users/me", headers=bearer(token))).status_code == 401
    unknown = await zoom.get("https://us06web.zoom.us/rec/download/not-a-real-token", headers=host)
    assert unknown.status_code == 404


async def test_download_token_expires_with_its_ttl(
    zoom: httpx.AsyncClient, host: dict[str, str], ctx: Any
) -> None:
    uuid = recorded(ctx, ORCHID_ID)[0]["id"]
    body = (
        await zoom.get(
            f"{V2}/meetings/{double_encode(uuid)}/recordings",
            headers=host,
            params={"include_fields": "download_access_token", "ttl": 60},
        )
    ).json()
    url = next(f for f in body["recording_files"] if f["file_type"] == "M4A")["download_url"]
    assert (await zoom.get(url, params={"access_token": body["download_access_token"]})).status_code == 302
    zoom.hub.clock.advance(timedelta(minutes=5))  # type: ignore[attr-defined]
    try:
        assert (
            await zoom.get(url, params={"access_token": body["download_access_token"]})
        ).status_code == 401
    finally:
        zoom.hub.clock.advance(timedelta(minutes=-5))  # type: ignore[attr-defined]


async def test_signed_url_is_checked_and_expires(
    zoom: httpx.AsyncClient, host: dict[str, str], ctx: Any
) -> None:
    entry = (await files_of(zoom, host, recorded(ctx, ORCHID_ID)[0]["id"]))["TRANSCRIPT"]
    url = (await zoom.get(entry["download_url"], headers=host)).headers["location"]
    assert (await zoom.get(url)).status_code == 200
    tampered = url.replace("Signature=", "Signature=x")
    assert (await zoom.get(tampered)).status_code == 403
    assert (await zoom.get(url.split("?")[0])).status_code == 403
    zoom.hub.clock.advance(timedelta(hours=2))  # type: ignore[attr-defined]
    try:
        assert (await zoom.get(url)).status_code == 403
    finally:
        zoom.hub.clock.advance(timedelta(hours=-2))  # type: ignore[attr-defined]


async def test_trashed_recordings_cannot_be_downloaded(
    zoom: httpx.AsyncClient, host: dict[str, str], ctx: Any
) -> None:
    uuid = recorded(ctx, ORCHID_ID)[2]["id"]
    entry = (await files_of(zoom, host, uuid))["M4A"]
    assert (
        await zoom.delete(f"{V2}/meetings/{double_encode(uuid)}/recordings/{entry['id']}", headers=host)
    ).status_code == 204
    assert (await zoom.get(entry["download_url"], headers=host)).status_code == 404
    assert (
        await zoom.put(
            f"{V2}/meetings/{double_encode(uuid)}/recordings/{entry['id']}/status",
            headers=host,
            json={"action": "recover"},
        )
    ).status_code == 204
    assert (await zoom.get(entry["download_url"], headers=host)).status_code == 302


async def test_web_pages_and_join_links(zoom: httpx.AsyncClient, host: dict[str, str], ctx: Any) -> None:
    rec = (
        await zoom.get(
            f"{V2}/meetings/{double_encode(recorded(ctx, ORCHID_ID)[0]['id'])}/recordings", headers=host
        )
    ).json()
    share = await zoom.get(rec["share_url"])
    assert (
        share.status_code == 200
        and "text/html" in share.headers["content-type"]
        and rec["topic"] in share.text
    )
    mp4 = next(f for f in rec["recording_files"] if f["file_type"] == "MP4")
    assert (await zoom.get(mp4["play_url"])).status_code == 200
    assert (await zoom.get("https://us06web.zoom.us/rec/share/unknown")).status_code == 404
    meeting = (await zoom.get(f"{V2}/meetings/{ORCHID_ID}", headers=host)).json()
    page = await zoom.get(meeting["join_url"])
    assert page.status_code == 200 and meeting["topic"].split(" ")[0] in page.text
    assert (await zoom.get("https://us06web.zoom.us/j/1")).status_code == 404
    user = (await zoom.get(f"{V2}/users/me", headers=host)).json()
    avatar = await zoom.get(user["pic_url"])
    assert (
        avatar.status_code == 200
        and avatar.headers["content-type"] == "image/png"
        and avatar.content[:4] == b"\x89PNG"
    )
    _ = MAYA
