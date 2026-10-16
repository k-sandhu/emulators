"""Messenger Platform handlers (mixin for :class:`MetaAPI`).

Send API with the 24-hour standard messaging window (7 days for the HUMAN_AGENT
tag), conversations/messages edges, the User Profile API, reusable attachments
and the Messenger Profile (greeting / get-started / persistent menu).
"""

from __future__ import annotations

from typing import Any

from emulators.core.provider import ProviderContext
from emulators.providers.meta import shapes
from emulators.providers.meta.common import (
    apply_fields,
    fail,
    graph_time,
    invalid_param,
    new_mid,
    not_found,
    numeric_id,
    paged,
    require_auth,
)
from emulators.providers.meta.webhooks_out import deliver_ms_delivery_and_read

MESSAGE_TAGS = {
    "CONFIRMED_EVENT_UPDATE",
    "CONFIRMED_EVENT_REMINDER",
    "POST_PURCHASE_UPDATE",
    "ACCOUNT_UPDATE",
    "HUMAN_AGENT",
    "CUSTOMER_FEEDBACK",
    "APPLICATION_UPDATE",
    "PAIRING_UPDATE",
    "TRANSPORTATION_UPDATE",
    "FEATURE_FUNCTIONALITY_UPDATE",
}


class MessengerMixin:
    ctx: ProviderContext
    request: Any
    version: str
    params: dict[str, Any]
    body: dict[str, Any]

    def require_page(self, page_id: str | None = None):
        auth = require_auth(
            self.ctx,
            self.request,
            self.params,
            types={"PAGE"},
            perm_message="Requires pages_messaging permission and a page access token to "
            "perform this operation.",
            perm_code=200,
        )
        if page_id is not None and auth.page_id and auth.page_id != page_id:
            invalid_param(
                self.ctx,
                f"Object with ID '{page_id}' does not exist, cannot be loaded due to missing "
                "permissions, or does not support this operation.",
                subcode=33,
            )
        return auth

    def get_page(self, page_id: str) -> dict[str, Any]:
        page = self.ctx.store.collection("pages").get(page_id)
        if page is None:
            not_found(self.ctx, page_id)
        assert page is not None
        return page

    def ms_window(self, psid_record: dict[str, Any]) -> tuple[bool, bool]:
        """(within 24h, within 7 days) for the PSID's last inbound message."""
        last = psid_record.get("_last_inbound_ts")
        if not last:
            return False, False
        age = int(self.ctx.clock.now().timestamp()) - int(last)
        return age < 86400, age < 7 * 86400

    # -- Send API ----------------------------------------------------------------------------------
    async def handle_ms_send(self, page_id: str) -> dict[str, Any]:
        self.require_page(page_id)
        page = self.get_page(page_id)
        body = self.body
        recipient = body.get("recipient") or {}
        psid = recipient.get("id")
        user_ref = recipient.get("user_ref")
        if not psid and not user_ref and recipient.get("phone_number"):
            from emulators.providers.meta.common import digits_only

            wanted = digits_only(str(recipient["phone_number"]))
            for person in self.ctx.world.people:
                phones = [digits_only(p or "") for p in (person.phone_mobile, person.phone_business)]
                if wanted and wanted in phones:
                    match = self.ctx.store.collection("psids").first(_person_id=person.id)
                    if match is not None:
                        psid = match["id"]
                    break
        if not psid and not user_ref:
            invalid_param(
                self.ctx, "The parameter recipient is required (recipient.id or recipient.user_ref)."
            )
        if psid:
            psid_record = self.ctx.store.collection("psids").get(str(psid))
            if psid_record is None:
                fail(self.ctx, 400, 100, "(#100) No matching user found.", subcode=2018001)
        else:
            psid_record = self.ctx.store.collection("psids").first(_user_ref=str(user_ref))  # type: ignore[attr-defined]
            if psid_record is None:
                fail(self.ctx, 400, 100, "(#100) No matching user found.", subcode=2018001)
            psid = psid_record["id"]
        assert psid_record is not None and psid is not None
        sender_action = body.get("sender_action")
        if sender_action:
            if sender_action not in ("typing_on", "typing_off", "mark_seen"):
                fail(
                    self.ctx,
                    400,
                    100,
                    "(#100) Invalid sender_action. Must be one of typing_on, typing_off or mark_seen.",
                    subcode=2018065,
                )
            if sender_action == "mark_seen":
                conv = self.ctx.store.collection("ms_conversations").get(psid_record.get("_conversation_id"))
                if conv is not None:
                    conv["unread_count"] = 0
            return {"recipient_id": str(psid)}
        message = body.get("message")
        if not isinstance(message, dict) or not (
            message.get("text") or message.get("attachment") or message.get("quick_replies")
        ):
            fail(
                self.ctx,
                400,
                100,
                "(#100) Message cannot be empty: provide text or an attachment.",
                subcode=2018035,
            )
        assert isinstance(message, dict)
        if len(str(message.get("text") or "")) > 2000:
            fail(
                self.ctx, 400, 100, "(#100) Message text is too long (max 2000 characters).", subcode=2018023
            )
        quick = message.get("quick_replies") or []
        if len(quick) > 13:
            fail(self.ctx, 400, 100, "(#100) Too many quick replies (max 13).", subcode=2018296)
        messaging_type = body.get("messaging_type", "RESPONSE")
        if messaging_type not in ("RESPONSE", "UPDATE", "MESSAGE_TAG"):
            invalid_param(self.ctx, f"Invalid messaging_type '{messaging_type}'.")
        within_24h, within_7d = self.ms_window(psid_record)
        if messaging_type == "MESSAGE_TAG":
            tag = body.get("tag")
            if not tag or tag not in MESSAGE_TAGS:
                fail(
                    self.ctx,
                    400,
                    100,
                    "(#100) A valid tag is required for MESSAGE_TAG messages.",
                    subcode=2018108,
                )
            if tag == "HUMAN_AGENT" and not within_7d:
                fail(
                    self.ctx,
                    400,
                    10,
                    "(#10) This message is sent outside of allowed window.",
                    subcode=2018278,
                    err_type="OAuthException",
                )
        elif not within_24h:
            fail(
                self.ctx,
                400,
                10,
                "(#10) This message is sent outside of allowed window.",
                subcode=2018278,
            )
        attachment_record: dict[str, Any] | None = None
        attachment = message.get("attachment") if isinstance(message, dict) else None
        attachments_shape: dict[str, Any] | None = None
        if isinstance(attachment, dict):
            payload = attachment.get("payload") or {}
            if isinstance(payload, dict) and payload.get("attachment_id"):
                attachment_record = self.ctx.store.collection("attachments").get(
                    str(payload["attachment_id"])
                )
                if attachment_record is None:
                    invalid_param(self.ctx, "attachment_id does not exist.", subcode=33)
            attachments_shape = {
                "data": [
                    {
                        "id": (attachment_record or {}).get("id")
                        or numeric_id("msattach", str(payload)[:40], digits=15),
                        "mime_type": (attachment_record or {}).get("mime_type", "application/octet-stream"),
                        "name": (attachment_record or {}).get("name", "attachment"),
                        "size": (attachment_record or {}).get("size", 0),
                    }
                ]
            }
        mid = new_mid(self.ctx)
        now = self.ctx.clock.now()
        record: dict[str, Any] = {
            "id": mid,
            "created_time": graph_time(now),
            "from": {"id": page_id, "name": page["name"]},
            "to": {"data": [{"id": str(psid), "name": psid_record["name"]}]},
            "message": str(message.get("text") or ""),
            "_conversation_id": psid_record.get("_conversation_id"),
            "_psid": str(psid),
            "_person_id": psid_record.get("_person_id"),
            "_direction": "outbound",
            "_author_id": None,
            "_ts": int(now.timestamp()),
            "_messaging_type": messaging_type,
            "_tag": body.get("tag"),
        }
        if attachments_shape is not None:
            record["_attachments"] = attachments_shape
            record["_attachment_payload"] = attachment
        if quick:
            record["_quick_replies"] = quick
        self.ctx.store.collection("ms_messages").put(record)
        conv = self.ctx.store.collection("ms_conversations").get(record["_conversation_id"])
        if conv is not None:
            conv["_message_ids"] = [*conv.get("_message_ids", []), mid]
            conv["message_count"] = len(conv["_message_ids"])
            conv["updated_time"] = graph_time(now)
            conv["_updated_ts"] = int(now.timestamp())
            conv["snippet"] = str(message.get("text") or "")[:100]
        await deliver_ms_delivery_and_read(self.ctx, str(psid), mid, int(now.timestamp() * 1000))
        return {"recipient_id": str(psid), "message_id": mid}

    # -- conversations / messages edges -------------------------------------------------------------
    def handle_ms_conversations(self, page_id: str) -> dict[str, Any]:
        self.require_page(page_id)
        platform = self.params.get("platform")
        if platform and platform != "messenger":
            return {"data": []}
        records = [r for r in self.ctx.store.collection("ms_conversations") if r.get("_page_id") == page_id]
        records.sort(key=lambda r: r.get("_updated_ts", 0), reverse=True)
        user_id_filter = self.params.get("user_id")
        if user_id_filter:
            records = [r for r in records if r.get("_psid") == str(user_id_filter)]
        items = [
            apply_fields(shapes.ms_conversation_shape(self.ctx, r), self.params.get("fields"))
            for r in records
        ]
        return paged(self.ctx, self.request, self.version, f"{page_id}/conversations", items, self.params)

    def get_ms_conversation(self, conv_id: str) -> dict[str, Any]:
        conv = self.ctx.store.collection("ms_conversations").get(conv_id)
        if conv is None:
            not_found(self.ctx, conv_id)
        assert conv is not None
        return conv

    def handle_ms_conversation(self, conv_id: str) -> dict[str, Any]:
        conv = self.get_ms_conversation(conv_id)
        self.require_page(conv["_page_id"])
        return apply_fields(shapes.ms_conversation_shape(self.ctx, conv), self.params.get("fields"))

    def handle_ms_conversation_messages(self, conv_id: str) -> dict[str, Any]:
        conv = self.get_ms_conversation(conv_id)
        self.require_page(conv["_page_id"])
        coll = self.ctx.store.collection("ms_messages")
        records = [coll.get(mid) for mid in conv.get("_message_ids") or []]
        records = [r for r in records if r is not None]
        records.sort(key=lambda r: r["_ts"])
        items = [apply_fields(shapes.ms_message_stub(r), self.params.get("fields")) for r in records]  # type: ignore[arg-type]
        return paged(self.ctx, self.request, self.version, f"{conv_id}/messages", items, self.params)

    def handle_ms_message(self, message_id: str) -> dict[str, Any]:
        record = self.ctx.store.collection("ms_messages").get(message_id)
        if record is None:
            not_found(self.ctx, message_id)
        assert record is not None
        conv = self.ctx.store.collection("ms_conversations").get(record.get("_conversation_id"))
        self.require_page(conv["_page_id"] if conv else None)
        return apply_fields(shapes.ms_message_shape(self.ctx, record), self.params.get("fields"))

    def handle_psid_profile(self, psid: str) -> dict[str, Any]:
        self.require_page()
        record = self.ctx.store.collection("psids").get(psid)
        if record is None:
            not_found(self.ctx, psid)
        assert record is not None
        return apply_fields(shapes.psid_shape(self.ctx, self.request, record), self.params.get("fields"))

    # -- attachments / messenger profile ------------------------------------------------------------
    async def handle_ms_attachments(self, page_id: str) -> dict[str, Any]:
        self.require_page(page_id)
        content_type = self.request.headers.get("content-type", "")
        blob_key: str | None = None
        mime = "application/octet-stream"
        name = "attachment"
        size = 0
        attach_type = "file"
        url: str | None = None
        if "multipart/form-data" in content_type:
            form = await self.request.form()
            upload = form.get("filedata")
            if upload is None or not hasattr(upload, "read"):
                invalid_param(self.ctx, "The parameter filedata is required for multipart uploads.")
            content: bytes = await upload.read()  # type: ignore[union-attr]
            blob_key = self.ctx.store.blobs.put(content)
            mime = str(getattr(upload, "content_type", "") or mime)
            name = str(getattr(upload, "filename", "") or name)
            size = len(content)
            raw_message = form.get("message")
            if raw_message:
                import json

                try:
                    parsed = json.loads(str(raw_message))
                    attach_type = str(((parsed.get("attachment") or {}).get("type")) or attach_type)
                except ValueError:
                    pass
        else:
            attachment = ((self.body.get("message") or {}).get("attachment")) or {}
            payload = attachment.get("payload") or {}
            attach_type = str(attachment.get("type") or attach_type)
            url = payload.get("url") if isinstance(payload, dict) else None
            if url is None and payload.get("is_reusable") is None:
                invalid_param(self.ctx, "attachment.payload.url is required.")
        attachment_id = numeric_id(
            "msattach", blob_key or url or "", self.ctx.ids.sequence("msattach"), digits=16
        )
        self.ctx.store.collection("attachments").put(
            {
                "id": attachment_id,
                "type": attach_type,
                "mime_type": mime,
                "name": name,
                "size": size,
                "url": url,
                "_blob_key": blob_key,
                "_page_id": page_id,
            }
        )
        return {"attachment_id": attachment_id}

    PROFILE_KEYS = ("greeting", "get_started", "persistent_menu", "ice_breakers", "domain_whitelist")

    def handle_ms_profile_get(self, page_id: str) -> dict[str, Any]:
        self.require_page(page_id)
        page = self.get_page(page_id)
        profile = page.get("_profile") or {}
        wanted = [f.strip() for f in str(self.params.get("fields") or "").split(",") if f.strip()]
        item = {k: profile[k] for k in self.PROFILE_KEYS if k in profile and (not wanted or k in wanted)}
        return {"data": [item] if item else []}

    def handle_ms_profile_post(self, page_id: str) -> dict[str, Any]:
        self.require_page(page_id)
        page = self.get_page(page_id)
        profile = dict(page.get("_profile") or {})
        for key in self.PROFILE_KEYS:
            if key in self.body:
                profile[key] = self.body[key]
        page["_profile"] = profile
        return {"result": "success"}

    def handle_ms_profile_delete(self, page_id: str) -> dict[str, Any]:
        self.require_page(page_id)
        page = self.get_page(page_id)
        profile = dict(page.get("_profile") or {})
        for key in self.body.get("fields") or []:
            profile.pop(str(key), None)
        page["_profile"] = profile
        return {"result": "success"}
