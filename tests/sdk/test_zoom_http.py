"""The exact flow a transcript-ingesting client (parlance) runs against Zoom, with plain httpx/requests.

Real sockets: ``emulator_intercept`` reroutes ``zoom.us``/``api.zoom.us``/``ssrweb.zoom.us`` to a
localhost server, so UUID path-encoding, the 302 to ssrweb and the Bearer-on-redirect behaviour are
exercised exactly as on the wire. Run with::

    EMULATORS_PROVIDERS=zoom uv run pytest tests/sdk/test_zoom_http.py -q
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

import httpx
import pytest
import requests

from emulators.world import media

pytestmark = pytest.mark.sdk

BASIC = ("parlance-client-id", "parlance-client-secret")
REDIRECT = "https://parlance.example.test/oauth/zoom/callback"
HOST_EMAIL = "benjamin.adler@halcyonadvisory.example"
PREFERENCE = ("VTT", "M4A", "MP4")  # transcript first, then audio, then video


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def path_uuid(uuid: str) -> str:
    """Zoom: double-encode a UUID that begins with '/' or contains '//'; encode once otherwise."""
    once = quote(uuid, safe="")
    return quote(once, safe="") if uuid.startswith("/") or "//" in uuid else once


def authorize(http: httpx.Client, login_hint: str = HOST_EMAIL) -> dict[str, Any]:
    verifier, challenge = pkce_pair()
    redirect = http.get(
        "https://zoom.us/oauth/authorize",
        params={
            "response_type": "code",
            "client_id": BASIC[0],
            "redirect_uri": REDIRECT,
            "state": "state-123",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "login_hint": login_hint,
        },
    )
    assert redirect.status_code == 302
    query = parse_qs(urlparse(redirect.headers["location"]).query)
    assert query["state"] == ["state-123"]
    token = http.post(
        "https://zoom.us/oauth/token",
        auth=BASIC,
        data={
            "grant_type": "authorization_code",
            "code": query["code"][0],
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        },
    )
    assert token.status_code == 200, token.text
    return token.json()


def list_recordings(http: httpx.Client, headers: dict[str, str]) -> list[dict[str, Any]]:
    meetings: list[dict[str, Any]] = []
    token = ""
    while True:
        page = http.get(
            "https://api.zoom.us/v2/users/me/recordings",
            headers=headers,
            params={"from": "2026-09-01", "to": "2026-09-30", "page_size": 3, "next_page_token": token},
        )
        assert page.status_code == 200, page.text
        body = page.json()
        meetings += body["meetings"]
        token = body["next_page_token"]
        if not token:
            return meetings


def pick_file(files: list[dict[str, Any]]) -> dict[str, Any] | None:
    completed = [f for f in files if f.get("status") == "completed"]
    for extension in PREFERENCE:
        for f in completed:
            if f.get("file_extension") == extension:
                return f
    return None


@pytest.fixture
def zoom_only(emulator_intercept: Any) -> Any:
    if "zoom" not in emulator_intercept.hub.providers:
        pytest.skip("the zoom provider is not loaded (check EMULATORS_PROVIDERS)")
    return emulator_intercept


def test_parlance_flow_with_httpx(zoom_only: Any) -> None:
    with httpx.Client(timeout=30) as http:
        tokens = authorize(http)
        assert tokens["token_type"] == "bearer" and tokens["api_url"] == "https://api.zoom.us"
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}

        listed = list_recordings(http, headers)
        assert len(listed) == 8
        assert all(type(m["type"]) is str for m in listed)
        assert any(m["uuid"].startswith("/") for m in listed)  # the awkward UUID shapes are in the data
        downloaded = 0
        for item in listed:
            detail = http.get(
                f"https://api.zoom.us/v2/meetings/{path_uuid(item['uuid'])}/recordings", headers=headers
            )
            assert detail.status_code == 200, (item["uuid"], detail.text)
            assert detail.json()["uuid"] == item["uuid"]
            assert detail.json()["type"] == item["type"]
            chosen = pick_file(detail.json()["recording_files"])
            assert chosen is not None and chosen["file_extension"] == "VTT"
            # Bearer header + following the 302 to ssrweb (httpx drops the header across hosts)
            file = http.get(chosen["download_url"], headers=headers, follow_redirects=True)
            assert file.status_code == 200
            assert urlparse(file.history[-1].headers["location"]).netloc == "ssrweb.zoom.us"
            assert file.headers["content-type"].startswith("text/vtt")
            assert int(file.headers["content-length"]) == chosen["file_size"] == len(file.content)
            cues = media.parse_vtt(file.content)
            assert cues and all(c.speaker for c in cues)
            downloaded += 1
        assert downloaded == 8

        # refresh tokens rotate: the second use of the same refresh token fails
        refresh = http.post(
            "https://zoom.us/oauth/token",
            auth=BASIC,
            data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
        )
        assert refresh.status_code == 200
        reuse = http.post(
            "https://zoom.us/oauth/token",
            auth=BASIC,
            data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"]},
        )
        assert reuse.status_code == 400 and reuse.json() == {
            "reason": "Invalid Token!",
            "error": "invalid_request",
        }


def test_audio_fallback_and_range_with_requests(zoom_only: Any) -> None:
    with httpx.Client(timeout=30) as http:
        headers = {
            "Authorization": f"Bearer {authorize(http, 'maya.chen@halcyonadvisory.example')['access_token']}"
        }
        meeting = next(
            m
            for m in list_recordings(http, headers)
            if not any(f["file_type"] == "TRANSCRIPT" for f in m["recording_files"])
        )
    chosen = pick_file(meeting["recording_files"])
    assert chosen is not None and chosen["file_extension"] == "M4A"
    first = requests.get(chosen["download_url"], headers=headers, timeout=30)  # follows the redirect
    assert first.status_code == 200 and first.content[4:8] == b"ftyp" and first.content[8:12] == b"M4A "
    signed = first.history[-1].headers["location"]  # the pre-signed ssrweb URL
    ranged = requests.get(signed, headers={"Range": "bytes=0-31"}, timeout=30)
    assert ranged.status_code == 206 and ranged.content == first.content[:32]


async def test_server_to_server_token_and_async_client(zoom_only: Any) -> None:
    account = zoom_only.hub.world.org.zoom_account_id
    async with httpx.AsyncClient(timeout=30) as http:
        token = await http.post(
            "https://zoom.us/oauth/token",
            params={"grant_type": "account_credentials", "account_id": account},
            auth=BASIC,
        )
        assert token.status_code == 200 and "refresh_token" not in token.json()
        headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
        users = await http.get("https://api.zoom.us/v2/users", headers=headers, params={"page_size": 300})
        assert users.status_code == 200 and users.json()["total_records"] == 45
        me = await http.get("https://api.zoom.us/v2/users/me", headers=headers)
        assert (
            me.json()["email"] == "eleanor.whitfield@halcyonadvisory.example"
        )  # S2S apps act as the account owner
        missing = await http.get("https://api.zoom.us/v2/meetings/42", headers=headers)
        assert missing.status_code == 404 and missing.json()["code"] == 3001
        assert missing.headers["x-zm-trackingid"]


def test_uuid_encodings_over_a_real_socket(zoom_only: Any) -> None:
    with httpx.Client(timeout=30) as http:
        headers = {"Authorization": f"Bearer {authorize(http)['access_token']}"}
        uuids = [m["uuid"] for m in list_recordings(http, headers)]
        leading = next(u for u in uuids if u.startswith("/"))
        double = next(u for u in uuids if "//" in u)
        plain = next(u for u in uuids if "/" not in u)
        for uuid in (leading, double, plain):
            forms = {quote(uuid, safe=""), quote(quote(uuid, safe=""), safe="")}
            for form in forms:
                for path in ("recordings", "meeting_summary"):
                    r = http.get(f"https://api.zoom.us/v2/meetings/{form}/{path}", headers=headers)
                    assert r.status_code == 200, (uuid, form, path, r.status_code, r.text[:120])
                past = http.get(f"https://api.zoom.us/v2/past_meetings/{form}", headers=headers)
                assert past.status_code == 200 and past.json()["uuid"] == uuid
        raw = http.get(f"https://api.zoom.us/v2/past_meetings/{plain}/participants", headers=headers)
        assert raw.status_code == 200  # a UUID with a literal "+" and "=" in the path


def test_adversarial_payloads_and_client_bound_revoke_over_real_socket(zoom_only: Any) -> None:
    with httpx.Client(timeout=30) as http:
        tokens = authorize(http)
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        malformed = http.post(
            "https://api.zoom.us/v2/users/me/meetings",
            headers=headers,
            json={"type": 8, "recurrence": {"type": [1]}},
        )
        assert malformed.status_code == 400 and malformed.json()["code"] == 300
        assert malformed.headers["x-zm-trackingid"]
        rejected = http.post(
            "https://zoom.us/oauth/revoke",
            auth=("other-client", "test-only"),
            data={"token": tokens["access_token"]},
        )
        assert rejected.status_code == 400
        assert http.get("https://api.zoom.us/v2/users/me/meetings", headers=headers).status_code == 200
