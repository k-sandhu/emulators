"""Voice calls/recordings and the Conversations API."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

from tests.providers.twilio.conftest import API, CONTACT, CONVERSATIONS, FROM_NUMBER, account_base

VOICE_URL = "http://consumer.test/voice"
CALL_STATUS_URL = "http://consumer.test/call-status"


class VoiceReceiver:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        app = FastAPI()

        @app.post("/voice")
        async def voice(request: Request) -> Response:
            return await self._capture(request, "voice", "<Response><Say>Hello</Say></Response>")

        @app.post("/call-status")
        async def call_status(request: Request) -> Response:
            return await self._capture(request, "status", "")

        self.app = app

    async def _capture(self, request: Request, kind: str, reply: str) -> Response:
        raw = (await request.body()).decode()
        self.calls.append(
            {
                "kind": kind,
                "params": dict(parse_qsl(raw, keep_blank_values=True)),
                "signature": request.headers.get("x-twilio-signature"),
                "url": VOICE_URL if kind == "voice" else CALL_STATUS_URL,
            }
        )
        return Response(reply, media_type="text/xml")

    def of_kind(self, kind: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["kind"] == kind]


def attach(client: httpx.AsyncClient, receiver: VoiceReceiver) -> Any:
    hub = client.hub  # type: ignore[attr-defined]
    hub.webhook_transport = httpx.ASGITransport(app=receiver.app)
    return hub.context("twilio")


async def test_call_lifecycle_callbacks_and_recording(twilio: httpx.AsyncClient) -> None:
    receiver = VoiceReceiver()
    ctx = attach(twilio, receiver)
    r = await twilio.post(
        f"{account_base(twilio)}/Calls.json",
        data={
            "To": CONTACT,
            "From": FROM_NUMBER,
            "Url": VOICE_URL,
            "StatusCallback": CALL_STATUS_URL,
            "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
            "Record": "true",
        },
    )
    assert r.status_code == 201, r.text
    call = r.json()
    assert call["sid"].startswith("CA")
    assert call["status"] == "queued"  # create response shows the initial status
    assert call["direction"] == "outbound-api"
    assert call["from_formatted"] == "(416) 555-0188"
    assert call["to_formatted"] == "(416) 555-0141"
    assert call["duration"] is None

    fetched = (await twilio.get(f"{account_base(twilio)}/Calls/{call['sid']}.json")).json()
    assert fetched["status"] == "completed"
    assert int(fetched["duration"]) > 0
    assert fetched["start_time"] and fetched["end_time"]
    assert fetched["price"].startswith("-0.")

    statuses = [c["params"]["CallStatus"] for c in receiver.of_kind("status")]
    assert statuses == ["queued", "ringing", "in-progress", "completed"]
    from twilio.request_validator import RequestValidator

    validator = RequestValidator(ctx.store.meta["credentials"]["auth_token"])
    for entry in receiver.calls:
        assert validator.validate(entry["url"], entry["params"], entry["signature"]), entry
    completed = receiver.of_kind("status")[-1]["params"]
    assert completed["CallSid"] == call["sid"]
    assert completed["CallDuration"] == fetched["duration"]
    assert int(completed["Duration"]) >= 1
    # Twilio fetched the voice URL once, when the call was answered.
    (voice_hit,) = receiver.of_kind("voice")
    assert voice_hit["params"]["CallStatus"] == "in-progress"

    recordings = (await twilio.get(f"{account_base(twilio)}/Calls/{call['sid']}/Recordings.json")).json()
    assert len(recordings["recordings"]) == 1
    recording = recordings["recordings"][0]
    assert recording["sid"].startswith("RE")
    assert recording["call_sid"] == call["sid"]
    assert recording["status"] == "completed"
    assert recording["media_url"].endswith(f"/Recordings/{recording['sid']}")
    wav = await twilio.get(f"{account_base(twilio)}/Recordings/{recording['sid']}.wav")
    assert wav.status_code == 200
    assert wav.content[:4] == b"RIFF" and wav.content[8:12] == b"WAVE"
    bare = await twilio.get(recording["media_url"])
    assert bare.status_code == 200 and bare.content[:4] == b"RIFF"


async def test_call_validation_and_hangup(twilio: httpx.AsyncClient) -> None:
    base = f"{account_base(twilio)}/Calls.json"
    r = await twilio.post(base, data={"To": CONTACT, "From": FROM_NUMBER})
    assert r.status_code == 400  # Url/Twiml/ApplicationSid required
    r = await twilio.post(base, data={"To": CONTACT, "From": "+16475550151", "Url": VOICE_URL})
    assert r.status_code == 400 and r.json()["code"] == 21210
    r = await twilio.post(base, data={"To": "nope", "From": FROM_NUMBER, "Url": VOICE_URL})
    assert r.status_code == 400 and r.json()["code"] == 21211

    created = await twilio.post(
        base, data={"To": CONTACT, "From": FROM_NUMBER, "Twiml": "<Response><Say>hi</Say></Response>"}
    )
    assert created.status_code == 201, created.text
    url = f"{account_base(twilio)}/Calls/{created.json()['sid']}.json"
    # Already completed in sync mode; hanging up again is a no-op success.
    assert (await twilio.get(url)).json()["status"] == "completed"
    r = await twilio.post(url, data={"Status": "completed"})
    assert r.status_code == 200 and r.json()["status"] == "completed"
    r = await twilio.get(f"{account_base(twilio)}/Calls/{'CA' + '0' * 32}.json")
    assert r.status_code == 404 and r.json()["code"] == 20404

    listing = (await twilio.get(base, params={"Status": "completed", "To": CONTACT})).json()
    assert listing["calls"] and all(c["status"] == "completed" for c in listing["calls"])


async def test_seeded_calls_and_recording_download(twilio: httpx.AsyncClient) -> None:
    calls = (await twilio.get(f"{account_base(twilio)}/Calls.json")).json()["calls"]
    assert len(calls) == 3
    assert all(c["status"] == "completed" for c in calls)
    recordings = (await twilio.get(f"{account_base(twilio)}/Recordings.json")).json()["recordings"]
    assert len(recordings) == 1
    rec = recordings[0]
    fetched = (await twilio.get(f"{account_base(twilio)}/Recordings/{rec['sid']}.json")).json()
    assert fetched["channels"] == 1
    assert int(fetched["duration"]) == 90
    mp3 = await twilio.get(f"{account_base(twilio)}/Recordings/{rec['sid']}.mp3")
    assert mp3.status_code == 200 and mp3.headers["content-type"].startswith("audio/mpeg")
    assert (await twilio.delete(f"{account_base(twilio)}/Recordings/{rec['sid']}.json")).status_code == 204


async def test_inbound_call_action(twilio: httpx.AsyncClient) -> None:
    receiver = VoiceReceiver()
    attach(twilio, receiver)
    numbers = (await twilio.get(f"{account_base(twilio)}/IncomingPhoneNumbers.json")).json()[
        "incoming_phone_numbers"
    ]
    pn = next(n["sid"] for n in numbers if n["phone_number"] == FROM_NUMBER)
    await twilio.post(f"{account_base(twilio)}/IncomingPhoneNumbers/{pn}.json", data={"VoiceUrl": VOICE_URL})
    r = await twilio.post(f"{API}/_emulator/actions/inbound_call", json={"from": CONTACT, "to": FROM_NUMBER})
    assert r.status_code == 200, r.text
    call = r.json()["call"]
    assert call["direction"] == "inbound"
    assert call["status"] == "completed"
    assert receiver.of_kind("voice"), "VoiceUrl was requested"


async def test_conversations_seed_and_flow(twilio: httpx.AsyncClient) -> None:
    listing = (await twilio.get(f"{CONVERSATIONS}/v1/Conversations", params={"PageSize": 100})).json()
    assert listing["meta"]["key"] == "conversations"
    convs = listing["conversations"]
    assert len(convs) == 18  # 10 SMS + 8 WhatsApp world threads
    seeded = next(c for c in convs if c["unique_name"] == "conv-sms-001")
    assert seeded["sid"].startswith("CH")
    assert seeded["chat_service_sid"].startswith("IS")
    assert seeded["state"] == "active"
    assert seeded["links"]["messages"].endswith("/Messages")

    participants = (
        await twilio.get(f"{CONVERSATIONS}/v1/Conversations/{seeded['sid']}/Participants")
    ).json()["participants"]
    assert len(participants) == 2
    chat = next(p for p in participants if p["identity"] == "halcyon-advisory")
    assert chat["sid"].startswith("MB")
    sms = next(p for p in participants if p["identity"] is None)
    assert sms["messaging_binding"]["address"] == CONTACT
    assert sms["messaging_binding"]["proxy_address"] == FROM_NUMBER

    messages = (await twilio.get(f"{CONVERSATIONS}/v1/Conversations/{seeded['sid']}/Messages")).json()[
        "messages"
    ]
    assert len(messages) == 6
    assert [m["index"] for m in messages] == list(range(6))
    assert messages[0]["author"] == "halcyon-advisory"
    assert "Noah Williams" in messages[0]["body"]

    posted = await twilio.post(
        f"{CONVERSATIONS}/v1/Conversations/{seeded['sid']}/Messages",
        data={"Author": "halcyon-advisory", "Body": "One more thing…"},
    )
    assert posted.status_code == 201, posted.text
    assert posted.json()["index"] == 6
    assert posted.json()["participant_sid"] == chat["sid"]
    assert posted.json()["sid"].startswith("IM")

    # Create a fresh conversation with participants.
    created = await twilio.post(
        f"{CONVERSATIONS}/v1/Conversations", data={"FriendlyName": "New thread", "UniqueName": "thread-1"}
    )
    assert created.status_code == 201, created.text
    conv_sid = created.json()["sid"]
    dupe = await twilio.post(f"{CONVERSATIONS}/v1/Conversations", data={"UniqueName": "thread-1"})
    assert dupe.status_code == 400
    agent = await twilio.post(
        f"{CONVERSATIONS}/v1/Conversations/{conv_sid}/Participants", data={"Identity": "agent-1"}
    )
    assert agent.status_code == 201, agent.text
    guest = await twilio.post(
        f"{CONVERSATIONS}/v1/Conversations/{conv_sid}/Participants",
        data={"MessagingBinding.Address": CONTACT, "MessagingBinding.ProxyAddress": FROM_NUMBER},
    )
    assert guest.status_code == 201, guest.text
    assert guest.json()["messaging_binding"]["type"] == "sms"
    empty = await twilio.post(f"{CONVERSATIONS}/v1/Conversations/{conv_sid}/Participants", data={})
    assert empty.status_code == 400

    edited = await twilio.post(
        f"{CONVERSATIONS}/v1/Conversations/{conv_sid}/Participants/{agent.json()['sid']}",
        data={"LastReadMessageIndex": "0"},
    )
    assert edited.json()["last_read_message_index"] == 0
    fetched_by_name = await twilio.get(f"{CONVERSATIONS}/v1/Conversations/thread-1")
    assert fetched_by_name.json()["sid"] == conv_sid
    assert (await twilio.delete(f"{CONVERSATIONS}/v1/Conversations/{conv_sid}")).status_code == 204
    assert (await twilio.get(f"{CONVERSATIONS}/v1/Conversations/{conv_sid}")).status_code == 404
